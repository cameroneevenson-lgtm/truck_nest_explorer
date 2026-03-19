from __future__ import annotations

from pathlib import Path
from typing import Callable

import fitz
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from models import PdfMatch


class PdfPreviewPane(QWidget):
    def __init__(
        self,
        *,
        open_path_cb: Callable[[Path], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._open_path_cb = open_path_cb
        self._pdf_path: Path | None = None
        self._page_index = 0
        self._page_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)

        self.path_label = QLabel("Nest Summary PDF: (none)")
        self.path_label.setWordWrap(True)
        self.open_button = QPushButton("Open Nest Summary")
        self.open_button.clicked.connect(self._open_current_pdf)
        self.prev_button = QPushButton("Prev Page")
        self.prev_button.clicked.connect(self._show_prev_page)
        self.page_label = QLabel("Page 0 / 0")
        self.next_button = QPushButton("Next Page")
        self.next_button.clicked.connect(self._show_next_page)

        controls.addWidget(self.path_label, 1)
        controls.addWidget(self.open_button)
        controls.addWidget(self.prev_button)
        controls.addWidget(self.page_label)
        controls.addWidget(self.next_button)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignCenter)

        self.preview_label = QLabel("Select a kit to preview its Nest Summary PDF.")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumSize(320, 220)
        self.scroll_area.setWidget(self.preview_label)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)

        layout.addLayout(controls)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.info_label)

        self._update_controls()

    def set_pdf_match(self, match: PdfMatch) -> None:
        if match.chosen_path is None:
            message = "No Nest Summary PDF found on the L side yet."
            if match.issue == "no_selection":
                message = "Select a kit to preview its Nest Summary PDF."
            elif match.issue == "project_missing":
                message = "The L-side project folder does not exist yet."
            elif match.issue == "project_not_configured":
                message = "The L-side project folder is not configured."
            elif match.candidates:
                names = ", ".join(path.name for path in match.candidates[:5])
                message = f"Nest Summary candidates: {names}"
            self._clear(message=message)
            return

        info_text = ""
        if len(match.candidates) > 1:
            extra_count = len(match.candidates) - 1
            info_text = f"Showing the nearest Nest Summary PDF. {extra_count} additional match(es) found."
        self.set_pdf_path(match.chosen_path, info_text=info_text)

    def set_pdf_path(self, path: Path | None, *, info_text: str = "") -> None:
        next_path = Path(path) if path is not None else None
        if next_path != self._pdf_path:
            self._pdf_path = next_path
            self._page_index = 0
        self.info_label.setText(info_text)
        self._render_current_page()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if self._pdf_path is not None:
            self._render_current_page()

    def _clear(self, *, message: str) -> None:
        self._pdf_path = None
        self._page_index = 0
        self._page_count = 0
        self.path_label.setText("Nest Summary PDF: (none)")
        self.preview_label.clear()
        self.preview_label.setText(message)
        self.info_label.clear()
        self._update_controls()

    def _render_current_page(self) -> None:
        if self._pdf_path is None:
            self._clear(message="Select a kit to preview its Nest Summary PDF.")
            return
        if not self._pdf_path.exists():
            self._clear(message=f"Nest Summary PDF is missing:\n{self._pdf_path}")
            return

        try:
            with fitz.open(str(self._pdf_path)) as document:
                self._page_count = int(document.page_count or 0)
                if self._page_count <= 0:
                    self._clear(message=f"Nest Summary PDF has no pages:\n{self._pdf_path.name}")
                    return
                self._page_index = max(0, min(self._page_index, self._page_count - 1))
                page = document.load_page(self._page_index)
                target_width = max(320, self.scroll_area.viewport().width() - 24)
                page_rect = page.rect
                zoom = max(0.35, min(2.4, target_width / max(1.0, float(page_rect.width))))
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        except Exception as exc:
            self._clear(message=f"Could not render preview PDF:\n{exc}")
            return

        image = QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format_RGB888,
        ).copy()
        self.preview_label.setPixmap(QPixmap.fromImage(image))
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.path_label.setText(f"Nest Summary PDF: {self._pdf_path.name}")
        self._update_controls()

    def _update_controls(self) -> None:
        has_pdf = self._pdf_path is not None
        self.open_button.setEnabled(has_pdf)
        self.prev_button.setEnabled(has_pdf and self._page_index > 0)
        self.next_button.setEnabled(has_pdf and self._page_count > 0 and self._page_index < self._page_count - 1)
        self.page_label.setText(f"Page {self._page_index + 1 if has_pdf and self._page_count else 0} / {self._page_count}")

    def _open_current_pdf(self) -> None:
        if self._pdf_path is None:
            return
        self._open_path_cb(self._pdf_path)

    def _show_prev_page(self) -> None:
        if self._pdf_path is None or self._page_index <= 0:
            return
        self._page_index -= 1
        self._render_current_page()

    def _show_next_page(self) -> None:
        if self._pdf_path is None or self._page_index >= self._page_count - 1:
            return
        self._page_index += 1
        self._render_current_page()
