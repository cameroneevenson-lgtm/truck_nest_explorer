from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import html
import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThreadPool
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from packet_build_service import (
    PacketBuildReadinessError,
    build_assembly_packet,
    build_cut_list_packet,
    create_main_packet_worker,
    prepare_packet_build_context,
    validate_print_packet_readiness,
)
from flow_bridge import (
    FlowTruckInsight,
    empty_flow_truck_insight,
    flow_kit_insight_for_explorer_kit,
    flow_probe_cache_token,
    load_flow_truck_insight,
    normalize_flow_insight_for_local_release,
)
from models import (
    canonicalize_client_numbers_by_truck,
    canonicalize_hidden_kit_entries,
    ExplorerSettings,
    InventorOutputPaths,
    KitStatus,
    build_hidden_kit_key,
    materialize_legacy_punch_codes_for_kit,
    normalize_hidden_truck_entries,
    normalize_hidden_truck_number,
    normalize_truck_order_entries,
    resolve_punch_code_text,
)
from services import (
    collect_kit_statuses,
    configured_kit_mappings,
    create_kit_scaffold,
    detect_assembly_packet_pdf,
    detect_cut_list_packet_pdf,
    detect_print_packet_pdf,
    discover_trucks,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    inventor_output_paths,
    InventorToRadanInlineNeedsUi,
    is_hidden_kit,
    is_hidden_truck,
    launch_inventor_to_radan,
    launch_launcher,
    launch_radan_csv_import,
    launch_tool,
    move_inventor_outputs_to_project,
    open_external_target,
    open_path,
    radan_csv_missing_symbols,
    radan_csv_import_lock_status,
    release_text_for_status,
    resolve_existing_inventor_csv,
    restore_truck_visibility,
    run_inventor_to_radan_inline,
    sort_truck_numbers_by_fabrication_order,
    visible_radan_sessions,
)
from settings_store import load_settings, save_settings


@dataclass
class PendingInventorJob:
    truck_number: str
    kit_name: str
    spreadsheet_path: Path
    project_dir: Path
    outputs: InventorOutputPaths
    process: subprocess.Popen[object]
    started_at_monotonic: float
    first_output_seen_at_monotonic: float | None = None
    last_output_signature: tuple[tuple[str, int, int], ...] | None = None
    stable_polls: int = 0
    launcher_exit_code: int | None = None


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


class ImportLogDialog(QDialog):
    def __init__(self, log_path: Path, parent: QWidget | None = None, completion_callback=None):
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


