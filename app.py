from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def _place_window_on_second_screen(app: QApplication, window: MainWindow) -> None:
    screens = app.screens()
    if not screens:
        return

    target_screen = screens[1] if len(screens) > 1 else screens[0]
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(target_screen)

    geometry = target_screen.availableGeometry()
    width = max(1400, geometry.width() - 80)
    height = max(840, geometry.height() - 80)
    window.resize(width, height)
    top_left = geometry.center() - window.rect().center()
    window.move(top_left)


def _bring_window_to_front(window: MainWindow) -> None:
    window.raise_()
    window.activateWindow()
    try:
        hwnd = int(window.winId())
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Truck Nest Explorer")

    base_dir = Path(__file__).resolve().parent
    hot_reload_active = os.environ.get("TNE_HOT_RELOAD_ACTIVE") == "1"
    window = MainWindow(
        hot_reload_active=hot_reload_active,
        runtime_dir=base_dir,
    )
    window.show()
    _place_window_on_second_screen(app, window)
    _bring_window_to_front(window)
    QTimer.singleShot(120, lambda: _bring_window_to_front(window))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
