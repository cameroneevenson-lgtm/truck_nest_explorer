from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget


class ImportLogDialog(QDialog):
    def __init__(
        self,
        log_path: Path,
        parent: QWidget | None = None,
        completion_callback: Callable[["ImportLogDialog"], object] | None = None,
    ):
        super().__init__(parent)
        self.log_path = log_path
        self._process: subprocess.Popen[object] | None = None
        self._process_assigned = False
        self._completed = False
        self._completion_callback = completion_callback
        self._completion_notified = False
        self.setWindowTitle("RADAN CSV Import Log")
        self.resize(900, 520)

        self.label = QLabel(str(log_path))
        self.label.setWordWrap(True)
        self.helper_label = QLabel("Import is running. This window will stay open until the helper finishes.")
        self.helper_label.setWordWrap(True)
        self.helper_label.setStyleSheet("color: #64748B;")

        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setPlaceholderText("Waiting for the RADAN import helper to write progress...")

        self.close_button = QPushButton("Running...")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.helper_label)
        layout.addWidget(self.viewer, 1)
        layout.addWidget(self.close_button)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh_log()

    @property
    def is_complete(self) -> bool:
        return self._completed

    @property
    def process_id(self) -> int | None:
        if self._process is None:
            return None
        return int(self._process.pid)

    def set_process(self, process: subprocess.Popen[object]) -> None:
        self._process = process
        self._process_assigned = True
        self._completed = False
        self.helper_label.setText("Import is running. This window will stay open until the helper finishes.")
        self.helper_label.setStyleSheet("color: #64748B;")
        self.close_button.setText("Running...")
        self.close_button.setEnabled(False)
        self._refresh_process_state()

    def mark_launch_failed(self, message: str) -> None:
        detail = message.strip() or "The import helper could not be launched."
        self._process = None
        self._process_assigned = True
        self._mark_complete(f"Import did not launch: {detail}", success=False)

    def force_close(self) -> None:
        self._completed = True
        self.close()

    def reject(self) -> None:
        if not self._completed:
            self.raise_()
            self.activateWindow()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if not self._completed:
            event.ignore()
            self.raise_()
            self.activateWindow()
            return
        super().closeEvent(event)

    def _tick(self) -> None:
        self.refresh_log()
        self._refresh_process_state()

    def refresh_log(self) -> None:
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError:
            text = "Starting RADAN import helper..."
        if self.viewer.toPlainText() == text:
            return
        scrollbar = self.viewer.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.viewer.setPlainText(text)
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _refresh_process_state(self) -> None:
        if self._completed or not self._process_assigned or self._process is None:
            return
        return_code = self._process.poll()
        if return_code is None:
            return
        if return_code == 0:
            self._mark_complete("Import helper finished successfully. Review the log, then dismiss this window.", success=True)
        else:
            self._mark_complete(
                f"Import helper finished with exit code {return_code}. Review the log, then dismiss this window.",
                success=False,
            )

    def _mark_complete(self, message: str, *, success: bool) -> None:
        self.refresh_log()
        self._completed = True
        self.helper_label.setText(message)
        self.helper_label.setStyleSheet("color: #15803D;" if success else "color: #B91C1C; font-weight: 700;")
        self.close_button.setText("Dismiss")
        self.close_button.setEnabled(True)
        self._timer.stop()
        if not self._completion_notified and callable(self._completion_callback):
            self._completion_notified = True
            self._completion_callback(self)