class MainWindow(QMainWindow):
    FLOW_GANTT_HEIGHT = 176
    ASSEMBLY_PACKET_BUILD_ENABLED = False
    CUT_LIST_BUILD_ENABLED = False
    ASSEMBLY_PACKET_DISABLED_REASON = (
        "Assembly packet generation is paused because it is known to hang and is not production-ready yet."
    )
    CUT_LIST_DISABLED_REASON = "Cut list packet generation is paused until the flow has been tested."
    EXTERNAL_STATUS_REFRESH_INTERVAL_MS = 10000
    KITTER_STATUS_REFRESH_INTERVAL_MS = 5000
    KITTER_STATUS_REFRESH_ATTEMPTS = 360
    TABLE_COLUMNS = (
        "Kit",
        "Project File",
        "Nest Summary",
        "Print Packet",
        "Assembly Packet",
        "Cut List",
        "Release",
        "Flow",
        "Punch Code",
        "Notes",
    )
    PROJECT_FILE_COLUMN = 1
    NEST_SUMMARY_COLUMN = 2
    PRINT_PACKET_COLUMN = 3
    ASSEMBLY_PACKET_COLUMN = 4
    CUT_LIST_COLUMN = 5
    FLOW_COLUMN = 7
    PUNCH_CODE_COLUMN = 8
    NOTES_COLUMN = 9

    def __init__(
        self,
        hot_reload_active: bool = False,
        *,
        runtime_dir: Path | None = None,
    ):
        super().__init__()
        self.setWindowTitle("Truck Nest Explorer")
        self.resize(1680, 980)

        self.settings = load_settings()
        self._all_trucks: list[str] = []
        self._all_statuses: list[KitStatus] = []
        self._current_statuses: list[KitStatus] = []
        self._runtime_dir = runtime_dir if runtime_dir is not None else Path(__file__).resolve().parent
        self._hot_reload_enabled = hot_reload_active
        self._hot_reload_request_id: str = ""
        self._hot_reload_canceled_request_id: str = ""
        self._hot_reload_request_path: Path | None = None
        self._hot_reload_response_path: Path | None = None
        self._hot_reload_bar: QFrame | None = None
        self._hot_reload_label: QLabel | None = None
        self._hot_reload_accept_button: QPushButton | None = None
        self._hot_reload_cancel_button: QPushButton | None = None
        self._hot_reload_timer = None
        self._hot_reload_end_time: float | None = None
        self._updating_kit_table = False
        self._current_flow_truck_insight: FlowTruckInsight = empty_flow_truck_insight()
        self._pending_inventor_job: PendingInventorJob | None = None
        self._radan_import_log_dialog: ImportLogDialog | None = None
        self._radan_import_log_dialogs: list[ImportLogDialog] = []
        self._status_cache_by_truck: dict[str, list[KitStatus]] = {}
        self._flow_cache_by_truck: dict[str, tuple[str, FlowTruckInsight]] = {}
        self._flow_gantt_source_bytes: bytes | None = None
        self._flow_gantt_source_pixmap: QPixmap | None = None
        self._kitter_refresh_truck_number = ""
        self._kitter_refresh_remaining = 0
        self._pending_truck_selection = ""
        self._truck_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_truck_future: Future[list[str]] | None = None
        self._truck_request_serial = 0
        self._pending_truck_request_serial = 0
        self._status_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_status_by_truck: dict[str, tuple[str, Future[list[KitStatus]]]] = {}
        self._flow_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_flow_by_truck: dict[str, tuple[str, str, Future[FlowTruckInsight]]] = {}
        self._inventor_watch_timer = QTimer(self)
        self._inventor_watch_timer.setInterval(1500)
        self._inventor_watch_timer.timeout.connect(self._poll_pending_inventor_job)
        self._truck_watch_timer = QTimer(self)
        self._truck_watch_timer.setInterval(120)
        self._truck_watch_timer.timeout.connect(self._poll_pending_truck_future)
        self._status_watch_timer = QTimer(self)
        self._status_watch_timer.setInterval(120)
        self._status_watch_timer.timeout.connect(self._poll_pending_status_future)
        self._flow_watch_timer = QTimer(self)
        self._flow_watch_timer.setInterval(120)
        self._flow_watch_timer.timeout.connect(self._poll_pending_flow_future)
        self._flow_cache_refresh_timer = QTimer(self)
        self._flow_cache_refresh_timer.setInterval(1500)
        self._flow_cache_refresh_timer.timeout.connect(self._check_current_flow_cache)
        self._external_status_refresh_timer = QTimer(self)
        self._external_status_refresh_timer.setInterval(self.EXTERNAL_STATUS_REFRESH_INTERVAL_MS)
        self._external_status_refresh_timer.timeout.connect(self._refresh_current_status_from_external_changes)
        self._kitter_status_refresh_timer = QTimer(self)
        self._kitter_status_refresh_timer.setInterval(self.KITTER_STATUS_REFRESH_INTERVAL_MS)
        self._kitter_status_refresh_timer.timeout.connect(self._poll_kitter_status_refresh)

        self._build_ui()
        self._apply_dashboard_style()
        self._load_settings_into_form()
        self._flow_cache_refresh_timer.start()
        self._external_status_refresh_timer.start()
        QTimer.singleShot(0, self.refresh_trucks)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("main_root")
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        if self._hot_reload_enabled:
            self._hot_reload_request_path = self._runtime_dir / "_runtime" / "hot_reload_request.json"
            self._hot_reload_response_path = self._runtime_dir / "_runtime" / "hot_reload_response.json"

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
            hot_reload_accept_button.clicked.connect(self._accept_hot_reload_from_banner)
            hot_reload_cancel_button = QPushButton("Cancel Reload")
            hot_reload_cancel_button.setMinimumHeight(24)
            hot_reload_cancel_button.clicked.connect(self._cancel_hot_reload_from_banner)
            hot_reload_layout.addWidget(hot_reload_label)
            hot_reload_layout.addWidget(hot_reload_accept_button)
            hot_reload_layout.addWidget(hot_reload_cancel_button)
            root_layout.addWidget(hot_reload_bar)
            self._hot_reload_bar = hot_reload_bar
            self._hot_reload_label = hot_reload_label
            self._hot_reload_accept_button = hot_reload_accept_button
            self._hot_reload_cancel_button = hot_reload_cancel_button

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        root_layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready", 3000)
        if self._hot_reload_enabled:
            self._hot_reload_timer = self.startTimer(800)
            self._poll_hot_reload_request()

    def _apply_dashboard_style(self) -> None:
        self.setStyleSheet(
            """
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
        )

    def timerEvent(self, event):  # type: ignore[override]
        if self._hot_reload_timer is not None and event.timerId() == self._hot_reload_timer:
            self._poll_hot_reload_request()
            return
        super().timerEvent(event)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        QTimer.singleShot(0, self._rescale_flow_gantt_pixmap)

    def _build_left_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Trucks")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_trucks)
        self.new_truck_button = QPushButton("Add Truck")
        self.new_truck_button.clicked.connect(self.create_new_truck)
        self.show_hidden_trucks_button = QPushButton("Show Hidden (0)")
        self.show_hidden_trucks_button.setCheckable(True)
        self.show_hidden_trucks_button.setToolTip("Temporarily show trucks hidden from the active list.")
        self.show_hidden_trucks_button.toggled.connect(self._apply_truck_filter)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.show_hidden_trucks_button)
        header.addWidget(self.refresh_button)
        header.addWidget(self.new_truck_button)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter trucks...")
        self.search_edit.textChanged.connect(self._apply_truck_filter)

        truck_controls = QVBoxLayout()
        truck_controls.setContentsMargins(0, 0, 0, 0)
        truck_controls.setSpacing(6)
        self.move_truck_up_button = QPushButton("↑")
        self.move_truck_up_button.setToolTip("Move selected truck earlier in fabrication order")
        self.move_truck_up_button.clicked.connect(lambda: self._move_selected_truck(-1))
        self.move_truck_down_button = QPushButton("↓")
        self.move_truck_down_button.setToolTip("Move selected truck later in fabrication order")
        self.move_truck_down_button.clicked.connect(lambda: self._move_selected_truck(1))
        truck_controls.addWidget(self.move_truck_up_button)
        truck_controls.addWidget(self.move_truck_down_button)
        truck_controls.addStretch(1)

        self.truck_list = QListWidget()
        self.truck_list.setObjectName("truck_list")
        self.truck_list.currentItemChanged.connect(self._on_truck_changed)

        list_row = QHBoxLayout()
        list_row.setContentsMargins(0, 0, 0, 0)
        list_row.setSpacing(8)
        list_row.addWidget(self.truck_list, 1)
        list_row.addLayout(truck_controls)

        layout.addLayout(header)
        layout.addWidget(self.search_edit)
        layout.addLayout(list_row, 1)
        box.setMinimumWidth(320)
        return box

    def _build_right_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        main_column = QWidget()
        main_layout = QVBoxLayout(main_column)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)
        main_layout.addWidget(self._build_actions_group())
        main_layout.addWidget(self._build_table_group(), 1)

        sidebar_column = QWidget()
        sidebar_column.setMinimumWidth(280)
        sidebar_layout = QVBoxLayout(sidebar_column)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(10)
        sidebar_layout.addWidget(self._build_details_group(), 1)

        content_splitter = QSplitter(Qt.Horizontal)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(main_column)
        content_splitter.addWidget(sidebar_column)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 0)
        content_splitter.setSizes([1180, 320])

        layout.addWidget(content_splitter, 1)
        return box

    def _build_settings_group(self) -> QWidget:
        group = QGroupBox("Settings")
        layout = QGridLayout(group)
        layout.setColumnStretch(1, 1)

        self.release_root_edit = QLineEdit()
        self.fabrication_root_edit = QLineEdit()
        self.radan_kitter_edit = QLineEdit()
        self.inventor_entry_edit = QLineEdit()
        self.create_support_folders_checkbox = QCheckBox("Create _bak / _out / _kits support folders")

        browse_release = QPushButton("Browse")
        browse_release.clicked.connect(lambda: self._pick_directory(self.release_root_edit))
        browse_fabrication = QPushButton("Browse")
        browse_fabrication.clicked.connect(lambda: self._pick_directory(self.fabrication_root_edit))
        browse_kitter = QPushButton("Browse")
        browse_kitter.clicked.connect(
            lambda: self._pick_file(
                self.radan_kitter_edit,
                "Select radan_kitter launcher",
                "Batch File (*.bat);;All Files (*.*)",
            )
        )
        browse_inventor = QPushButton("Browse")
        browse_inventor.clicked.connect(
            lambda: self._pick_file(
                self.inventor_entry_edit,
                "Select inventor_to_radan launcher",
                "Batch or Python (*.bat *.cmd *.py);;All Files (*.*)",
            )
        )

        save_button = QPushButton("Save Settings")
        save_button.clicked.connect(self.save_settings_from_form)

        row = 0
        layout.addWidget(QLabel("Release Root"), row, 0)
        layout.addWidget(self.release_root_edit, row, 1)
        layout.addWidget(browse_release, row, 2)
        row += 1
        layout.addWidget(QLabel("Fabrication Root"), row, 0)
        layout.addWidget(self.fabrication_root_edit, row, 1)
        layout.addWidget(browse_fabrication, row, 2)
        row += 1
        layout.addWidget(QLabel("RADAN Kitter Launcher"), row, 0)
        layout.addWidget(self.radan_kitter_edit, row, 1)
        layout.addWidget(browse_kitter, row, 2)
        row += 1
        layout.addWidget(QLabel("Inventor Launcher"), row, 0)
        layout.addWidget(self.inventor_entry_edit, row, 1)
        layout.addWidget(browse_inventor, row, 2)
        row += 1
        layout.addWidget(self.create_support_folders_checkbox, row, 0, 1, 3)
        row += 1
        layout.addWidget(save_button, row, 2)

        return group

    def _build_actions_group(self) -> QWidget:
        group = QGroupBox("Truck / Kit Actions")
        layout = QVBoxLayout(group)

        self.current_truck_label = QLabel("Selected Truck: (none)")
        self.current_truck_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.current_flow_label = QLabel("Flow: (none)")
        self.current_flow_label.setWordWrap(True)
        self.current_flow_label.setStyleSheet("font-size: 12px; color: #475569;")
        self.flow_gantt_label = QLabel()
        self.flow_gantt_label.setObjectName("flow_gantt_label")
        self.flow_gantt_label.setAlignment(Qt.AlignCenter)
        self.flow_gantt_scroll = QScrollArea()
        self.flow_gantt_scroll.setObjectName("flow_gantt_scroll")
        self.flow_gantt_scroll.setWidget(self.flow_gantt_label)
        self.flow_gantt_scroll.setWidgetResizable(True)
        self.flow_gantt_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.flow_gantt_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.flow_gantt_scroll.setFixedHeight(self.FLOW_GANTT_HEIGHT)
        self.flow_gantt_scroll.setVisible(False)
        actions_helper_label = QLabel("Hover a button for details.")
        actions_helper_label.setStyleSheet("color: #6C757D;")

        truck_row = QHBoxLayout()
        self.create_missing_button = QPushButton("Create Missing")
        self.create_missing_button.setToolTip(
            "Create any missing L-side kit folders and project files for every canonical kit on the selected truck."
        )
        self.create_missing_button.clicked.connect(self.create_missing_kits_for_selected_truck)
        self.create_selected_button = QPushButton("Repair Selected")
        self.create_selected_button.setToolTip(
            "Create or repair the L-side folders and project files for the selected kit rows only."
        )
        self.create_selected_button.clicked.connect(self.create_selected_kits)
        self.open_truck_release_button = QPushButton("Open L Truck")
        self.open_truck_release_button.setToolTip("Open the selected truck folder on L.")
        self.open_truck_release_button.clicked.connect(self.open_selected_truck_release)
        self.open_truck_fabrication_button = QPushButton("Open W Truck")
        self.open_truck_fabrication_button.setToolTip("Open the selected truck folder on W.")
        self.open_truck_fabrication_button.clicked.connect(self.open_selected_truck_fabrication)
        self.launch_dashboard_button = QPushButton("Flow App")
        self.launch_dashboard_button.setToolTip("Launch the fabrication flow app.")
        self.launch_dashboard_button.clicked.connect(self.open_flow_app)
        self.edit_truck_client_button = QPushButton("Client")
        self.edit_truck_client_button.setToolTip("Store or update the client number for the selected truck.")
        self.edit_truck_client_button.clicked.connect(self.edit_current_truck_client_number)
        self.toggle_truck_hidden_button = QPushButton("Hide Truck")
        self.toggle_truck_hidden_button.setToolTip(
            "Hide or unhide the selected truck in the explorer without deleting anything."
        )
        self.toggle_truck_hidden_button.clicked.connect(self.toggle_current_truck_hidden)
        truck_row.addWidget(self.create_missing_button)
        truck_row.addWidget(self.create_selected_button)
        truck_row.addWidget(self.open_truck_release_button)
        truck_row.addWidget(self.open_truck_fabrication_button)
        truck_row.addWidget(self.launch_dashboard_button)
        truck_row.addWidget(self.edit_truck_client_button)
        truck_row.addWidget(self.toggle_truck_hidden_button)
        truck_row.addStretch(1)

        kit_row = QHBoxLayout()
        self.open_release_folder_button = QPushButton("Open L Kit")
        self.open_release_folder_button.setToolTip("Open the selected kit folder on L.")
        self.open_release_folder_button.clicked.connect(self.open_selected_release_folder)
        self.open_fabrication_folder_button = QPushButton("Open W Kit")
        self.open_fabrication_folder_button.setToolTip("Open the selected kit source folder on W.")
        self.open_fabrication_folder_button.clicked.connect(self.open_selected_fabrication_folder)
        self.open_spreadsheet_button = QPushButton("BOM")
        self.open_spreadsheet_button.setToolTip("Open the single spreadsheet found for the selected kit on W.")
        self.open_spreadsheet_button.clicked.connect(self.open_selected_spreadsheet)
        self.open_flow_pdf_button = QPushButton("Flow Link")
        self.open_flow_pdf_button.setToolTip(
            "Open the linked file or URL for the selected mapped flow kit from the fabrication flow dashboard."
        )
        self.open_flow_pdf_button.clicked.connect(self.open_selected_flow_pdf)
        self.build_print_packet_button = QPushButton("Build Print Packet")
        self.build_print_packet_button.setToolTip(
            "Build the QTY print packet from the selected kit's saved RPD."
        )
        self.build_print_packet_button.clicked.connect(self.build_selected_print_packet)
        self.build_assembly_packet_button = QPushButton("Build Assembly Packet")
        self.build_assembly_packet_button.setToolTip(
            self.ASSEMBLY_PACKET_DISABLED_REASON
        )
        self.build_assembly_packet_button.setEnabled(False)
        self.build_assembly_packet_button.clicked.connect(self.build_selected_assembly_packet)
        self.build_cut_list_button = QPushButton("Build Cut List")
        self.build_cut_list_button.setToolTip(
            self.CUT_LIST_DISABLED_REASON
        )
        self.build_cut_list_button.setEnabled(False)
        self.build_cut_list_button.clicked.connect(self.build_selected_cut_list_packet)
        self.launch_kitter_button = QPushButton("Run Kitter")
        self.launch_kitter_button.setToolTip("Launch RADAN Kitter on the selected project file.")
        self.launch_kitter_button.clicked.connect(self.launch_selected_kitter)
        self.launch_inventor_button = QPushButton("Run Inventor Tool")
        self.launch_inventor_button.setToolTip(
            "Run the Inventor-to-RADAN launcher on the selected spreadsheet, then move the generated output into the matching L project folder."
        )
        self.launch_inventor_button.clicked.connect(self.run_selected_inventor_flow)
        self.import_csv_button = QPushButton("Import CSV")
        self.import_csv_button.setToolTip(
            "Import the selected kit's generated _Radan.csv, then refresh the project sheet list through hidden RADAN automation."
        )
        self.import_csv_button.clicked.connect(lambda _checked=False: self.import_selected_csv_to_radan())
        self.radan_dxf_source_label = QLabel("DXF:")
        self.radan_dxf_source_left_label = QLabel("Src")
        self.radan_dxf_source_right_label = QLabel("Clean")
        self.radan_dxf_source_slider = QSlider(Qt.Horizontal)
        self.radan_dxf_source_slider.setRange(0, 1)
        self.radan_dxf_source_slider.setSingleStep(1)
        self.radan_dxf_source_slider.setPageStep(1)
        self.radan_dxf_source_slider.setFixedWidth(52)
        self.radan_dxf_source_slider.setValue(1)
        self.radan_dxf_source_slider.setToolTip(
            "Copy/preprocess source DXFs into the L-side _preprocessed_dxfs folder, then let RADAN create symbols from those cleaned copies."
        )
        self.radan_project_update_label = QLabel("Project:")
        self.radan_project_update_left_label = QLabel("RPD")
        self.radan_project_update_right_label = QLabel("Proj")
        self.radan_project_update_slider = QSlider(Qt.Horizontal)
        self.radan_project_update_slider.setRange(0, 1)
        self.radan_project_update_slider.setSingleStep(1)
        self.radan_project_update_slider.setPageStep(1)
        self.radan_project_update_slider.setFixedWidth(52)
        self.radan_project_update_slider.setToolTip(
            "Disabled for now: RADAN's NST schedule API did not persist imported parts into the RPD."
        )
        self.radan_project_update_slider.setEnabled(False)
        self.radan_project_update_right_label.setEnabled(False)
        self.toggle_selected_kits_hidden_button = QPushButton("Hide Selected Kits")
        self.toggle_selected_kits_hidden_button.setToolTip(
            "Hide or unhide the selected kits in the explorer without deleting anything."
        )
        self.toggle_selected_kits_hidden_button.clicked.connect(self.toggle_selected_kits_hidden)
        for button in (
            self.open_release_folder_button,
            self.open_fabrication_folder_button,
            self.open_spreadsheet_button,
            self.open_flow_pdf_button,
            self.build_print_packet_button,
            self.build_assembly_packet_button,
            self.build_cut_list_button,
            self.launch_kitter_button,
            self.launch_inventor_button,
            self.toggle_selected_kits_hidden_button,
        ):
            kit_row.addWidget(button)
        kit_row.addStretch(1)

        radan_row = QHBoxLayout()
        radan_row.addWidget(QLabel("RADAN CSV:"))
        radan_row.addWidget(self.radan_dxf_source_label)
        radan_row.addWidget(self.radan_dxf_source_left_label)
        radan_row.addWidget(self.radan_dxf_source_slider)
        radan_row.addWidget(self.radan_dxf_source_right_label)
        radan_row.addWidget(self.radan_project_update_label)
        radan_row.addWidget(self.radan_project_update_left_label)
        radan_row.addWidget(self.radan_project_update_slider)
        radan_row.addWidget(self.radan_project_update_right_label)
        radan_row.addWidget(self.import_csv_button)
        radan_row.addStretch(1)

        layout.addWidget(self.current_truck_label)
        layout.addWidget(self.current_flow_label)
        layout.addWidget(self.flow_gantt_scroll)
        layout.addWidget(actions_helper_label)
        layout.addLayout(truck_row)
        layout.addLayout(kit_row)
        layout.addLayout(radan_row)
        return group

    def _build_table_group(self) -> QWidget:
        group = QGroupBox("Kit Explorer")
        layout = QVBoxLayout(group)

        controls = QHBoxLayout()
        self.show_hidden_kits_checkbox = QCheckBox("Show hidden kits")
        self.show_hidden_kits_checkbox.toggled.connect(self._render_current_statuses)
        controls.addWidget(self.show_hidden_kits_checkbox)
        controls.addStretch(1)

        self.kit_table = QTableWidget(0, len(self.TABLE_COLUMNS))
        self.kit_table.setObjectName("kit_table")
        self.kit_table.setHorizontalHeaderLabels(self.TABLE_COLUMNS)
        self.kit_table.setAlternatingRowColors(True)
        self.kit_table.setWordWrap(False)
        self.kit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.kit_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.kit_table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self.kit_table.itemSelectionChanged.connect(self._on_kit_selection_changed)
        self.kit_table.itemClicked.connect(self._on_kit_table_item_clicked)
        self.kit_table.itemChanged.connect(self._on_kit_table_item_changed)
        self.kit_table.itemDoubleClicked.connect(self._on_kit_table_item_double_clicked)
        self.kit_table.verticalHeader().setVisible(False)
        self.kit_table.verticalHeader().setDefaultSectionSize(26)
        self.kit_table.verticalHeader().setMinimumSectionSize(22)
        header = self.kit_table.horizontalHeader()
        for column in range(len(self.TABLE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.NOTES_COLUMN, QHeaderView.Stretch)

        layout.addLayout(controls)
        layout.addWidget(self.kit_table)
        return group

    def _build_details_group(self) -> QWidget:
        group = QGroupBox("Selection Summary")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.details_summary_label = QLabel("No truck selected")
        self.details_summary_label.setWordWrap(True)
        self.details_summary_label.setStyleSheet("font-size: 14px; font-weight: 700; color: #0F172A;")

        self.details_helper_label = QLabel("Choose a truck to inspect status, readiness, and likely next actions.")
        self.details_helper_label.setWordWrap(True)
        self.details_helper_label.setStyleSheet("color: #64748B;")

        self.details_text = QPlainTextEdit()
        self.details_text.setObjectName("details_text")
        self.details_text.setReadOnly(True)
        self.details_text.setPlaceholderText("Truck and kit summary will appear here.")

        layout.addWidget(self.details_summary_label)
        layout.addWidget(self.details_helper_label)
        layout.addWidget(self.details_text, 1)
        group.setMinimumHeight(220)
        return group

    def _release_text_for_status(self, status: KitStatus) -> str:
        flow_insight = self._flow_insight_for_status(status)
        return release_text_for_status(
            fabrication_folder_exists=status.fabrication_folder_exists,
            fabrication_has_files=status.fabrication_has_files,
            flow_display_text=flow_insight.display_text,
        )

    def _status_summary_for_display(self, status: KitStatus) -> str:
        release_text = self._release_text_for_status(status)
        if release_text == "Complete":
            return f"Complete in flow | {status.status_summary}"
        return status.status_summary

    def _flow_insight_for_status(self, status: KitStatus):
        flow_insight = flow_kit_insight_for_explorer_kit(status.kit_name, self._current_flow_truck_insight)
        return normalize_flow_insight_for_local_release(
            flow_insight,
            fabrication_folder_exists=status.fabrication_folder_exists,
            fabrication_has_files=status.fabrication_has_files,
        )

    def _match_summary_text(
        self,
        *,
        chosen_path: Path | None,
        candidates: tuple[Path, ...],
        missing_label: str,
    ) -> str:
        if len(candidates) > 1:
            return f"Multiple matches ({len(candidates)})"
        if chosen_path is not None:
            return "Ready"
        return missing_label

    def _recommended_action_for_status(self, status: KitStatus) -> str:
        print_packet_match = detect_print_packet_pdf(status.paths)
        assembly_packet_match = detect_assembly_packet_pdf(status.paths)
        cut_list_match = detect_cut_list_packet_pdf(status.paths)
        if not status.project_folder_exists or not status.rpd_exists:
            return "Repair Selected: the L-side project setup is incomplete."
        if status.spreadsheet_match.issue == "multiple_spreadsheets":
            return "BOM: clean up multiple spreadsheet matches in W before running tools."
        if status.spreadsheet_match.chosen_path is not None and not status.fabrication_has_files:
            if self.settings.inventor_to_radan_entry.strip():
                return "Run Inventor Tool: spreadsheet is ready and the kit is not released yet."
            return "BOM: spreadsheet is ready, but the Inventor launcher is not configured."
        if status.preview_pdf_match.chosen_path is None:
            return "Open Project: review the kit because the Nest Summary is still missing."
        if (
            status.rpd_exists
            and status.paths.fabrication_kit_dir is not None
            and status.paths.fabrication_kit_dir.exists()
            and print_packet_match.chosen_path is None
        ):
            return "Build Print Packet: generate the QTY packet from Explorer."
        if (
            self.ASSEMBLY_PACKET_BUILD_ENABLED
            and status.rpd_exists
            and status.paths.fabrication_kit_dir is not None
            and status.paths.fabrication_kit_dir.exists()
            and assembly_packet_match.chosen_path is None
        ):
            return "Build Assembly Packet: generate the .iam-backed assembly drawing packet from Explorer."
        if (
            self.CUT_LIST_BUILD_ENABLED
            and status.rpd_exists
            and status.paths.fabrication_kit_dir is not None
            and status.paths.fabrication_kit_dir.exists()
            and cut_list_match.chosen_path is None
        ):
            return "Build Cut List: generate the non-laser first-token PDF packet from Explorer."
        if (
            status.rpd_exists
            and self.settings.radan_kitter_launcher.strip()
            and status.paths.fabrication_kit_dir is not None
            and status.paths.fabrication_kit_dir.exists()
            and (assembly_packet_match.chosen_path is None or cut_list_match.chosen_path is None)
        ):
            return "Run Kitter: packet side-flows are paused in Explorer; use Kitter for assembly packets for now."
        if status.rpd_exists and self.settings.radan_kitter_launcher.strip():
            return "Run Kitter: the project file is ready."
        return "Open Project: the kit is mostly ready and worth a quick review."

    def _available_actions_for_status(self, status: KitStatus) -> str:
        flow_insight = self._flow_insight_for_status(status)
        packet_match = detect_print_packet_pdf(status.paths)
        assembly_packet_match = detect_assembly_packet_pdf(status.paths)
        cut_list_match = detect_cut_list_packet_pdf(status.paths)
        actions: list[str] = []
        if not status.project_folder_exists or not status.rpd_exists:
            actions.append("Repair Selected")
        if status.rpd_exists:
            actions.append("Open Project")
        if status.project_folder_exists:
            actions.append("Open L Kit")
        if status.fabrication_folder_exists:
            actions.append("Open W Kit")
        if status.spreadsheet_match.chosen_path is not None:
            actions.append("BOM")
        if str(flow_insight.pdf_link or "").strip():
            actions.append("Flow Link")
        if status.preview_pdf_match.chosen_path is not None:
            actions.append("Open Nest Summary")
        if packet_match.chosen_path is not None:
            actions.append("Open Print Packet")
        if assembly_packet_match.chosen_path is not None:
            actions.append("Open Assembly Packet")
        if cut_list_match.chosen_path is not None:
            actions.append("Open Cut List")
        if status.rpd_exists and status.paths.fabrication_kit_dir is not None and status.paths.fabrication_kit_dir.exists():
            actions.append("Build Print Packet")
            if self.ASSEMBLY_PACKET_BUILD_ENABLED:
                actions.append("Build Assembly Packet")
            if self.CUT_LIST_BUILD_ENABLED:
                actions.append("Build Cut List")
        if status.rpd_exists and self.settings.radan_kitter_launcher.strip():
            actions.append("Run Kitter")
        if status.spreadsheet_match.chosen_path is not None and self.settings.inventor_to_radan_entry.strip():
            actions.append("Run Inventor Tool")
        if status.spreadsheet_match.chosen_path is not None and status.paths.project_dir is not None:
            outputs = inventor_output_paths(status.spreadsheet_match.chosen_path, status.paths.project_dir)
            if (
                (outputs.target_csv_path is not None and outputs.target_csv_path.exists())
                or outputs.source_csv_path.exists()
            ):
                actions.append("Import CSV to RADAN")
        hidden = is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
        actions.append("Show Kit" if hidden else "Hide Kit")
        return ", ".join(actions) if actions else "(none)"

    def _truck_rollup_lines(self) -> list[str]:
        total_kits = len(self._all_statuses)
        visible_kits = len(self._current_statuses)
        complete = sum(1 for status in self._all_statuses if self._release_text_for_status(status) == "Complete")
        released = sum(1 for status in self._all_statuses if self._release_text_for_status(status) == "Released")
        not_released = sum(1 for status in self._all_statuses if self._release_text_for_status(status) == "Not released")
        w_missing = sum(1 for status in self._all_statuses if self._release_text_for_status(status) == "W missing")
        rpd_ready = sum(1 for status in self._all_statuses if status.rpd_exists)
        spreadsheet_ready = sum(1 for status in self._all_statuses if status.spreadsheet_match.is_unique)
        spreadsheet_ambiguous = sum(
            1 for status in self._all_statuses if status.spreadsheet_match.issue == "multiple_spreadsheets"
        )
        spreadsheet_missing = sum(
            1
            for status in self._all_statuses
            if status.spreadsheet_match.chosen_path is None and status.spreadsheet_match.issue != "multiple_spreadsheets"
        )
        nest_ready = sum(1 for status in self._all_statuses if status.preview_pdf_match.chosen_path is not None)
        print_packet_ready = sum(1 for status in self._all_statuses if detect_print_packet_pdf(status.paths).chosen_path is not None)
        assembly_packet_ready = sum(
            1 for status in self._all_statuses if detect_assembly_packet_pdf(status.paths).chosen_path is not None
        )
        cut_list_ready = sum(
            1 for status in self._all_statuses if detect_cut_list_packet_pdf(status.paths).chosen_path is not None
        )
        hidden_filtered = max(0, total_kits - visible_kits)
        lines = [
            f"Kits in truck: {total_kits}",
            f"Kits visible in table: {visible_kits}",
            f"Complete: {complete} | Released: {released} | Not released: {not_released} | W missing: {w_missing}",
            f"Project files ready: {rpd_ready}/{total_kits}",
            f"Spreadsheets ready: {spreadsheet_ready} | Ambiguous: {spreadsheet_ambiguous} | Missing: {spreadsheet_missing}",
            f"Nest summaries ready: {nest_ready}/{total_kits}",
            f"Print packets ready: {print_packet_ready}/{total_kits} | Assembly packets ready: {assembly_packet_ready}/{total_kits} | Cut lists ready: {cut_list_ready}/{total_kits}",
        ]
        if hidden_filtered:
            lines.append(f"Filtered out by hidden toggle: {hidden_filtered}")
        return lines

    def _selection_rollup_lines(self, statuses: list[KitStatus]) -> list[str]:
        missing_projects = sum(1 for status in statuses if not status.project_folder_exists or not status.rpd_exists)
        complete = sum(1 for status in statuses if self._release_text_for_status(status) == "Complete")
        unreleased = sum(
            1
            for status in statuses
            if self._release_text_for_status(status) in {"Not released", "W missing"}
        )
        spreadsheet_ready = sum(1 for status in statuses if status.spreadsheet_match.is_unique)
        spreadsheet_ambiguous = sum(
            1 for status in statuses if status.spreadsheet_match.issue == "multiple_spreadsheets"
        )
        nest_missing = sum(1 for status in statuses if status.preview_pdf_match.chosen_path is None)
        hidden_count = sum(
            1
            for status in statuses
            if is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
        )
        lines = [
            f"Selection size: {len(statuses)}",
            f"Need repair: {missing_projects}",
            f"Complete in flow: {complete}",
            f"Not fully released yet: {unreleased}",
            f"Spreadsheets ready: {spreadsheet_ready} | Ambiguous: {spreadsheet_ambiguous}",
            f"Nest summaries missing: {nest_missing}",
        ]
        if hidden_count:
            lines.append(f"Already hidden: {hidden_count}")
        return lines

    def _kit_details_lines(self, status: KitStatus) -> list[str]:
        flow_insight = self._flow_insight_for_status(status)
        packet_match = detect_print_packet_pdf(status.paths)
        assembly_packet_match = detect_assembly_packet_pdf(status.paths)
        lines = [
            f"Kit: {status.paths.display_name}",
            f"RADAN name: {status.kit_name}",
            f"Kit hidden: {'Yes' if is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings) else 'No'}",
            f"Status summary: {self._status_summary_for_display(status)}",
            f"Release state: {self._release_text_for_status(status)}",
            f"Flow status: {flow_insight.display_text or 'Not mapped'}",
            f"Project file: {'Ready' if status.rpd_exists else 'Missing'}",
            (
                f"Nest summary: {self._match_summary_text(chosen_path=status.preview_pdf_match.chosen_path, candidates=status.preview_pdf_match.candidates, missing_label='Missing')}"
            ),
            (
                f"Spreadsheet: {self._match_summary_text(chosen_path=status.spreadsheet_match.chosen_path, candidates=status.spreadsheet_match.candidates, missing_label='Missing')}"
            ),
            (
                f"Print packet: {self._match_summary_text(chosen_path=packet_match.chosen_path, candidates=packet_match.candidates, missing_label='Missing')}"
            ),
            (
                f"Assembly packet: {self._match_summary_text(chosen_path=assembly_packet_match.chosen_path, candidates=assembly_packet_match.candidates, missing_label='Missing')}"
            ),
            (
                f"Cut list: {self._match_summary_text(chosen_path=cut_list_match.chosen_path, candidates=cut_list_match.candidates, missing_label='Missing')}"
            ),
            f"Punch code: {self._punch_code_text_for_status(status) or '(blank)'}",
            f"Notes: {self._note_text_for_status(status) or '(blank)'}",
            "",
            f"Available actions: {self._available_actions_for_status(status)}",
            f"Recommended next step: {self._recommended_action_for_status(status)}",
        ]
        return lines

    def _refresh_details_pane(self) -> None:
        truck_number = self.current_truck_number()
        selected_statuses = self._selected_statuses()

        if not truck_number:
            self.details_summary_label.setText("No truck selected")
            self.details_helper_label.setText("Choose a truck to inspect status, readiness, and likely next actions.")
            self.details_text.setPlainText(
                "This panel follows the truck list and selected kit rows. It focuses on readiness, counts, and actions."
            )
            return

        flow_summary = str(self._current_flow_truck_insight.summary_text or "").strip()
        if flow_summary.casefold().startswith("flow:"):
            flow_summary = flow_summary[5:].strip()
        if not flow_summary:
            flow_summary = "Unavailable"

        loading_statuses = truck_number.casefold() in self._pending_status_by_truck

        detail_lines = [
            f"Truck: {truck_number}",
            f"Client: {self._client_number_for_truck(truck_number) or '(not set)'}",
            f"Truck hidden: {'Yes' if is_hidden_truck(truck_number, self.settings) else 'No'}",
        ]
        detail_lines.append(f"Truck flow: {flow_summary}")
        if loading_statuses:
            detail_lines.append("Kit statuses: loading...")
        else:
            detail_lines.extend(self._truck_rollup_lines())

        if not selected_statuses:
            self.details_summary_label.setText(f"{truck_number} overview")
            if loading_statuses:
                self.details_helper_label.setText("Kit summary will appear once the current truck finishes loading.")
            elif self._current_statuses:
                self.details_helper_label.setText("Select a kit row to see readiness and the next likely action.")
            else:
                self.details_helper_label.setText("No visible kits are currently available for this truck.")
            self.details_text.setPlainText("\n".join(detail_lines))
            return

        if len(selected_statuses) == 1:
            status = selected_statuses[0]
            self.details_summary_label.setText(f"{status.paths.display_name} on {truck_number}")
            self.details_helper_label.setText(self._status_summary_for_display(status))
            detail_lines.extend(["", *self._kit_details_lines(status)])
            self.details_text.setPlainText("\n".join(detail_lines))
            return

        selected_names = [status.paths.display_name for status in selected_statuses]
        visible_names = ", ".join(selected_names[:6])
        if len(selected_names) > 6:
            visible_names = f"{visible_names}, +{len(selected_names) - 6} more"
        self.details_summary_label.setText(f"{len(selected_statuses)} kits selected on {truck_number}")
        self.details_helper_label.setText("Showing the selection rollup plus the first selected kit as a representative example.")
        detail_lines.extend(
            [
                "",
                f"Selected kits: {visible_names}",
                *self._selection_rollup_lines(selected_statuses),
                "",
                "First selected kit:",
                *self._kit_details_lines(selected_statuses[0]),
            ]
        )
        self.details_text.setPlainText("\n".join(detail_lines))

    def _load_settings_into_form(self) -> None:
        return

    def _settings_from_form(self) -> ExplorerSettings:
        return ExplorerSettings(
            release_root=self.settings.release_root,
            fabrication_root=self.settings.fabrication_root,
            dashboard_launcher=self.settings.dashboard_launcher,
            radan_kitter_launcher=self.settings.radan_kitter_launcher,
            inventor_to_radan_entry=self.settings.inventor_to_radan_entry,
            rpd_template_path=self.settings.rpd_template_path,
            template_replacements_text=self.settings.template_replacements_text,
            punch_codes_text=self.settings.punch_codes_text,
            punch_codes_by_kit=dict(self.settings.punch_codes_by_kit),
            notes_by_kit=dict(self.settings.notes_by_kit),
            client_numbers_by_truck=dict(self.settings.client_numbers_by_truck),
            create_support_folders=self.settings.create_support_folders,
            kit_templates=list(self.settings.kit_templates),
            truck_order=list(self.settings.truck_order),
            hidden_trucks=list(self.settings.hidden_trucks),
            hidden_kits=list(self.settings.hidden_kits),
        )

    def save_settings_from_form(self) -> None:
        self.settings = self._settings_from_form()
        save_path = save_settings(self.settings)
        self.log(f"Saved settings to {save_path}")
        self.refresh_trucks()

    def edit_template_rules(self) -> None:
        dialog = MultilineEditorDialog(
            title="Edit Template Replacement Rules",
            value=self.settings.template_replacements_text,
            helper_text=(
                "Use one rule per line in the form FIND => REPLACE.\n"
                "Available replacement placeholders: {truck_number}, {kit_name}, "
                "{project_name}, {rpd_stem}.\n"
                "Example:\n"
                "TEMPLATE PROJECT => {project_name}"
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings.template_replacements_text = dialog.value()
        self.save_settings_from_form()

    def _pick_directory(self, line_edit: QLineEdit) -> None:
        start_dir = line_edit.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Select folder", start_dir)
        if path:
            line_edit.setText(path)

    def _pick_file(self, line_edit: QLineEdit, title: str, file_filter: str) -> None:
        start_dir = line_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(self, title, start_dir, file_filter)
        if path:
            line_edit.setText(path)

    def refresh_trucks(self) -> None:
        self._status_cache_by_truck.clear()
        self._flow_cache_by_truck.clear()
        self._pending_status_by_truck.clear()
        self._pending_flow_by_truck.clear()
        previous_future = self._pending_truck_future
        if previous_future is not None and not previous_future.done():
            previous_future.cancel()

        settings_snapshot = self._settings_from_form()
        self._truck_request_serial += 1
        self._pending_truck_request_serial = self._truck_request_serial
        self._pending_truck_future = self._truck_executor.submit(
            self._discover_truck_numbers,
            settings_snapshot,
        )
        self.refresh_button.setEnabled(False)
        self.statusBar().showMessage("Loading trucks...")
        self._truck_watch_timer.start()

    def _discover_truck_numbers(self, settings: ExplorerSettings) -> list[str]:
        return sort_truck_numbers_by_fabrication_order(
            discover_trucks(settings),
            settings,
        )

    def _poll_pending_truck_future(self) -> None:
        future = self._pending_truck_future
        if future is None:
            self._truck_watch_timer.stop()
            return
        if not future.done():
            return

        request_serial = self._pending_truck_request_serial
        self._pending_truck_future = None
        self._truck_watch_timer.stop()
        self.refresh_button.setEnabled(True)

        try:
            trucks = future.result()
        except Exception as exc:
            self.log(f"Could not load trucks: {exc}")
            trucks = []

        if request_serial != self._truck_request_serial:
            return

        self._all_trucks = trucks
        release_root = Path(self.settings.release_root)
        if not release_root.exists():
            self.log(f"Release root not found: {release_root}")
        self._apply_truck_filter()
        if not self.truck_list.count():
            self._current_flow_truck_insight = empty_flow_truck_insight()
            self._set_current_statuses([], cache=False)

        truck_count = len(self._all_trucks)
        noun = "truck" if truck_count == 1 else "trucks"
        self.statusBar().showMessage(f"Loaded {truck_count} {noun}.", 3000)

    def _apply_truck_filter(self) -> None:
        wanted = self.search_edit.text().strip().casefold()
        current = self.current_truck_number()
        self.truck_list.clear()
        visible_trucks = filter_truck_numbers(
            self._all_trucks,
            self.settings,
            show_hidden=self.show_hidden_trucks_button.isChecked(),
        )
        hidden_foreground = QColor("#6C757D")
        for truck_number in visible_trucks:
            client_number = self._client_number_for_truck(truck_number)
            if wanted and wanted not in truck_number.casefold() and wanted not in client_number.casefold():
                continue
            item = QListWidgetItem(truck_number)
            tooltip_parts: list[str] = []
            if client_number:
                tooltip_parts.append(f"Client: {client_number}")
            if is_hidden_truck(truck_number, self.settings):
                item.setForeground(hidden_foreground)
                tooltip_parts.append("Hidden truck")
            if tooltip_parts:
                item.setToolTip("\n".join(tooltip_parts))
            self.truck_list.addItem(item)
        self._refresh_show_hidden_trucks_button()
        pending_selection = self._pending_truck_selection.strip()
        if pending_selection and self._select_truck(pending_selection):
            self._pending_truck_selection = ""
        elif current and not self._select_truck(current) and self.truck_list.count():
            self.truck_list.setCurrentRow(0)
        self._refresh_hidden_action_labels()
        self._refresh_truck_order_buttons()

    def _select_truck(self, truck_number: str) -> bool:
        for row in range(self.truck_list.count()):
            item = self.truck_list.item(row)
            if item and item.text() == truck_number:
                self.truck_list.setCurrentRow(row)
                return True
        return False

    def current_truck_number(self) -> str:
        item = self.truck_list.currentItem()
        return item.text().strip() if item else ""

    def _on_truck_changed(self) -> None:
        truck_number = self.current_truck_number()
        truck_key = truck_number.casefold()
        self._refresh_current_truck_heading()
        self._refresh_truck_order_buttons()
        if not truck_number:
            self._current_flow_truck_insight = empty_flow_truck_insight()
            self._set_current_statuses([])
            return
        self._load_flow_for_truck(truck_number)

        cached_statuses = self._status_cache_by_truck.get(truck_key)
        if cached_statuses is not None:
            self._set_current_statuses(list(cached_statuses))
        else:
            self._set_current_statuses([], cache=False)
            pending_status = self._pending_status_by_truck.get(truck_key)
            if pending_status is None:
                self.log(f"Loading kit statuses for {truck_number}...")
                self._pending_status_by_truck[truck_key] = (
                    truck_number,
                    self._status_executor.submit(collect_kit_statuses, truck_number, self.settings),
                )
                self._status_watch_timer.start()

    def _loading_flow_insight(self, truck_number: str) -> FlowTruckInsight:
        return FlowTruckInsight(
            available=False,
            truck_number=truck_number,
            summary_text="Flow: loading...",
            issue="loading",
            tooltip_text="Loading scheduling insights from the fabrication flow dashboard.",
        )

    def _load_flow_for_truck(self, truck_number: str) -> None:
        truck_key = truck_number.casefold()
        current_flow_token = flow_probe_cache_token()
        cached_flow = self._flow_cache_by_truck.get(truck_key)
        if cached_flow is not None:
            cached_token, cached_insight = cached_flow
            if cached_token == current_flow_token:
                self._current_flow_truck_insight = cached_insight
                self._refresh_current_truck_heading()
                return
            self._flow_cache_by_truck.pop(truck_key, None)

        pending_flow = self._pending_flow_by_truck.get(truck_key)
        if pending_flow is not None:
            _pending_truck_number, pending_token, _future = pending_flow
            if pending_token == current_flow_token:
                self._current_flow_truck_insight = self._loading_flow_insight(truck_number)
                self._refresh_current_truck_heading()
                return
            self._pending_flow_by_truck.pop(truck_key, None)

        self._current_flow_truck_insight = self._loading_flow_insight(truck_number)
        self._refresh_current_truck_heading()
        self._pending_flow_by_truck[truck_key] = (
            truck_number,
            current_flow_token,
            self._flow_executor.submit(load_flow_truck_insight, truck_number),
        )
        self._flow_watch_timer.start()

    def _set_current_statuses(self, statuses: list[KitStatus], *, cache: bool = True) -> None:
        self._all_statuses = list(statuses)
        truck_number = self.current_truck_number()
        if cache and truck_number:
            self._status_cache_by_truck[truck_number.casefold()] = list(statuses)
        self._render_current_statuses()

    def _poll_pending_status_future(self) -> None:
        if not self._pending_status_by_truck:
            self._status_watch_timer.stop()
            return
        completed: list[tuple[str, str, Future[list[KitStatus]]]] = []
        for truck_key, (truck_number, future) in list(self._pending_status_by_truck.items()):
            if not future.done():
                continue
            completed.append((truck_key, truck_number, future))
            self._pending_status_by_truck.pop(truck_key, None)
        if not self._pending_status_by_truck:
            self._status_watch_timer.stop()
        if not completed:
            return

        current_key = self.current_truck_number().casefold()
        for truck_key, truck_number, future in completed:
            try:
                statuses = future.result()
            except Exception as exc:
                self.log(f"Could not load kit statuses for {truck_number}: {exc}")
                statuses = []

            self._status_cache_by_truck[truck_key] = list(statuses)
            if truck_key != current_key:
                continue
            self._set_current_statuses(list(statuses))

    def _poll_pending_flow_future(self) -> None:
        if not self._pending_flow_by_truck:
            self._flow_watch_timer.stop()
            return
        completed: list[tuple[str, str, str, Future[FlowTruckInsight]]] = []
        for truck_key, (truck_number, cache_token, future) in list(self._pending_flow_by_truck.items()):
            if not future.done():
                continue
            completed.append((truck_key, truck_number, cache_token, future))
            self._pending_flow_by_truck.pop(truck_key, None)
        if not self._pending_flow_by_truck:
            self._flow_watch_timer.stop()
        if not completed:
            return

        current_key = self.current_truck_number().casefold()
        current_token = flow_probe_cache_token()
        for truck_key, truck_number, cache_token, future in completed:
            try:
                insight = future.result()
            except Exception as exc:
                insight = FlowTruckInsight(
                    available=False,
                    truck_number=truck_number,
                    summary_text="Flow: unavailable.",
                    issue="load_failed",
                    tooltip_text=str(exc),
                )

            self._flow_cache_by_truck[truck_key] = (cache_token, insight)
            if truck_key != current_key or cache_token != current_token:
                continue
            self._current_flow_truck_insight = insight
            self._refresh_current_truck_heading()
            self._render_current_statuses()

    def _check_current_flow_cache(self) -> None:
        truck_number = self.current_truck_number().strip()
        if not truck_number:
            return
        truck_key = truck_number.casefold()
        if truck_key in self._pending_flow_by_truck:
            return
        current_token = flow_probe_cache_token()
        cached_flow = self._flow_cache_by_truck.get(truck_key)
        if cached_flow is not None and cached_flow[0] == current_token:
            return
        self._load_flow_for_truck(truck_number)
        self._render_current_statuses()

    def _render_current_statuses(self) -> None:
        previous_kit_name = self._current_status().kit_name if self._current_status() is not None else ""
        visible_statuses = filter_kit_statuses(
            self._all_statuses,
            self.settings,
            show_hidden=self.show_hidden_kits_checkbox.isChecked(),
        )
        self._current_statuses = visible_statuses
        self._updating_kit_table = True
        self.kit_table.setRowCount(len(visible_statuses))
        try:
            for row, status in enumerate(visible_statuses):
                self._populate_status_row(row, status)
        finally:
            self._updating_kit_table = False

        selected_row = -1
        if previous_kit_name:
            for row, status in enumerate(visible_statuses):
                if status.kit_name.casefold() == previous_kit_name.casefold():
                    selected_row = row
                    break
        if selected_row >= 0:
            self.kit_table.selectRow(selected_row)
        elif visible_statuses:
            self.kit_table.selectRow(0)
        else:
            self.kit_table.setRowCount(0)
        for row in range(len(visible_statuses)):
            self.kit_table.setRowHeight(row, 26)
        self._refresh_hidden_action_labels()

    def _on_kit_selection_changed(self) -> None:
        self._refresh_hidden_action_labels()

    def _make_item(
        self,
        text: str,
        *,
        background: QColor | None = None,
        foreground: QColor | None = None,
    ) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if background is not None:
            item.setBackground(background)
        if foreground is not None:
            item.setForeground(foreground)
        return item

    def _make_open_link_item(
        self,
        *,
        exists: bool,
        tooltip: str,
        background: QColor,
        hidden_foreground: QColor | None,
    ) -> QTableWidgetItem:
        link_foreground = hidden_foreground if hidden_foreground is not None else QColor("#0F172A")
        item = self._make_item(
            "Open" if exists else "",
            background=background,
            foreground=link_foreground,
        )
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def _punch_code_text_for_status(self, status: KitStatus) -> str:
        return resolve_punch_code_text(
            self.settings.punch_codes_by_kit,
            status.paths.truck_number,
            status.kit_name,
        )

    def _note_text_for_status(self, status: KitStatus) -> str:
        note_key = build_hidden_kit_key(status.paths.truck_number, status.kit_name)
        if not note_key:
            return ""
        return str(self.settings.notes_by_kit.get(note_key) or "")

    def _on_kit_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_kit_table or item.column() not in {self.PUNCH_CODE_COLUMN, self.NOTES_COLUMN}:
            return
        row = item.row()
        if row < 0 or row >= len(self._current_statuses):
            return
        status = self._current_statuses[row]
        kit_key = build_hidden_kit_key(status.paths.truck_number, status.kit_name)
        if not kit_key:
            return
        if item.column() == self.PUNCH_CODE_COLUMN:
            text = item.text().strip()
            updated = materialize_legacy_punch_codes_for_kit(
                self.settings.punch_codes_by_kit,
                self._all_trucks,
                status.kit_name,
            )
            if text:
                updated[kit_key] = text
            else:
                updated.pop(kit_key, None)
            self.settings.punch_codes_by_kit = updated
            save_settings(self.settings)
            self.log(f"Saved punch code for {status.paths.display_name}.")
            self._refresh_details_pane()
            return

        raw_text = item.text()
        if raw_text.strip():
            self.settings.notes_by_kit[kit_key] = raw_text
        else:
            self.settings.notes_by_kit.pop(kit_key, None)
        save_settings(self.settings)
        self.log(f"Saved notes for {status.paths.display_name}.")
        self._refresh_details_pane()

    def _on_kit_table_item_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self._current_statuses):
            return
        status = self._current_statuses[row]
        if item.column() == self.PROJECT_FILE_COLUMN:
            if status.rpd_exists and status.paths.rpd_path is not None:
                self._open_rpd_for_status(status)
            return
        if item.column() == self.NEST_SUMMARY_COLUMN:
            if status.preview_pdf_match.chosen_path is not None:
                self._open_nest_summary_for_status(status)
            return
        if item.column() == self.PRINT_PACKET_COLUMN:
            if detect_print_packet_pdf(status.paths).chosen_path is not None:
                self._open_print_packet_for_status(status)
            return
        if item.column() == self.ASSEMBLY_PACKET_COLUMN:
            if detect_assembly_packet_pdf(status.paths).chosen_path is not None:
                self._open_assembly_packet_for_status(status)
            return
        if item.column() == self.CUT_LIST_COLUMN:
            if detect_cut_list_packet_pdf(status.paths).chosen_path is not None:
                self._open_cut_list_for_status(status)

    def _on_kit_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self._current_statuses):
            return
        status = self._current_statuses[row]
        if item.column() == self.PROJECT_FILE_COLUMN:
            if status.rpd_exists and status.paths.rpd_path is not None:
                self._open_rpd_for_status(status)
            return
        if item.column() == self.NEST_SUMMARY_COLUMN:
            if status.preview_pdf_match.chosen_path is not None:
                self._open_nest_summary_for_status(status)
            return
        if item.column() == self.PRINT_PACKET_COLUMN:
            if detect_print_packet_pdf(status.paths).chosen_path is not None:
                self._open_print_packet_for_status(status)
            return
        if item.column() == self.ASSEMBLY_PACKET_COLUMN:
            if detect_assembly_packet_pdf(status.paths).chosen_path is not None:
                self._open_assembly_packet_for_status(status)
            return
        if item.column() == self.CUT_LIST_COLUMN:
            if detect_cut_list_packet_pdf(status.paths).chosen_path is not None:
                self._open_cut_list_for_status(status)

    def _populate_status_row(self, row: int, status: KitStatus) -> None:
        green = QColor("#D8F3DC")
        yellow = QColor("#FFF3BF")
        red = QColor("#F8D7DA")
        blue = QColor("#D6E4FF")
        neutral = QColor("#E9ECEF")
        muted = QColor("#6C757D")
        hidden = is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
        hidden_foreground = muted if hidden else None

        release_text = self._release_text_for_status(status)
        release_color = red
        if release_text in {"Complete", "Released"}:
            release_color = green
        elif release_text == "Not released":
            release_color = yellow

        nest_summary_color = red
        if status.preview_pdf_match.chosen_path is not None:
            nest_summary_color = green if len(status.preview_pdf_match.candidates) == 1 else yellow
        elif status.preview_pdf_match.candidates:
            nest_summary_color = yellow
        packet_match = detect_print_packet_pdf(status.paths)
        assembly_packet_match = detect_assembly_packet_pdf(status.paths)
        cut_list_match = detect_cut_list_packet_pdf(status.paths)
        print_packet_color = red
        if packet_match.chosen_path is not None:
            print_packet_color = green if len(packet_match.candidates) == 1 else yellow
        elif packet_match.candidates:
            print_packet_color = yellow
        assembly_packet_color = red
        if assembly_packet_match.chosen_path is not None:
            assembly_packet_color = green if len(assembly_packet_match.candidates) == 1 else yellow
        elif assembly_packet_match.candidates:
            assembly_packet_color = yellow
        cut_list_color = red
        if cut_list_match.chosen_path is not None:
            cut_list_color = green if len(cut_list_match.candidates) == 1 else yellow
        elif cut_list_match.candidates:
            cut_list_color = yellow

        punch_code_item = self._make_item(
            self._punch_code_text_for_status(status),
            foreground=hidden_foreground,
        )
        punch_code_item.setFlags(punch_code_item.flags() | Qt.ItemFlag.ItemIsEditable)
        punch_code_item.setToolTip("Double-click to edit punch code notes for this truck.")

        note_text = self._note_text_for_status(status)
        notes_item = self._make_item(
            note_text,
            foreground=hidden_foreground,
        )
        notes_item.setFlags(notes_item.flags() | Qt.ItemFlag.ItemIsEditable)
        notes_item.setToolTip(note_text if note_text else "Double-click to add freeform notes for this truck and kit.")

        project_file_item = self._make_open_link_item(
            exists=bool(status.rpd_exists and status.paths.rpd_path is not None),
            tooltip=(
                f"Click to open project file:\n{status.paths.rpd_path}"
                if status.rpd_exists and status.paths.rpd_path is not None
                else "No project file found on L for this kit."
            ),
            background=green if status.rpd_exists else red,
            hidden_foreground=hidden_foreground,
        )
        nest_summary_item = self._make_open_link_item(
            exists=status.preview_pdf_match.chosen_path is not None,
            tooltip=(
                f"Click to open Nest Summary:\n{status.preview_pdf_match.chosen_path}"
                if status.preview_pdf_match.chosen_path is not None
                else "No Nest Summary PDF found on L for this kit."
            ),
            background=nest_summary_color,
            hidden_foreground=hidden_foreground,
        )
        print_packet_item = self._make_open_link_item(
            exists=packet_match.chosen_path is not None,
            tooltip=(
                f"Click to open Print Packet:\n{packet_match.chosen_path}"
                if packet_match.chosen_path is not None
                else "No Print Packet PDF found on L for this kit."
            ),
            background=print_packet_color,
            hidden_foreground=hidden_foreground,
        )
        assembly_packet_item = self._make_open_link_item(
            exists=assembly_packet_match.chosen_path is not None,
            tooltip=(
                f"Click to open Assembly Packet:\n{assembly_packet_match.chosen_path}"
                if assembly_packet_match.chosen_path is not None
                else "No Assembly Packet PDF found on L for this kit."
            ),
            background=assembly_packet_color,
            hidden_foreground=hidden_foreground,
        )
        cut_list_item = self._make_open_link_item(
            exists=cut_list_match.chosen_path is not None,
            tooltip=(
                f"Click to open Cut List:\n{cut_list_match.chosen_path}"
                if cut_list_match.chosen_path is not None
                else "No Cut List PDF found on L for this kit."
            ),
            background=cut_list_color,
            hidden_foreground=hidden_foreground,
        )

        flow_insight = self._flow_insight_for_status(status)
        flow_background = neutral
        if flow_insight.status_key == "red":
            flow_background = red
        elif flow_insight.status_key == "yellow":
            flow_background = yellow
        elif flow_insight.status_key == "green":
            flow_background = green
        elif flow_insight.status_key == "blue":
            flow_background = blue
        flow_item = self._make_item(
            flow_insight.display_text,
            background=flow_background,
            foreground=hidden_foreground,
        )
        if flow_insight.tooltip_text:
            flow_item.setToolTip(flow_insight.tooltip_text)

        items = (
            self._make_item(
                f"{status.paths.display_name} [hidden]" if hidden else status.paths.display_name,
                foreground=hidden_foreground,
            ),
            project_file_item,
            nest_summary_item,
            print_packet_item,
            assembly_packet_item,
            cut_list_item,
            self._make_item(release_text, background=release_color, foreground=hidden_foreground),
            flow_item,
            punch_code_item,
            notes_item,
        )
        if status.paths.display_name.casefold() != status.kit_name.casefold():
            items[0].setToolTip(f"RADAN name: {status.kit_name}")
        for column, item in enumerate(items):
            self.kit_table.setItem(row, column, item)

    def _selected_statuses(self) -> list[KitStatus]:
        rows = {index.row() for index in self.kit_table.selectionModel().selectedRows()}
        if not rows and self.kit_table.currentRow() >= 0:
            rows = {self.kit_table.currentRow()}
        return [
            self._current_statuses[row]
            for row in sorted(rows)
            if 0 <= row < len(self._current_statuses)
        ]

    def _current_status(self) -> KitStatus | None:
        selected = self._selected_statuses()
        return selected[0] if selected else None

    def _ensure_saved_settings(self) -> None:
        return

    def _clear_flow_gantt(self) -> None:
        self._flow_gantt_source_bytes = None
        self._flow_gantt_source_pixmap = None
        self.flow_gantt_label.clear()
        self.flow_gantt_label.resize(0, 0)
        self.flow_gantt_scroll.setVisible(False)

    def _rescale_flow_gantt_pixmap(self) -> None:
        source = self._flow_gantt_source_pixmap
        if source is None or source.isNull():
            return
        viewport_width = max(32, int(self.flow_gantt_scroll.viewport().width()))
        viewport_height = max(32, int(self.flow_gantt_scroll.viewport().height()))
        pixmap = source.scaled(
            viewport_width,
            viewport_height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.flow_gantt_label.setPixmap(pixmap)
        self.flow_gantt_label.setFixedSize(viewport_width, viewport_height)
        self.flow_gantt_scroll.setVisible(True)

    def _set_flow_gantt_png(self, png_bytes: bytes | None) -> None:
        if not png_bytes:
            self._clear_flow_gantt()
            return
        if self._flow_gantt_source_bytes != png_bytes:
            pixmap = QPixmap()
            if not pixmap.loadFromData(png_bytes):
                self._clear_flow_gantt()
                return
            self._flow_gantt_source_bytes = png_bytes
            self._flow_gantt_source_pixmap = pixmap
        self._rescale_flow_gantt_pixmap()
        QTimer.singleShot(0, self._rescale_flow_gantt_pixmap)

    def _client_number_for_truck(self, truck_number: str) -> str:
        key = normalize_hidden_truck_number(truck_number)
        if not key:
            return ""
        return str(self.settings.client_numbers_by_truck.get(key) or "").strip()

    def _refresh_current_truck_heading(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            self.current_truck_label.setText("Selected Truck: (none)")
            self.current_flow_label.setText("Flow: (none)")
            self.current_flow_label.setToolTip("")
            self._clear_flow_gantt()
            return
        client_number = self._client_number_for_truck(truck_number)
        if client_number:
            self.current_truck_label.setText(f"Selected Truck: {truck_number} | Client: {client_number}")
        else:
            self.current_truck_label.setText(f"Selected Truck: {truck_number}")

        flow_summary = str(self._current_flow_truck_insight.summary_text or "").strip()
        if flow_summary:
            if flow_summary.casefold().startswith("flow:"):
                self.current_flow_label.setText(flow_summary)
            else:
                self.current_flow_label.setText(f"Flow: {flow_summary}")
        else:
            self.current_flow_label.setText("Flow: unavailable.")
        self.current_flow_label.setToolTip(str(self._current_flow_truck_insight.tooltip_text or flow_summary))
        self._set_flow_gantt_png(self._current_flow_truck_insight.gantt_png_bytes)

    def _refresh_hidden_action_labels(self) -> None:
        truck_number = self.current_truck_number()
        truck_hidden = bool(truck_number and is_hidden_truck(truck_number, self.settings))
        self.toggle_truck_hidden_button.setEnabled(bool(truck_number))
        self.toggle_truck_hidden_button.setText("Show Truck" if truck_hidden else "Hide Truck")
        self.edit_truck_client_button.setEnabled(bool(truck_number))
        self.edit_truck_client_button.setText(
            "Client"
        )
        self._refresh_show_hidden_trucks_button()
        self._refresh_current_truck_heading()

        selected_statuses = self._selected_statuses()
        selected_hidden = bool(selected_statuses) and all(
            is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
            for status in selected_statuses
        )
        self.toggle_selected_kits_hidden_button.setEnabled(bool(selected_statuses))
        self.toggle_selected_kits_hidden_button.setText(
            "Show Kits" if selected_hidden else "Hide Kits"
        )
        self._refresh_details_pane()

    def _refresh_show_hidden_trucks_button(self) -> None:
        hidden_count = len(normalize_hidden_truck_entries(self.settings.hidden_trucks))
        showing_hidden = self.show_hidden_trucks_button.isChecked()
        if hidden_count == 0 and showing_hidden:
            self.show_hidden_trucks_button.blockSignals(True)
            self.show_hidden_trucks_button.setChecked(False)
            self.show_hidden_trucks_button.blockSignals(False)
            showing_hidden = False
        label_prefix = "Hide Hidden" if showing_hidden else "Show Hidden"
        self.show_hidden_trucks_button.setText(f"{label_prefix} ({hidden_count})")
        if hidden_count:
            self.show_hidden_trucks_button.setEnabled(True)
            self.show_hidden_trucks_button.setToolTip(
                f"{hidden_count} hidden truck(s). Toggle to {'hide' if showing_hidden else 'show'} them in the truck list."
            )
            return
        if showing_hidden:
            self.show_hidden_trucks_button.setEnabled(True)
            self.show_hidden_trucks_button.setToolTip("No trucks are hidden right now.")
            return
        self.show_hidden_trucks_button.setEnabled(False)
        self.show_hidden_trucks_button.setToolTip("No trucks are hidden right now.")

    def _refresh_truck_order_buttons(self) -> None:
        row = self.truck_list.currentRow()
        count = self.truck_list.count()
        self.move_truck_up_button.setEnabled(count > 0 and row > 0)
        self.move_truck_down_button.setEnabled(count > 0 and 0 <= row < count - 1)

    def _visible_truck_numbers(self) -> list[str]:
        return [
            self.truck_list.item(row).text().strip()
            for row in range(self.truck_list.count())
            if self.truck_list.item(row) is not None
        ]

    def _persist_truck_order(self) -> None:
        self.settings.truck_order = normalize_truck_order_entries(self._all_trucks)
        save_settings(self.settings)

    def _move_selected_truck(self, direction: int) -> None:
        current_row = self.truck_list.currentRow()
        if current_row < 0:
            return
        target_row = current_row + direction
        visible_trucks = self._visible_truck_numbers()
        if target_row < 0 or target_row >= len(visible_trucks):
            return

        current_truck = visible_trucks[current_row]
        target_truck = visible_trucks[target_row]
        try:
            all_current_index = next(
                index for index, truck_number in enumerate(self._all_trucks)
                if truck_number.casefold() == current_truck.casefold()
            )
            all_target_index = next(
                index for index, truck_number in enumerate(self._all_trucks)
                if truck_number.casefold() == target_truck.casefold()
            )
        except StopIteration:
            return

        self._all_trucks[all_current_index], self._all_trucks[all_target_index] = (
            self._all_trucks[all_target_index],
            self._all_trucks[all_current_index],
        )
        self._persist_truck_order()
        self._apply_truck_filter()
        self._select_truck(current_truck)
        self.log(f"Updated fabrication truck order: {current_truck}")

    def _save_hidden_state(self) -> None:
        self.settings.hidden_trucks = normalize_hidden_truck_entries(self.settings.hidden_trucks)
        self.settings.hidden_kits = canonicalize_hidden_kit_entries(
            self.settings.hidden_kits,
            self.settings.kit_templates,
        )
        save_settings(self.settings)

    def toggle_current_truck_hidden(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Hide Truck", "Select a truck first.")
            return

        self._ensure_saved_settings()
        hidden = is_hidden_truck(truck_number, self.settings)
        if hidden:
            self.settings.hidden_trucks = [
                value
                for value in self.settings.hidden_trucks
                if value.casefold() != truck_number.casefold()
            ]
            action_text = "Unhid"
        else:
            self.settings.hidden_trucks = list(self.settings.hidden_trucks) + [truck_number]
            action_text = "Hid"

        self._save_hidden_state()
        self.refresh_trucks()
        if hidden or self.show_hidden_trucks_button.isChecked():
            self._select_truck(truck_number)
        self.log(f"{action_text} truck {truck_number} from the explorer list.")

    def edit_current_truck_client_number(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Client Number", "Select a truck first.")
            return

        self._ensure_saved_settings()
        current_value = self._client_number_for_truck(truck_number)
        client_number, ok = QInputDialog.getText(
            self,
            "Client Number",
            f"Client number for {truck_number}:",
            text=current_value,
        )
        if not ok:
            return

        truck_key = normalize_hidden_truck_number(truck_number)
        client_text = client_number.strip()
        if client_text:
            self.settings.client_numbers_by_truck[truck_key] = client_text
            action_text = f"Set client number for {truck_number} to {client_text}."
        else:
            self.settings.client_numbers_by_truck.pop(truck_key, None)
            action_text = f"Cleared client number for {truck_number}."

        self.settings.client_numbers_by_truck = canonicalize_client_numbers_by_truck(
            self.settings.client_numbers_by_truck
        )
        save_settings(self.settings)
        self._apply_truck_filter()
        self._select_truck(truck_number)
        self._refresh_current_truck_heading()
        self.log(action_text)

    def toggle_selected_kits_hidden(self) -> None:
        selected_statuses = self._selected_statuses()
        if not selected_statuses:
            QMessageBox.information(self, "Hide Selected Kits", "Select at least one kit row.")
            return

        self._ensure_saved_settings()
        should_unhide = all(
            is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
            for status in selected_statuses
        )

        hidden_keys = {
            value.casefold()
            for value in canonicalize_hidden_kit_entries(self.settings.hidden_kits, self.settings.kit_templates)
        }
        for status in selected_statuses:
            key = build_hidden_kit_key(status.paths.truck_number, status.kit_name)
            if not key:
                continue
            if should_unhide:
                hidden_keys.discard(key.casefold())
            else:
                hidden_keys.add(key.casefold())

        canonical_hidden_kits: list[str] = []
        seen: set[str] = set()
        for value in self.settings.hidden_kits:
            key = value.casefold()
            if key in hidden_keys and key not in seen:
                canonical_hidden_kits.append(value)
                seen.add(key)
        for status in selected_statuses:
            key = build_hidden_kit_key(status.paths.truck_number, status.kit_name)
            if not key:
                continue
            lowered = key.casefold()
            if lowered in hidden_keys and lowered not in seen:
                canonical_hidden_kits.append(key)
                seen.add(lowered)

        self.settings.hidden_kits = canonical_hidden_kits
        self._save_hidden_state()
        self._render_current_statuses()

        action_text = "Unhid" if should_unhide else "Hid"
        names = ", ".join(status.paths.display_name for status in selected_statuses)
        self.log(f"{action_text} kit(s) for {selected_statuses[0].paths.truck_number}: {names}")

    def create_new_truck(self) -> None:
        self._ensure_saved_settings()
        truck_number, ok = QInputDialog.getText(self, "Add Truck", "Truck number:")
        if not ok or not truck_number.strip():
            return

        truck_text = normalize_hidden_truck_number(truck_number)
        if not truck_text:
            QMessageBox.warning(
                self,
                "Add Truck",
                "Enter a truck number in the F##### format, for example F55985.",
            )
            return

        fabrication_truck_dir = find_fabrication_truck_dir(truck_text, self.settings)
        if fabrication_truck_dir is None:
            QMessageBox.warning(
                self,
                "Add Truck",
                f"Could not find truck {truck_text} under the configured W root.",
            )
            return

        created_count = 0
        errors: list[str] = []
        for mapping in configured_kit_mappings(self.settings):
            try:
                result = create_kit_scaffold(truck_text, mapping.kit_name, self.settings)
                created_count += len(result.created_paths)
            except Exception as exc:
                errors.append(f"{mapping.kit_name}: {exc}")

        restored_hidden_truck, restored_hidden_kit_count = restore_truck_visibility(truck_text, self.settings)
        if restored_hidden_truck or restored_hidden_kit_count:
            self._save_hidden_state()

        self._pending_truck_selection = truck_text
        self.search_edit.clear()
        self.refresh_trucks()
        if errors:
            QMessageBox.warning(
                self,
                "Truck scaffold completed with warnings",
                "\n".join(errors),
            )
        self.log(
            f"Created L-side truck scaffold for {truck_text} using W folder {fabrication_truck_dir}. "
            f"Paths touched: {created_count}"
            + (
                f"; restored hidden truck and {restored_hidden_kit_count} hidden kit(s)."
                if restored_hidden_truck or restored_hidden_kit_count
                else "."
            )
        )

    def create_missing_kits_for_selected_truck(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Create Missing Kits", "Select a truck first.")
            return
        self._ensure_saved_settings()
        created = 0
        errors: list[str] = []
        for status in self._current_statuses:
            try:
                result = create_kit_scaffold(truck_number, status.kit_name, self.settings)
            except Exception as exc:
                errors.append(f"{status.paths.display_name}: {exc}")
                continue
            created += len(result.created_paths)
        self._set_current_statuses(collect_kit_statuses(truck_number, self.settings))
        if errors:
            QMessageBox.warning(self, "Create Missing Kits", "\n".join(errors))
        self.log(f"Ensured all kit scaffolds for {truck_number}. Paths touched: {created}")

    def create_selected_kits(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Create / Repair Selected", "Select a truck first.")
            return
        selected = self._selected_statuses()
        if not selected:
            QMessageBox.information(self, "Create / Repair Selected", "Select at least one kit row.")
            return

        self._ensure_saved_settings()
        created = 0
        notes: list[str] = []
        errors: list[str] = []
        for status in selected:
            try:
                result = create_kit_scaffold(truck_number, status.kit_name, self.settings)
            except Exception as exc:
                errors.append(f"{status.paths.display_name}: {exc}")
                continue
            created += len(result.created_paths)
            notes.extend(result.notes)
        self._set_current_statuses(collect_kit_statuses(truck_number, self.settings))
        if errors:
            QMessageBox.warning(self, "Create / Repair Selected", "\n".join(errors))
        if notes:
            self.log(" | ".join(notes))
        self.log(f"Ensured {len(selected)} selected kit scaffold(s). Paths touched: {created}")

    def open_selected_truck_release(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Open Truck Release", "Select a truck first.")
            return
        release_root = Path(self.settings.release_root)
        if not release_root.exists():
            QMessageBox.warning(self, "Open Truck Release", f"Release root not found:\n{release_root}")
            return
        self._open_path_with_message(release_root / truck_number)

    def open_selected_truck_fabrication(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Open Truck W Folder", "Select a truck first.")
            return
        fabrication_truck_dir = find_fabrication_truck_dir(truck_number, self.settings)
        if fabrication_truck_dir is None:
            QMessageBox.warning(self, "Open Truck W Folder", "Could not find that truck under the W root.")
            return
        self._open_path_with_message(fabrication_truck_dir)

    def open_selected_rpd(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Project File", "Select a kit first.")
            return
        self._open_rpd_for_status(status)

    def _open_rpd_for_status(self, status: KitStatus) -> None:
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(
                self,
                "Open Project File",
                "Could not find the project file on the L side for this kit.",
            )
            return
        self._open_path_with_message(status.paths.rpd_path)

    def open_selected_release_folder(self) -> None:
        status = self._current_status()
        if status is None or status.paths.project_dir is None:
            QMessageBox.information(self, "Open Release Folder", "Select a kit first.")
            return
        self._open_path_with_message(status.paths.project_dir)

    def open_selected_fabrication_folder(self) -> None:
        status = self._current_status()
        if status is None or status.paths.fabrication_kit_dir is None:
            QMessageBox.information(self, "Open W Folder", "Select a kit first.")
            return
        self._open_path_with_message(status.paths.fabrication_kit_dir)

    def open_selected_spreadsheet(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Spreadsheet", "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                self,
                "Open Spreadsheet",
                "This kit does not have exactly one spreadsheet candidate in the W folder.",
            )
            return
        self._open_path_with_message(spreadsheet_path)

    def open_selected_flow_pdf(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Flow Link", "Select a kit first.")
            return

        flow_insight = flow_kit_insight_for_explorer_kit(status.kit_name, self._current_flow_truck_insight)
        if not flow_insight.flow_kit_name:
            QMessageBox.information(
                self,
                "Open Flow Link",
                "This kit is not tracked as its own scheduled flow kit in the fabrication flow dashboard.",
            )
            return

        pdf_link = str(flow_insight.pdf_link or "").strip()
        if not pdf_link:
            QMessageBox.warning(
                self,
                "Open Flow Link",
                f"No linked file or URL is set in the fabrication flow dashboard for {flow_insight.flow_kit_name}.",
            )
            return

        try:
            open_external_target(pdf_link)
        except Exception as exc:
            QMessageBox.warning(self, "Open Flow Link", str(exc))
            return
        self.log(f"Opened flow link {pdf_link}")

    def _open_nest_summary_for_status(self, status: KitStatus) -> None:
        summary_path = status.preview_pdf_match.chosen_path
        if summary_path is None:
            QMessageBox.warning(
                self,
                "Open Nest Summary",
                "Could not find a Nest Summary PDF on the L side for this kit.",
            )
            return
        self._open_path_with_message(summary_path)

    def _open_print_packet_for_status(self, status: KitStatus) -> None:
        packet_match = detect_print_packet_pdf(status.paths)
        packet_path = packet_match.chosen_path
        if packet_path is None:
            QMessageBox.warning(
                self,
                "Open Print Packet",
                "Could not find a Print Packet PDF on the L side for this kit.",
            )
            return
        self._open_path_with_message(packet_path)

    def _open_assembly_packet_for_status(self, status: KitStatus) -> None:
        packet_match = detect_assembly_packet_pdf(status.paths)
        packet_path = packet_match.chosen_path
        if packet_path is None:
            QMessageBox.warning(
                self,
                "Open Assembly Packet",
                "Could not find an Assembly Packet PDF on the L side for this kit.",
            )
            return
        self._open_path_with_message(packet_path)

    def _open_cut_list_for_status(self, status: KitStatus) -> None:
        packet_match = detect_cut_list_packet_pdf(status.paths)
        packet_path = packet_match.chosen_path
        if packet_path is None:
            QMessageBox.warning(
                self,
                "Open Cut List",
                "Could not find a Cut List PDF on the L side for this kit.",
            )
            return
        self._open_path_with_message(packet_path)

    def open_selected_nest_summary(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Nest Summary", "Select a kit first.")
            return
        self._open_nest_summary_for_status(status)

    def open_selected_print_packet(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Print Packet", "Select a kit first.")
            return
        self._open_print_packet_for_status(status)

    def open_selected_assembly_packet(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Assembly Packet", "Select a kit first.")
            return
        self._open_assembly_packet_for_status(status)

    def open_selected_cut_list(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Cut List", "Select a kit first.")
            return
        self._open_cut_list_for_status(status)

    def _prepare_packet_build_context(self, title: str):
        if bool(getattr(self, "_packet_build_running", False)):
            QMessageBox.information(
                self,
                title,
                "A packet build is already running. Let it finish before starting another one.",
            )
            return None, None

        status = self._current_status()
        if status is None:
            QMessageBox.information(self, title, "Select a kit first.")
            return None, None
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(
                self,
                title,
                "The L-side project file is missing for this kit.",
            )
            return None, None
        running_import, _lock_path, lock_pid = radan_csv_import_lock_status(status.paths.rpd_path)
        if running_import:
            pid_text = f" PID {lock_pid}" if lock_pid is not None else ""
            QMessageBox.warning(
                self,
                title,
                "A RADAN CSV import is still running for this project.\n\n"
                f"Let the import helper{pid_text} finish before building packets.",
            )
            return None, None
        if status.paths.fabrication_kit_dir is None or not status.paths.fabrication_kit_dir.exists():
            QMessageBox.warning(
                self,
                title,
                "The W-side kit folder is missing for this kit.",
            )
            return None, None

        try:
            context = prepare_packet_build_context(
                rpd_path=status.paths.rpd_path,
                fabrication_dir=status.paths.fabrication_kit_dir,
                settings=self.settings,
            )
        except Exception as exc:
            QMessageBox.critical(self, title, str(exc))
            return None, None

        return status, context

    def _refresh_packet_statuses(self, status: KitStatus) -> None:
        if status.paths.truck_number.casefold() == self.current_truck_number().casefold():
            self._set_current_statuses(collect_kit_statuses(status.paths.truck_number, self.settings))

    def _queue_status_refresh_for_truck(self, truck_number: str) -> bool:
        truck_text = str(truck_number or "").strip()
        if not truck_text:
            return False
        truck_key = truck_text.casefold()
        if truck_key in self._pending_status_by_truck:
            return False
        self._pending_status_by_truck[truck_key] = (
            truck_text,
            self._status_executor.submit(collect_kit_statuses, truck_text, self.settings),
        )
        self._status_watch_timer.start()
        return True

    def _start_kitter_status_refresh(self, truck_number: str) -> None:
        truck_text = str(truck_number or "").strip()
        if not truck_text:
            return
        self._kitter_refresh_truck_number = truck_text
        self._kitter_refresh_remaining = self.KITTER_STATUS_REFRESH_ATTEMPTS
        self._queue_status_refresh_for_truck(truck_text)
        self._kitter_status_refresh_timer.start()

    def _poll_kitter_status_refresh(self) -> None:
        truck_text = self._kitter_refresh_truck_number.strip()
        if not truck_text or self._kitter_refresh_remaining <= 0:
            self._kitter_status_refresh_timer.stop()
            return
        if self._queue_status_refresh_for_truck(truck_text):
            self._kitter_refresh_remaining -= 1

    def _refresh_current_status_from_external_changes(self) -> None:
        self._queue_status_refresh_for_truck(self.current_truck_number())

    def build_selected_print_packet(self) -> None:
        status, context = self._prepare_packet_build_context("Build Print Packet")
        if status is None or context is None:
            return
        if not context.parts:
            QMessageBox.information(
                self,
                "Build Print Packet",
                "No parts were found in the selected RPD.",
            )
            return

        expected_csv_path = None
        if status.spreadsheet_match.chosen_path is not None:
            try:
                expected_csv_path = resolve_existing_inventor_csv(
                    status.spreadsheet_match.chosen_path,
                    status.paths.project_dir,
                )
            except Exception:
                expected_csv_path = None
        try:
            validate_print_packet_readiness(
                rpd_path=status.paths.rpd_path,
                parts=context.parts,
                expected_csv_path=expected_csv_path,
            )
        except PacketBuildReadinessError as exc:
            QMessageBox.warning(self, "Build Print Packet", str(exc))
            return

        total_steps = max(1, len(context.parts))
        progress = QProgressDialog("Building print packet...", "Cancel", 0, total_steps, self)
        progress.setWindowTitle("Build Print Packet")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = create_main_packet_worker(
            context=context,
            rpd_path=status.paths.rpd_path,
        )
        setattr(self, "_packet_build_running", True)
        setattr(self, "_packet_build_worker", worker)

        def _set_progress(done: int, status_text: str) -> None:
            progress.setMaximum(total_steps)
            progress.setValue(max(0, min(total_steps, int(done))))
            progress.setLabelText(f"Building print packet...\n{status_text}")
            QApplication.processEvents()

        def _cleanup() -> None:
            try:
                progress.close()
            except Exception:
                pass
            setattr(self, "_packet_build_worker", None)
            setattr(self, "_packet_build_running", False)

        def _on_progress(done: int, total: int, status_text: str) -> None:
            _set_progress(int(done), status_text)

        def _on_done(packet_path: str, pages: int, missing: int) -> None:
            self._refresh_packet_statuses(status)
            _cleanup()
            try:
                open_path(Path(packet_path))
            except Exception:
                pass
            QMessageBox.information(
                self,
                "Build Print Packet",
                (
                    f"Print packet pages: {int(pages)}\n"
                    f"Missing part PDFs: {int(missing)}\n"
                    f"Print packet: {packet_path}"
                ),
            )
            self.log(f"Built print packet for {status.paths.display_name}.")

        def _on_canceled(pages: int, missing: int) -> None:
            _cleanup()
            self.log("Print packet build canceled.")

        def _on_empty(message: str, pages: int, missing: int) -> None:
            _cleanup()
            QMessageBox.information(
                self,
                "Build Print Packet",
                f"{message}\n\nMissing part PDFs: {int(missing)}",
            )

        def _on_error(tb: str) -> None:
            _cleanup()
            QMessageBox.critical(
                self,
                "Build Print Packet",
                str(tb or "").strip() or "Packet build failed.",
            )

        worker.signals.progress.connect(_on_progress)
        worker.signals.done.connect(_on_done)
        worker.signals.canceled.connect(_on_canceled)
        worker.signals.empty.connect(_on_empty)
        worker.signals.error.connect(_on_error)
        progress.canceled.connect(worker.request_stop)
        _set_progress(0, "Starting")
        QThreadPool.globalInstance().start(worker)

    def build_selected_assembly_packet(self) -> None:
        if not self.ASSEMBLY_PACKET_BUILD_ENABLED:
            QMessageBox.information(self, "Build Assembly Packet", self.ASSEMBLY_PACKET_DISABLED_REASON)
            return
        status, context = self._prepare_packet_build_context("Build Assembly Packet")
        if status is None or context is None:
            return
        if not context.assembly_source_pdfs:
            searched = "\n".join(str(path) for path in context.assembly_search_roots) or "(none)"
            QMessageBox.information(
                self,
                "Build Assembly Packet",
                "No .iam-backed assembly drawing PDFs were found.\n\nSearched:\n" + searched,
            )
            return

        total_steps = max(1, len(context.assembly_source_pdfs))
        progress = QProgressDialog("Building assembly packet...", "Cancel", 0, total_steps, self)
        progress.setWindowTitle("Build Assembly Packet")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        setattr(self, "_packet_build_running", True)
        try:
            result = build_assembly_packet(
                rpd_path=status.paths.rpd_path,
                source_pdfs=context.assembly_source_pdfs,
                progress_cb=lambda done, total, status_text: (
                    progress.setMaximum(total_steps),
                    progress.setValue(max(0, min(total_steps, int(done)))),
                    progress.setLabelText(f"Building assembly packet...\n{status_text}"),
                    QApplication.processEvents(),
                ),
                should_cancel_cb=progress.wasCanceled,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Build Assembly Packet", str(exc))
            result = None
        finally:
            setattr(self, "_packet_build_running", False)
            try:
                progress.close()
            except Exception:
                pass

        if result is None:
            return

        self._refresh_packet_statuses(status)

        if result.packet_path:
            try:
                open_path(Path(result.packet_path))
            except Exception:
                pass
            QMessageBox.information(
                self,
                "Build Assembly Packet",
                (
                    f"Assembly packet documents: {int(result.source_documents)}\n"
                    f"Assembly packet pages: {int(result.output_pages)}\n"
                    f"Assembly packet: {result.packet_path}"
                ),
            )
            self.log(f"Built assembly packet for {status.paths.display_name}.")
            return

        if result.skipped:
            self.log("Assembly packet build canceled.")
            return

        QMessageBox.information(
            self,
            "Build Assembly Packet",
            "No .iam-backed assembly drawing PDFs were found.",
        )

    def build_selected_cut_list_packet(self) -> None:
        if not self.CUT_LIST_BUILD_ENABLED:
            QMessageBox.information(self, "Build Cut List", self.CUT_LIST_DISABLED_REASON)
            return
        status, context = self._prepare_packet_build_context("Build Cut List")
        if status is None or context is None:
            return
        if not context.cut_list_source_pdfs:
            searched = "\n".join(str(path) for path in context.assembly_search_roots) or "(none)"
            QMessageBox.information(
                self,
                "Build Cut List",
                "No non-laser cut list PDFs were found.\n\nSearched:\n" + searched,
            )
            return

        total_steps = max(1, len(context.cut_list_source_pdfs))
        progress = QProgressDialog("Building cut list...", "Cancel", 0, total_steps, self)
        progress.setWindowTitle("Build Cut List")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        setattr(self, "_packet_build_running", True)
        try:
            result = build_cut_list_packet(
                rpd_path=status.paths.rpd_path,
                source_pdfs=context.cut_list_source_pdfs,
                progress_cb=lambda done, total, status_text: (
                    progress.setMaximum(total_steps),
                    progress.setValue(max(0, min(total_steps, int(done)))),
                    progress.setLabelText(f"Building cut list...\n{status_text}"),
                    QApplication.processEvents(),
                ),
                should_cancel_cb=progress.wasCanceled,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Build Cut List", str(exc))
            result = None
        finally:
            setattr(self, "_packet_build_running", False)
            try:
                progress.close()
            except Exception:
                pass

        if result is None:
            return

        self._refresh_packet_statuses(status)

        if result.packet_path:
            try:
                open_path(Path(result.packet_path))
            except Exception:
                pass
            QMessageBox.information(
                self,
                "Build Cut List",
                (
                    f"Cut list documents: {int(result.source_documents)}\n"
                    f"Cut list pages: {int(result.output_pages)}\n"
                    f"Cut list: {result.packet_path}"
                ),
            )
            self.log(f"Built cut list for {status.paths.display_name}.")
            return

        if result.skipped:
            self.log("Cut list build canceled.")
            return

        QMessageBox.information(
            self,
            "Build Cut List",
            "No non-laser cut list PDFs were found.",
        )

    def launch_selected_kitter(self) -> None:
        status = self._current_status()
        if status is None or status.paths.rpd_path is None:
            QMessageBox.information(self, "Launch RADAN Kitter", "Select a kit first.")
            return
        launcher_text = self.settings.radan_kitter_launcher.strip()
        if not launcher_text:
            QMessageBox.warning(self, "Launch RADAN Kitter", "RADAN Kitter launcher is not configured.")
            return
        try:
            launch_launcher(Path(launcher_text), status.paths.rpd_path)
        except Exception as exc:
            QMessageBox.critical(self, "Launch RADAN Kitter", str(exc))
            return
        self._start_kitter_status_refresh(status.paths.truck_number)
        self.log(f"Launched RADAN Kitter on {status.paths.rpd_path}")

    def open_flow_app(self) -> None:
        launcher_text = self.settings.dashboard_launcher.strip()
        if not launcher_text:
            QMessageBox.warning(self, "Open Flow App", "Flow app launcher is not configured.")
            return
        try:
            launch_tool(Path(launcher_text))
        except Exception as exc:
            QMessageBox.critical(self, "Open Flow App", str(exc))
            return
        self.log("Launched fabrication flow app.")

    def open_dashboard(self) -> None:
        self.open_flow_app()

    def run_selected_inventor_flow(self) -> None:
        if self._pending_inventor_job is not None:
            QMessageBox.information(
                self,
                "Run Inventor Tool",
                "An Inventor output watch is already active. Let it finish before starting another one.",
            )
            return

        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Run Inventor Tool", "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                self,
                "Run Inventor Tool",
                "This kit does not have exactly one spreadsheet candidate in the W folder.",
            )
            return
        if status.paths.project_dir is None:
            QMessageBox.warning(
                self,
                "Run Inventor Tool",
                "The L-side project folder is not available for this kit.",
            )
            return

        self._ensure_saved_settings()
        entry_text = self.settings.inventor_to_radan_entry.strip()
        if not entry_text:
            QMessageBox.warning(self, "Run Inventor Tool", "Inventor launcher is not configured.")
            return
        entry_path = Path(entry_text)

        self.launch_inventor_button.setEnabled(False)
        self.launch_inventor_button.setText("Running Inventor...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            result = run_inventor_to_radan_inline(entry_path, spreadsheet_path)
            outputs, moved_paths = move_inventor_outputs_to_project(
                spreadsheet_path,
                status.paths.project_dir,
            )
        except InventorToRadanInlineNeedsUi as exc:
            QApplication.restoreOverrideCursor()
            self.launch_inventor_button.setEnabled(True)
            self.launch_inventor_button.setText("Run Inventor Tool")
            result = None
            moved_paths = ()
            fallback_reason = str(exc)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self.launch_inventor_button.setEnabled(True)
            self.launch_inventor_button.setText("Run Inventor Tool")
            QMessageBox.critical(self, "Run Inventor Tool", str(exc))
            return
        else:
            QApplication.restoreOverrideCursor()
            self.launch_inventor_button.setEnabled(True)
            self.launch_inventor_button.setText("Run Inventor Tool")
            if status.paths.truck_number.casefold() == self.current_truck_number().casefold():
                self._set_current_statuses(collect_kit_statuses(status.paths.truck_number, self.settings))
            added_count = getattr(result, "added_count", None)
            row_text = f" ({added_count} RADAN row{'s' if added_count != 1 else ''})" if added_count is not None else ""
            self.log("Ran Inventor-to-RADAN inline and moved outputs to L: " + ", ".join(str(path) for path in moved_paths))
            report_path = outputs.target_report_path
            if report_path is None or not report_path.exists():
                report_path = next((path for path in moved_paths if path.suffix.casefold() == ".txt"), None)
            if report_path is not None and report_path.exists():
                try:
                    review_dialog = InventorReportReviewDialog(report_path, self)
                    review_result = review_dialog.exec()
                except Exception as exc:
                    QMessageBox.critical(self, "Run Inventor Tool", f"Could not show report review:\n\n{exc}")
                    return
                if review_result != QDialog.Accepted:
                    discard_paths = tuple(
                        path
                        for path in moved_paths
                        if path.suffix.casefold() in {".csv", ".txt"}
                    )
                    deleted_paths, failed_deletes = delete_paths(discard_paths)
                    if status.paths.truck_number.casefold() == self.current_truck_number().casefold():
                        self._set_current_statuses(collect_kit_statuses(status.paths.truck_number, self.settings))
                    self.log("Discarded unacknowledged Inventor-to-RADAN outputs: " + ", ".join(str(path) for path in deleted_paths))
                    if failed_deletes:
                        QMessageBox.warning(
                            self,
                            "Run Inventor Tool",
                            "The Inventor-to-RADAN report was not acknowledged, but some generated files could not be deleted.\n\n"
                            + "\n".join(failed_deletes),
                        )
                    else:
                        QMessageBox.information(
                            self,
                            "Run Inventor Tool",
                            "The Inventor-to-RADAN report was not acknowledged, so the generated CSV/report were deleted.\n\n"
                            + "\n".join(str(path) for path in deleted_paths),
                        )
                    return
            else:
                QMessageBox.critical(
                    self,
                    "Run Inventor Tool",
                    "Inventor-to-RADAN completed, but the generated report could not be found for review.",
                )
                return
            QMessageBox.information(
                self,
                "Run Inventor Tool",
                f"Inventor-to-RADAN ran inline{row_text} and the output was moved to L.\n\n"
                + "\n".join(str(path) for path in moved_paths),
            )
            return

        try:
            process = launch_inventor_to_radan(entry_path, spreadsheet_path)
        except Exception as exc:
            QMessageBox.critical(self, "Run Inventor Tool", str(exc))
            return

        self._pending_inventor_job = PendingInventorJob(
            truck_number=status.paths.truck_number,
            kit_name=status.kit_name,
            spreadsheet_path=spreadsheet_path,
            project_dir=status.paths.project_dir,
            outputs=inventor_output_paths(spreadsheet_path, status.paths.project_dir),
            process=process,
            started_at_monotonic=time.monotonic(),
        )
        self.launch_inventor_button.setEnabled(False)
        self.launch_inventor_button.setText("Watching Inventor...")
        self._inventor_watch_timer.start()
        self.log(
            f"Launched Inventor tool for {spreadsheet_path.name} because {fallback_reason.lower()} "
            "Finish the external prompts; output will be moved to L automatically when ready."
        )
        QMessageBox.information(
            self,
            "Run Inventor Tool",
            f"{fallback_reason}\n\nInventor has been launched in its own window.\n\n"
            "Complete its prompts there. This explorer will keep watching and move the generated output to L once the files exist and stop changing.",
        )

    def import_selected_csv_to_radan(self) -> None:
        title = "Import CSV to RADAN"
        use_cleaned_dxf = (
            getattr(self, "radan_dxf_source_slider", None) is not None
            and self.radan_dxf_source_slider.value() == 1
        )
        project_update_method = "direct-xml"
        if (
            getattr(self, "radan_project_update_slider", None) is not None
            and self.radan_project_update_slider.isEnabled()
            and self.radan_project_update_slider.value() == 1
        ):
            project_update_method = "radan-nst"
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, title, "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                self,
                title,
                "This kit does not have exactly one spreadsheet candidate, so the expected _Radan.csv path is ambiguous.",
            )
            return
        if status.paths.project_dir is None or not status.paths.project_dir.exists():
            QMessageBox.warning(
                self,
                title,
                "The L-side project folder is not available for this kit.",
            )
            return
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(
                self,
                title,
                "The L-side project file is missing for this kit.",
            )
            return
        try:
            csv_path = resolve_existing_inventor_csv(spreadsheet_path, status.paths.project_dir)
        except Exception as exc:
            QMessageBox.warning(self, title, str(exc))
            return

        output_folder = status.paths.release_kit_dir
        if output_folder is None or not output_folder.exists():
            QMessageBox.warning(
                self,
                title,
                f"The expected RADAN symbol output folder is missing:\n{output_folder}",
            )
            return
        running_import, lock_path, lock_pid = radan_csv_import_lock_status(status.paths.rpd_path)
        if running_import:
            QMessageBox.information(
                self,
                title,
                "A RADAN CSV import is already running for this project.\n\n"
                f"PID: {lock_pid}\nLock: {lock_path}",
            )
            self.log(f"RADAN CSV import already running for {status.paths.rpd_path} (PID {lock_pid}).")
            return
        allow_visible_radan = False
        try:
            missing_symbols = radan_csv_missing_symbols(csv_path, output_folder)
        except Exception as exc:
            QMessageBox.warning(self, title, f"Could not inspect CSV symbols:\n{exc}")
            return
        if missing_symbols or use_cleaned_dxf or project_update_method == "radan-nst":
            try:
                visible_sessions = visible_radan_sessions()
            except Exception:
                visible_sessions = ()
            if visible_sessions:
                if use_cleaned_dxf:
                    sample_symbols = "All CSV symbols will be rebuilt from cleaned L-side DXF working copies."
                else:
                    sample_symbols = "\n".join(str(path.name) for path in missing_symbols[:12])
                    if len(missing_symbols) > 12:
                        sample_symbols += f"\n... (+{len(missing_symbols) - 12} more)"
                    if not sample_symbols:
                        sample_symbols = "No symbol conversion; project update will use RADAN NST."
                session_text = "\n".join(f"{pid}: {title}" for pid, title in visible_sessions[:8])
                if len(visible_sessions) > 8:
                    session_text += f"\n... (+{len(visible_sessions) - 8} more)"
                choice = QMessageBox.warning(
                    self,
                    "Visible RADAN Sessions Are Open",
                    "This import still needs RADAN automation.\n\n"
                    "During conversion or RADAN NST project update, RADAN COM automation can redraw or disturb already-open RADAN windows, "
                    "and those sessions may not be safe to keep saving afterward.\n\n"
                    f"Open RADAN sessions:\n{session_text}\n\n"
                    f"Work requiring RADAN:\n{sample_symbols}\n\n"
                    "Continue anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if choice != QMessageBox.Yes:
                    self.log(
                        "RADAN CSV import cancelled before conversion because visible RADAN sessions were open."
                    )
                    return
                allow_visible_radan = True
        log_dir = self._runtime_dir / "_runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(
            character if character.isalnum() else "_"
            for character in f"{status.paths.truck_number}_{status.paths.project_name}"
        ).strip("_")
        if use_cleaned_dxf:
            log_prefix = "radan_csv_cleaned_import"
        else:
            log_prefix = "radan_csv_import"
        log_path = log_dir / f"{log_prefix}_{safe_name}_{int(time.time())}.log"
        log_dialog = self._show_radan_import_log(log_path)
        try:
            process = launch_radan_csv_import(
                csv_path,
                output_folder,
                project_path=status.paths.rpd_path,
                log_path=log_path,
                allow_visible_radan=allow_visible_radan,
                rebuild_symbols=use_cleaned_dxf,
                preprocess_dxf_outer_profile=use_cleaned_dxf,
                preprocess_dxf_tolerance=0.002 if use_cleaned_dxf else None,
                project_update_method=project_update_method,
                refresh_project_sheets=True,
            )
        except Exception as exc:
            log_dialog.mark_launch_failed(str(exc))
            QMessageBox.critical(self, title, str(exc))
            return
        self._write_radan_import_marker(
            process=process,
            log_path=log_path,
            project_path=status.paths.rpd_path,
        )
        log_dialog.set_process(process)

        if use_cleaned_dxf:
            self.log(
                f"Launched RADAN CSV import for {csv_path.name} using cleaned L-side DXF working copies; "
                f"output folder is {output_folder}; project_update={project_update_method}; sheet_refresh=on."
            )
        else:
            self.log(
                f"Launched RADAN CSV import for {csv_path.name}; "
                f"output folder is {output_folder}; "
                f"project_update={project_update_method}; sheet_refresh=on."
            )

    def _show_radan_import_log(self, log_path: Path) -> ImportLogDialog:
        self._radan_import_log_dialogs = [
            dialog
            for dialog in self._radan_import_log_dialogs
            if not dialog.is_complete or dialog.isVisible()
        ]
        dialog = self._radan_import_log_dialog
        if dialog is not None and dialog.is_complete:
            try:
                dialog.force_close()
            except Exception:
                pass
        dialog = ImportLogDialog(log_path, self, completion_callback=self._on_radan_import_log_complete)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._radan_import_log_dialog = dialog
        self._radan_import_log_dialogs.append(dialog)
        return dialog

    def _radan_import_marker_path(self) -> Path:
        return self._runtime_dir / "_runtime" / "radan_import_active.json"

    def _write_radan_import_marker(
        self,
        *,
        process: subprocess.Popen[object],
        log_path: Path,
        project_path: Path,
    ) -> None:
        marker_path = self._radan_import_marker_path()
        payload = {
            "pid": int(process.pid),
            "log_path": str(log_path),
            "project_path": str(project_path),
            "started_at_epoch": time.time(),
        }
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            self.log(f"Could not write RADAN import active marker: {exc}")

    def _clear_radan_import_marker(self, *, process_id: int | None = None) -> None:
        marker_path = self._radan_import_marker_path()
        if process_id is not None:
            try:
                payload = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            try:
                marker_pid = int(payload.get("pid", 0))
            except (AttributeError, TypeError, ValueError):
                marker_pid = 0
            if marker_pid and marker_pid != int(process_id):
                return
        try:
            marker_path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"Could not clear RADAN import active marker: {exc}")

    def _on_radan_import_log_complete(self, dialog: ImportLogDialog) -> None:
        self._clear_radan_import_marker(process_id=dialog.process_id)

    def closeEvent(self, event) -> None:
        running_import_logs = [
            dialog
            for dialog in self._radan_import_log_dialogs
            if not dialog.is_complete
        ]
        if running_import_logs:
            dialog = running_import_logs[-1]
            QMessageBox.warning(
                self,
                "RADAN Import Running",
                "A RADAN CSV import helper is still running. The Explorer window will stay open until it finishes.",
            )
            dialog.raise_()
            dialog.activateWindow()
            event.ignore()
            return
        super().closeEvent(event)

    @staticmethod
    def _inventor_output_signature(outputs: InventorOutputPaths) -> tuple[tuple[str, int, int], ...] | None:
        if not outputs.source_csv_path.exists():
            return None
        signature: list[tuple[str, int, int]] = []
        try:
            csv_stat = outputs.source_csv_path.stat()
        except OSError:
            return
        signature.append(
            (
                outputs.source_csv_path.name,
                int(csv_stat.st_size),
                int(csv_stat.st_mtime_ns),
            )
        )
        if outputs.source_report_path.exists():
            try:
                report_stat = outputs.source_report_path.stat()
            except OSError:
                report_stat = None
            if report_stat is not None:
                signature.append(
                    (
                        outputs.source_report_path.name,
                        int(report_stat.st_size),
                        int(report_stat.st_mtime_ns),
                    )
                )
        return tuple(signature)

    def _finish_pending_inventor_job(self, *, reset_button: bool = True) -> None:
        self._pending_inventor_job = None
        self._inventor_watch_timer.stop()
        if reset_button:
            self.launch_inventor_button.setEnabled(True)
            self.launch_inventor_button.setText("Run Inventor Tool")

    def _poll_pending_inventor_job(self) -> None:
        job = self._pending_inventor_job
        if job is None:
            self._inventor_watch_timer.stop()
            return

        if job.launcher_exit_code is None:
            try:
                job.launcher_exit_code = job.process.poll()
            except Exception:
                job.launcher_exit_code = None

        signature = self._inventor_output_signature(job.outputs)
        now = time.monotonic()
        if signature is not None:
            if job.first_output_seen_at_monotonic is None:
                job.first_output_seen_at_monotonic = now
                job.last_output_signature = signature
                job.stable_polls = 0
                self.log(f"Inventor output appeared for {job.spreadsheet_path.name}; waiting for it to settle.")
                return
            if signature == job.last_output_signature:
                job.stable_polls += 1
            else:
                job.last_output_signature = signature
                job.stable_polls = 0

            if job.stable_polls < 2:
                return

            moved_paths: tuple[Path, ...] = ()
            try:
                _outputs, moved_paths = move_inventor_outputs_to_project(
                    job.spreadsheet_path,
                    job.project_dir,
                )
            except Exception as exc:
                self._finish_pending_inventor_job()
                QMessageBox.warning(
                    self,
                    "Run Inventor Tool",
                    "The output appeared, but moving it to L failed.\n\n"
                    f"{exc}",
                )
                return

            if job.truck_number.casefold() == self.current_truck_number().casefold():
                self._set_current_statuses(collect_kit_statuses(job.truck_number, self.settings))
            self._finish_pending_inventor_job()
            self.log("Moved inventor outputs from W to L: " + ", ".join(str(path) for path in moved_paths))
            QMessageBox.information(
                self,
                "Run Inventor Tool",
                "Inventor output was moved to L.\n\n" + "\n".join(str(path) for path in moved_paths),
            )
            return

        if now - job.started_at_monotonic > 45 * 60:
            self._finish_pending_inventor_job()
            QMessageBox.warning(
                self,
                "Run Inventor Tool",
                "Timed out waiting for Inventor output files to appear in W.\n\n"
                "The launcher was started, but no settled output was found to move.",
            )

    def _open_path_with_message(self, path: Path) -> None:
        try:
            open_path(path)
        except Exception as exc:
            QMessageBox.warning(self, "Open Path", str(exc))
            return
        self.log(f"Opened {path}")

    def log(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    def _poll_hot_reload_request(self) -> None:
        if not self._hot_reload_enabled:
            return
        if self._hot_reload_request_path is None:
            return

        if not self._hot_reload_request_path.exists():
            if self._hot_reload_request_id:
                self._hot_reload_request_id = ""
                self._hot_reload_canceled_request_id = ""
                self._clear_hot_reload_banner()
            return

        request = self._read_hot_reload_request()
        request_id = str(request.get("request_id", "")).strip()
        if not request_id:
            return
        if request_id == self._hot_reload_canceled_request_id:
            return
        if request_id != self._hot_reload_request_id:
            self._hot_reload_request_id = request_id
            self._hot_reload_canceled_request_id = ""
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
            self._hot_reload_end_time = ts_float + timeout_float

        now = float(time.time())
        end_time = self._hot_reload_end_time
        if end_time is None:
            end_time = now + 10.0
            self._hot_reload_end_time = end_time

        file_count = request.get("change_count", None)
        files = request.get("files", [])
        seconds_remaining = max(0, int(end_time - now))
        file_text = f"{int(file_count)} file(s)" if isinstance(file_count, int) else "update(s)"
        if self._hot_reload_label is None:
            return
        if isinstance(files, list) and files:
            sample = ", ".join(str(x) for x in files[:3])
            if len(files) > 3:
                sample += ", ..."
            self._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Click Accept Reload to apply now. Sample: {sample}"
            )
        else:
            self._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Click Accept Reload to apply now."
            )
        if self._hot_reload_bar is not None:
            self._hot_reload_bar.setVisible(True)

    def _read_hot_reload_request(self) -> dict[str, str | int | float | list[str]]:
        if self._hot_reload_request_path is None or not self._hot_reload_request_path.exists():
            return {}
        try:
            with self._hot_reload_request_path.open("r", encoding="utf-8") as handle:
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

    def _clear_hot_reload_banner(self) -> None:
        if self._hot_reload_bar is not None:
            self._hot_reload_bar.setVisible(False)

    def _accept_hot_reload_from_banner(self) -> None:
        if not self._hot_reload_request_id:
            return
        self._write_hot_reload_response("accept")
        self._clear_hot_reload_banner()
        self.statusBar().showMessage("Hot reload accepted; restarting app.", 3000)

    def _cancel_hot_reload_from_banner(self) -> None:
        if not self._hot_reload_request_id:
            return
        self._write_hot_reload_response("reject")
        self._hot_reload_canceled_request_id = self._hot_reload_request_id
        self._clear_hot_reload_banner()
        self.statusBar().showMessage("Hot reload canceled for current change batch.", 3000)

    def _write_hot_reload_response(self, action: str) -> None:
        if not self._hot_reload_response_path or not self._hot_reload_request_id:
            return
        payload = {
            "request_id": self._hot_reload_request_id,
            "action": str(action or "").strip().lower(),
        }
        try:
            self._hot_reload_response_path.parent.mkdir(parents=True, exist_ok=True)
            self._hot_reload_response_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return
