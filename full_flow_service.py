from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Callable

from models import ExplorerSettings, KitStatus
from packet_build_service import (
    apply_assembly_context_to_sym_comments,
    build_assembly_packet,
    build_cut_list_packet,
    prepare_packet_build_context,
    scan_assembly_bom_context,
    validate_print_packet_readiness,
    write_assembly_bom_context_csv,
)
from services import (
    InventorToRadanInlineNeedsUi,
    launch_radan_csv_import,
    move_inventor_outputs_to_project,
    radan_csv_import_lock_status,
    resolve_existing_inventor_csv,
    run_inventor_to_radan_inline,
)


ProgressCallback = Callable[[str], None]


class FullFlowError(RuntimeError):
    pass


class FullFlowNeedsUserAction(FullFlowError):
    pass


@dataclass(frozen=True)
class InventorFlowResult:
    moved_paths: tuple[Path, ...]
    added_count: int | None = None


@dataclass(frozen=True)
class CsvImportResult:
    log_path: Path
    return_code: int


@dataclass(frozen=True)
class RfAssignmentResult:
    predicted_count: int
    skipped_count: int
    model_source: str
    kit_count: int
    backup_path: Path


@dataclass(frozen=True)
class PacketFlowResult:
    packet_paths: tuple[Path, ...]
    print_pages: int
    print_missing: int
    assembly_pages: int
    cut_list_pages: int
    assembly_context_path: Path | None = None
    sym_comment_updated_count: int = 0


@dataclass(frozen=True)
class FullFlowResult:
    project_path: Path
    inventor: InventorFlowResult
    csv_import: CsvImportResult
    rf_assignment: RfAssignmentResult
    packets: PacketFlowResult


@dataclass(frozen=True)
class NesterResult:
    ok: bool
    return_code: int | None
    elapsed_seconds: float
    log_path: Path
    drg_count: int
    before_snapshot: dict[str, object] | None = None
    after_snapshot: dict[str, object] | None = None


def _emit(progress_cb: ProgressCallback | None, message: str) -> None:
    if progress_cb is None:
        return
    progress_cb(str(message))


def _safe_name(value: object) -> str:
    text = "".join(character if character.isalnum() else "_" for character in str(value or ""))
    return text.strip("_") or "full_flow"


def _runtime_log_path(runtime_dir: Path, prefix: str, status: KitStatus) -> Path:
    log_dir = Path(runtime_dir) / "_runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(f"{status.paths.truck_number}_{status.paths.project_name}")
    return log_dir / f"{prefix}_{safe}_{int(time.time())}.log"


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _radan_kitter_root(settings: ExplorerSettings) -> Path:
    launcher_text = str(getattr(settings, "radan_kitter_launcher", "") or "").strip()
    if launcher_text:
        launcher = Path(launcher_text)
        if launcher.exists():
            return launcher.parent
    return _tools_root() / "radan_kitter"


def _ensure_sys_path(path: Path) -> None:
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _load_radan_kitter_modules(settings: ExplorerSettings):
    root = _radan_kitter_root(settings)
    if not root.exists():
        raise FileNotFoundError(f"RADAN Kitter folder not found: {root}")
    _ensure_sys_path(root)
    modules = {
        "assets": importlib.import_module("assets"),
        "config": importlib.import_module("config"),
        "kit_service": importlib.import_module("kit_service"),
        "packet_service": importlib.import_module("packet_service"),
        "rf_service": importlib.import_module("rf_service"),
        "rpd_io": importlib.import_module("rpd_io"),
    }
    return modules


def _load_radan_automation_modules():
    root = _tools_root() / "radan_automation"
    if not root.exists():
        raise FileNotFoundError(f"RADAN Automation folder not found: {root}")
    _ensure_sys_path(root)
    import_parts = importlib.import_module("import_parts_csv_headless")
    radan_com = importlib.import_module("radan_com")
    return import_parts, radan_com


def _project_snapshot(project_path: Path) -> dict[str, object]:
    root = _tools_root() / "radan_automation"
    if root.exists():
        _ensure_sys_path(root)
    try:
        gate = importlib.import_module("copied_project_nester_gate")
        snapshot = gate.project_snapshot(Path(project_path))
        return dict(snapshot)
    except Exception as exc:
        return {
            "project_path": str(project_path),
            "snapshot_error": f"{type(exc).__name__}: {exc}",
        }


