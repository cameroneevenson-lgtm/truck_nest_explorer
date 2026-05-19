from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class MultilineEditorDialog(QDialog):
    def __init__(self, title: str, value: str, helper_text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)

        helper = QLabel(helper_text)
        helper.setWordWrap(True)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(value)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(helper)
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)

    def value(self) -> str:
        return self.editor.toPlainText()
