from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def _target_screen():
    screens = QGuiApplication.screens()
    if len(screens) >= 2:
        return screens[1]
    return QGuiApplication.primaryScreen()


def _lock_to_screen_maximized(window: MainWindow, screen) -> None:
    if screen is None:
        window.showMaximized()
        return
    try:
        geometry = screen.availableGeometry()
        window.setMinimumSize(geometry.size())
        window.setMaximumSize(geometry.size())
        window.move(geometry.topLeft())
    except Exception:
        pass
    window.showMaximized()


def _place_maximized_on_screen2(window: MainWindow) -> None:
    screen = _target_screen()
    if screen is None:
        _lock_to_screen_maximized(window, QGuiApplication.primaryScreen())
        return

    handle = window.windowHandle()
    if handle is not None:
        try:
            handle.setScreen(screen)
        except Exception:
            pass
    try:
        window.move(screen.geometry().topLeft())
    except Exception:
        pass
    _lock_to_screen_maximized(window, screen)


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
    _place_maximized_on_screen2(window)
    _bring_window_to_front(window)
    QTimer.singleShot(120, lambda: _bring_window_to_front(window))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
