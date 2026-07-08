from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

if TYPE_CHECKING:
    from main_window import MainWindow


class HotReloadController:
    def __init__(self, window: "MainWindow", runtime_dir: Path):
        self.window = window
        self.runtime_dir = runtime_dir

    def build_banner(self, root_layout: QVBoxLayout) -> None:
        window = self.window
        window._hot_reload_request_path = self.runtime_dir / "_runtime" / "hot_reload_request.json"
        window._hot_reload_response_path = self.runtime_dir / "_runtime" / "hot_reload_response.json"

        hot_reload_bar = QFrame()
        hot_reload_bar.setVisible(False)
        hot_reload_bar.setFixedHeight(36)
        hot_reload_bar.setStyleSheet(
            "QFrame { background: #fff4cf; border: 1px solid #d7be6f; border-radius: 6px; }"
            "QLabel { color: #4f3f07; background: transparent; border: none; }"
        )
        hot_reload_layout = QHBoxLayout(hot_reload_bar)
        hot_reload_layout.setContentsMargins(10, 3, 10, 3)
        hot_reload_layout.setSpacing(8)
        hot_reload_label = QLabel("Hot reload requested.")
        hot_reload_label.setStyleSheet("font-size: 13px; font-weight: 700;")
        hot_reload_accept_button = QPushButton("Accept Reload")
        hot_reload_accept_button.setMinimumHeight(24)
        hot_reload_accept_button.clicked.connect(window._accept_hot_reload_from_banner)
        hot_reload_cancel_button = QPushButton("Cancel Reload")
        hot_reload_cancel_button.setMinimumHeight(24)
        hot_reload_cancel_button.clicked.connect(window._cancel_hot_reload_from_banner)
        hot_reload_layout.addWidget(hot_reload_label)
        hot_reload_layout.addWidget(hot_reload_accept_button)
        hot_reload_layout.addWidget(hot_reload_cancel_button)
        root_layout.addWidget(hot_reload_bar)
        window._hot_reload_bar = hot_reload_bar
        window._hot_reload_label = hot_reload_label
        window._hot_reload_accept_button = hot_reload_accept_button
        window._hot_reload_cancel_button = hot_reload_cancel_button

    def poll_request(self) -> None:
        window = self.window
        if not window._hot_reload_enabled:
            return
        if window._hot_reload_request_path is None:
            return

        if not window._hot_reload_request_path.exists():
            if window._hot_reload_request_id:
                window._hot_reload_request_id = ""
                window._hot_reload_canceled_request_id = ""
                self.clear_banner()
            return

        request = self.read_request()
        request_id = str(request.get("request_id", "")).strip()
        if not request_id:
            return
        if request_id == window._hot_reload_canceled_request_id:
            return
        if request_id != window._hot_reload_request_id:
            window._hot_reload_request_id = request_id
            window._hot_reload_canceled_request_id = ""
            ts_epoch = request.get("ts_epoch", 0)
            timeout_sec = request.get("decision_timeout_sec", 10.0)
            try:
                ts_float = float(ts_epoch)
            except (TypeError, ValueError):
                ts_float = float(time.time())
            try:
                timeout_float = max(1.0, float(timeout_sec))
            except (TypeError, ValueError):
                timeout_float = 10.0
            window._hot_reload_end_time = ts_float + timeout_float

        now = float(time.time())
        end_time = window._hot_reload_end_time
        if end_time is None:
            end_time = now + 10.0
            window._hot_reload_end_time = end_time

        file_count = request.get("change_count", None)
        files = request.get("files", [])
        seconds_remaining = max(0, int(end_time - now))
        file_text = f"{int(file_count)} file(s)" if isinstance(file_count, int) else "update(s)"
        if window._hot_reload_label is None:
            return
        if isinstance(files, list) and files:
            sample = ", ".join(str(x) for x in files[:3])
            if len(files) > 3:
                sample += ", ..."
            window._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Click Accept Reload to apply now. Sample: {sample}"
            )
        else:
            window._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Click Accept Reload to apply now."
            )
        if window._hot_reload_bar is not None:
            window._hot_reload_bar.setVisible(True)

    def read_request(self) -> dict[str, str | int | float | list[str]]:
        window = self.window
        if window._hot_reload_request_path is None or not window._hot_reload_request_path.exists():
            return {}
        try:
            with window._hot_reload_request_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, str | int | float | list[str]] = {}
        for key in ("request_id", "ts_epoch", "decision_timeout_sec", "change_count", "files"):
            if key not in payload:
                continue
            out[key] = payload[key]  # type: ignore[assignment]
        return out

    def clear_banner(self) -> None:
        if self.window._hot_reload_bar is not None:
            self.window._hot_reload_bar.setVisible(False)

    def accept_from_banner(self) -> None:
        window = self.window
        if not window._hot_reload_request_id:
            return
        request_id = window._hot_reload_request_id
        self.write_response("accept")
        # Treat an accepted request as handled immediately. The launcher should
        # restart the app on the next poll, but suppress this same request in
        # the UI until that happens so the countdown cannot keep repainting.
        window._hot_reload_canceled_request_id = request_id
        window._hot_reload_request_id = ""
        window._hot_reload_end_time = None
        self.clear_banner()
        window.statusBar().showMessage("Hot reload accepted; restarting app.", 3000)

    def cancel_from_banner(self) -> None:
        window = self.window
        if not window._hot_reload_request_id:
            return
        self.write_response("reject")
        window._hot_reload_canceled_request_id = window._hot_reload_request_id
        self.clear_banner()
        window.statusBar().showMessage("Hot reload canceled for current change batch.", 3000)

    def write_response(self, action: str) -> None:
        window = self.window
        if not window._hot_reload_response_path or not window._hot_reload_request_id:
            return
        payload = {
            "request_id": window._hot_reload_request_id,
            "action": str(action or "").strip().lower(),
        }
        try:
            window._hot_reload_response_path.parent.mkdir(parents=True, exist_ok=True)
            window._hot_reload_response_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return
