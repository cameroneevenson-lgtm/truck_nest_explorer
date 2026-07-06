from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from background_job import BackgroundJobWorker
from dialogs.inventor_report_review_dialog import InventorReviewState, review_inventor_result
from full_flow_service import FullFlowError, FullFlowResult, run_full_flow_after_inventor_review, run_headless_nester
from inventor_service import InventorNeedsUserAction, InventorRunResult, InventorServiceError, run_inventor_for_status
from models import KitStatus
from services import open_path, visible_radan_sessions


class FullFlowPhase(Enum):
    IDLE = "idle"
    INVENTOR = "inventor"
    REPORT_REVIEW = "report_review"
    POST_REVIEW = "post_review"
    NESTER = "nester"
    FINALIZING = "finalizing"


@dataclass
class FullFlowRunContext:
    run_id: int
    status: KitStatus
    run_nester: bool
    phase: FullFlowPhase
    inventor_result: InventorRunResult | None = None
    full_flow_result: FullFlowResult | None = None
    opened_packet_count: int = 0
    finished: bool = False


class _FullFlowProgressDialog(QDialog):
    def __init__(self, parent: QWidget | None, status: KitStatus) -> None:
        super().__init__(parent)
        self._active = True
        self.setWindowTitle("Run Full Flow Progress")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.resize(900, 540)

        title_label = QLabel(f"Running full flow for {status.paths.display_name}")
        title_label.setStyleSheet("font-size: 14px; font-weight: 700;")
        self._status_label = QLabel("Starting...")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #475569;")

        self._viewer = QPlainTextEdit()
        self._viewer.setReadOnly(True)
        self._viewer.setPlaceholderText("Progress will appear here as each step reports back.")

        self._close_button = QPushButton("Running...")
        self._close_button.setEnabled(False)
        self._close_button.clicked.connect(self.close)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._viewer, 1)
        layout.addLayout(button_row)

    def append(self, message: str) -> None:
        text = str(message or "Working...").strip() or "Working..."
        self._status_label.setText(text)
        self._viewer.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")
        scrollbar = self._viewer.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        QApplication.processEvents()

    def finish(self, message: str | None = None) -> None:
        if message:
            self.append(message)
        self._active = False
        self.setWindowFlag(Qt.WindowCloseButtonHint, True)
        self._close_button.setText("Dismiss")
        self._close_button.setEnabled(True)
        QApplication.processEvents()

    def closeEvent(self, event) -> None:
        if self._active:
            event.ignore()
            return
        super().closeEvent(event)


class _ActionLock:
    def __init__(
        self,
        *,
        widgets: tuple[QWidget, ...],
        editable_table: QAbstractItemView,
        full_flow_button: QPushButton,
    ) -> None:
        self._widgets = tuple(widgets)
        self._editable_table = editable_table
        self._full_flow_button = full_flow_button
        self._widget_states: list[tuple[QWidget, bool]] = []
        self._table_edit_triggers = None
        self._button_text = "Run Full Flow"
        self._released = True

    def acquire(self) -> None:
        if not self._released:
            self.reapply()
            return
        self._widget_states = [(widget, bool(widget.isEnabled())) for widget in self._widgets]
        self._table_edit_triggers = self._editable_table.editTriggers()
        self._button_text = str(self._full_flow_button.text() or "Run Full Flow")
        self._released = False
        self.reapply()

    def reapply(self) -> None:
        if self._released:
            return
        for widget, _enabled in self._widget_states:
            try:
                widget.setEnabled(False)
            except RuntimeError:
                pass
        try:
            self._editable_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        except RuntimeError:
            pass
        try:
            self._full_flow_button.setText("Running Full Flow...")
        except RuntimeError:
            pass

    def release(self) -> None:
        if self._released:
            return
        for widget, enabled in self._widget_states:
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                pass
        if self._table_edit_triggers is not None:
            try:
                self._editable_table.setEditTriggers(self._table_edit_triggers)
            except RuntimeError:
                pass
        try:
            self._full_flow_button.setText(self._button_text or "Run Full Flow")
        except RuntimeError:
            pass
        self._widget_states = []
        self._table_edit_triggers = None
        self._released = True


