from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from models import ExplorerSettings, KitStatus
from services import (
    InventorToRadanInlineNeedsUi,
    invalidate_filesystem_cache_for_paths,
    inventor_output_paths,
    move_inventor_outputs_to_project,
    run_inventor_to_radan_inline,
)


ProgressCallback = Callable[[str], None]


class InventorServiceError(RuntimeError):
    pass


class InventorValidationError(InventorServiceError):
    pass


class InventorNeedsUserAction(InventorServiceError):
    pass


class InventorRunError(InventorServiceError):
    pass


class InventorOutputError(InventorServiceError):
    pass


@dataclass(frozen=True)
class InventorRunResult:
    spreadsheet_path: Path
    project_dir: Path
    entry_path: Path
    moved_paths: tuple[Path, ...]
    report_path: Path
    discard_paths: tuple[Path, ...]
    added_count: int | None = None


@dataclass(frozen=True)
class InventorDiscardResult:
    deleted_paths: tuple[Path, ...]
    failed_deletes: tuple[str, ...]


def _emit(progress_cb: ProgressCallback | None, message: str) -> None:
    if progress_cb is not None:
        progress_cb(str(message))


def _candidate_paths(status: KitStatus) -> tuple[Path, ...]:
    match = getattr(status, "spreadsheet_match", None)
    return tuple(Path(path) for path in (getattr(match, "candidates", ()) or ()))


def _validate_spreadsheet(status: KitStatus) -> Path:
    match = getattr(status, "spreadsheet_match", None)
    chosen_path = getattr(match, "chosen_path", None)
    candidates = _candidate_paths(status)
    if chosen_path is None or len(candidates) != 1:
        if len(candidates) > 1:
            detail = "\n".join(str(path) for path in candidates)
            raise InventorValidationError(
                "This kit has multiple BOM candidates in the W folder, so the Inventor input is ambiguous.\n\n"
                + detail
            )
        raise InventorValidationError("This kit does not have exactly one BOM candidate in the W folder.")
    spreadsheet_path = Path(chosen_path)
    if not spreadsheet_path.exists():
        raise InventorValidationError(f"The selected BOM does not exist: {spreadsheet_path}")
    return spreadsheet_path


def _validate_project_dir(status: KitStatus) -> Path:
    project_dir = getattr(getattr(status, "paths", None), "project_dir", None)
    if project_dir is None:
        raise InventorValidationError("The L-side project folder is not available for this kit.")
    project_path = Path(project_dir)
    if not project_path.exists():
        raise InventorValidationError(f"The L-side project folder does not exist: {project_path}")
    if not project_path.is_dir():
        raise InventorValidationError(f"The L-side project path is not a folder: {project_path}")
    return project_path


def _validate_entry(settings: ExplorerSettings) -> Path:
    entry_text = str(getattr(settings, "inventor_to_radan_entry", "") or "").strip()
    if not entry_text:
        raise InventorValidationError("Inventor launcher is not configured.")
    entry_path = Path(entry_text)
    if not entry_path.exists():
        raise InventorValidationError(f"Inventor launcher does not exist: {entry_path}")
    return entry_path


def _coerce_added_count(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _locate_report_path(spreadsheet_path: Path, project_dir: Path, moved_paths: tuple[Path, ...]) -> Path:
    outputs = inventor_output_paths(spreadsheet_path, project_dir)
    report_path = outputs.target_report_path
    if report_path is not None and report_path.exists():
        return report_path
    for path in moved_paths:
        candidate = Path(path)
        if candidate.suffix.casefold() == ".txt" and candidate.exists():
            return candidate
    expected = report_path if report_path is not None else outputs.source_report_path
    raise InventorOutputError(
        "Inventor-to-RADAN completed, but the generated report could not be found for review.\n\n"
        f"Expected: {expected}"
    )


def _eligible_discard_paths(moved_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    eligible: list[Path] = []
    seen: set[str] = set()
    for path in moved_paths:
        candidate = Path(path)
        if candidate.suffix.casefold() not in {".csv", ".txt"}:
            continue
        key = str(candidate.resolve() if candidate.exists() else candidate.absolute()).casefold()
        if key in seen:
            continue
        seen.add(key)
        eligible.append(candidate)
    return tuple(eligible)


def run_inventor_for_status(
    status: KitStatus,
    settings: ExplorerSettings,
    *,
    progress_cb: ProgressCallback | None = None,
) -> InventorRunResult:
    spreadsheet_path = _validate_spreadsheet(status)
    project_dir = _validate_project_dir(status)
    entry_path = _validate_entry(settings)

    _emit(progress_cb, f"Inventor: converting {spreadsheet_path.name}")
    try:
        inline_result = run_inventor_to_radan_inline(entry_path, spreadsheet_path)
    except InventorToRadanInlineNeedsUi as exc:
        raise InventorNeedsUserAction(
            f"Inventor-to-RADAN needs user input before this flow can continue: {exc}"
        ) from exc
    except Exception as exc:
        raise InventorRunError(str(exc)) from exc

    _emit(progress_cb, "Inventor: moving generated CSV/report to L")
    try:
        _outputs, moved_paths_raw = move_inventor_outputs_to_project(spreadsheet_path, project_dir)
    except InventorServiceError:
        raise
    except Exception as exc:
        raise InventorOutputError(str(exc)) from exc

    moved_paths = tuple(Path(path) for path in moved_paths_raw)
    report_path = _locate_report_path(spreadsheet_path, project_dir, moved_paths)
    discard_paths = _eligible_discard_paths(moved_paths)
    added_count = _coerce_added_count(getattr(inline_result, "added_count", None))
    _emit(progress_cb, f"Inventor: report ready for review: {report_path.name}")
    return InventorRunResult(
        spreadsheet_path=spreadsheet_path,
        project_dir=project_dir,
        entry_path=entry_path,
        moved_paths=moved_paths,
        report_path=report_path,
        discard_paths=discard_paths,
        added_count=added_count,
    )


def discard_inventor_result(result: InventorRunResult) -> InventorDiscardResult:
    allowed_keys = {
        str((Path(path).resolve() if Path(path).exists() else Path(path).absolute())).casefold()
        for path in result.discard_paths
        if Path(path).suffix.casefold() in {".csv", ".txt"}
    }
    deleted: list[Path] = []
    failed: list[str] = []
    for path in result.discard_paths:
        candidate = Path(path)
        key = str((candidate.resolve() if candidate.exists() else candidate.absolute())).casefold()
        if candidate.suffix.casefold() not in {".csv", ".txt"} or key not in allowed_keys:
            continue
        try:
            if candidate.exists():
                candidate.unlink()
                deleted.append(candidate)
        except OSError as exc:
            failed.append(f"{candidate}: {exc}")
    if deleted:
        invalidate_filesystem_cache_for_paths(tuple(deleted))
    return InventorDiscardResult(deleted_paths=tuple(deleted), failed_deletes=tuple(failed))
