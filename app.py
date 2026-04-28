from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

SHARED_VENV_ROOT = Path(r"C:\Tools\.venv")
SHARED_VENV_DIR = SHARED_VENV_ROOT / "Scripts"
SHARED_VENV_PYTHON = SHARED_VENV_DIR / "python.exe"
SHARED_VENV_PYTHONW = SHARED_VENV_DIR / "pythonw.exe"
_REEXEC_ENV = "TNE_REEXEC_SHARED_VENV"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return str(left.resolve()).casefold() == str(right.resolve()).casefold()
    except OSError:
        return str(left).casefold() == str(right).casefold()


def _running_from_shared_venv() -> bool:
    if _same_path(Path(sys.prefix), SHARED_VENV_ROOT):
        return True
    current = Path(sys.executable)
    allowed = [SHARED_VENV_PYTHON]
    if SHARED_VENV_PYTHONW.exists():
        allowed.append(SHARED_VENV_PYTHONW)
    return any(_same_path(current, candidate) for candidate in allowed)


def _preferred_shared_python() -> Path:
    current_name = Path(sys.executable).name.casefold()
    if current_name == "pythonw.exe" and SHARED_VENV_PYTHONW.exists():
        return SHARED_VENV_PYTHONW
    return SHARED_VENV_PYTHON


def _ensure_shared_venv() -> None:
    if _running_from_shared_venv():
        return
    if os.environ.get(_REEXEC_ENV) == "1":
        raise SystemExit(f"Truck Nest Explorer must run from shared venv: {SHARED_VENV_PYTHON}")
    target = _preferred_shared_python()
    if not target.exists():
        raise SystemExit(f"Shared venv Python was not found: {target}")
    env = os.environ.copy()
    env[_REEXEC_ENV] = "1"
    subprocess.Popen([str(target), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(Path(__file__).resolve().parent), env=env)
    raise SystemExit(0)


_ensure_shared_venv()

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