def _snapshot_summary(snapshot: dict[str, object] | None) -> str:
    if not snapshot:
        return "snapshot unavailable"
    if snapshot.get("snapshot_error"):
        return f"snapshot unavailable ({snapshot.get('snapshot_error')})"
    return (
        f"{snapshot.get('part_count', '?')} part(s), "
        f"{snapshot.get('sheet_count', '?')} sheet(s), "
        f"{snapshot.get('nest_count', '?')} nest row(s), "
        f"{snapshot.get('made_nonzero_count', '?')} made row(s), "
        f"{snapshot.get('used_nest_count', '?')} used nest row(s)"
    )


def _configure_kitter_assets(rk_assets, settings: ExplorerSettings) -> None:
    release_root = str(getattr(settings, "release_root", "") or "").strip()
    fabrication_root = str(getattr(settings, "fabrication_root", "") or "").strip()
    if not fabrication_root:
        return
    mapping = []
    if release_root:
        mapping.append((release_root, fabrication_root))
    mapping.append((r"L:\BATTLESHIELD", fabrication_root))
    try:
        rk_assets.configure_release_mapping(
            w_release_root=fabrication_root,
            eng_release_map=mapping,
            remember_base=False,
        )
    except TypeError:
        rk_assets.configure_release_mapping(fabrication_root, mapping)


def run_inventor_inline_for_status(
    status: KitStatus,
    settings: ExplorerSettings,
    *,
    progress_cb: ProgressCallback | None = None,
) -> InventorFlowResult:
    spreadsheet_path = status.spreadsheet_match.chosen_path
    if spreadsheet_path is None:
        raise FullFlowError("This kit does not have exactly one BOM candidate in the W folder.")
    if status.paths.project_dir is None:
        raise FullFlowError("The L-side project folder is not available for this kit.")

    entry_text = str(getattr(settings, "inventor_to_radan_entry", "") or "").strip()
    if not entry_text:
        raise FullFlowError("Inventor launcher is not configured.")

    _emit(progress_cb, f"Inventor: converting {spreadsheet_path.name}")
    try:
        result = run_inventor_to_radan_inline(Path(entry_text), spreadsheet_path)
    except InventorToRadanInlineNeedsUi as exc:
        raise FullFlowNeedsUserAction(
            f"Inventor-to-RADAN needs user input before the full flow can continue: {exc}"
        ) from exc

    _emit(progress_cb, "Inventor: moving generated CSV/report to L")
    _outputs, moved_paths = move_inventor_outputs_to_project(spreadsheet_path, status.paths.project_dir)
    added_count = getattr(result, "added_count", None)
    try:
        added_count = int(added_count) if added_count is not None else None
    except (TypeError, ValueError):
        added_count = None
    return InventorFlowResult(moved_paths=tuple(Path(path) for path in moved_paths), added_count=added_count)


def run_csv_import_for_status(
    status: KitStatus,
    *,
    runtime_dir: Path,
    progress_cb: ProgressCallback | None = None,
) -> CsvImportResult:
    spreadsheet_path = status.spreadsheet_match.chosen_path
    if spreadsheet_path is None:
        raise FullFlowError("This kit does not have exactly one BOM candidate, so the _Radan.csv path is ambiguous.")
    if status.paths.project_dir is None or not status.paths.project_dir.exists():
        raise FullFlowError("The L-side project folder is not available for this kit.")
    if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
        raise FullFlowError("The L-side project file is missing for this kit.")
    if status.paths.release_kit_dir is None or not status.paths.release_kit_dir.exists():
        raise FullFlowError(f"The expected RADAN symbol output folder is missing: {status.paths.release_kit_dir}")

    running_import, lock_path, lock_pid = radan_csv_import_lock_status(status.paths.rpd_path)
    if running_import:
        raise FullFlowError(f"A RADAN CSV import is already running for this project. PID: {lock_pid}; lock: {lock_path}")

    csv_path = resolve_existing_inventor_csv(spreadsheet_path, status.paths.project_dir)
    log_path = _runtime_log_path(runtime_dir, "full_flow_radan_csv_import", status)
    _emit(progress_cb, f"RADAN import: pushing {csv_path.name} into the RPD")
    process = launch_radan_csv_import(
        csv_path,
        status.paths.release_kit_dir,
        project_path=status.paths.rpd_path,
        log_path=log_path,
        allow_visible_radan=False,
        rebuild_symbols=True,
        preprocess_dxf_outer_profile=True,
        preprocess_dxf_tolerance=0.002,
        project_update_method="direct-xml",
        refresh_project_sheets=True,
    )

    started = time.time()
    while True:
        return_code = process.poll()
        if return_code is not None:
            break
        _emit(progress_cb, f"RADAN import: helper PID {process.pid} still running ({time.time() - started:.0f}s)")
        time.sleep(0.35)

    if int(return_code) != 0:
        detail = ""
        try:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            detail = ""
        raise FullFlowError(
            f"RADAN CSV import failed with exit code {return_code}.\n\nLog: {log_path}\n\n{detail}".strip()
        )
    _emit(progress_cb, f"RADAN import: finished successfully; log {log_path}")
    return CsvImportResult(log_path=log_path, return_code=int(return_code))


