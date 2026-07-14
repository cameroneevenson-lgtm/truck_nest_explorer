from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog, QPushButton

from background_job import BackgroundJobWorker
from models import KitStatus
from packet_build_service import (
    DEFAULT_PACKET_OUT_DIR,
    PacketBuildReadinessError,
    apply_assembly_context_to_sym_comments,
    apply_assembly_notes_to_parts,
    apply_title_block_descriptions_from_csv,
    build_assembly_packet,
    build_cut_list_packet,
    create_main_packet_worker,
    prepare_packet_build_context,
    review_pdf_assets_for_action,
    scan_assembly_bom_context,
    scan_title_block_descriptions,
    validate_print_packet_readiness,
    write_assembly_bom_context_csv,
    write_title_block_descriptions_csv,
)
from performance_metrics import normalize_cache_path
from services import (
    FILE_METADATA_CACHE,
    detect_assembly_packet_pdf,
    detect_cut_list_packet_pdf,
    detect_print_packet_pdf,
    fabrication_kit_dir_ready,
    open_path,
    radan_csv_import_lock_status,
    resolve_existing_inventor_csv,
)


@dataclass(frozen=True)
class PacketBuildGuard:
    title: str
    action_key: str
    status: KitStatus
    lock_key: tuple[str, str]


# Kits made up of standalone structural/mechanical hardware rather than body
# panels - their parts don't turn up referenced in other kits' .iam assembly
# drawings, so scanning for assembly context (and stamping "ASM: ..." notes)
# is just wasted search time and noise, not a missing feature.
ASSEMBLY_CONTEXT_EXEMPT_KIT_NAMES = frozenset({"pump house", "pump mounts", "pump brackets"})


def _kit_wants_assembly_context(status: KitStatus | None) -> bool:
    if status is None:
        return True
    kit_name = str(getattr(status.paths, "kit_name", "") or "").strip().casefold()
    return kit_name not in ASSEMBLY_CONTEXT_EXEMPT_KIT_NAMES


