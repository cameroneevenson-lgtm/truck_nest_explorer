from __future__ import annotations


def dashboard_stylesheet() -> str:
    return """
            QWidget#main_root {
                background-color: #EEF3F8;
            }
            QGroupBox {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 10px;
                color: #0F172A;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 4px;
                background-color: #F8FAFC;
                color: #0F172A;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton {
                color: #0F172A;
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                border-color: #94A3B8;
            }
            QPushButton:pressed {
                background-color: #E2E8F0;
            }
            QPushButton:checked {
                background-color: #DBEAFE;
                border-color: #60A5FA;
            }
            QPushButton:disabled {
                color: #94A3B8;
                background-color: #F8FAFC;
                border-color: #E2E8F0;
            }
            QLineEdit, QPlainTextEdit {
                color: #0F172A;
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QListWidget#truck_list, QTableWidget#kit_table {
                background: #FFFFFF;
                color: #0F172A;
                alternate-background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                gridline-color: #E2E8F0;
                selection-color: #0F172A;
            }
            QListWidget#truck_list {
                selection-background-color: #E2E8F0;
            }
            QListWidget#truck_list::item:selected {
                background: #E2E8F0;
                color: #0F172A;
            }
            QTableWidget#kit_table {
                selection-background-color: rgba(148, 163, 184, 0.18);
            }
            QTableWidget#kit_table QLineEdit {
                padding: 2px 6px;
                margin: 0px;
            }
            QTableWidget#kit_table::item:selected {
                background: rgba(148, 163, 184, 0.18);
                color: #0F172A;
            }
            QTableWidget#kit_table::item:hover {
                background: rgba(226, 232, 240, 0.20);
                color: #0F172A;
            }
            QListWidget#truck_list::item:hover {
                background: #EEF4FB;
                color: #0F172A;
            }
            QHeaderView::section, QTableCornerButton::section {
                background: #E2E8F0;
                color: #334155;
                border: 1px solid #CBD5E1;
                padding: 6px;
                font-weight: 700;
            }
            QLabel {
                color: #334155;
            }
            QCheckBox {
                color: #334155;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 1px solid #94A3B8;
                border-radius: 3px;
                background: #FFFFFF;
            }
            QCheckBox::indicator:checked {
                background: #93C5FD;
                border-color: #60A5FA;
            }
            QSplitter::handle {
                background: #E2E8F0;
            }
            QStatusBar {
                background: #F8FAFC;
                color: #475569;
                border-top: 1px solid #D5DEE7;
            }
            QStatusBar::item {
                border: none;
            }
            QScrollArea#flow_gantt_scroll {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
            }
            QLabel#flow_gantt_label {
                background: #FFFFFF;
            }
            """
