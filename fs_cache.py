"""Generic filesystem-metadata caching primitives.

Extracted from services.py so lower-level "does this path exist / how big is
it" helpers can be shared by services.py, packet_pdf_detection.py, and
w_block_transfer.py without those modules needing to import back from
services.py (which would create a circular import, since services.py imports
detection/transfer functions from those modules for re-export).
"""
from __future__ import annotations

import re
from pathlib import Path

from performance_metrics import BoundedTTLCache, GLOBAL_METRICS, normalize_cache_path

FILESYSTEM_CACHE_POSITIVE_TTL_SECONDS = 5.0
FILESYSTEM_CACHE_NEGATIVE_TTL_SECONDS = 2.0

FILE_METADATA_CACHE: BoundedTTLCache[object] = BoundedTTLCache(
    "filesystem_metadata",
    max_size=4096,
    positive_ttl_seconds=FILESYSTEM_CACHE_POSITIVE_TTL_SECONDS,
    negative_ttl_seconds=FILESYSTEM_CACHE_NEGATIVE_TTL_SECONDS,
)


def clean_text(value: object) -> str:
    return str(value or "").strip()


def natural_sort_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def cached_path_exists(path: Path | None, *, cache: BoundedTTLCache[object] | None = None) -> bool:
    if path is None:
        return False
    if cache is None:
        GLOBAL_METRICS.record_filesystem_check()
        return path.exists()
    cache_obj = cache
    key = ("exists", normalize_cache_path(path))
    hit, value = cache_obj.get(key)
    if hit:
        return bool(value)
    GLOBAL_METRICS.record_filesystem_check()
    exists = path.exists()
    cache_obj.set(key, exists, negative=not exists)
    return exists


def cached_path_size(path: Path | None, *, cache: BoundedTTLCache[object] | None = None) -> int:
    if path is None:
        return 0
    if cache is None:
        GLOBAL_METRICS.record_filesystem_check()
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    cache_obj = cache
    key = ("stat_size", normalize_cache_path(path))
    hit, value = cache_obj.get(key)
    if hit:
        return int(value or 0)
    GLOBAL_METRICS.record_filesystem_check()
    try:
        size = int(path.stat().st_size)
    except OSError:
        size = 0
    cache_obj.set(key, size, negative=size == 0)
    return size


def invalidate_filesystem_cache_for_path(path: Path | str | None) -> None:
    if path is None:
        return
    target_key = normalize_cache_path(path)
    FILE_METADATA_CACHE.invalidate_where(
        lambda key, _value: isinstance(key, tuple)
        and any(isinstance(part, str) and (part == target_key or part.startswith(f"{target_key}\\")) for part in key)
    )


def invalidate_filesystem_cache_for_paths(paths: tuple[Path, ...] | list[Path]) -> None:
    seen: set[str] = set()
    for path in paths:
        current = Path(path)
        for candidate in (current, current.parent):
            key = normalize_cache_path(candidate)
            if key in seen:
                continue
            seen.add(key)
            invalidate_filesystem_cache_for_path(candidate)
