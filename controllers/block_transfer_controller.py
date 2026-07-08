from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QPushButton

from background_job import BackgroundJobWorker
from services import build_project_block_transfer_plan, send_project_block_files_to_machine


class BlockTransferController:
    def __init__(self, window, *, send_blocks_button: QPushButton) -> None:
        self.window = window
        self._send_blocks_button = send_blocks_button
        self._worker: BackgroundJobWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._worker is not None

    def can_close(self) -> bool:
        return not self.is_running

    def refresh_button_state(self) -> None:
        button = self._send_blocks_button
        if self._worker is not None:
            button.setEnabled(False)
            button.setText("Sending Blocks...")
            button.setToolTip("Block files are being copied to the machine and L-side kit folder.")
            return
        status = self.window._current_status()
        button.setText("Send Blocks")
        if status is None:
            button.setEnabled(False)
            button.setToolTip("Select a kit first.")
            return
        if status.paths.project_dir is None or not status.paths.project_dir.exists():
            button.setEnabled(False)
            button.setToolTip("The selected kit does not have an L-side project folder yet.")
            return
        button.setEnabled(True)
        button.setToolTip(
            "Copy this project's matched block files to the mirrored A: machine folder, "
            "archive them in the L-side kit folder, then delete each source block file after checksum verification."
        )

    def send_selected(self) -> None:
        title = "Send Blocks"
        if self._worker is not None:
            QMessageBox.information(self.window, title, "Block files are already being sent to the machine.")
            return
        status = self.window._current_status()
        if status is None:
            QMessageBox.information(self.window, title, "Select a kit first.")
            return
        if status.paths.project_dir is None or not status.paths.project_dir.exists():
            QMessageBox.warning(self.window, title, "The selected kit does not have an L-side project folder yet.")
            return
        try:
            plan = build_project_block_transfer_plan(status.paths.project_dir, self.window.settings.release_root)
        except Exception as exc:
            QMessageBox.warning(self.window, title, str(exc))
            return
        if not plan.drg_paths:
            QMessageBox.information(
                self.window,
                title,
                f"No nest .drg files were found under:\n{status.paths.project_dir}",
            )
            return
        if not plan.matches:
            if plan.already_sent_paths and not plan.missing_drg_paths:
                already_sent = "\n".join(path.name for path in plan.already_sent_paths[:20])
                if len(plan.already_sent_paths) > 20:
                    already_sent += f"\n... (+{len(plan.already_sent_paths) - 20} more)"
                QMessageBox.information(
                    self.window,
                    title,
                    "All matching block files already appear to be on the machine.\n\n"
                    f"Destination:\n{plan.target_dir}\n\n"
                    f"Already present:\n{already_sent}",
                )
                return
            missing = "\n".join(path.name for path in plan.missing_drg_paths[:20])
            if len(plan.missing_drg_paths) > 20:
                missing += f"\n... (+{len(plan.missing_drg_paths) - 20} more)"
            already_sent_text = ""
            if plan.already_sent_paths:
                already_sent_text = f"\n\nAlready on machine: {len(plan.already_sent_paths)}"
            QMessageBox.information(
                self.window,
                title,
                "No matching block .cnc files were found.\n\n"
                f"Source:\n{plan.source_root}\n\n"
                f"Missing for:\n{missing or '(none)'}"
                f"{already_sent_text}",
            )
            return

        matched_lines = [
            f"- {match.source_path.name} -> {match.target_path.name}"
            for match in plan.matches[:12]
        ]
        if len(plan.matches) > 12:
            matched_lines.append(f"... (+{len(plan.matches) - 12} more)")
        missing_text = ""
        if plan.missing_drg_paths:
            missing_lines = [path.name for path in plan.missing_drg_paths[:8]]
            if len(plan.missing_drg_paths) > 8:
                missing_lines.append(f"... (+{len(plan.missing_drg_paths) - 8} more)")
            missing_text = (
                "\n\nNo matching block file was found for these nest drawing(s):\n"
                + "\n".join(missing_lines)
            )
        already_sent_text = ""
        if plan.already_sent_paths:
            already_lines = [path.name for path in plan.already_sent_paths[:8]]
            if len(plan.already_sent_paths) > 8:
                already_lines.append(f"... (+{len(plan.already_sent_paths) - 8} more)")
            already_sent_text = "\n\nAlready on machine:\n" + "\n".join(already_lines)
        choice = QMessageBox.warning(
            self.window,
            title,
            "Send matched block files to the machine?\n\n"
            f"Machine destination:\n{plan.target_dir}\n\n"
            f"L-side copy:\n{plan.local_target_dir}\n\n"
            "Each file will be copied using the full nest .drg name, checksum-verified in both places, then deleted from "
            f"{plan.source_root}. Existing machine or L-side archive files with the same names will be overwritten.\n\n"
            + "\n".join(matched_lines)
            + already_sent_text
            + missing_text,
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if choice != QMessageBox.Ok:
            self.window.log("Send Blocks cancelled before transfer.")
            return

        progress = QProgressDialog(
            "Sending block files to the machine...", "Cancel", 0, len(plan.matches), self.window
        )
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        def _job(worker: BackgroundJobWorker):
            return send_project_block_files_to_machine(
                status.paths.project_dir,
                self.window.settings.release_root,
                progress_cb=lambda done, total, status_text: worker.emit_progress(done, total, status_text),
                should_cancel_cb=worker.should_cancel,
            )

        worker = BackgroundJobWorker(_job)
        self._worker = worker
        self.refresh_button_state()

        def _cleanup() -> None:
            try:
                progress.close()
            except Exception:
                pass
            self._worker = None
            self.refresh_button_state()

        def _on_progress(done: int, total: int, status_text: str) -> None:
            if int(total) <= 0:
                progress.setRange(0, 0)
            else:
                progress.setRange(0, int(total))
                progress.setValue(max(0, min(int(total), int(done))))
            progress.setLabelText(f"Sending block files to the machine...\n{status_text}")

        def _on_done(payload: object) -> None:
            _cleanup()
            result = payload
            transferred_paths = tuple(getattr(result, "transferred_paths", ()) or ())
            local_transferred_paths = tuple(getattr(result, "local_transferred_paths", ()) or ())
            skipped_paths = tuple(getattr(result, "skipped_paths", ()) or ())
            result_plan = getattr(result, "plan", plan)
            if bool(getattr(result, "canceled", False)):
                QMessageBox.information(
                    self.window,
                    title,
                    f"Send Blocks was canceled.\n\nSent: {len(transferred_paths)}\nSkipped: {len(skipped_paths)}",
                )
                self.window.log(
                    f"Send Blocks canceled for {status.paths.display_name}; "
                    f"sent {len(transferred_paths)}, skipped {len(skipped_paths)}."
                )
                return
            QMessageBox.information(
                self.window,
                title,
                f"Sent {len(transferred_paths)} block file(s) to machine:\n"
                f"{getattr(result_plan, 'target_dir', plan.target_dir)}\n\n"
                f"Copied {len(local_transferred_paths)} L-side archive file(s) to:\n"
                f"{getattr(result_plan, 'local_target_dir', plan.local_target_dir)}",
            )
            self.window.log(
                f"Sent {len(transferred_paths)} block file(s) for {status.paths.display_name} to "
                f"{getattr(result_plan, 'target_dir', plan.target_dir)} and copied "
                f"{len(local_transferred_paths)} to {getattr(result_plan, 'local_target_dir', plan.local_target_dir)}."
            )

        def _on_error(traceback_text: str) -> None:
            _cleanup()
            message = str(traceback_text or "").strip() or "Send Blocks failed."
            QMessageBox.critical(self.window, title, message)
            self.window.log(f"Send Blocks failed for {status.paths.display_name}: {message}")

        progress.canceled.connect(worker.request_stop)
        worker.signals.progress.connect(_on_progress)
        worker.signals.done.connect(_on_done)
        worker.signals.error.connect(_on_error)
        QThreadPool.globalInstance().start(worker)
