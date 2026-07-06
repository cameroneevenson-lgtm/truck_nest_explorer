from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal


class BackgroundJobSignals(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)
    error = Signal(str)


class BackgroundJobWorker(QRunnable):
    def __init__(self, job_fn):
        super().__init__()
        self.job_fn = job_fn
        self.signals = BackgroundJobSignals()
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def should_cancel(self) -> bool:
        return bool(self._stop)

    def emit_progress(self, done: int, total: int, status_text: str) -> None:
        self.signals.progress.emit(int(done), int(total), str(status_text))

    def run(self) -> None:
        try:
            self.signals.done.emit(self.job_fn(self))
        except Exception:
            self.signals.error.emit(traceback.format_exc())
