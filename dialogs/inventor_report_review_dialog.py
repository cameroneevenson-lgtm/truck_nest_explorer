from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services import open_path


class InventorReportReviewDialog(QDialog):
    REVIEW_SECTION_LEVELS = {
        "Expected laser but missing DXF": "red",
        "Orphan DXFs": "yellow",
        "DXFs missing PDFs": "yellow",
        "Non-laser parts": "yellow",
    }

    def __init__(self, report_path: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.report_path = report_path
        self._acknowledged = False
        self.rejected_without_ack = False
        self.setWindowTitle("Review Inventor-to-RADAN Report")
        self.resize(920, 680)

        title = QLabel("Review required before production use")
        title.setStyleSheet("font-weight: 700;")
        title.setWordWrap(True)

        report_text = self._read_report_text()
        critical_count, review_count = self._warning_counts(report_text)
        if critical_count:
            detail_text = (
                f"This report contains {critical_count} critical line(s) and "
                f"{review_count} review line(s). "
                "Read the report below before acknowledging completion."
            )
            detail_style = "color: #B91C1C; font-weight: 700;"
        elif review_count:
            detail_text = (
                f"This report contains {review_count} line(s) to check. "
                "Read the yellow sections below before acknowledging completion."
            )
            detail_style = "color: #A16207; font-weight: 700;"
        else:
            detail_text = "No report warnings were found. Review the green confirmation sections before continuing."
            detail_style = "color: #15803D; font-weight: 700;"
        detail = QLabel(detail_text)
        detail.setWordWrap(True)
        detail.setStyleSheet(detail_style)

        path_label = QLabel(str(report_path))
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.viewer = QTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QTextEdit.NoWrap)
        self.viewer.setHtml(self._report_html(report_text))

        self.ack_checkbox = QCheckBox(
            "I have reviewed this report and understand any warnings before production."
        )
        self.ack_checkbox.stateChanged.connect(self._update_ack_button)

        self.open_button = QPushButton("Open Report File")
        self.open_button.clicked.connect(self.open_report)
        self.ack_button = QPushButton("Acknowledge Report")
        self.ack_button.setEnabled(False)
        self.ack_button.clicked.connect(self.accept)
        self.discard_button = QPushButton("Discard CSV/Report")
        self.discard_button.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addWidget(self.open_button)
        button_row.addWidget(self.discard_button)
        button_row.addWidget(self.ack_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(path_label)
        layout.addWidget(self.viewer, 1)
        layout.addWidget(self.ack_checkbox)
        layout.addLayout(button_row)

    def _read_report_text(self) -> str:
        try:
            return self.report_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Could not read report file:\n{exc}"

    @classmethod
    def _warning_counts(cls, report_text: str) -> tuple[int, int]:
        critical_count = 0
        review_count = 0
        active_section = ""
        active_level = ""
        for line in report_text.splitlines():
            stripped = line.strip()
            if stripped.endswith(":"):
                active_section = stripped
                active_level = ""
                for section, level in cls.REVIEW_SECTION_LEVELS.items():
                    if active_section.startswith(section):
                        active_level = level
                        break
                continue
            if not stripped or stripped == "(none)":
                continue
            if active_level == "red":
                critical_count += 1
            elif active_level == "yellow":
                review_count += 1
        return critical_count, review_count

    @classmethod
    def _report_html(cls, report_text: str) -> str:
        colors = {
            "base": "#111827",
            "muted": "#475569",
            "green": "#15803D",
            "yellow": "#A16207",
            "red": "#B91C1C",
        }
        active_level = ""
        rows: list[str] = []
        for line in report_text.splitlines():
            stripped = line.strip()
            if stripped.endswith(":"):
                active_level = ""
                for section, level in cls.REVIEW_SECTION_LEVELS.items():
                    if stripped.startswith(section):
                        active_level = level
                        break
                color = colors.get(active_level, colors["base"])
                weight = "700" if active_level else "600"
            elif stripped == "(none)" and active_level:
                color = colors["green"]
                weight = "700"
            elif stripped and active_level:
                color = colors[active_level]
                weight = "700"
            elif stripped:
                color = colors["base"]
                weight = "400"
            else:
                color = colors["muted"]
                weight = "400"
            rows.append(
                "<div style='white-space: pre-wrap; "
                f"color: {color}; font-weight: {weight};'>"
                f"{html.escape(line) or '&nbsp;'}</div>"
            )
        body = "\n".join(rows)
        return (
            "<html><body style='font-family: Consolas, monospace; "
            "font-size: 10pt; background: #FFFFFF;'>"
            f"{body}</body></html>"
        )

    def _update_ack_button(self) -> None:
        self.ack_button.setEnabled(self.ack_checkbox.isChecked())

    def open_report(self) -> None:
        try:
            open_path(self.report_path)
        except Exception as exc:
            QMessageBox.warning(self, "Open Report", str(exc))

    def accept(self) -> None:
        if not self.ack_checkbox.isChecked():
            QMessageBox.warning(
                self,
                "Review Required",
                "Review the report and check the acknowledgement before continuing.",
            )
            return
        self._acknowledged = True
        super().accept()

    def reject(self) -> None:
        if not self._acknowledged:
            choice = QMessageBox.question(
                self,
                "Discard Inventor Output?",
                "Close without acknowledging this report?\n\n"
                "The generated RADAN CSV and report will be deleted.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if choice != QMessageBox.Yes:
                return
            self.rejected_without_ack = True
            super().reject()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._acknowledged:
            event.accept()
            return
        choice = QMessageBox.question(
            self,
            "Discard Inventor Output?",
            "Close without acknowledging this report?\n\n"
            "The generated RADAN CSV and report will be deleted.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice == QMessageBox.Yes:
            self.rejected_without_ack = True
            event.accept()
            return
        event.ignore()


def delete_paths(paths: tuple[Path, ...]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    deleted: list[Path] = []
    failed: list[str] = []
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                deleted.append(path)
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    return tuple(deleted), tuple(failed)
