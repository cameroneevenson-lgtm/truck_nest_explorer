from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PerformanceSnapshot:
    database_queries: int
    filesystem_checks: int
    truck_switch_started: int
    truck_switch_completed: int
    stale_results_ignored: int
    cache_hits: dict[str, int]
    cache_misses: dict[str, int]
    cache_invalidations: dict[str, int]


class PerformanceMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._database_queries = 0
            self._filesystem_checks = 0
            self._truck_switch_started = 0
            self._truck_switch_completed = 0
            self._stale_results_ignored = 0
            self._cache_hits: defaultdict[str, int] = defaultdict(int)
            self._cache_misses: defaultdict[str, int] = defaultdict(int)
            self._cache_invalidations: defaultdict[str, int] = defaultdict(int)

    def record_database_query(self, count: int = 1) -> None:
        with self._lock:
            self._database_queries += int(count)

    def record_filesystem_check(self, count: int = 1) -> None:
        with self._lock:
            self._filesystem_checks += int(count)

    def record_truck_switch_started(self) -> None:
        with self._lock:
            self._truck_switch_started += 1

    def record_truck_switch_completed(self) -> None:
        with self._lock:
            self._truck_switch_completed += 1

    def record_stale_result_ignored(self) -> None:
        with self._lock:
            self._stale_results_ignored += 1

    def record_cache_hit(self, cache_name: str) -> None:
        with self._lock:
            self._cache_hits[str(cache_name)] += 1

    def record_cache_miss(self, cache_name: str) -> None:
        with self._lock:
            self._cache_misses[str(cache_name)] += 1

    def record_cache_invalidation(self, cache_name: str, count: int = 1) -> None:
        with self._lock:
            self._cache_invalidations[str(cache_name)] += int(count)

    def snapshot(self) -> PerformanceSnapshot:
        with self._lock:
            return PerformanceSnapshot(
                database_queries=self._database_queries,
                filesystem_checks=self._filesystem_checks,
                truck_switch_started=self._truck_switch_started,
                truck_switch_completed=self._truck_switch_completed,
                stale_results_ignored=self._stale_results_ignored,
                cache_hits=dict(self._cache_hits),
                cache_misses=dict(self._cache_misses),
                cache_invalidations=dict(self._cache_invalidations),
            )


GLOBAL_METRICS = PerformanceMetrics()


@dataclass(frozen=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class BoundedTTLCache(Generic[T]):
    def __init__(
        self,
        name: str,
        *,
        max_size: int,
        positive_ttl_seconds: float,
        negative_ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be greater than zero.")
        self.name = str(name)
        self.max_size = int(max_size)
        self.positive_ttl_seconds = float(positive_ttl_seconds)
        self.negative_ttl_seconds = (
            float(negative_ttl_seconds)
            if negative_ttl_seconds is not None
            else float(positive_ttl_seconds)
        )
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._entries: OrderedDict[object, _CacheEntry[T]] = OrderedDict()

    def get(self, key: object) -> tuple[bool, T | None]:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                GLOBAL_METRICS.record_cache_miss(self.name)
                return False, None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                GLOBAL_METRICS.record_cache_miss(self.name)
                return False, None
            self._entries.move_to_end(key)
            GLOBAL_METRICS.record_cache_hit(self.name)
            return True, entry.value

    def set(self, key: object, value: T, *, negative: bool = False) -> None:
        ttl = self.negative_ttl_seconds if negative else self.positive_ttl_seconds
        if ttl <= 0:
            return
        with self._lock:
            self._entries[key] = _CacheEntry(value=value, expires_at=self._clock() + ttl)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_size:
                self._entries.popitem(last=False)

    def invalidate(self, key: object) -> bool:
        with self._lock:
            removed = self._entries.pop(key, None) is not None
        if removed:
            GLOBAL_METRICS.record_cache_invalidation(self.name)
        return removed

    def invalidate_where(self, predicate: Callable[[object, T], bool]) -> int:
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if predicate(key, entry.value)
            ]
            for key in keys:
                self._entries.pop(key, None)
        if keys:
            GLOBAL_METRICS.record_cache_invalidation(self.name, len(keys))
        return len(keys)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        if count:
            GLOBAL_METRICS.record_cache_invalidation(self.name, count)
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def normalize_cache_path(path: Path | str | None) -> str:
    if path is None:
        return ""
    try:
        candidate = Path(str(path))
        if candidate.exists():
            return str(candidate.resolve()).casefold()
        return str(candidate.absolute()).casefold()
    except OSError:
        return str(path).casefold()


def settings_cache_signature(settings: object) -> str:
    if hasattr(settings, "__dataclass_fields__"):
        payload = asdict(settings)
    else:
        payload = dict(getattr(settings, "__dict__", {}))
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]


def reset_performance_metrics() -> None:
    GLOBAL_METRICS.reset()


def performance_snapshot() -> PerformanceSnapshot:
    return GLOBAL_METRICS.snapshot()
