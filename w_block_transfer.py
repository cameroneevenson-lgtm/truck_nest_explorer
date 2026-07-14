"""W/L block-file (.cnc) transfer: matching DRG nests to source block files
and copying them (with checksum verification) to the machine EIA share and
the local L: archive.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shutil
from pathlib import Path, PureWindowsPath

from fs_cache import invalidate_filesystem_cache_for_path, invalidate_filesystem_cache_for_paths
from models import DEFAULT_P_RELEASE_ROOT
from performance_metrics import normalize_cache_path

DEFAULT_BLOCK_FILES_ROOT = Path(r"L:\BATTLESHIELD\BLOCK FILES")
DEFAULT_MACHINE_EIA_ROOT = Path(r"A:\EiaFiles\Battleshield\F-LARGE FLEET")
DEFAULT_P_MACHINE_EIA_ROOT = DEFAULT_MACHINE_EIA_ROOT.parent / PureWindowsPath(DEFAULT_P_RELEASE_ROOT).name


@dataclass(frozen=True)
class BlockFileMatch:
    drg_path: Path
    source_path: Path
    target_path: Path
    local_target_path: Path
    match_reason: str


@dataclass(frozen=True)
class BlockFileTransferPlan:
    project_dir: Path
    source_root: Path
    machine_root: Path
    target_dir: Path
    local_target_dir: Path
    drg_paths: tuple[Path, ...]
    matches: tuple[BlockFileMatch, ...]
    already_sent_paths: tuple[Path, ...]
    missing_drg_paths: tuple[Path, ...]


@dataclass(frozen=True)
class BlockFileTransferResult:
    plan: BlockFileTransferPlan
    operation: str
    transferred_paths: tuple[Path, ...]
    skipped_paths: tuple[Path, ...]
    local_transferred_paths: tuple[Path, ...] = ()
    canceled: bool = False


def _normalize_block_match_stem(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _relative_path_case_insensitive(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        path_text = str(path).replace("/", "\\").rstrip("\\")
        root_text = str(root).replace("/", "\\").rstrip("\\")
        root_prefix = f"{root_text}\\"
        if path_text.casefold().startswith(root_prefix.casefold()):
            return Path(*PureWindowsPath(path_text[len(root_prefix) :]).parts)
        raise ValueError(f"{path} is not under {root}") from None


def machine_block_project_dir(
    project_dir: Path | str,
    release_root: Path | str,
    *,
    machine_root: Path | str = DEFAULT_MACHINE_EIA_ROOT,
) -> Path:
    project = Path(str(project_dir))
    release = Path(str(release_root))
    root = Path(str(machine_root))
    # RPD projects live one folder below the shop-facing kit folder
    # (for example F57524\CONSOLE PACK\F57524 CONSOLE PACK). The machine
    # expects block files in the kit folder, not inside that inner project folder.
    relative_kit_folder = _relative_path_case_insensitive(project.parent, release)
    return root / relative_kit_folder


def machine_block_root_for_release_root(
    release_root: Path | str,
    *,
    machine_root: Path | str = DEFAULT_MACHINE_EIA_ROOT,
) -> Path:
    release_name = PureWindowsPath(str(release_root)).name
    p_release_name = PureWindowsPath(DEFAULT_P_RELEASE_ROOT).name
    root = Path(str(machine_root))
    if release_name.casefold() == p_release_name.casefold():
        machine_name = PureWindowsPath(str(root)).name
        if machine_name.casefold() == p_release_name.casefold():
            return root
        return root.parent / p_release_name
    return root


def local_block_project_dir(project_dir: Path | str) -> Path:
    project = Path(str(project_dir))
    # Keep the L-side archive beside the inner RADAN project folder, matching
    # the machine folder layout and avoiding an extra project-name layer.
    return project.parent


def discover_project_drg_paths(project_dir: Path | str) -> tuple[Path, ...]:
    project = Path(str(project_dir))
    if not project.exists():
        raise FileNotFoundError(str(project))
    nests_dir = project / "nests"
    search_root = nests_dir if nests_dir.exists() else project
    return tuple(
        sorted(
            (
                path
                for path in search_root.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".drg"
            ),
            key=lambda item: str(item).casefold(),
        )
    )


def _discover_block_files(source_root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in source_root.iterdir()
                if path.is_file() and path.suffix.casefold() == ".cnc"
            ),
            key=lambda item: str(item).casefold(),
        )
    )


def _block_match_for_drg(drg_path: Path, block_files: tuple[Path, ...]) -> tuple[Path | None, str]:
    drg_stem = _normalize_block_match_stem(drg_path.stem)
    candidates: list[tuple[int, str, Path]] = []
    for block_path in block_files:
        block_stem = _normalize_block_match_stem(block_path.stem)
        if not block_stem:
            continue
        if block_stem == drg_stem:
            candidates.append((len(block_stem), "exact_stem", block_path))
        elif drg_stem.startswith(block_stem):
            candidates.append((len(block_stem), "truncated_prefix", block_path))
    if not candidates:
        return None, ""
    candidates.sort(key=lambda item: (item[0], item[1] == "exact_stem"), reverse=True)
    best_length = candidates[0][0]
    best = [candidate for candidate in candidates if candidate[0] == best_length]
    if len(best) > 1:
        names = ", ".join(path.name for _length, _reason, path in best)
        raise ValueError(f"Multiple block files match {drg_path.name}: {names}")
    _length, reason, path = best[0]
    return path, reason


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source_path: Path, target_path: Path, *, expected_size: int, expected_sha256: str) -> None:
    if normalize_cache_path(source_path) == normalize_cache_path(target_path):
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    shutil.copy2(source_path, target_path)
    if not target_path.exists():
        raise RuntimeError(f"Copied block file did not verify: {target_path}")
    if target_path.stat().st_size != expected_size:
        raise RuntimeError(f"Copied block file size did not verify: {target_path}")
    if _sha256_path(target_path) != expected_sha256:
        raise RuntimeError(f"Copied block file checksum did not verify: {target_path}")


def build_project_block_transfer_plan(
    project_dir: Path | str,
    release_root: Path | str,
    *,
    source_root: Path | str = DEFAULT_BLOCK_FILES_ROOT,
    machine_root: Path | str = DEFAULT_MACHINE_EIA_ROOT,
) -> BlockFileTransferPlan:
    project = Path(str(project_dir))
    source = Path(str(source_root))
    machine = machine_block_root_for_release_root(release_root, machine_root=machine_root)
    if not project.exists():
        raise FileNotFoundError(str(project))
    if not source.exists():
        raise FileNotFoundError(f"Block file source folder is not available: {source}")
    if not machine.exists():
        raise FileNotFoundError(f"Machine block destination is not available: {machine}")
    target_dir = machine_block_project_dir(project, release_root, machine_root=machine)
    local_target_dir = local_block_project_dir(project)
    drg_paths = discover_project_drg_paths(project)
    block_files = _discover_block_files(source)
    matches: list[BlockFileMatch] = []
    already_sent: list[Path] = []
    missing: list[Path] = []
    used_sources: set[str] = set()
    for drg_path in drg_paths:
        source_path, reason = _block_match_for_drg(drg_path, block_files)
        if source_path is None:
            target_path = target_dir / f"{drg_path.stem}.cnc"
            if target_path.exists():
                already_sent.append(target_path)
                continue
            missing.append(drg_path)
            continue
        source_key = normalize_cache_path(source_path)
        if source_key in used_sources:
            raise ValueError(f"Block file {source_path.name} matches more than one DRG.")
        used_sources.add(source_key)
        # The post output can truncate names; write the machine copy with the full DRG stem.
        target_path = target_dir / f"{drg_path.stem}{source_path.suffix}"
        local_target_path = local_target_dir / f"{drg_path.stem}{source_path.suffix}"
        matches.append(
            BlockFileMatch(
                drg_path=drg_path,
                source_path=source_path,
                target_path=target_path,
                local_target_path=local_target_path,
                match_reason=reason,
            )
        )
    return BlockFileTransferPlan(
        project_dir=project,
        source_root=source,
        machine_root=machine,
        target_dir=target_dir,
        local_target_dir=local_target_dir,
        drg_paths=drg_paths,
        matches=tuple(matches),
        already_sent_paths=tuple(already_sent),
        missing_drg_paths=tuple(missing),
    )


def send_project_block_files_to_machine(
    project_dir: Path | str,
    release_root: Path | str,
    *,
    source_root: Path | str = DEFAULT_BLOCK_FILES_ROOT,
    machine_root: Path | str = DEFAULT_MACHINE_EIA_ROOT,
    progress_cb=None,
    should_cancel_cb=None,
) -> BlockFileTransferResult:
    plan = build_project_block_transfer_plan(
        project_dir,
        release_root,
        source_root=source_root,
        machine_root=machine_root,
    )
    if not plan.matches:
        return BlockFileTransferResult(
            plan=plan,
            operation="copy_then_delete",
            transferred_paths=(),
            skipped_paths=(),
            local_transferred_paths=(),
        )
    plan.target_dir.mkdir(parents=True, exist_ok=True)
    plan.local_target_dir.mkdir(parents=True, exist_ok=True)
    transferred: list[Path] = []
    local_transferred: list[Path] = []
    skipped: list[Path] = []
    total = len(plan.matches)
    for index, match in enumerate(plan.matches, start=1):
        if should_cancel_cb is not None and should_cancel_cb():
            skipped.extend(item.source_path for item in plan.matches[index - 1 :])
            return BlockFileTransferResult(
                plan=plan,
                operation="copy_then_delete",
                transferred_paths=tuple(transferred),
                skipped_paths=tuple(skipped),
                local_transferred_paths=tuple(local_transferred),
                canceled=True,
            )
        if progress_cb is not None:
            progress_cb(index - 1, total, f"Sending {match.source_path.name}")
        source_size = match.source_path.stat().st_size
        source_sha256 = _sha256_path(match.source_path)
        _copy_verified(
            match.source_path,
            match.target_path,
            expected_size=source_size,
            expected_sha256=source_sha256,
        )
        _copy_verified(
            match.source_path,
            match.local_target_path,
            expected_size=source_size,
            expected_sha256=source_sha256,
        )
        match.source_path.unlink()
        transferred.append(match.target_path)
        local_transferred.append(match.local_target_path)
        if progress_cb is not None:
            progress_cb(index, total, f"Sent {match.target_path.name}")
    invalidate_filesystem_cache_for_paths(tuple(transferred) + tuple(local_transferred))
    invalidate_filesystem_cache_for_path(plan.source_root)
    invalidate_filesystem_cache_for_path(plan.local_target_dir)
    return BlockFileTransferResult(
        plan=plan,
        operation="copy_then_delete",
        transferred_paths=tuple(transferred),
        skipped_paths=tuple(skipped),
        local_transferred_paths=tuple(local_transferred),
    )