class FullFlowController:
    def __init__(
        self,
        window,
        *,
        mutating_widgets: tuple[QWidget, ...],
        editable_table: QAbstractItemView,
    ) -> None:
        self.window = window
        self._mutating_widgets = tuple(mutating_widgets)
        self._editable_table = editable_table
        self._run_serial = 0
        self._context: FullFlowRunContext | None = None
        self._worker: BackgroundJobWorker | None = None
        self._worker_run_id: int | None = None
        self._action_lock: _ActionLock | None = None
        self._progress_dialog: _FullFlowProgressDialog | None = None

    @property
    def is_running(self) -> bool:
        context = self._context
        return context is not None and not context.finished

    def can_close(self) -> bool:
        return not self.is_running

    def reapply_action_lock(self) -> None:
        action_lock = self._action_lock
        if action_lock is not None:
            action_lock.reapply()

    def start_selected(self) -> None:
        if self.is_running:
            QMessageBox.information(
                self.window,
                "Run Full Flow",
                "Full Flow is already running. Let it finish before starting another one.",
            )
            return
        status = self.window._current_status()
        if status is None:
            QMessageBox.information(self.window, "Run Full Flow", "Select a kit first.")
            return
        if status.paths.rpd_path is None:
            QMessageBox.warning(self.window, "Run Full Flow", "The L-side project file is missing for this kit.")
            return

        prompt = QMessageBox(self.window)
        prompt.setWindowTitle("Run Full Flow")
        prompt.setIcon(QMessageBox.Question)
        prompt.setText(f"Run the full flow for {status.paths.display_name}?")
        prompt.setInformativeText(
            "This will run Inventor, import parts into the RPD, apply RF kit assignments, "
            "build and open packets, optionally try headless nesting, then open the RPD. "
            "If RADAN needs sheet-fit decisions, click Run Nester manually after the project opens."
        )
        nester_checkbox = QCheckBox("Try headless nester before opening RADAN")
        nester_checkbox.setChecked(False)
        prompt.setCheckBox(nester_checkbox)
        prompt.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        prompt.setDefaultButton(QMessageBox.Yes)
        if prompt.exec() != QMessageBox.Yes:
            return

        self._run_serial += 1
        context = FullFlowRunContext(
            run_id=self._run_serial,
            status=status,
            run_nester=bool(nester_checkbox.isChecked()),
            phase=FullFlowPhase.INVENTOR,
        )
        self._context = context
        self._show_progress_dialog(status)
        self._action_lock = _ActionLock(
            widgets=self._mutating_widgets,
            editable_table=self._editable_table,
            full_flow_button=self.window.full_flow_button,
        )
        self._action_lock.acquire()
        self._progress(f"Selected kit: {status.paths.display_name}")
        if context.run_nester:
            self._progress("Headless nester option: ON. The project will still open afterward.")
        else:
            self._progress("Headless nester option: OFF. RADAN will open for the manual Run Nester button.")
        self._start_inventor(context)

    def _show_progress_dialog(self, status: KitStatus) -> None:
        dialog = _FullFlowProgressDialog(self.window, status)
        self._progress_dialog = dialog

        def _clear_dialog_reference(*_args) -> None:
            if self._progress_dialog is dialog:
                self._progress_dialog = None

        dialog.destroyed.connect(_clear_dialog_reference)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.append("Full flow started. Checking the selected kit and configured tools.")

    def _progress(self, message: str) -> None:
        dialog = self._progress_dialog
        if dialog is not None:
            dialog.append(str(message))

    def _is_active_run(self, run_id: int) -> bool:
        context = self._context
        return context is not None and context.run_id == run_id and not context.finished

    def _start_worker(
        self,
        run_id: int,
        job_fn: Callable[[BackgroundJobWorker], object],
        done_cb: Callable[[int, object], None],
    ) -> None:
        worker = BackgroundJobWorker(job_fn)
        self._worker = worker
        self._worker_run_id = run_id

        def _on_progress(_done: int, _total: int, status_text: str) -> None:
            if self._is_active_run(run_id):
                self._progress(str(status_text))

        def _on_done(payload: object) -> None:
            if self._is_active_run(run_id):
                done_cb(run_id, payload)

        def _on_error(traceback_text: str) -> None:
            self._on_worker_error(run_id, traceback_text)

        worker.signals.progress.connect(_on_progress)
        worker.signals.done.connect(_on_done)
        worker.signals.error.connect(_on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_worker_error(self, run_id: int, traceback_text: str) -> None:
        if self._finish_run(run_id, "Failed with an unexpected error. See the error dialog for details."):
            QMessageBox.critical(
                self.window,
                "Run Full Flow",
                str(traceback_text or "").strip() or "Full Flow failed.",
            )

    def _finish_run(self, run_id: int, message: str | None = None) -> bool:
        context = self._context
        if context is None or context.run_id != run_id or context.finished:
            return False
        context.finished = True
        context.phase = FullFlowPhase.IDLE
        if self._worker_run_id == run_id:
            self._worker = None
            self._worker_run_id = None
        action_lock = self._action_lock
        self._action_lock = None
        if action_lock is not None:
            action_lock.release()
        dialog = self._progress_dialog
        if dialog is not None:
            dialog.finish(message)
        self._context = None
        return True

    def _start_inventor(self, context: FullFlowRunContext) -> None:
        context.phase = FullFlowPhase.INVENTOR
        status = context.status

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            try:
                inventor = run_inventor_for_status(
                    status,
                    self.window.settings,
                    progress_cb=lambda message: worker.emit_progress(0, 0, message),
                )
                return {"state": "done", "inventor": inventor}
            except InventorNeedsUserAction as exc:
                return {"state": "needs_user_action", "message": str(exc)}
            except InventorServiceError as exc:
                return {"state": "error", "message": str(exc)}

        self._start_worker(context.run_id, _job, self._on_inventor_done)

    def _on_inventor_done(self, run_id: int, payload: object) -> None:
        if not self._is_active_run(run_id):
            return
        data = payload if isinstance(payload, dict) else {}
        state = str(data.get("state") or "")
        if state == "needs_user_action":
            message = str(data.get("message") or "Inventor-to-RADAN needs user action.")
            if self._finish_run(run_id, f"Stopped for user action: {message}"):
                QMessageBox.information(self.window, "Run Full Flow", message)
            return
        if state == "error":
            message = str(data.get("message") or "Inventor-to-RADAN failed.")
            if self._finish_run(run_id, f"Failed: {message}"):
                QMessageBox.critical(self.window, "Run Full Flow", message)
            return
        inventor = data.get("inventor")
        if not isinstance(inventor, InventorRunResult):
            if self._finish_run(run_id, "Inventor-to-RADAN returned an invalid result."):
                QMessageBox.critical(self.window, "Run Full Flow", "Inventor-to-RADAN returned an invalid result.")
            return
        context = self._context
        if context is None or context.run_id != run_id:
            return
        context.inventor_result = inventor
        self._review_report(context)

    def _review_report(self, context: FullFlowRunContext) -> None:
        context.phase = FullFlowPhase.REPORT_REVIEW
        inventor = context.inventor_result
        if inventor is None:
            if self._finish_run(context.run_id, "Inventor-to-RADAN returned an invalid result."):
                QMessageBox.critical(self.window, "Run Full Flow", "Inventor-to-RADAN returned an invalid result.")
            return
        self._progress(f"Inventor report ready for operator review: {getattr(inventor, 'report_path', '')}")
        outcome = review_inventor_result(self.window, inventor)
        if not self._is_active_run(context.run_id):
            return
        if outcome.state == InventorReviewState.ACCEPTED:
            self._progress("Inventor report accepted. Continuing to RADAN CSV import.")
            self._start_post_review(context)
            return
        if outcome.state == InventorReviewState.DISCARDED:
            discard_result = outcome.discard_result
            deleted_paths = tuple(getattr(discard_result, "deleted_paths", ()) or ())
            failed_deletes = tuple(getattr(discard_result, "failed_deletes", ()) or ())
            self.window._queue_status_refresh_for_truck(context.status.paths.truck_number)
            self._log(
                "Discarded Inventor-to-RADAN outputs during Full Flow: "
                + ", ".join(str(path) for path in deleted_paths)
            )
            self._progress("Stopped by operator after Inventor report review.")
            if not self._finish_run(context.run_id, "Stopped by operator after Inventor report review."):
                return
            if failed_deletes:
                QMessageBox.warning(
                    self.window,
                    "Run Full Flow",
                    "The Inventor-to-RADAN report was not acknowledged, but some generated files could not be deleted.\n\n"
                    + "\n".join(failed_deletes),
                )
            else:
                QMessageBox.information(
                    self.window,
                    "Run Full Flow",
                    "Full Flow stopped by operator after Inventor report review.\n\n"
                    "The generated CSV/report were deleted.\n\n"
                    + "\n".join(str(path) for path in deleted_paths),
                )
            return
        message = outcome.message or "Inventor report review failed before RADAN CSV import."
        self._progress(message)
        if self._finish_run(context.run_id, "Failed before RADAN CSV import."):
            QMessageBox.critical(self.window, "Run Full Flow", message)

    def _start_post_review(self, context: FullFlowRunContext) -> None:
        context.phase = FullFlowPhase.POST_REVIEW
        status = context.status
        inventor = context.inventor_result

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            try:
                result = run_full_flow_after_inventor_review(
                    status,
                    self.window.settings,
                    inventor=inventor,
                    runtime_dir=self.window._runtime_dir,
                    progress_cb=lambda message: worker.emit_progress(0, 0, message),
                )
                return {"state": "done", "result": result}
            except FullFlowError as exc:
                return {"state": "error", "message": str(exc)}

        self._start_worker(context.run_id, _job, self._on_post_review_done)

    def _on_post_review_done(self, run_id: int, payload: object) -> None:
        if not self._is_active_run(run_id):
            return
        data = payload if isinstance(payload, dict) else {}
        if data.get("state") == "error":
            message = str(data.get("message") or "Full Flow failed.")
            if self._finish_run(run_id, f"Failed: {message}"):
                QMessageBox.critical(self.window, "Run Full Flow", message)
            return
        result = data.get("result")
        if not isinstance(result, FullFlowResult):
            if self._finish_run(run_id, "Full Flow returned an invalid result."):
                QMessageBox.critical(self.window, "Run Full Flow", "Full Flow returned an invalid result.")
            return
        context = self._context
        if context is None or context.run_id != run_id:
            return
        context.full_flow_result = result
        self._after_post_review(context)

    def _after_post_review(self, context: FullFlowRunContext) -> None:
        result = context.full_flow_result
        if result is None:
            if self._finish_run(context.run_id, "Full Flow returned an invalid result."):
                QMessageBox.critical(self.window, "Run Full Flow", "Full Flow returned an invalid result.")
            return

        opened_packets = 0
        self._progress(f"Opening {len(result.packets.packet_paths)} packet(s).")
        for packet_path in result.packets.packet_paths:
            try:
                open_path(packet_path)
                opened_packets += 1
                self._progress(f"Opened packet: {Path(packet_path).name}")
            except Exception as exc:
                message = f"Could not open packet {packet_path}: {exc}"
                self._log(message)
                self._progress(message)
        context.opened_packet_count = opened_packets

        nester_message = "Headless nester skipped; RADAN opened for manual Run Nester."
        if not context.run_nester:
            self._progress("Skipping headless nester. RADAN will open for manual Run Nester.")
            self._finalize(context, nester_message)
            return

        self._progress("Waiting for close-RADAN confirmation before the headless nester attempt.")
        if not self._confirm_close_radan_for_full_flow():
            self._progress("Headless nester skipped after the close-RADAN confirmation. RADAN will open normally.")
            self._finalize(context, nester_message)
            return

        self._start_nester(context)

    def _start_nester(self, context: FullFlowRunContext) -> None:
        context.phase = FullFlowPhase.NESTER
        result = context.full_flow_result

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            try:
                nester_result = run_headless_nester(
                    result.project_path,
                    runtime_dir=self.window._runtime_dir,
                    progress_cb=lambda message: worker.emit_progress(0, 0, message),
                )
                return {"state": "done", "result": nester_result}
            except Exception as exc:
                return {"state": "error", "message": str(exc)}

        self._start_worker(context.run_id, _job, self._on_nester_done)

    def _on_nester_done(self, run_id: int, payload: object) -> None:
        if not self._is_active_run(run_id):
            return
        context = self._context
        if context is None:
            return
        data = payload if isinstance(payload, dict) else {}
        if data.get("state") == "error":
            nester_msg = f"Headless nester failed: {data.get('message')}"
            self._progress(f"{nester_msg}. RADAN will open for manual Run Nester.")
            QMessageBox.warning(self.window, "Run Full Flow", nester_msg)
            self._finalize(context, nester_msg)
            return
        nester_result = data.get("result")
        changed_count = len(getattr(nester_result, "changed_drg_paths", ()) or ())
        nester_msg = (
            f"Headless nester return: {getattr(nester_result, 'return_code', None)}; "
            f"new/updated DRGs: {changed_count}; "
            f"total DRGs found: {getattr(nester_result, 'drg_count', 0)}; "
            f"log: {getattr(nester_result, 'log_path', '')}"
        )
        if bool(getattr(nester_result, "ok", False)):
            self._progress(f"Headless nester created or updated {changed_count} DRG(s).")
        else:
            self._progress("Headless nester did not create new nests. RADAN will open next; click Run Nester manually.")
            QMessageBox.warning(
                self.window,
                "Run Full Flow",
                (
                    "RADAN did not create new or updated nests during the headless attempt.\n\n"
                    f"Return code: {getattr(nester_result, 'return_code', None)}\n"
                    f"New/updated DRGs: {changed_count}\n"
                    f"Total DRGs found: {getattr(nester_result, 'drg_count', 0)}\n"
                    f"Log: {getattr(nester_result, 'log_path', '')}\n\n"
                    "This can happen when old DRGs already exist, a part is larger than the available sheet, "
                    "or RADAN needs an operator decision. The project will open now; click Run Nester manually in RADAN."
                ),
            )
        self._finalize(context, nester_msg)

    def _finalize(self, context: FullFlowRunContext, nester_message: str) -> None:
        if not self._is_active_run(context.run_id):
            return
        context.phase = FullFlowPhase.FINALIZING
        result = context.full_flow_result
        if result is None:
            if self._finish_run(context.run_id, "Full Flow returned an invalid result."):
                QMessageBox.critical(self.window, "Run Full Flow", "Full Flow returned an invalid result.")
            return
        self._progress(f"Opening RADAN project: {result.project_path}")
        try:
            open_path(result.project_path)
            self._progress("RADAN project open command sent. Click Run Nester manually if nests are not already created.")
        except Exception as exc:
            self._progress(f"Could not open RADAN project: {exc}")
            QMessageBox.warning(self.window, "Run Full Flow", f"Could not open RADAN project:\n{exc}")

        rf_summary = (
            f"Kitter RF skipped: {result.rf_assignment.skipped_reason}"
            if result.rf_assignment.skipped_reason
            else f"RF assignments: {result.rf_assignment.predicted_count}"
        )
        self.window._queue_status_refresh_for_truck(context.status.paths.truck_number)
        self._log(
            f"Full flow complete for {context.status.paths.display_name}: "
            f"{rf_summary}, "
            f"{context.opened_packet_count} packet(s) opened. {nester_message}"
        )
        if self._finish_run(context.run_id, "Full flow complete. RADAN is open or opening for the manual Run Nester step."):
            QMessageBox.information(
                self.window,
                "Run Full Flow",
                (
                    f"Inventor outputs moved: {len(result.inventor.moved_paths)}\n"
                    f"{rf_summary}\n"
                    f"Print packet pages: {result.packets.print_pages} "
                    f"(missing PDFs: {result.packets.print_missing})\n"
                    f"Assembly packet pages: {result.packets.assembly_pages}\n"
                    f"Cut list pages: {result.packets.cut_list_pages}\n"
                    f"Packets opened: {context.opened_packet_count}\n"
                    f"{nester_message}"
                ),
            )

    def _confirm_close_radan_for_full_flow(self) -> bool:
        try:
            visible_sessions = visible_radan_sessions()
        except Exception:
            visible_sessions = ()
        session_text = ""
        if visible_sessions:
            session_text = "\n\nCurrently open RADAN sessions:\n" + "\n".join(
                f"{pid}: {title}" for pid, title in visible_sessions[:8]
            )
            if len(visible_sessions) > 8:
                session_text += f"\n... (+{len(visible_sessions) - 8} more)"
        choice = QMessageBox.warning(
            self.window,
            "Close RADAN Before Nesting",
            "Close any open RADAN windows now, then click OK to run the headless nester.\n\n"
            "Click Cancel to skip nesting and open the RPD instead."
            f"{session_text}",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok,
        )
        if choice != QMessageBox.Ok:
            return False
        try:
            remaining_sessions = visible_radan_sessions()
        except Exception:
            remaining_sessions = ()
        if remaining_sessions:
            QMessageBox.warning(
                self.window,
                "Close RADAN Before Nesting",
                "RADAN still appears to be open, so the headless nester was skipped. The RPD will open normally.",
            )
            return False
        return True

    def _log(self, message: str) -> None:
        log_fn = getattr(self.window, "log", None)
        if callable(log_fn):
            log_fn(str(message))