def run_kitter_rf_assignment_for_project(
    project_path: Path,
    settings: ExplorerSettings,
    *,
    progress_cb: ProgressCallback | None = None,
) -> RfAssignmentResult:
    modules = _load_radan_kitter_modules(settings)
    rk_assets = modules["assets"]
    rk_config = modules["config"]
    rk_kit_service = modules["kit_service"]
    rk_rf_service = modules["rf_service"]
    rk_rpd_io = modules["rpd_io"]
    _configure_kitter_assets(rk_assets, settings)

    _emit(progress_cb, "Kitter RF: loading project")
    tree, parts, _debug = rk_rpd_io.load_rpd(str(project_path))
    if not parts:
        raise FullFlowError("Kitter RF could not find any parts in the RPD.")

    def _rf_progress(done: int, total: int, status_text: str) -> None:
        _emit(progress_cb, f"Kitter RF: {done}/{total} {status_text}")

    predictions, source = rk_rf_service.run_rf_suggestions(
        parts,
        dataset_path=rk_config.GLOBAL_DATASET_PATH,
        model_path=rk_config.RF_MODEL_PATH,
        meta_path=rk_config.RF_META_PATH,
        feature_cols=rk_config.RF_FEATURES,
        allowed_labels=rk_config.CANON_KITS + [rk_config.BALANCE_KIT],
        resolve_asset_fn=rk_assets.resolve_asset_fast,
        progress_cb=_rf_progress,
    )
    if source == "canceled":
        raise FullFlowError("Kitter RF suggestion was canceled.")

    predicted_count = 0
    for part, prediction in zip(parts, predictions):
        label = str((prediction or ("", 0.0))[0] or "").strip()
        if not label:
            continue
        part.kit_label = label
        predicted_count += 1

    if predicted_count <= 0:
        raise FullFlowError("Kitter RF did not produce any kit assignments.")

    _emit(progress_cb, f"Kitter RF: writing {predicted_count} predicted assignment(s)")
    kit_count = rk_kit_service.prepare_kits(
        parts,
        rpd_path=str(project_path),
        donor_template_path=rk_config.DONOR_TEMPLATE_PATH,
        bak_dirname=rk_config.BAK_DIRNAME,
        kits_dirname=rk_config.KITS_DIRNAME,
        kit_to_priority=rk_config.KIT_TO_PRIORITY,
        progress_cb=lambda done, total, text: _emit(progress_cb, f"Kitter RF: prepare kits {done}/{total} {text}"),
    )
    backup_path = Path(
        rk_kit_service.write_rpd_with_backup(
            tree,
            parts,
            rpd_path=str(project_path),
            bak_dirname=rk_config.BAK_DIRNAME,
        )
    )
    return RfAssignmentResult(
        predicted_count=predicted_count,
        skipped_count=max(0, len(parts) - predicted_count),
        model_source=str(source),
        kit_count=int(kit_count),
        backup_path=backup_path,
    )


