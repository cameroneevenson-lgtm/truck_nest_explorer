from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models import (
    canonicalize_hidden_kit_entries,
    ExplorerSettings,
    KitStatus,
    PdfMatch,
    build_hidden_kit_key,
    normalize_hidden_truck_entries,
    normalize_kit_template_entries,
)
from pdf_preview import PdfPreviewPane
from services import (
    collect_kit_statuses,
    configured_kit_mappings,
    copy_inventor_outputs_to_project,
    create_kit_scaffold,
    detect_print_packet_pdf,
    discover_trucks,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    is_hidden_kit,
    is_hidden_truck,
    launch_launcher,
    open_path,
    run_inventor_to_radan,
)
from settings_store import load_settings, save_settings


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


class MainWindow(QMainWindow):
    TABLE_COLUMNS = (
        "Kit",
        "L Folder",
        "Project",
        "RPD",
        "W Folder",
        "Spreadsheet",
        "Import CSV",
        "Summary",
    )

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
        self._punch_codes_save_timer = QTimer(self)
        self._punch_codes_save_timer.setSingleShot(True)
        self._punch_codes_save_timer.timeout.connect(self._save_punch_codes_only)
        self._active_punch_codes_kit_name = ""
        self._punch_codes_dirty = False
        self._punch_codes_loaded_from_legacy = False

        self._build_ui()
        self._load_settings_into_form()
        self.refresh_trucks()

    def _build_ui(self) -> None:
        central = QWidget()
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

    def timerEvent(self, event):  # type: ignore[override]
        if self._hot_reload_timer is not None and event.timerId() == self._hot_reload_timer:
            self._poll_hot_reload_request()
            return
        super().timerEvent(event)

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
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.refresh_button)
        header.addWidget(self.new_truck_button)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter trucks...")
        self.search_edit.textChanged.connect(self._apply_truck_filter)

        self.show_hidden_trucks_checkbox = QCheckBox("Show hidden trucks")
        self.show_hidden_trucks_checkbox.toggled.connect(self._apply_truck_filter)

        self.truck_list = QListWidget()
        self.truck_list.currentItemChanged.connect(self._on_truck_changed)

        layout.addLayout(header)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.show_hidden_trucks_checkbox)
        layout.addWidget(self.truck_list, 1)
        box.setMinimumWidth(320)
        return box

    def _build_right_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_settings_group())
        layout.addWidget(self._build_actions_group())

        workspace_splitter = QSplitter(Qt.Vertical)
        workspace_splitter.addWidget(self._build_table_group())
        workspace_splitter.addWidget(self._build_detail_panel())
        workspace_splitter.setStretchFactor(0, 3)
        workspace_splitter.setStretchFactor(1, 2)
        layout.addWidget(workspace_splitter, 1)
        return box

    def _build_settings_group(self) -> QWidget:
        group = QGroupBox("Settings")
        layout = QGridLayout(group)
        layout.setColumnStretch(1, 1)

        self.release_root_edit = QLineEdit()
        self.fabrication_root_edit = QLineEdit()
        self.rpd_template_edit = QLineEdit()
        self.radan_kitter_edit = QLineEdit()
        self.inventor_entry_edit = QLineEdit()
        self.create_support_folders_checkbox = QCheckBox("Create _bak / _out / _kits support folders")

        browse_release = QPushButton("Browse")
        browse_release.clicked.connect(lambda: self._pick_directory(self.release_root_edit))
        browse_fabrication = QPushButton("Browse")
        browse_fabrication.clicked.connect(lambda: self._pick_directory(self.fabrication_root_edit))
        browse_template = QPushButton("Browse")
        browse_template.clicked.connect(
            lambda: self._pick_file(
                self.rpd_template_edit,
                "Select blank template RPD",
                "RADAN Project (*.rpd);;All Files (*.*)",
            )
        )
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
                "Select inventor_to_radan entry",
                "Python or Batch (*.py *.bat *.cmd);;All Files (*.*)",
            )
        )

        self.template_summary_label = QLabel()
        self.replacements_summary_label = QLabel()
        self.template_summary_label.setWordWrap(True)
        self.replacements_summary_label.setWordWrap(True)

        edit_templates_button = QPushButton("Edit Kit Mappings")
        edit_templates_button.clicked.connect(self.edit_kit_templates)
        edit_rules_button = QPushButton("Edit Template Rules")
        edit_rules_button.clicked.connect(self.edit_template_rules)
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
        layout.addWidget(QLabel("Blank Template RPD"), row, 0)
        layout.addWidget(self.rpd_template_edit, row, 1)
        layout.addWidget(browse_template, row, 2)
        row += 1
        layout.addWidget(QLabel("RADAN Kitter Launcher"), row, 0)
        layout.addWidget(self.radan_kitter_edit, row, 1)
        layout.addWidget(browse_kitter, row, 2)
        row += 1
        layout.addWidget(QLabel("Inventor Entry"), row, 0)
        layout.addWidget(self.inventor_entry_edit, row, 1)
        layout.addWidget(browse_inventor, row, 2)
        row += 1
        layout.addWidget(self.create_support_folders_checkbox, row, 0, 1, 3)
        row += 1
        layout.addWidget(self.template_summary_label, row, 0, 1, 2)
        layout.addWidget(edit_templates_button, row, 2)
        row += 1
        layout.addWidget(self.replacements_summary_label, row, 0, 1, 2)
        layout.addWidget(edit_rules_button, row, 2)
        row += 1
        layout.addWidget(save_button, row, 2)

        return group

    def _build_actions_group(self) -> QWidget:
        group = QGroupBox("Truck / Kit Actions")
        layout = QVBoxLayout(group)

        self.current_truck_label = QLabel("Selected Truck: (none)")
        self.current_truck_label.setStyleSheet("font-size: 18px; font-weight: 700;")

        truck_row = QHBoxLayout()
        self.create_missing_button = QPushButton("Create Missing Kits")
        self.create_missing_button.clicked.connect(self.create_missing_kits_for_selected_truck)
        self.create_selected_button = QPushButton("Create / Repair Selected")
        self.create_selected_button.clicked.connect(self.create_selected_kits)
        self.open_truck_release_button = QPushButton("Open Truck Release")
        self.open_truck_release_button.clicked.connect(self.open_selected_truck_release)
        self.open_truck_fabrication_button = QPushButton("Open Truck W Folder")
        self.open_truck_fabrication_button.clicked.connect(self.open_selected_truck_fabrication)
        self.toggle_truck_hidden_button = QPushButton("Hide Truck")
        self.toggle_truck_hidden_button.clicked.connect(self.toggle_current_truck_hidden)
        truck_row.addWidget(self.create_missing_button)
        truck_row.addWidget(self.create_selected_button)
        truck_row.addWidget(self.open_truck_release_button)
        truck_row.addWidget(self.open_truck_fabrication_button)
        truck_row.addWidget(self.toggle_truck_hidden_button)
        truck_row.addStretch(1)

        kit_row = QHBoxLayout()
        self.open_rpd_button = QPushButton("Open RPD")
        self.open_rpd_button.clicked.connect(self.open_selected_rpd)
        self.open_release_folder_button = QPushButton("Open Release Folder")
        self.open_release_folder_button.clicked.connect(self.open_selected_release_folder)
        self.open_fabrication_folder_button = QPushButton("Open W Folder")
        self.open_fabrication_folder_button.clicked.connect(self.open_selected_fabrication_folder)
        self.open_spreadsheet_button = QPushButton("Open Spreadsheet")
        self.open_spreadsheet_button.clicked.connect(self.open_selected_spreadsheet)
        self.open_import_csv_button = QPushButton("Open Import CSV")
        self.open_import_csv_button.clicked.connect(self.open_selected_import_csv)
        self.open_print_packet_button = QPushButton("Open Print Packet")
        self.open_print_packet_button.clicked.connect(self.open_selected_print_packet)
        self.launch_kitter_button = QPushButton("Launch RADAN Kitter")
        self.launch_kitter_button.clicked.connect(self.launch_selected_kitter)
        self.launch_inventor_button = QPushButton("Run Inventor -> Radan -> Copy to L")
        self.launch_inventor_button.clicked.connect(self.run_selected_inventor_flow)
        self.toggle_selected_kits_hidden_button = QPushButton("Hide Selected Kits")
        self.toggle_selected_kits_hidden_button.clicked.connect(self.toggle_selected_kits_hidden)
        for button in (
            self.open_rpd_button,
            self.open_release_folder_button,
            self.open_fabrication_folder_button,
            self.open_spreadsheet_button,
            self.open_import_csv_button,
            self.open_print_packet_button,
            self.launch_kitter_button,
            self.launch_inventor_button,
            self.toggle_selected_kits_hidden_button,
        ):
            kit_row.addWidget(button)
        kit_row.addStretch(1)

        layout.addWidget(self.current_truck_label)
        layout.addLayout(truck_row)
        layout.addLayout(kit_row)
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
        self.kit_table.setHorizontalHeaderLabels(self.TABLE_COLUMNS)
        self.kit_table.setAlternatingRowColors(True)
        self.kit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.kit_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.kit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.kit_table.itemSelectionChanged.connect(self._on_kit_selection_changed)
        self.kit_table.verticalHeader().setVisible(False)
        header = self.kit_table.horizontalHeader()
        for column in range(len(self.TABLE_COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(self.TABLE_COLUMNS) - 1, QHeaderView.Stretch)

        layout.addLayout(controls)
        layout.addWidget(self.kit_table)
        return group

    def _build_detail_panel(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_preview_group())
        splitter.addWidget(self._build_punch_codes_group())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        return splitter

    def _build_preview_group(self) -> QWidget:
        group = QGroupBox("Nest Summary")
        layout = QVBoxLayout(group)
        self.pdf_preview = PdfPreviewPane(open_path_cb=self._open_path_with_message)
        layout.addWidget(self.pdf_preview)
        return group

    def _build_punch_codes_group(self) -> QWidget:
        group = QGroupBox("Punch Codes")
        layout = QVBoxLayout(group)

        self.punch_codes_kit_label = QLabel("Selected kit: (none)")
        self.punch_codes_kit_label.setStyleSheet("font-size: 14px; font-weight: 700;")

        helper = QLabel(
            "Store punch-code notes for the selected kit here. Notes are saved per kit."
        )
        helper.setWordWrap(True)

        self.punch_codes_edit = QPlainTextEdit()
        self.punch_codes_edit.setPlaceholderText(
            "Select a kit to view or edit its punch codes."
        )
        self.punch_codes_edit.setEnabled(False)
        self.punch_codes_edit.textChanged.connect(self._on_punch_codes_changed)

        controls = QHBoxLayout()
        self.punch_codes_status_label = QLabel("No kit selected")
        save_button = QPushButton("Save Kit Punch Codes")
        save_button.clicked.connect(self._save_punch_codes_only)
        controls.addWidget(self.punch_codes_status_label)
        controls.addStretch(1)
        controls.addWidget(save_button)

        layout.addWidget(self.punch_codes_kit_label)
        layout.addWidget(helper)
        layout.addWidget(self.punch_codes_edit, 1)
        layout.addLayout(controls)
        return group

    def _load_settings_into_form(self) -> None:
        self.release_root_edit.setText(self.settings.release_root)
        self.fabrication_root_edit.setText(self.settings.fabrication_root)
        self.rpd_template_edit.setText(self.settings.rpd_template_path)
        self.radan_kitter_edit.setText(self.settings.radan_kitter_launcher)
        self.inventor_entry_edit.setText(self.settings.inventor_to_radan_entry)
        self.create_support_folders_checkbox.setChecked(self.settings.create_support_folders)
        self._refresh_settings_summaries()
        self._load_punch_codes_for_status(None)

    def _refresh_settings_summaries(self) -> None:
        kit_count = len(configured_kit_mappings(self.settings))
        self.template_summary_label.setText(f"Kit mappings loaded: {kit_count}")
        rule_count = len(
            [
                line
                for line in self.settings.template_replacements_text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        )
        self.replacements_summary_label.setText(
            "Template replacement rules: "
            f"{rule_count} "
            "(placeholders: {truck_number}, {kit_name}, {project_name}, {rpd_stem})"
        )

    def _settings_from_form(self) -> ExplorerSettings:
        return ExplorerSettings(
            release_root=self.release_root_edit.text().strip(),
            fabrication_root=self.fabrication_root_edit.text().strip(),
            radan_kitter_launcher=self.radan_kitter_edit.text().strip(),
            inventor_to_radan_entry=self.inventor_entry_edit.text().strip(),
            rpd_template_path=self.rpd_template_edit.text().strip(),
            template_replacements_text=self.settings.template_replacements_text,
            punch_codes_text=self.settings.punch_codes_text,
            punch_codes_by_kit=self._current_punch_codes_map(),
            create_support_folders=self.create_support_folders_checkbox.isChecked(),
            kit_templates=list(self.settings.kit_templates),
            hidden_trucks=list(self.settings.hidden_trucks),
            hidden_kits=list(self.settings.hidden_kits),
        )

    def save_settings_from_form(self) -> None:
        self.settings = self._settings_from_form()
        save_path = save_settings(self.settings)
        self._refresh_settings_summaries()
        self.log(f"Saved settings to {save_path}")
        self.refresh_trucks()

    def edit_kit_templates(self) -> None:
        dialog = MultilineEditorDialog(
            title="Edit Kit Mappings",
            value="\n".join(self.settings.kit_templates),
            helper_text=(
                "Enter one L-side kit mapping per line.\n"
                "Use `DASHBOARD NAME | RADAN NAME` when you want a friendlier label in the UI.\n"
                "Add `=> W\\nested\\relative\\path` when the W-side folder is nested.\n"
                "Examples:\n"
                "BODY | PAINT PACK\n"
                "CONSOLE PACK => LASER\\CONSOLE\\PACK"
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings.kit_templates = normalize_kit_template_entries(dialog.value().splitlines())
        self.save_settings_from_form()

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
        current = self.current_truck_number()
        self.settings = self._settings_from_form()
        self._all_trucks = discover_trucks(self.settings)
        self._apply_truck_filter()
        if current and self._select_truck(current):
            return
        if self.truck_list.count():
            self.truck_list.setCurrentRow(0)
        else:
            self._set_current_statuses([])

    def _apply_truck_filter(self) -> None:
        wanted = self.search_edit.text().strip().casefold()
        current = self.current_truck_number()
        self.truck_list.clear()
        visible_trucks = filter_truck_numbers(
            self._all_trucks,
            self.settings,
            show_hidden=self.show_hidden_trucks_checkbox.isChecked(),
        )
        hidden_foreground = QColor("#6C757D")
        for truck_number in visible_trucks:
            if wanted and wanted not in truck_number.casefold():
                continue
            item = QListWidgetItem(truck_number)
            if is_hidden_truck(truck_number, self.settings):
                item.setForeground(hidden_foreground)
                item.setToolTip("Hidden truck")
            self.truck_list.addItem(item)
        if current and not self._select_truck(current) and self.truck_list.count():
            self.truck_list.setCurrentRow(0)
        self._refresh_hidden_action_labels()

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
        self._save_punch_codes_only()
        truck_number = self.current_truck_number()
        self.current_truck_label.setText(
            f"Selected Truck: {truck_number if truck_number else '(none)'}"
        )
        if not truck_number:
            self._set_current_statuses([])
            return
        self._set_current_statuses(collect_kit_statuses(truck_number, self.settings))

    def _set_current_statuses(self, statuses: list[KitStatus]) -> None:
        self._all_statuses = list(statuses)
        self._render_current_statuses()

    def _render_current_statuses(self) -> None:
        self._save_punch_codes_only()
        previous_kit_name = self._current_status().kit_name if self._current_status() is not None else ""
        visible_statuses = filter_kit_statuses(
            self._all_statuses,
            self.settings,
            show_hidden=self.show_hidden_kits_checkbox.isChecked(),
        )
        self._current_statuses = visible_statuses
        self.kit_table.setRowCount(len(visible_statuses))
        for row, status in enumerate(visible_statuses):
            self._populate_status_row(row, status)

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
        self._sync_detail_panels()
        self._refresh_hidden_action_labels()

    def _on_kit_selection_changed(self) -> None:
        self._save_punch_codes_only()
        self._sync_detail_panels()
        self._refresh_hidden_action_labels()

    def _sync_detail_panels(self) -> None:
        status = self._current_status()
        if status is None:
            self.pdf_preview.set_pdf_match(self._empty_preview_match())
            self._load_punch_codes_for_status(None)
            return
        self.pdf_preview.set_pdf_match(status.preview_pdf_match)
        self._load_punch_codes_for_status(status)

    def _empty_preview_match(self) -> PdfMatch:
        return PdfMatch(chosen_path=None, candidates=(), issue="no_selection")

    def _on_punch_codes_changed(self) -> None:
        if not self._active_punch_codes_kit_name:
            return
        self._punch_codes_dirty = True
        self._set_punch_codes_status("Saving...")
        self._punch_codes_save_timer.start(700)

    def _save_punch_codes_only(self) -> None:
        if not self._active_punch_codes_kit_name:
            return
        self._punch_codes_save_timer.stop()
        self.settings = self._settings_from_form()
        save_settings(self.settings)
        if self._punch_codes_loaded_from_legacy and not self._punch_codes_dirty:
            self._set_punch_codes_status("Using legacy shared note")
            return
        self._punch_codes_loaded_from_legacy = False
        self._punch_codes_dirty = False
        if self._active_punch_codes_kit_name in self.settings.punch_codes_by_kit:
            self._set_punch_codes_status(f"Saved {time.strftime('%H:%M:%S')}")
        else:
            self._set_punch_codes_status("No note for this kit")

    def _set_punch_codes_status(self, text: str) -> None:
        self.punch_codes_status_label.setText(text)

    def _current_punch_codes_map(self) -> dict[str, str]:
        punch_codes_by_kit = dict(self.settings.punch_codes_by_kit)
        active_key = self._active_punch_codes_kit_name.strip()
        if not active_key:
            return punch_codes_by_kit
        if self._punch_codes_loaded_from_legacy and not self._punch_codes_dirty:
            return punch_codes_by_kit

        text = self.punch_codes_edit.toPlainText()
        if text.strip():
            punch_codes_by_kit[active_key] = text
        else:
            punch_codes_by_kit.pop(active_key, None)
        return punch_codes_by_kit

    def _load_punch_codes_for_status(self, status: KitStatus | None) -> None:
        self._punch_codes_save_timer.stop()
        self.punch_codes_edit.blockSignals(True)
        try:
            if status is None:
                self._active_punch_codes_kit_name = ""
                self._punch_codes_dirty = False
                self._punch_codes_loaded_from_legacy = False
                self.punch_codes_kit_label.setText("Selected kit: (none)")
                self.punch_codes_edit.clear()
                self.punch_codes_edit.setEnabled(False)
                self.punch_codes_edit.setPlaceholderText("Select a kit to view or edit its punch codes.")
                self._set_punch_codes_status("No kit selected")
                return

            display_name = status.paths.display_name
            kit_name = status.paths.kit_name
            label_text = f"Selected kit: {display_name}"
            if display_name.casefold() != kit_name.casefold():
                label_text += f" (RADAN: {kit_name})"
            self.punch_codes_kit_label.setText(label_text)
            self.punch_codes_edit.setEnabled(True)
            self.punch_codes_edit.setPlaceholderText(
                f"Example for {display_name}:\nP01 = Roof vent pattern\nP02 = Pump panel slots\n..."
            )

            note_text = self.settings.punch_codes_by_kit.get(kit_name, "")
            using_legacy = False
            if not note_text and self.settings.punch_codes_text.strip():
                note_text = self.settings.punch_codes_text
                using_legacy = True

            self._active_punch_codes_kit_name = kit_name
            self._punch_codes_dirty = False
            self._punch_codes_loaded_from_legacy = using_legacy
            self.punch_codes_edit.setPlainText(note_text)
            if using_legacy:
                self._set_punch_codes_status("Using legacy shared note")
            elif note_text.strip():
                self._set_punch_codes_status("Saved")
            else:
                self._set_punch_codes_status("No note for this kit")
        finally:
            self.punch_codes_edit.blockSignals(False)

    def _make_item(
        self,
        text: str,
        *,
        background: QColor | None = None,
        foreground: QColor | None = None,
    ) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if background is not None:
            item.setBackground(background)
        if foreground is not None:
            item.setForeground(foreground)
        return item

    def _populate_status_row(self, row: int, status: KitStatus) -> None:
        green = QColor("#D8F3DC")
        yellow = QColor("#FFF3BF")
        red = QColor("#F8D7DA")
        muted = QColor("#6C757D")
        hidden = is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
        hidden_foreground = muted if hidden else None

        spreadsheet_text = "(missing)"
        spreadsheet_color = red
        if status.spreadsheet_match.is_unique and status.spreadsheet_match.chosen_path is not None:
            spreadsheet_text = status.spreadsheet_match.chosen_path.name
            spreadsheet_color = green
        elif status.spreadsheet_match.candidates:
            spreadsheet_text = ", ".join(path.name for path in status.spreadsheet_match.candidates)
            spreadsheet_color = yellow

        import_csv_text = "(not generated)"
        import_csv_color = red
        if (
            status.inventor_outputs is not None
            and status.inventor_outputs.target_csv_path is not None
            and status.inventor_outputs.target_csv_path.exists()
        ):
            import_csv_text = status.inventor_outputs.target_csv_path.name
            import_csv_color = green
        elif status.inventor_outputs is not None and status.inventor_outputs.target_csv_path is not None:
            import_csv_text = status.inventor_outputs.target_csv_path.name
            import_csv_color = yellow

        items = (
            self._make_item(
                f"{status.paths.display_name} [hidden]" if hidden else status.paths.display_name,
                foreground=hidden_foreground,
            ),
            self._make_item(
                "Yes" if status.release_folder_exists else "Missing",
                background=green if status.release_folder_exists else red,
                foreground=hidden_foreground,
            ),
            self._make_item(
                "Yes" if status.project_folder_exists else "Missing",
                background=green if status.project_folder_exists else red,
                foreground=hidden_foreground,
            ),
            self._make_item(
                f"{status.paths.rpd_path.name} ({status.rpd_size_bytes} B)"
                if status.rpd_exists and status.paths.rpd_path is not None
                else "(missing)",
                background=green if status.rpd_exists else red,
                foreground=hidden_foreground,
            ),
            self._make_item(
                status.paths.fabrication_relative_path + (" (found)" if status.fabrication_folder_exists else " (missing)"),
                background=green if status.fabrication_folder_exists else red,
                foreground=hidden_foreground,
            ),
            self._make_item(spreadsheet_text, background=spreadsheet_color, foreground=hidden_foreground),
            self._make_item(import_csv_text, background=import_csv_color, foreground=hidden_foreground),
            self._make_item(
                f"{status.status_summary} | Hidden" if hidden else status.status_summary,
                foreground=hidden_foreground,
            ),
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
        self.settings = self._settings_from_form()

    def _refresh_hidden_action_labels(self) -> None:
        truck_number = self.current_truck_number()
        truck_hidden = bool(truck_number and is_hidden_truck(truck_number, self.settings))
        self.toggle_truck_hidden_button.setEnabled(bool(truck_number))
        self.toggle_truck_hidden_button.setText("Unhide Truck" if truck_hidden else "Hide Truck")

        selected_statuses = self._selected_statuses()
        selected_hidden = bool(selected_statuses) and all(
            is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
            for status in selected_statuses
        )
        self.toggle_selected_kits_hidden_button.setEnabled(bool(selected_statuses))
        self.toggle_selected_kits_hidden_button.setText(
            "Unhide Selected Kits" if selected_hidden else "Hide Selected Kits"
        )

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
        if hidden or self.show_hidden_trucks_checkbox.isChecked():
            self._select_truck(truck_number)
        self.log(f"{action_text} truck {truck_number} from the explorer list.")

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

        truck_text = truck_number.strip()
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

        self.refresh_trucks()
        self._select_truck(truck_text)
        if errors:
            QMessageBox.warning(
                self,
                "Truck scaffold completed with warnings",
                "\n".join(errors),
            )
        self.log(
            f"Created L-side truck scaffold for {truck_text} using W folder {fabrication_truck_dir}. "
            f"Paths touched: {created_count}"
        )

    def create_missing_kits_for_selected_truck(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Create Missing Kits", "Select a truck first.")
            return
        self._ensure_saved_settings()
        created = 0
        for status in self._current_statuses:
            result = create_kit_scaffold(truck_number, status.kit_name, self.settings)
            created += len(result.created_paths)
        self._set_current_statuses(collect_kit_statuses(truck_number, self.settings))
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
        for status in selected:
            result = create_kit_scaffold(truck_number, status.kit_name, self.settings)
            created += len(result.created_paths)
            notes.extend(result.notes)
        self._set_current_statuses(collect_kit_statuses(truck_number, self.settings))
        if notes:
            self.log(" | ".join(notes))
        self.log(f"Ensured {len(selected)} selected kit scaffold(s). Paths touched: {created}")

    def open_selected_truck_release(self) -> None:
        truck_number = self.current_truck_number()
        if not truck_number:
            QMessageBox.information(self, "Open Truck Release", "Select a truck first.")
            return
        release_root = Path(self.release_root_edit.text().strip()) if self.release_root_edit.text().strip() else None
        if release_root is None:
            QMessageBox.warning(self, "Open Truck Release", "Release root is not configured.")
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
        if status is None or status.paths.rpd_path is None:
            QMessageBox.information(self, "Open RPD", "Select a kit first.")
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

    def open_selected_import_csv(self) -> None:
        status = self._current_status()
        if status is None or status.inventor_outputs is None or status.inventor_outputs.target_csv_path is None:
            QMessageBox.information(self, "Open Import CSV", "Select a kit with an inventor output target.")
            return
        self._open_path_with_message(status.inventor_outputs.target_csv_path)

    def open_selected_print_packet(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Open Print Packet", "Select a kit first.")
            return

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

    def launch_selected_kitter(self) -> None:
        status = self._current_status()
        if status is None or status.paths.rpd_path is None:
            QMessageBox.information(self, "Launch RADAN Kitter", "Select a kit first.")
            return
        launcher_text = self.radan_kitter_edit.text().strip()
        if not launcher_text:
            QMessageBox.warning(self, "Launch RADAN Kitter", "RADAN Kitter launcher is not configured.")
            return
        try:
            launch_launcher(Path(launcher_text), status.paths.rpd_path)
        except Exception as exc:
            QMessageBox.critical(self, "Launch RADAN Kitter", str(exc))
            return
        self.log(f"Launched RADAN Kitter on {status.paths.rpd_path}")

    def run_selected_inventor_flow(self) -> None:
        status = self._current_status()
        if status is None:
            QMessageBox.information(self, "Run Inventor -> Radan", "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                self,
                "Run Inventor -> Radan",
                "This kit does not have exactly one spreadsheet candidate in the W folder.",
            )
            return
        if status.paths.project_dir is None:
            QMessageBox.warning(
                self,
                "Run Inventor -> Radan",
                "The L-side project folder is not available for this kit.",
            )
            return

        self._ensure_saved_settings()
        entry_text = self.inventor_entry_edit.text().strip()
        if not entry_text:
            QMessageBox.warning(self, "Run Inventor -> Radan", "Inventor entry is not configured.")
            return
        entry_path = Path(entry_text)
        try:
            completed = run_inventor_to_radan(entry_path, spreadsheet_path)
        except Exception as exc:
            QMessageBox.critical(self, "Run Inventor -> Radan", str(exc))
            return

        copied_paths: tuple[Path, ...] = ()
        copy_error = ""
        try:
            _outputs, copied_paths = copy_inventor_outputs_to_project(
                spreadsheet_path,
                status.paths.project_dir,
            )
        except Exception as exc:
            copy_error = str(exc)

        self._set_current_statuses(collect_kit_statuses(self.current_truck_number(), self.settings))
        self.log(
            f"Inventor tool finished for {spreadsheet_path} with return code {completed.returncode}."
        )
        if copied_paths:
            self.log("Copied inventor outputs to L: " + ", ".join(str(path) for path in copied_paths))

        message_parts = [f"Return code: {completed.returncode}"]
        if copied_paths:
            message_parts.append("Copied to L:")
            message_parts.extend(str(path) for path in copied_paths)
        if copy_error:
            message_parts.append(f"Copy step: {copy_error}")
        if completed.stderr.strip():
            message_parts.append("stderr:")
            message_parts.append(completed.stderr.strip())

        if completed.returncode not in (0, 2):
            QMessageBox.warning(self, "Run Inventor -> Radan", "\n".join(message_parts))
            return
        if copy_error and not copied_paths:
            QMessageBox.warning(self, "Run Inventor -> Radan", "\n".join(message_parts))
            return
        QMessageBox.information(self, "Run Inventor -> Radan", "\n".join(message_parts))

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
