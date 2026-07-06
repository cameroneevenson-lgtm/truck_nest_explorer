from __future__ import annotations

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from background_job import BackgroundJobWorker
from dialogs.inventor_report_review_dialog import InventorReviewState, review_inventor_result
from inventor_service import InventorNeedsUserAction, InventorRunResult, InventorServiceError, run_inventor_for_status
from services import collect_kit_statuses


class InventorController:
    def __init__(self, window) -> None:
        self.window = window
        self._running = False
        self._worker: BackgroundJobWorker | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._button_text = "Run Inventor Tool"
        self._button_was_enabled = True
        self._status = None

    def start_selected(self) -> None:
        if self._running:
            QMessageBox.information(
                self.window,
                "Run Inventor Tool",
                "Inventor-to-RADAN is already running. Let it finish before starting another one.",
            )
            return

        status = self.window._current_status()
        if status is None:
            QMessageBox.information(self.window, "Run Inventor Tool", "Select a kit first.")
            return

        self.window._ensure_saved_settings()
        self._status = status
        self._set_running(True)
        self._show_progress("Starting Inventor-to-RADAN...")

        def _job(worker: BackgroundJobWorker) -> dict[str, object]:
            try:
                result = run_inventor_for_status(
                    status,
                    self.window.settings,
                    progress_cb=lambda message: worker.emit_progress(0, 0, message),
                )
                return {"state": "done", "result": result}
            except InventorNeedsUserAction as exc:
                return {"state": "needs_user_action", "message": str(exc)}
            except InventorServiceError as exc:
                return {"state": "error", "message": str(exc)}

        worker = BackgroundJobWorker(_job)
        self._worker = worker
        worker.signals.progress.connect(self._on_progress)
        worker.signals.done.connect(self._on_worker_done)
        worker.signals.error.connect(self._on_worker_error)
        self._start_worker(worker)

    def _start_worker(self, worker: BackgroundJobWorker) -> None:
        QThreadPool.globalInstance().start(worker)

    def _set_running(self, running: bool) -> None:
        button = self.window.launch_inventor_button
        if running:
            self._button_was_enabled = bool(button.isEnabled())
            self._button_text = str(button.text())
            button.setEnabled(False)
            button.setText("Running Inventor...")
            self._running = True
            return
        button.setEnabled(self._button_was_enabled)
        button.setText(self._button_text or "Run Inventor Tool")
        self._running = False

    def _show_progress(self, message: str) -> None:
        progress = QProgressDialog(str(message), "", 0, 0, self.window)
        progress.setWindowTitle("Run Inventor Tool")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setCancelButton(None)
        progress.show()
        self._progress_dialog = progress
        QApplication.processEvents()

    def _on_progress(self, _done: int, _total: int, status_text: str) -> None:
        progress = self._progress_dialog
        if progress is not None:
            progress.setLabelText(str(status_text or "Working..."))
            QApplication.processEvents()

    def _finish_worker(self) -> None:
        progress = self._progress_dialog
        self._progress_dialog = None
        if progress is not None:
            try:
                progress.close()
            except RuntimeError:
                pass
        self._worker = None
        self._set_running(False)

    def _on_worker_error(self, traceback_text: str) -> None:
        self._finish_worker()
        QMessageBox.critical(
            self.window,
            "Run Inventor Tool",
            str(traceback_text or "").strip() or "Inventor-to-RADAN failed.",
        )

    def _on_worker_done(self, payload: object) -> None:
        self._finish_worker()
        data = payload if isinstance(payload, dict) else {}
        state = str(data.get("state") or "")
        status = self._status
        if state == "needs_user_action":
            message = str(data.get("message") or "Inventor-to-RADAN needs user action.")
            self._log(f"Inventor-to-RADAN stopped for user action: {message}")
            QMessageBox.information(self.window, "Run Inventor Tool", message)
            return
        if state == "error":
            message = str(data.get("message") or "Inventor-to-RADAN failed.")
            self._log(f"Inventor-to-RADAN failed: {message}")
            QMessageBox.critical(self.window, "Run Inventor Tool", message)
            return

        result = data.get("result")
        if not isinstance(result, InventorRunResult):
            QMessageBox.critical(self.window, "Run Inventor Tool", "Inventor-to-RADAN returned an invalid result.")
            return

        outcome = review_inventor_result(self.window, result)
        self._refresh_status(status)
        if outcome.state == InventorReviewState.ACCEPTED:
            added_count = result.added_count
            row_text = f" ({added_count} RADAN row{'s' if added_count != 1 else ''})" if added_count is not None else ""
            self._log(
                "Ran Inventor-to-RADAN inline and moved outputs to L: "
                + ", ".join(str(path) for path in result.moved_paths)
            )
            QMessageBox.information(
                self.window,
                "Run Inventor Tool",
                f"Inventor-to-RADAN ran inline{row_text} and the output was moved to L.\n\n"
                + "\n".join(str(path) for path in result.moved_paths),
            )
            return
        if outcome.state == InventorReviewState.DISCARDED:
            discard_result = outcome.discard_result
            deleted_paths = tuple(getattr(discard_result, "deleted_paths", ()) or ())
            failed_deletes = tuple(getattr(discard_result, "failed_deletes", ()) or ())
            self._log("Discarded Inventor-to-RADAN outputs: " + ", ".join(str(path) for path in deleted_paths))
            if failed_deletes:
                QMessageBox.warning(
                    self.window,
                    "Run Inventor Tool",
                    "The Inventor-to-RADAN report was not acknowledged, but some generated files could not be deleted.\n\n"
                    + "\n".join(failed_deletes),
                )
            else:
                QMessageBox.information(
                    self.window,
                    "Run Inventor Tool",
                    "The Inventor-to-RADAN report was not acknowledged, so the generated CSV/report were deleted.\n\n"
                    + "\n".join(str(path) for path in deleted_paths),
                )
            return
        QMessageBox.critical(
            self.window,
            "Run Inventor Tool",
            outcome.message or "Inventor-to-RADAN completed, but the generated report could not be reviewed.",
        )

    def _refresh_status(self, status) -> None:
        if status is None:
            return
        try:
            truck_number = status.paths.truck_number
            if truck_number.casefold() == self.window.current_truck_number().casefold():
                self.window._set_current_statuses(collect_kit_statuses(truck_number, self.window.settings))
        except Exception as exc:
            self._log(f"Could not refresh status after Inventor-to-RADAN: {exc}")

    def _log(self, message: str) -> None:
        log_fn = getattr(self.window, "log", None)
        if callable(log_fn):
            log_fn(str(message))