def build_all_packets_for_status(
    status: KitStatus,
    settings: ExplorerSettings,
    *,
    progress_cb: ProgressCallback | None = None,
) -> PacketFlowResult:
    if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
        raise FullFlowError("The L-side project file is missing for this kit.")
    if status.paths.fabrication_kit_dir is None or not status.paths.fabrication_kit_dir.exists():
        raise FullFlowError("The W-side kit folder is missing for this kit.")

    _emit(progress_cb, "Packets: preparing build context")
    context = prepare_packet_build_context(
        rpd_path=status.paths.rpd_path,
        fabrication_dir=status.paths.fabrication_kit_dir,
        settings=settings,
        include_assembly_sources=True,
        include_cut_list_sources=True,
    )
    if not context.parts:
        raise FullFlowError("No parts were found in the selected RPD.")

    expected_csv_path = None
    if status.spreadsheet_match.chosen_path is not None and status.paths.project_dir is not None:
        try:
            expected_csv_path = resolve_existing_inventor_csv(status.spreadsheet_match.chosen_path, status.paths.project_dir)
        except Exception:
            expected_csv_path = None
    readiness_warning = validate_print_packet_readiness(
        rpd_path=status.paths.rpd_path,
        parts=context.parts,
        expected_csv_path=expected_csv_path,
    )
    if readiness_warning:
        raise FullFlowError(readiness_warning)

    modules = _load_radan_kitter_modules(settings)
    rk_packet_service = modules["packet_service"]
    packet_paths: list[Path] = []

    def _packet_progress(done: int, total: int, status_text: str) -> None:
        _emit(progress_cb, f"Packets: print {done}/{total} {status_text}")

    _emit(progress_cb, "Packets: building print packet")
    print_packet_path, print_pages, print_missing = rk_packet_service.build_packet(
        list(context.parts),
        rpd_path=str(status.paths.rpd_path),
        out_dirname="_out",
        resolve_asset_fn=context.resolve_asset_fn,
        progress_cb=_packet_progress,
        render_mode="vector",
    )
    packet_paths.append(Path(print_packet_path))

    _emit(progress_cb, "Packets: scanning assembly context")
    assembly_context = scan_assembly_bom_context(
        parts=context.parts,
        source_pdfs=context.assembly_source_pdfs,
        progress_cb=lambda done, total, text: _emit(progress_cb, f"Packets: assembly context {done}/{total} {text}"),
    )
    sym_comment_result = apply_assembly_context_to_sym_comments(
        parts=context.parts,
        result=assembly_context,
        backup_dir=status.paths.rpd_path.parent / "_bak" / "assembly_comments",
    )
    assembly_context_path = write_assembly_bom_context_csv(
        rpd_path=status.paths.rpd_path,
        result=assembly_context,
    )

    _emit(progress_cb, "Packets: building assembly packet")
    assembly_result = build_assembly_packet(
        rpd_path=status.paths.rpd_path,
        source_pdfs=context.assembly_source_pdfs,
        progress_cb=lambda done, total, text: _emit(progress_cb, f"Packets: assembly {done}/{total} {text}"),
    )
    if assembly_result.packet_path:
        packet_paths.append(Path(assembly_result.packet_path))

    _emit(progress_cb, "Packets: building cut list")
    cut_list_result = build_cut_list_packet(
        rpd_path=status.paths.rpd_path,
        source_pdfs=context.cut_list_source_pdfs,
        progress_cb=lambda done, total, text: _emit(progress_cb, f"Packets: cut list {done}/{total} {text}"),
    )
    if cut_list_result.packet_path:
        packet_paths.append(Path(cut_list_result.packet_path))

    return PacketFlowResult(
        packet_paths=tuple(packet_paths),
        print_pages=int(print_pages),
        print_missing=int(print_missing),
        assembly_pages=int(assembly_result.output_pages),
        cut_list_pages=int(cut_list_result.output_pages),
        assembly_context_path=assembly_context_path,
        sym_comment_updated_count=int(sym_comment_result.updated_count),
    )


def run_full_flow_before_nester(
    status: KitStatus,
    settings: ExplorerSettings,
    *,
    runtime_dir: Path,
    progress_cb: ProgressCallback | None = None,
) -> FullFlowResult:
    if status.paths.rpd_path is None:
        raise FullFlowError("The L-side project file is not available for this kit.")
    _emit(progress_cb, "Full flow 1/4: running Inventor tool")
    inventor = run_inventor_inline_for_status(status, settings, progress_cb=progress_cb)
    _emit(progress_cb, "Full flow 2/4: pushing parts into the RPD")
    csv_import = run_csv_import_for_status(status, runtime_dir=runtime_dir, progress_cb=progress_cb)
    _emit(progress_cb, "Full flow 3/4: running Kitter RF assignment")
    rf_assignment = run_kitter_rf_assignment_for_project(status.paths.rpd_path, settings, progress_cb=progress_cb)
    _emit(progress_cb, "Full flow 4/4: building print, assembly, and cut-list packets")
    packets = build_all_packets_for_status(status, settings, progress_cb=progress_cb)
    _emit(progress_cb, "Full flow: pre-nester work complete")
    return FullFlowResult(
        project_path=status.paths.rpd_path,
        inventor=inventor,
        csv_import=csv_import,
        rf_assignment=rf_assignment,
        packets=packets,
    )