class PacketBuildController:
    def __init__(
        self,
        window,
        *,
        print_button: QPushButton,
        assembly_button: QPushButton,
        cut_list_button: QPushButton,
        title_scan_button: QPushButton | None = None,
        title_apply_button: QPushButton | None = None,
    ) -> None:
        self.window = window
        self._print_button = print_button
        self._assembly_button = assembly_button
        self._cut_list_button = cut_list_button
        self._title_scan_button = title_scan_button
        self._title_apply_button = title_apply_button
        self._lock = threading.RLock()
        self._active_keys: set[tuple[str, str]] = set()
        self._running = False
        self._worker = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._running or self._active_keys)

    def _guard_key(self, status: KitStatus, action_key: str) -> tuple[str, str]:
        path_source = status.paths.rpd_path or status.paths.project_dir or status.paths.display_name
        return (str(action_key or "").strip().casefold(), normalize_cache_path(path_source))

    def _begin_guard(self, title: str, status: KitStatus, action_key: str) -> PacketBuildGuard | None:
        guard = PacketBuildGuard(
            title=title,
            action_key=str(action_key or "").strip().casefold(),
            status=status,
            lock_key=self._guard_key(status, action_key),
        )
        with self._lock:
            if self._running or self._active_keys:
                QMessageBox.information(
                    self.window,
                    title,
                    "A packet build is already running. Let it finish before starting another one.",
                )
                return None
            self._active_keys.add(guard.lock_key)
            self._running = True
        self.refresh_button_states()
        return guard

    def _finish_guard(self, guard: PacketBuildGuard | None) -> None:
        if guard is None:
            return
        with self._lock:
            self._active_keys.discard(guard.lock_key)
            self._running = bool(self._active_keys)
            if not self._running:
                self._worker = None
        self.refresh_button_states()

    def _match_for_action(self, status: KitStatus, action_key: str, *, use_cache: bool) -> object:
        cache = FILE_METADATA_CACHE if use_cache else None
        normalized = str(action_key or "").strip().casefold()
        if normalized in {"print", "part"}:
            return detect_print_packet_pdf(status.paths, fs_cache=cache)
        if normalized == "assembly":
            return detect_assembly_packet_pdf(status.paths, fs_cache=cache)
        if normalized == "cut_list":
            return detect_cut_list_packet_pdf(status.paths, fs_cache=cache)
        raise ValueError(f"Unknown packet build action: {action_key!r}")

    def _button_configs(self):
        window = self.window
        return (
            (
                "print",
                self._print_button,
                "Build Print Packet",
                "Print Packet Ready",
                "Build the QTY print packet from the selected kit's saved RPD.",
                "Print packet",
                "",
            ),
            (
                "assembly",
                self._assembly_button,
                "Build Assembly Packet",
                "Assembly Packet Ready",
                "Build the .iam-backed assembly drawing packet from the selected kit's saved RPD.",
                "Assembly packet",
                window.ASSEMBLY_PACKET_DISABLED_REASON if not window.ASSEMBLY_PACKET_BUILD_ENABLED else "",
            ),
            (
                "cut_list",
                self._cut_list_button,
                "Build Cut List",
                "Cut List Ready",
                "Build the non-laser cut list packet from the selected kit's saved RPD.",
                "Cut list",
                window.CUT_LIST_DISABLED_REASON if not window.CUT_LIST_BUILD_ENABLED else "",
            ),
        )

    def refresh_button_states(self) -> None:
        status = self.window._current_status()
        with self._lock:
            active = bool(self._running or self._active_keys)
            active_actions = {key[0] for key in self._active_keys}
        for action_key, button, default_text, ready_text, default_tooltip, packet_label, disabled_reason in (
            self._button_configs()
        ):
            button.setText(default_text)
            button.setToolTip(default_tooltip)
            button.setEnabled(status is not None)
            if disabled_reason:
                button.setEnabled(False)
                button.setToolTip(disabled_reason)
                continue
            if status is None:
                button.setEnabled(False)
                button.setToolTip("Select a kit first.")
                continue
            if active:
                button.setEnabled(False)
                if action_key in active_actions:
                    button.setText("Building...")
                button.setToolTip("A packet build is already running.")
                continue
            match = self._match_for_action(status, action_key, use_cache=True)
            packet_path = getattr(match, "chosen_path", None)
            if packet_path is not None:
                button.setText(ready_text)
                button.setEnabled(True)
                button.setToolTip(
                    f"{packet_label} already exists:\n{packet_path}\n\n"
                    "Use the packet cell in the table to open it, or click again to rebuild it."
                )

        for button in (self._title_scan_button, self._title_apply_button):
            if button is None:
                continue
            button.setEnabled(status is not None and not active)

    def _confirm_rebuild(
        self,
        *,
        title: str,
        status: KitStatus,
        action_key: str,
        match: object,
    ) -> bool:
        window = self.window
        packet_path = getattr(match, "chosen_path", None)
        packet_label = {
            "print": "Print packet",
            "part": "Print packet",
            "assembly": "Assembly packet",
            "cut_list": "Cut list",
        }.get(str(action_key or "").strip().casefold(), "Packet")
        choice = QMessageBox.question(
            window,
            title,
            (
                f"{packet_label} already exists for {status.paths.display_name}:\n"
                f"{packet_path}\n\n"
                "Rebuild it anyway? The existing file will be replaced."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            window.log(f"Skipped {title.lower()} for {status.paths.display_name}; existing packet: {packet_path}")
            window._refresh_packet_statuses(status)
            return False
        try:
            Path(packet_path).unlink(missing_ok=True)
        except Exception:
            pass
        window.log(
            f"Rebuilding {title.lower()} for {status.paths.display_name}; replacing existing packet: {packet_path}"
        )
        return True

    def prepare_context(
        self,
        title: str,
        *,
        action_key: str,
        include_assembly_sources: bool = False,
        include_cut_list_sources: bool = False,
    ):
        window = self.window
        status = window._current_status()
        if status is None:
            QMessageBox.information(window, title, "Select a kit first.")
            return None, None, None
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(
                window,
                title,
                "The L-side project file is missing for this kit.",
            )
            return None, None, None
        running_import, _lock_path, lock_pid = radan_csv_import_lock_status(status.paths.rpd_path)
        if running_import:
            pid_text = f" PID {lock_pid}" if lock_pid is not None else ""
            QMessageBox.warning(
                window,
                title,
                "A RADAN CSV import is still running for this project.\n\n"
                f"Let the import helper{pid_text} finish before building packets.",
            )
            return None, None, None
        if not fabrication_kit_dir_ready(status.paths.fabrication_kit_dir):
            QMessageBox.warning(
                window,
                title,
                "The W-side kit folder is missing for this kit.",
            )
            return None, None, None

        guard = self._begin_guard(title, status, action_key)
        if guard is None:
            return None, None, None

        # Idempotency check: packet builds create timestamped artifacts. Before
        # doing any RPD/PDF work, look for an existing generated packet with a
        # fresh filesystem read so repeated clicks, restarts, or externally
        # generated packets do not produce duplicate packet files.
        existing_match = self._match_for_action(status, action_key, use_cache=False)
        if getattr(existing_match, "chosen_path", None) is not None:
            if not self._confirm_rebuild(
                title=title,
                status=status,
                action_key=action_key,
                match=existing_match,
            ):
                self._finish_guard(guard)
                return None, None, None

        try:
            context = prepare_packet_build_context(
                rpd_path=status.paths.rpd_path,
                fabrication_dir=status.paths.fabrication_kit_dir,
                settings=window.settings,
                include_assembly_sources=include_assembly_sources,
                include_cut_list_sources=include_cut_list_sources,
            )
        except Exception as exc:
            self._finish_guard(guard)
            QMessageBox.critical(window, title, str(exc))
            return None, None, None

        return status, context, guard

    def _close_progress_and_finish(self, progress: QProgressDialog, guard: PacketBuildGuard) -> None:
        try:
            progress.close()
        except Exception:
            pass
        self._finish_guard(guard)

    def _run_dict_payload_build(
        self,
        *,
        title: str,
        guard: PacketBuildGuard,
        status: KitStatus,
        initial_message: str,
        progress_label_prefix: str,
        job_fn: Callable[[BackgroundJobWorker], dict[str, object]],
        no_sources_message: str,
        canceled_log_message: str,
        failure_message: str,
        on_success: Callable[[dict[str, object], object], None],
    ) -> None:
        """Shared shape for build_assembly_packet and build_cut_list_packet.

        Both discover their source PDFs and build the packet on a
        background thread via a `_job(worker) -> dict` closure that
        returns `{"state": "done"|"empty"|"canceled", "context": ..., "result": ...}`,
        show an indeterminate QProgressDialog while it runs, and dispatch
        the completed payload the same way: "empty" -> a "no source PDFs
        found" message with the searched folders, canceled (by flag or by
        `result.skipped`) -> a log line, `result is None` -> the same
        "no sources" message, otherwise refresh the packet-status cells
        and hand off to `on_success(data, result)` for the action-specific
        stats/opening/logging.

        build_print_packet does NOT use this: it runs synchronous
        pre-work (readiness checks, assembly-context PDF stamping) before
        the worker even starts, knows its total step count upfront (a
        determinate progress bar rather than a busy indicator), and uses
        create_main_packet_worker, which has a different signal contract
        (separate done/canceled/empty signals with positional arguments
        instead of one done signal carrying a state dict). Forcing it
        into this same template would mean either changing that worker's
        signal contract or bolting on enough parameters/branches that the
        template would be harder to follow than the duplication it
        replaced.
        """
        window = self.window
        progress = QProgressDialog(initial_message, "Cancel", 0, 0, window)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = BackgroundJobWorker(job_fn)
        self._worker = worker

        def _set_progress(done: int, total: int, status_text: str) -> None:
            if int(total) <= 0:
                progress.setRange(0, 0)
            else:
                progress.setRange(0, int(total))
                progress.setValue(max(0, min(int(total), int(done))))
            progress.setLabelText(f"{progress_label_prefix}\n{status_text}")

        def _cleanup() -> None:
            self._close_progress_and_finish(progress, guard)

        def _on_progress(done: int, total: int, status_text: str) -> None:
            _set_progress(int(done), int(total), status_text)

        def _on_done(payload: object) -> None:
            _cleanup()
            data = payload if isinstance(payload, dict) else {}
            result = data.get("result")
            discovered_context = data.get("context")
            state = str(data.get("state") or "")
            if state == "empty":
                searched_roots = getattr(discovered_context, "assembly_search_roots", ()) or ()
                searched = "\n".join(str(path) for path in searched_roots) or "(none)"
                QMessageBox.information(window, title, f"{no_sources_message}\n\nSearched:\n{searched}")
                return
            if state == "canceled" or bool(getattr(result, "skipped", False)):
                window.log(canceled_log_message)
                return
            if result is None:
                QMessageBox.information(window, title, no_sources_message)
                return

            window._refresh_packet_statuses(status)
            on_success(data, result)

        def _on_error(tb: str) -> None:
            _cleanup()
            QMessageBox.critical(window, title, str(tb or "").strip() or failure_message)

        worker.signals.progress.connect(_on_progress)
        worker.signals.done.connect(_on_done)
        worker.signals.error.connect(_on_error)
        progress.canceled.connect(worker.request_stop)
        _set_progress(0, 0, initial_message)
        worker_started = False
        try:
            QThreadPool.globalInstance().start(worker)
            worker_started = True
        finally:
            if not worker_started:
                self._finish_guard(guard)

    def build_print_packet(self) -> None:
        window = self.window
        include_assembly_sources = _kit_wants_assembly_context(window._current_status())
        status, context, guard = self.prepare_context(
            "Build Print Packet", action_key="print", include_assembly_sources=include_assembly_sources
        )
        if status is None or context is None or guard is None:
            return
        worker_started = False
        try:
            if not context.parts:
                QMessageBox.information(
                    window,
                    "Build Print Packet",
                    "No parts were found in the selected RPD.",
                )
                return

            expected_csv_path = None
            if status.spreadsheet_match.chosen_path is not None:
                try:
                    expected_csv_path = resolve_existing_inventor_csv(
                        status.spreadsheet_match.chosen_path,
                        status.paths.project_dir,
                    )
                except Exception:
                    expected_csv_path = None
            try:
                readiness_warning = validate_print_packet_readiness(
                    rpd_path=status.paths.rpd_path,
                    parts=context.parts,
                    expected_csv_path=expected_csv_path,
                )
            except PacketBuildReadinessError as exc:
                QMessageBox.warning(window, "Build Print Packet", str(exc))
                return
            if readiness_warning:
                choice = QMessageBox.question(
                    window,
                    "Build Print Packet",
                    f"{readiness_warning}\n\nBuild the print packet from the current saved RPD anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if choice != QMessageBox.Yes:
                    return

            if not review_pdf_assets_for_action(
                parent=window,
                action_name="Build Print Packet",
                context=context,
                rpd_path=status.paths.rpd_path,
            ):
                window.log("Print packet build canceled during PDF asset review.")
                return

            if context.assembly_source_pdfs:
                window.log("Build Print Packet: scanning assembly context...")
                assembly_context = scan_assembly_bom_context(
                    parts=context.parts,
                    source_pdfs=context.assembly_source_pdfs,
                )
                # Must run before the worker is created - the print packet
                # stamps each part's assembly_note (if any) under its QTY box.
                apply_assembly_notes_to_parts(context.parts, assembly_context)
                sym_comment_result = apply_assembly_context_to_sym_comments(
                    parts=context.parts,
                    result=assembly_context,
                    backup_dir=status.paths.rpd_path.parent / "_bak" / "assembly_comments",
                )
                window.log(
                    f"Build Print Packet: assembly context updated {sym_comment_result.updated_count} "
                    f".sym comment(s)."
                )

            total_steps = max(1, len(context.parts))
            progress = QProgressDialog("Building print packet...", "Cancel", 0, total_steps, window)
            progress.setWindowTitle("Build Print Packet")
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)
            progress.setAutoReset(False)

            worker = create_main_packet_worker(
                context=context,
                rpd_path=status.paths.rpd_path,
            )
            self._worker = worker

            def _set_progress(done: int, status_text: str) -> None:
                progress.setMaximum(total_steps)
                progress.setValue(max(0, min(total_steps, int(done))))
                progress.setLabelText(f"Building print packet...\n{status_text}")
                QApplication.processEvents()

            def _cleanup() -> None:
                self._close_progress_and_finish(progress, guard)

            def _on_progress(done: int, total: int, status_text: str) -> None:
                _set_progress(int(done), status_text)

            def _on_done(packet_path: str, pages: int, missing: int) -> None:
                _cleanup()
                window._refresh_packet_statuses(status)
                try:
                    open_path(Path(packet_path))
                except Exception:
                    pass
                QMessageBox.information(
                    window,
                    "Build Print Packet",
                    (
                        f"Print packet pages: {int(pages)}\n"
                        f"Missing part PDFs: {int(missing)}\n"
                        f"Print packet: {packet_path}"
                    ),
                )
                window.log(f"Built print packet for {status.paths.display_name}.")

            def _on_canceled(pages: int, missing: int) -> None:
                _cleanup()
                window.log("Print packet build canceled.")

            def _on_empty(message: str, pages: int, missing: int) -> None:
                _cleanup()
                QMessageBox.information(
                    window,
                    "Build Print Packet",
                    f"{message}\n\nMissing part PDFs: {int(missing)}",
                )

            def _on_error(tb: str) -> None:
                _cleanup()
                QMessageBox.critical(
                    window,
                    "Build Print Packet",
                    str(tb or "").strip() or "Packet build failed.",
                )

            worker.signals.progress.connect(_on_progress)
            worker.signals.done.connect(_on_done)
            worker.signals.canceled.connect(_on_canceled)
            worker.signals.empty.connect(_on_empty)
            worker.signals.error.connect(_on_error)
            progress.canceled.connect(worker.request_stop)
            _set_progress(0, "Starting")
            QThreadPool.globalInstance().start(worker)
            worker_started = True
        finally:
            if not worker_started:
                self._finish_guard(guard)

    def build_assembly_packet(self) -> None:
        window = self.window
        if not window.ASSEMBLY_PACKET_BUILD_ENABLED:
            QMessageBox.information(window, "Build Assembly Packet", window.ASSEMBLY_PACKET_DISABLED_REASON)
            return
        status, context, guard = self.prepare_context("Build Assembly Packet", action_key="assembly")
        if status is None or context is None or guard is None:
            return

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            worker.emit_progress(0, 0, "Searching W/L folders for .iam-backed drawing PDFs")
            discovered_context = prepare_packet_build_context(
                rpd_path=status.paths.rpd_path,
                fabrication_dir=status.paths.fabrication_kit_dir,
                settings=window.settings,
                include_assembly_sources=True,
                include_cut_list_sources=False,
            )
            if worker.should_cancel():
                return {"state": "canceled", "context": discovered_context, "result": None}
            if not discovered_context.assembly_source_pdfs:
                return {"state": "empty", "context": discovered_context, "result": None}
            context_result = scan_assembly_bom_context(
                parts=discovered_context.parts,
                source_pdfs=discovered_context.assembly_source_pdfs,
                progress_cb=lambda done, total, status_text: worker.emit_progress(done, total, status_text),
                should_cancel_cb=worker.should_cancel,
            )
            worker.emit_progress(0, 0, "Assembly context | Updating part SYM comments")
            sym_comment_result = apply_assembly_context_to_sym_comments(
                parts=discovered_context.parts,
                result=context_result,
                backup_dir=status.paths.rpd_path.parent / "_bak" / "assembly_comments",
            )
            context_report_path = write_assembly_bom_context_csv(
                rpd_path=status.paths.rpd_path,
                result=context_result,
            )
            if worker.should_cancel():
                return {
                    "state": "canceled",
                    "context": discovered_context,
                    "result": None,
                    "assembly_context": context_result,
                    "assembly_context_report": context_report_path,
                    "sym_comment_result": sym_comment_result,
                }
            result = build_assembly_packet(
                rpd_path=status.paths.rpd_path,
                source_pdfs=discovered_context.assembly_source_pdfs,
                progress_cb=lambda done, total, status_text: worker.emit_progress(done, total, status_text),
                should_cancel_cb=worker.should_cancel,
            )
            return {
                "state": "done",
                "context": discovered_context,
                "result": result,
                "assembly_context": context_result,
                "assembly_context_report": context_report_path,
                "sym_comment_result": sym_comment_result,
            }

        def _on_success(data: dict[str, object], result: object) -> None:
            assembly_context = data.get("assembly_context")
            assembly_context_report = data.get("assembly_context_report")
            sym_comment_result = data.get("sym_comment_result")
            packet_path = str(getattr(result, "packet_path", "") or "")
            context_report_text = str(assembly_context_report or "")
            reference_count = len(getattr(assembly_context, "references", ()) or ())
            read_error_count = len(getattr(assembly_context, "read_errors", ()) or ())
            sym_comment_updated = int(getattr(sym_comment_result, "updated_count", 0) or 0)
            sym_comment_skipped = int(getattr(sym_comment_result, "skipped_count", 0) or 0)
            sym_comment_missing = int(getattr(sym_comment_result, "missing_count", 0) or 0)
            sym_comment_errors = len(getattr(sym_comment_result, "errors", ()) or ())
            if packet_path:
                try:
                    open_path(Path(packet_path))
                except Exception:
                    pass
                QMessageBox.information(
                    window,
                    "Build Assembly Packet",
                    (
                        f"Assembly packet documents: {int(getattr(result, 'source_documents', 0))}\n"
                        f"Assembly packet pages: {int(getattr(result, 'output_pages', 0))}\n"
                        f"Assembly part references: {reference_count}\n"
                        f"SYM assembly comments: {sym_comment_updated} updated, {sym_comment_skipped} skipped, {sym_comment_missing} missing, {sym_comment_errors} error(s)\n"
                        f"Assembly context read errors: {read_error_count}\n"
                        f"Assembly context: {context_report_text}\n"
                        f"Assembly packet: {packet_path}"
                    ),
                )
                window.log(f"Built assembly packet for {status.paths.display_name}.")
                return

            QMessageBox.information(
                window,
                "Build Assembly Packet",
                "No drawing-size pages were found in the .iam-backed PDFs.",
            )

        self._run_dict_payload_build(
            title="Build Assembly Packet",
            guard=guard,
            status=status,
            initial_message="Searching for assembly drawing PDFs...",
            progress_label_prefix="Building assembly packet...",
            job_fn=_job,
            no_sources_message="No .iam-backed assembly drawing PDFs were found.",
            canceled_log_message="Assembly packet build canceled.",
            failure_message="Assembly packet build failed.",
            on_success=_on_success,
        )

    def build_cut_list_packet(self) -> None:
        window = self.window
        if not window.CUT_LIST_BUILD_ENABLED:
            QMessageBox.information(window, "Build Cut List", window.CUT_LIST_DISABLED_REASON)
            return
        status, context, guard = self.prepare_context("Build Cut List", action_key="cut_list")
        if status is None or context is None or guard is None:
            return

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            worker.emit_progress(0, 0, "Searching W/L folders for non-laser part PDFs")
            discovered_context = prepare_packet_build_context(
                rpd_path=status.paths.rpd_path,
                fabrication_dir=status.paths.fabrication_kit_dir,
                settings=window.settings,
                include_assembly_sources=_kit_wants_assembly_context(status),
                include_cut_list_sources=True,
            )
            if worker.should_cancel():
                return {"state": "canceled", "context": discovered_context, "result": None}
            if not discovered_context.cut_list_source_pdfs:
                return {"state": "empty", "context": discovered_context, "result": None}
            result = build_cut_list_packet(
                rpd_path=status.paths.rpd_path,
                source_pdfs=discovered_context.cut_list_source_pdfs,
                assembly_source_pdfs=discovered_context.assembly_source_pdfs,
                progress_cb=lambda done, total, status_text: worker.emit_progress(done, total, status_text),
                should_cancel_cb=worker.should_cancel,
            )
            return {"state": "done", "context": discovered_context, "result": result}

        def _on_success(data: dict[str, object], result: object) -> None:
            packet_path = str(getattr(result, "packet_path", "") or "")
            if packet_path:
                try:
                    open_path(Path(packet_path))
                except Exception:
                    pass
                QMessageBox.information(
                    window,
                    "Build Cut List",
                    (
                        f"Cut list documents: {int(getattr(result, 'source_documents', 0))}\n"
                        f"Cut list pages: {int(getattr(result, 'output_pages', 0))}\n"
                        f"Cut list: {packet_path}"
                    ),
                )
                window.log(f"Built cut list for {status.paths.display_name}.")
                return

            QMessageBox.information(
                window,
                "Build Cut List",
                "No pages were found in the non-laser cut list PDFs.",
            )

        self._run_dict_payload_build(
            title="Build Cut List",
            guard=guard,
            status=status,
            initial_message="Searching for non-laser cut list PDFs...",
            progress_label_prefix="Building cut list...",
            job_fn=_job,
            no_sources_message="No non-laser cut list PDFs were found.",
            canceled_log_message="Cut list build canceled.",
            failure_message="Cut list build failed.",
            on_success=_on_success,
        )

    def scan_title_descriptions(self) -> None:
        """Scan every part's source PDF for the description printed under
        its title-block "TITLE" label and write a CSV pairing it with that
        part's current .sym comment (RADAN attribute 109) for review. Does
        NOT touch any .sym file - apply_title_descriptions_from_csv is the
        separate, explicit step that writes the (possibly hand-edited) CSV
        back, since the comment field already carries real shop notes like
        "Weld" that a scan alone should never overwrite."""
        window = self.window
        title = "Scan Title Descriptions"
        status = window._current_status()
        if status is None:
            QMessageBox.information(window, title, "Select a kit first.")
            return
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(window, title, "The L-side project file is missing for this kit.")
            return

        try:
            context = prepare_packet_build_context(
                rpd_path=status.paths.rpd_path,
                fabrication_dir=status.paths.fabrication_kit_dir,
                settings=window.settings,
                include_assembly_sources=False,
                include_cut_list_sources=False,
            )
        except Exception as exc:
            QMessageBox.critical(window, title, str(exc))
            return
        if not context.parts:
            QMessageBox.information(window, title, "No parts were found in the selected RPD.")
            return

        try:
            result = scan_title_block_descriptions(
                parts=context.parts,
                resolve_asset_fn=context.resolve_asset_fn,
            )
        except Exception as exc:
            QMessageBox.critical(window, title, str(exc))
            return

        report_path = write_title_block_descriptions_csv(rpd_path=status.paths.rpd_path, result=result)
        try:
            open_path(report_path)
        except Exception:
            pass

        titles_found = sum(1 for row in result.rows if row.extracted_title)
        pending_changes = sum(1 for row in result.rows if row.proposed_comment != row.current_comment)
        QMessageBox.information(
            window,
            title,
            (
                f"Parts checked: {result.checked_part_count}\n"
                f"Source PDFs matched: {result.matched_pdf_count}\n"
                f"Titles found: {titles_found}\n"
                f"Proposed comment changes: {pending_changes}\n"
                f"Read errors: {len(result.read_errors)}\n\n"
                "Review (and edit if needed) the proposed_comment column, then use "
                "'Apply Title Descriptions' to push it into each part's .sym comment:\n"
                f"{report_path}"
            ),
        )
        window.log(f"Scanned title-block descriptions for {status.paths.display_name}: {report_path}")

    def apply_title_descriptions_from_csv(self) -> None:
        window = self.window
        title = "Apply Title Descriptions"
        status = window._current_status()
        if status is None:
            QMessageBox.information(window, title, "Select a kit first.")
            return
        if status.paths.rpd_path is None:
            QMessageBox.warning(window, title, "The L-side project file is missing for this kit.")
            return

        out_dir = status.paths.rpd_path.parent / DEFAULT_PACKET_OUT_DIR
        start_dir = str(out_dir) if out_dir.exists() else str(status.paths.rpd_path.parent)
        csv_path_text, _selected_filter = QFileDialog.getOpenFileName(
            window,
            title,
            start_dir,
            "Title Description CSV (TitleBlockDescriptions_*.csv);;CSV Files (*.csv);;All Files (*)",
        )
        if not csv_path_text:
            return

        choice = QMessageBox.question(
            window,
            title,
            (
                f"Apply proposed_comment from:\n{csv_path_text}\n\n"
                "to each row's .sym file? This overwrites the RADAN part comment field "
                "(a backup of each changed .sym is kept first)."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return

        try:
            result = apply_title_block_descriptions_from_csv(
                csv_path=Path(csv_path_text),
                backup_dir=status.paths.rpd_path.parent / "_bak" / "title_block_comments",
            )
        except Exception as exc:
            QMessageBox.critical(window, title, str(exc))
            return

        QMessageBox.information(
            window,
            title,
            (
                f"SYM comments updated: {result.updated_count}\n"
                f"Skipped (blank/unchanged): {result.skipped_count}\n"
                f"Missing .sym files: {result.missing_count}\n"
                f"Errors: {len(result.errors)}"
            ),
        )
        window.log(
            f"Applied title-block descriptions from {csv_path_text}: {result.updated_count} .sym comment(s) updated."
        )