def run_headless_nester(
    project_path: Path,
    *,
    runtime_dir: Path,
    progress_cb: ProgressCallback | None = None,
) -> NesterResult:
    import_parts, radan_com = _load_radan_automation_modules()
    project = Path(project_path)
    if not project.exists():
        raise FileNotFoundError(str(project))

    log_path = Path(runtime_dir) / "_runtime" / f"full_flow_nester_{_safe_name(project.stem)}_{int(time.time())}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = import_parts._Logger(log_path)
    preexisting_visible_pids = import_parts._visible_radan_process_ids()
    before_snapshot = _project_snapshot(project)
    logger.write(f"Before nesting snapshot: {_snapshot_summary(before_snapshot)}")
    _emit(progress_cb, f"Nester precheck: {_snapshot_summary(before_snapshot)}")
    app = None
    should_quit_app = False
    started = time.time()
    try:
        _emit(progress_cb, "Nester: starting hidden RADAN automation")
        app = radan_com.open_application(backend="win32com", force_new_instance=True)
        info, should_quit_app = import_parts._resolve_automation_instance(app, preexisting_visible_pids, logger)
        logger.write(f"Started hidden RADAN automation PID {info.process_id} for full flow nester.")
        app.visible = False
        try:
            app.interactive = False
        except Exception:
            pass
        mac = import_parts._mac_object(app)

        _emit(progress_cb, "Nester: opening project")
        if not bool(mac.prj_open(str(project))):
            raise RuntimeError(f"RADAN prj_open failed for {project}")
        try:
            opened_project_path = str(mac.prj_get_file_path())
        except Exception:
            opened_project_path = str(project)
        logger.write(f"Opened project: {opened_project_path}")

        _emit(progress_cb, "Nester: refreshing sheets")
        try:
            mac.Execute(import_parts.PROJECT_SHEETS_REFRESH_MAC_LINE)
            mac.prj_save()
        except Exception as exc:
            logger.write(f"Sheet refresh before nesting failed: {type(exc).__name__}: {exc}")

        refreshed_snapshot = _project_snapshot(project)
        logger.write(f"After sheet refresh snapshot: {_snapshot_summary(refreshed_snapshot)}")
        _emit(progress_cb, f"Nester: sheet refresh done; {_snapshot_summary(refreshed_snapshot)}")

        _emit(
            progress_cb,
            "Nester: running RADAN lay_run_nest(0). This can take a while or return no nests if parts do not fit sheets.",
        )
        return_code = mac.lay_run_nest(0)
        elapsed = time.time() - started
        logger.write(f"lay_run_nest(0) returned {return_code} in {elapsed:.3f}s.")
        try:
            mac.prj_save()
        except Exception as exc:
            logger.write(f"Save after nesting failed: {type(exc).__name__}: {exc}")
        try:
            mac.prj_close()
        except Exception as exc:
            logger.write(f"Project close after nesting failed: {type(exc).__name__}: {exc}")
        try:
            if should_quit_app:
                app.quit()
        except Exception as exc:
            logger.write(f"RADAN quit after nesting failed: {type(exc).__name__}: {exc}")

        after_snapshot = _project_snapshot(project)
        drg_count = len(tuple(project.parent.rglob("*.drg")))
        ok = int(return_code) == 0 and drg_count > 0
        logger.write(f"After nesting snapshot: {_snapshot_summary(after_snapshot)}")
        _emit(progress_cb, f"Nester: returned {return_code} after {elapsed:.1f}s; found {drg_count} DRG(s)")
        if not ok:
            _emit(
                progress_cb,
                "Nester: no usable nests were created. If RADAN has oversize-part or sheet-fit prompts, open RADAN and click Run Nester manually.",
            )
        payload = {
            "project_path": str(project),
            "return_code": return_code,
            "elapsed_seconds": elapsed,
            "drg_count": drg_count,
            "ok": ok,
            "before": before_snapshot,
            "after": after_snapshot,
        }
        log_path.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return NesterResult(
            ok=ok,
            return_code=int(return_code),
            elapsed_seconds=float(elapsed),
            log_path=log_path,
            drg_count=drg_count,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
    finally:
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
