from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
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
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from background_job import BackgroundJobWorker  # noqa: F401 - re-exported; asserted identical by tests/test_services.py
from controllers.block_transfer_controller import BlockTransferController
from controllers.full_flow_controller import FullFlowController
from controllers.hot_reload_controller import HotReloadController
from controllers.inventor_controller import InventorController
from controllers.packet_build_controller import PacketBuildController
from controllers.radan_import_controller import RadanImportController
from controllers.truck_ordering_controller import TruckOrderingController
from flow_bridge import (
    FlowTruckInsight,
    empty_flow_truck_insight,
    flow_kit_insight_for_explorer_kit,
    flow_probe_cache_token,
    invalidate_flow_insight_cache,
    load_cached_flow_truck_insight,
    map_explorer_kit_to_flow_kit,
    normalize_flow_insight_for_local_release,
)
from models import (
    canonicalize_client_numbers_by_truck,
    canonicalize_hidden_kit_entries,
    ExplorerSettings,
    KitStatus,
    build_hidden_kit_key,
    materialize_legacy_punch_codes_for_kit,
    normalize_hidden_truck_entries,
    normalize_hidden_truck_number,
    normalize_truck_order_entries,
    resolve_punch_code_text,
)
from performance_metrics import BoundedTTLCache, GLOBAL_METRICS, settings_cache_signature
from services import (
    FILE_METADATA_CACHE,
    clear_performance_caches,
    collect_kit_statuses,
    create_kit_scaffold,
    detect_assembly_packet_pdf,
    detect_cut_list_packet_pdf,
    detect_print_packet_pdf,
    discover_trucks,
    fabrication_kit_dir_ready,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    invalidate_filesystem_cache_for_path,
    invalidate_status_cache_for_truck,
    inventor_output_paths,
    is_hidden_kit,
    is_hidden_truck,
    is_standard_truck_number,
    launch_launcher,
    open_path,
    release_root_for_job,
    release_text_for_status,
    restore_truck_visibility,
    scaffold_kit_names_for_truck,
    sort_truck_numbers_by_fabrication_order,
)
from settings_store import load_settings, save_settings
from ui.main_window_styles import dashboard_stylesheet


@dataclass
class TruckSwitchRunContext:
    truck_number: str
    run_id: int
    loading: bool = True
    status_future: Future[list[KitStatus]] | None = None
    flow_future: Future[FlowTruckInsight] | None = None
    status_done: bool = False
    flow_done: bool = False
    error: str = ""
    completed: bool = False


class MainWindow(QMainWindow):
    _status_future_ready = Signal()
    _flow_future_ready = Signal()

    FLOW_GANTT_HEIGHT = 176
    ASSEMBLY_PACKET_BUILD_ENABLED = True
    CUT_LIST_BUILD_ENABLED = True
    ASSEMBLY_PACKET_DISABLED_REASON = (
        "Assembly packet generation is paused because it is known to hang and is not production-ready yet."
    )
    CUT_LIST_DISABLED_REASON = "Cut list packet generation is paused until the flow has been tested."
    EXTERNAL_STATUS_REFRESH_INTERVAL_MS = 30000
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
    RELEASE_COLUMN = 6
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
        self._hot_reload_controller = HotReloadController(self, self._runtime_dir)
        self._updating_kit_table = False
        self._kit_table_render_signature: tuple[object, ...] | None = None
        self._current_flow_truck_insight: FlowTruckInsight = empty_flow_truck_insight()
        self._status_cache_by_truck: BoundedTTLCache[list[KitStatus]] = BoundedTTLCache(
            "ui_status",
            max_size=128,
            positive_ttl_seconds=30.0,
            negative_ttl_seconds=2.0,
        )
        self._flow_cache_by_truck: BoundedTTLCache[tuple[str, FlowTruckInsight]] = BoundedTTLCache(
            "ui_flow",
            max_size=128,
            positive_ttl_seconds=3600.0,
            negative_ttl_seconds=600.0,
        )
        self._flow_gantt_source_bytes: bytes | None = None
        self._flow_gantt_source_pixmap: QPixmap | None = None
        self._kitter_refresh_truck_number = ""
        self._kitter_refresh_remaining = 0
        self._pending_truck_selection = ""
        self._prewarm_truck_keys: set[str] = set()
        self._truck_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_truck_future: Future[list[str]] | None = None
        self._truck_request_serial = 0
        self._pending_truck_request_serial = 0
        self._status_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_status_by_truck: dict[str, tuple[str, str, int, Future[list[KitStatus]]]] = {}
        self._flow_executor = ThreadPoolExecutor(max_workers=1)
        self._pending_flow_by_truck: dict[str, tuple[str, str, int, Future[FlowTruckInsight]]] = {}
        self._truck_switch_run_id = 0
        self._active_truck_switch: TruckSwitchRunContext | None = None
        self._truck_watch_timer = QTimer(self)
        self._truck_watch_timer.setInterval(120)
        self._truck_watch_timer.timeout.connect(self._poll_pending_truck_future)
        self._status_watch_timer = QTimer(self)
        self._status_watch_timer.setInterval(120)
        self._status_watch_timer.timeout.connect(self._poll_pending_status_future)
        self._flow_watch_timer = QTimer(self)
        self._flow_watch_timer.setInterval(120)
        self._flow_watch_timer.timeout.connect(self._poll_pending_flow_future)
        # Phase 6: the 120ms timers above remain as a correctness safety net (Phase 5
        # measured them as the dominant source of perceived cold-switch latency versus
        # the ~5ms of actual work they wait on). These signals let a background future
        # trigger the same poll immediately on completion instead of waiting for the
        # next tick; queued cross-thread emission is what makes it Qt-thread-safe.
        self._status_future_ready.connect(self._poll_pending_status_future)
        self._flow_future_ready.connect(self._poll_pending_flow_future)
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
        self.truck_ordering_controller = TruckOrderingController(self)
        self.inventor_controller = InventorController(self)
        full_flow_lock_widgets: list[QWidget] = []
        for widget_name in (
            "full_flow_button",
            "new_truck_button",
            "move_truck_up_button",
            "move_truck_down_button",
            "create_missing_button",
            "create_selected_button",
            "edit_truck_client_button",
            "toggle_truck_hidden_button",
            "toggle_selected_kits_hidden_button",
            "build_print_packet_button",
            "build_assembly_packet_button",
            "build_cut_list_button",
            "launch_kitter_button",
            "send_blocks_button",
            "launch_inventor_button",
            "import_csv_button",
        ):
            widget = getattr(self, widget_name, None)
            if isinstance(widget, QWidget):
                full_flow_lock_widgets.append(widget)
        self.full_flow_controller = FullFlowController(
            self,
            mutating_widgets=tuple(full_flow_lock_widgets),
            editable_table=self.kit_table,
        )
        self.block_transfer_controller = BlockTransferController(
            self,
            send_blocks_button=self.send_blocks_button,
        )
        self.radan_import_controller = RadanImportController(self)
        self.packet_build_controller = PacketBuildController(
            self,
            print_button=self.build_print_packet_button,
            assembly_button=self.build_assembly_packet_button,
            cut_list_button=self.build_cut_list_button,
        )
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
            self._hot_reload_controller.build_banner(root_layout)

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
        self.setStyleSheet(dashboard_stylesheet())

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
        self.show_hidden_trucks_button.setMinimumWidth(
            self.show_hidden_trucks_button.fontMetrics().horizontalAdvance("Show Hidden (000)") + 24
        )
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

        layout.addWidget(self._build_actions_group())
        layout.addWidget(self._build_table_group(), 1)
        return box

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

        full_flow_row = QHBoxLayout()
        self.full_flow_button = QPushButton("Run Full Flow")
        self.full_flow_button.setToolTip(
            "Run Inventor, import the RADAN CSV, assign RF kits, build packets, optionally nest headless, then open RADAN."
        )
        full_flow_font = self.full_flow_button.font()
        point_size = full_flow_font.pointSize()
        full_flow_font.setPointSize(max(16, point_size * 2 if point_size > 0 else 16))
        full_flow_font.setBold(True)
        self.full_flow_button.setFont(full_flow_font)
        self.full_flow_button.setMinimumHeight(64)
        self.full_flow_button.setMinimumWidth(320)
        self.full_flow_button.clicked.connect(self.run_selected_full_flow)
        full_flow_row.addWidget(self.full_flow_button)
        full_flow_row.addStretch(1)

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
        truck_row.addWidget(self.edit_truck_client_button)
        truck_row.addWidget(self.toggle_truck_hidden_button)
        truck_row.addStretch(1)

        kit_row = QHBoxLayout()
        production_row = QHBoxLayout()
        self.open_release_folder_button = QPushButton("Open L Kit")
        self.open_release_folder_button.setToolTip("Open the selected kit folder on L.")
        self.open_release_folder_button.clicked.connect(self.open_selected_release_folder)
        self.open_fabrication_folder_button = QPushButton("Open W Kit")
        self.open_fabrication_folder_button.setToolTip("Open the selected kit source folder on W.")
        self.open_fabrication_folder_button.clicked.connect(self.open_selected_fabrication_folder)
        self.open_spreadsheet_button = QPushButton("BOM")
        self.open_spreadsheet_button.setToolTip("Open the single spreadsheet found for the selected kit on W.")
        self.open_spreadsheet_button.clicked.connect(self.open_selected_spreadsheet)
        self.build_print_packet_button = QPushButton("Build Print Packet")
        self.build_print_packet_button.setToolTip(
            "Build the QTY print packet from the selected kit's saved RPD."
        )
        self.build_print_packet_button.clicked.connect(self.build_selected_print_packet)
        self.build_assembly_packet_button = QPushButton("Build Assembly Packet")
        self.build_assembly_packet_button.setToolTip(
            "Build the .iam-backed assembly drawing packet from the selected kit's saved RPD."
        )
        self.build_assembly_packet_button.clicked.connect(self.build_selected_assembly_packet)
        self.build_cut_list_button = QPushButton("Build Cut List")
        self.build_cut_list_button.setToolTip(
            "Build the non-laser cut list packet from the selected kit's saved RPD."
        )
        self.build_cut_list_button.clicked.connect(self.build_selected_cut_list_packet)
        self.launch_kitter_button = QPushButton("Run Kitter")
        self.launch_kitter_button.setToolTip("Launch RADAN Kitter on the selected project file.")
        self.launch_kitter_button.clicked.connect(self.launch_selected_kitter)
        self.send_blocks_button = QPushButton("Send Blocks")
        self.send_blocks_button.setToolTip(
            "Copy this project's block files to the machine folder and L-side kit folder, then delete the source after checksum verification."
        )
        self.send_blocks_button.clicked.connect(self.send_selected_block_files_to_machine)
        self.launch_inventor_button = QPushButton("Run Inventor Tool")
        self.launch_inventor_button.setToolTip(
            "Run the Inventor-to-RADAN launcher on the selected spreadsheet, then move the generated output into the matching L project folder."
        )
        self.launch_inventor_button.clicked.connect(self.run_selected_inventor_flow)
        self.import_csv_button = QPushButton("Import BOM")
        self.import_csv_button.setToolTip(
            "Import the selected kit's generated _Radan.csv into the matching RADAN project."
        )
        self.import_csv_button.clicked.connect(lambda _checked=False: self.import_selected_csv_to_radan())
        self.toggle_selected_kits_hidden_button = QPushButton("Hide Selected Kits")
        self.toggle_selected_kits_hidden_button.setToolTip(
            "Hide or unhide the selected kits in the explorer without deleting anything."
        )
        self.toggle_selected_kits_hidden_button.clicked.connect(self.toggle_selected_kits_hidden)
        for button in (
            self.open_release_folder_button,
            self.open_fabrication_folder_button,
            self.open_spreadsheet_button,
            self.build_print_packet_button,
            self.build_assembly_packet_button,
            self.build_cut_list_button,
        ):
            kit_row.addWidget(button)
        kit_row.addStretch(1)

        for button in (
            self.launch_kitter_button,
            self.send_blocks_button,
            self.launch_inventor_button,
            self.toggle_selected_kits_hidden_button,
        ):
            production_row.addWidget(button)
        production_row.addStretch(1)

        radan_row = QHBoxLayout()
        radan_row.addWidget(self.import_csv_button)
        radan_row.addStretch(1)

        layout.addWidget(self.current_truck_label)
        layout.addWidget(self.current_flow_label)
        layout.addLayout(full_flow_row)
        layout.addWidget(self.flow_gantt_scroll)
        layout.addWidget(actions_helper_label)
        layout.addLayout(truck_row)
        layout.addLayout(kit_row)
        layout.addLayout(production_row)
        layout.addLayout(radan_row)
        return group

    def _build_table_group(self) -> QWidget:
        group = QGroupBox("Kit Explorer")
        layout = QVBoxLayout(group)

        controls = QHBoxLayout()
        self.show_hidden_kits_checkbox = QCheckBox("Show hidden kits")
        self.show_hidden_kits_checkbox.toggled.connect(self._on_show_hidden_kits_toggled)
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
        self.kit_table.itemClicked.connect(self._on_kit_table_item_activated)
        self.kit_table.itemChanged.connect(self._on_kit_table_item_changed)
        self.kit_table.itemDoubleClicked.connect(self._on_kit_table_item_activated)
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

    def _print_packet_match_for_status(self, status: KitStatus):
        return detect_print_packet_pdf(status.paths, fs_cache=FILE_METADATA_CACHE)

    def _assembly_packet_match_for_status(self, status: KitStatus):
        return detect_assembly_packet_pdf(status.paths, fs_cache=FILE_METADATA_CACHE)

    def _cut_list_match_for_status(self, status: KitStatus):
        return detect_cut_list_packet_pdf(status.paths, fs_cache=FILE_METADATA_CACHE)

    def _recommended_action_for_status(self, status: KitStatus) -> str:
        print_packet_match = self._print_packet_match_for_status(status)
        assembly_packet_match = self._assembly_packet_match_for_status(status)
        cut_list_match = self._cut_list_match_for_status(status)
        fab_kit_dir_ready = fabrication_kit_dir_ready(status.paths.fabrication_kit_dir)
        if not status.project_folder_exists or not status.rpd_exists:
            return "Repair Selected: the L-side project setup is incomplete."
        if status.spreadsheet_match.issue == "multiple_spreadsheets":
            return "BOM: clean up multiple BOM matches in W before running tools."
        if status.spreadsheet_match.chosen_path is not None and not status.fabrication_has_files:
            if self.settings.inventor_to_radan_entry.strip():
                return "Run Inventor Tool: the kit is not released yet."
            return "BOM: Inventor launcher is not configured."
        if status.preview_pdf_match.chosen_path is None:
            return "Open Project: review the kit because the Nest Summary is still missing."
        if status.rpd_exists and fab_kit_dir_ready and print_packet_match.chosen_path is None:
            return "Build Print Packet: generate the QTY packet from Explorer."
        if (
            self.ASSEMBLY_PACKET_BUILD_ENABLED
            and status.rpd_exists
            and fab_kit_dir_ready
            and assembly_packet_match.chosen_path is None
        ):
            return "Build Assembly Packet: generate the .iam-backed assembly drawing packet from Explorer."
        if (
            self.CUT_LIST_BUILD_ENABLED
            and status.rpd_exists
            and fab_kit_dir_ready
            and cut_list_match.chosen_path is None
        ):
            return "Build Cut List: generate the non-laser first-token PDF packet from Explorer."
        if (
            status.rpd_exists
            and self.settings.radan_kitter_launcher.strip()
            and fab_kit_dir_ready
            and (assembly_packet_match.chosen_path is None or cut_list_match.chosen_path is None)
        ):
            return "Run Kitter: packet side-flows are paused in Explorer; use Kitter for assembly packets for now."
        if status.rpd_exists and self.settings.radan_kitter_launcher.strip():
            return "Run Kitter: the project file is ready."
        return "Open Project: the kit is mostly ready and worth a quick review."

    def _available_actions_for_status(self, status: KitStatus) -> str:
        packet_match = self._print_packet_match_for_status(status)
        assembly_packet_match = self._assembly_packet_match_for_status(status)
        cut_list_match = self._cut_list_match_for_status(status)
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
        if status.preview_pdf_match.chosen_path is not None:
            actions.append("Open Nest Summary")
        if packet_match.chosen_path is not None:
            actions.append("Open Print Packet")
        if assembly_packet_match.chosen_path is not None:
            actions.append("Open Assembly Packet")
        if cut_list_match.chosen_path is not None:
            actions.append("Open Cut List")
        if status.rpd_exists and fabrication_kit_dir_ready(status.paths.fabrication_kit_dir):
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
            odd_jobs_by_truck={truck: list(jobs) for truck, jobs in self.settings.odd_jobs_by_truck.items()},
            create_support_folders=self.settings.create_support_folders,
            kit_templates=list(self.settings.kit_templates),
            truck_order=list(self.settings.truck_order),
            hidden_trucks=list(self.settings.hidden_trucks),
            hidden_kits=list(self.settings.hidden_kits),
        )

    def _settings_signature(self) -> str:
        return settings_cache_signature(self.settings)

    def _status_cache_key(self, truck_number: str) -> tuple[str, str, str]:
        return ("ui_status", self._settings_signature(), str(truck_number or "").strip().casefold())

    def _flow_cache_key(self, truck_number: str, cache_token: str) -> tuple[str, str, str]:
        return ("ui_flow", str(truck_number or "").strip().casefold(), str(cache_token or ""))

    def _hidden_flow_kit_names_for_truck(self, truck_number: str) -> tuple[str, ...]:
        if bool(self.show_hidden_kits_checkbox.isChecked()):
            return ()
        truck_key = normalize_hidden_truck_number(truck_number).casefold()
        if not truck_key:
            return ()

        hidden_flow_names: list[str] = []
        seen: set[str] = set()
        for value in canonicalize_hidden_kit_entries(self.settings.hidden_kits, self.settings.kit_templates):
            if "::" not in value:
                continue
            hidden_truck, kit_name = value.split("::", 1)
            if normalize_hidden_truck_number(hidden_truck).casefold() != truck_key:
                continue
            flow_name = map_explorer_kit_to_flow_kit(kit_name)
            flow_key = flow_name.casefold()
            if not flow_name or flow_key in seen:
                continue
            seen.add(flow_key)
            hidden_flow_names.append(flow_name)
        return tuple(sorted(hidden_flow_names, key=lambda item: item.casefold()))

    def _flow_request_token(self, truck_number: str) -> str:
        token = flow_probe_cache_token()
        hidden_flow_names = self._hidden_flow_kit_names_for_truck(truck_number)
        if not hidden_flow_names:
            return token
        hidden_signature = ",".join(name.casefold() for name in hidden_flow_names)
        return f"{token}|hidden_gantt:{hidden_signature}"

    def _invalidate_status_for_truck(self, truck_number: str) -> None:
        truck_key = str(truck_number or "").strip().casefold()
        if not truck_key:
            return
        self._prewarm_truck_keys.discard(truck_key)
        self._status_cache_by_truck.invalidate_where(
            lambda key, _value: isinstance(key, tuple)
            and len(key) == 3
            and key[0] == "ui_status"
            and str(key[2]).casefold() == truck_key
        )
        invalidate_status_cache_for_truck(truck_number)
        for status in self._all_statuses:
            if status.paths.truck_number.casefold() != truck_key:
                continue
            for path in (
                status.paths.release_truck_dir,
                status.paths.release_kit_dir,
                status.paths.project_dir,
                status.paths.rpd_path,
                status.paths.fabrication_truck_dir,
                status.paths.fabrication_kit_dir,
            ):
                invalidate_filesystem_cache_for_path(path)

    def _start_truck_switch_run(self, truck_number: str) -> TruckSwitchRunContext:
        self._truck_switch_run_id += 1
        context = TruckSwitchRunContext(truck_number=str(truck_number or "").strip(), run_id=self._truck_switch_run_id)
        self._active_truck_switch = context
        GLOBAL_METRICS.record_truck_switch_started()
        return context

    def _is_active_truck_switch(self, run_id: int, truck_number: str) -> bool:
        context = self._active_truck_switch
        return (
            context is not None
            and context.run_id == run_id
            and context.truck_number.casefold() == str(truck_number or "").strip().casefold()
            and context.truck_number.casefold() == self.current_truck_number().casefold()
        )

    def _displayed_statuses_match_truck(self, truck_number: str) -> bool:
        truck_key = str(truck_number or "").strip().casefold()
        return bool(
            truck_key
            and self._all_statuses
            and all(status.paths.truck_number.casefold() == truck_key for status in self._all_statuses)
        )

    def _mark_status_done(self, run_id: int, truck_number: str) -> None:
        context = self._active_truck_switch
        if context is None or context.run_id != run_id or context.truck_number.casefold() != truck_number.casefold():
            return
        context.status_done = True
        self._maybe_complete_truck_switch(context)

    def _mark_flow_done(self, run_id: int, truck_number: str) -> None:
        context = self._active_truck_switch
        if context is None or context.run_id != run_id or context.truck_number.casefold() != truck_number.casefold():
            return
        context.flow_done = True
        self._maybe_complete_truck_switch(context)

    def _maybe_complete_truck_switch(self, context: TruckSwitchRunContext) -> None:
        if context.completed or not context.status_done or not context.flow_done:
            return
        context.loading = False
        context.completed = True
        GLOBAL_METRICS.record_truck_switch_completed()

    def refresh_trucks(self) -> None:
        self._status_cache_by_truck.clear()
        self._flow_cache_by_truck.clear()
        clear_performance_caches()
        invalidate_flow_insight_cache()
        self._pending_status_by_truck.clear()
        self._pending_flow_by_truck.clear()
        self._prewarm_truck_keys.clear()
        self._active_truck_switch = None
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
        else:
            self._prewarm_visible_truck_caches()

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
            self._active_truck_switch = None
            return
        context = self._start_truck_switch_run(truck_number)
        context.flow_done = self._load_flow_for_truck(truck_number, run_id=context.run_id)

        status_cache_key = self._status_cache_key(truck_number)
        hit, cached_statuses = self._status_cache_by_truck.get(status_cache_key)
        if hit and cached_statuses is not None:
            if self._displayed_statuses_match_truck(truck_number) and list(cached_statuses) == self._all_statuses:
                context.status_done = True
                if not context.flow_done:
                    self._refresh_flow_dependent_status_cells()
                else:
                    self._refresh_hidden_action_labels()
                self._maybe_complete_truck_switch(context)
                return
            self._set_current_statuses(list(cached_statuses), cache=False)
            context.status_done = True
        else:
            self._set_current_statuses([], cache=False)
            pending_status = self._pending_status_by_truck.get(truck_key)
            settings_signature = self._settings_signature()
            if pending_status is not None:
                pending_truck_number, pending_signature, _pending_run_id, future = pending_status
                if pending_signature == settings_signature:
                    self._pending_status_by_truck[truck_key] = (
                        pending_truck_number,
                        pending_signature,
                        context.run_id,
                        future,
                    )
                    context.status_future = future
                else:
                    self._pending_status_by_truck.pop(truck_key, None)
                    pending_status = None
            if pending_status is None:
                self.log(f"Loading kit statuses for {truck_number}...")
                future = self._status_executor.submit(collect_kit_statuses, truck_number, self.settings)
                future.add_done_callback(self._notify_status_future_ready)
                self._pending_status_by_truck[truck_key] = (
                    truck_number,
                    settings_signature,
                    context.run_id,
                    future,
                )
                context.status_future = future
                self._status_watch_timer.start()
        self._maybe_complete_truck_switch(context)

    def _loading_flow_insight(self, truck_number: str) -> FlowTruckInsight:
        return FlowTruckInsight(
            available=False,
            truck_number=truck_number,
            summary_text="Flow: loading...",
            issue="loading",
            tooltip_text="Loading scheduling insights from the fabrication flow dashboard.",
        )

    def _one_off_flow_insight(self, truck_number: str) -> FlowTruckInsight:
        return FlowTruckInsight(
            available=False,
            truck_number=truck_number,
            summary_text="Flow: one-off job.",
            issue="one_off_job",
            tooltip_text="One-off jobs are not checked against the fabrication flow dashboard.",
        )

    def _should_probe_flow_for_truck(self, truck_number: str) -> bool:
        return is_standard_truck_number(truck_number)

    def _load_flow_for_truck(self, truck_number: str, *, run_id: int) -> bool:
        truck_key = truck_number.casefold()
        current_flow_token = self._flow_request_token(truck_number)
        if not self._should_probe_flow_for_truck(truck_number):
            insight = self._one_off_flow_insight(truck_number)
            self._flow_cache_by_truck.set(
                self._flow_cache_key(truck_number, current_flow_token),
                (current_flow_token, insight),
                negative=False,
            )
            self._current_flow_truck_insight = insight
            self._refresh_current_truck_heading()
            return True
        hidden_flow_kit_names = self._hidden_flow_kit_names_for_truck(truck_number)
        flow_cache_key = self._flow_cache_key(truck_number, current_flow_token)
        hit, cached_flow = self._flow_cache_by_truck.get(flow_cache_key)
        if hit and cached_flow is not None:
            cached_token, cached_insight = cached_flow
            if cached_token == current_flow_token:
                self._current_flow_truck_insight = cached_insight
                self._refresh_current_truck_heading()
                return True
            self._flow_cache_by_truck.invalidate(flow_cache_key)

        pending_flow = self._pending_flow_by_truck.get(truck_key)
        if pending_flow is not None:
            pending_truck_number, pending_token, _pending_run_id, future = pending_flow
            if pending_token == current_flow_token:
                self._pending_flow_by_truck[truck_key] = (
                    pending_truck_number,
                    pending_token,
                    run_id,
                    future,
                )
                context = self._active_truck_switch
                if context is not None and context.run_id == run_id:
                    context.flow_future = future
                self._current_flow_truck_insight = self._loading_flow_insight(truck_number)
                self._refresh_current_truck_heading()
                return False
            self._pending_flow_by_truck.pop(truck_key, None)

        self._current_flow_truck_insight = self._loading_flow_insight(truck_number)
        self._refresh_current_truck_heading()
        future = self._flow_executor.submit(
            load_cached_flow_truck_insight,
            truck_number,
            hidden_flow_kit_names=hidden_flow_kit_names,
        )
        future.add_done_callback(self._notify_flow_future_ready)
        self._pending_flow_by_truck[truck_key] = (
            truck_number,
            current_flow_token,
            run_id,
            future,
        )
        context = self._active_truck_switch
        if context is not None and context.run_id == run_id:
            context.flow_future = future
        self._flow_watch_timer.start()
        return False

    def _set_current_statuses(self, statuses: list[KitStatus], *, cache: bool = True) -> None:
        self._all_statuses = list(statuses)
        truck_number = self.current_truck_number()
        if cache and truck_number:
            self._status_cache_by_truck.set(self._status_cache_key(truck_number), list(statuses), negative=not statuses)
        self._render_current_statuses()

    def _notify_status_future_ready(self, _future: object = None) -> None:
        # Runs as a concurrent.futures done-callback, i.e. on the executor's worker
        # thread. Emitting a Signal is safe to do from any thread; Qt queues the
        # connected slot onto this window's own thread. Guard against the window's
        # C++ object already being torn down if a task finishes during shutdown.
        try:
            self._status_future_ready.emit()
        except RuntimeError:
            pass

    def _notify_flow_future_ready(self, _future: object = None) -> None:
        try:
            self._flow_future_ready.emit()
        except RuntimeError:
            pass

    def _poll_pending_status_future(self) -> None:
        if not self._pending_status_by_truck:
            self._status_watch_timer.stop()
            return
        completed: list[tuple[str, str, str, int, Future[list[KitStatus]]]] = []
        for truck_key, (truck_number, settings_signature, run_id, future) in list(self._pending_status_by_truck.items()):
            if not future.done():
                continue
            completed.append((truck_key, truck_number, settings_signature, run_id, future))
            self._pending_status_by_truck.pop(truck_key, None)
        if not self._pending_status_by_truck:
            self._status_watch_timer.stop()
        if not completed:
            return

        current_key = self.current_truck_number().casefold()
        for truck_key, truck_number, settings_signature, run_id, future in completed:
            try:
                statuses = future.result()
            except Exception as exc:
                if not self._is_active_truck_switch(run_id, truck_number):
                    GLOBAL_METRICS.record_stale_result_ignored()
                    continue
                self.log(f"Could not load kit statuses for {truck_number}: {exc}")
                statuses = []

            self._status_cache_by_truck.set(
                ("ui_status", settings_signature, truck_key),
                list(statuses),
                negative=not statuses,
            )
            if run_id <= 0:
                continue
            if truck_key != current_key or not self._is_active_truck_switch(run_id, truck_number):
                GLOBAL_METRICS.record_stale_result_ignored()
                continue
            self._set_current_statuses(list(statuses), cache=False)
            self._mark_status_done(run_id, truck_number)

    def _poll_pending_flow_future(self) -> None:
        if not self._pending_flow_by_truck:
            self._flow_watch_timer.stop()
            return
        completed: list[tuple[str, str, str, int, Future[FlowTruckInsight]]] = []
        for truck_key, (truck_number, cache_token, run_id, future) in list(self._pending_flow_by_truck.items()):
            if not future.done():
                continue
            completed.append((truck_key, truck_number, cache_token, run_id, future))
            self._pending_flow_by_truck.pop(truck_key, None)
        if not self._pending_flow_by_truck:
            self._flow_watch_timer.stop()
        if not completed:
            return

        current_key = self.current_truck_number().casefold()
        current_token = self._flow_request_token(self.current_truck_number())
        for truck_key, truck_number, cache_token, run_id, future in completed:
            try:
                insight = future.result()
            except Exception as exc:
                if not self._is_active_truck_switch(run_id, truck_number):
                    GLOBAL_METRICS.record_stale_result_ignored()
                    continue
                insight = FlowTruckInsight(
                    available=False,
                    truck_number=truck_number,
                    summary_text="Flow: unavailable.",
                    issue="load_failed",
                    tooltip_text=str(exc),
                )

            self._flow_cache_by_truck.set(
                self._flow_cache_key(truck_number, cache_token),
                (cache_token, insight),
                negative=not insight.available,
            )
            if run_id <= 0:
                continue
            if (
                truck_key != current_key
                or cache_token != current_token
                or not self._is_active_truck_switch(run_id, truck_number)
            ):
                GLOBAL_METRICS.record_stale_result_ignored()
                continue
            self._current_flow_truck_insight = insight
            self._refresh_current_truck_heading()
            self._refresh_flow_dependent_status_cells()
            self._mark_flow_done(run_id, truck_number)

    def _check_current_flow_cache(self) -> None:
        truck_number = self.current_truck_number().strip()
        if not truck_number:
            return
        truck_key = truck_number.casefold()
        if truck_key in self._pending_flow_by_truck:
            return
        current_token = self._flow_request_token(truck_number)
        hit, cached_flow = self._flow_cache_by_truck.get(self._flow_cache_key(truck_number, current_token))
        if hit and cached_flow is not None and cached_flow[0] == current_token:
            return
        context = self._active_truck_switch
        run_id = context.run_id if context is not None and context.truck_number.casefold() == truck_key else self._truck_switch_run_id
        self._load_flow_for_truck(truck_number, run_id=run_id)
        self._refresh_flow_dependent_status_cells()

    def _kit_table_signature(self, visible_statuses: list[KitStatus]) -> tuple[object, ...]:
        return (
            self.current_truck_number().casefold(),
            bool(self.show_hidden_kits_checkbox.isChecked()),
            tuple(
                (
                    status.kit_name,
                    status.paths.display_name,
                    status.paths.truck_number,
                    status.rpd_exists,
                    status.rpd_size_bytes,
                    status.fabrication_folder_exists,
                    status.fabrication_has_files,
                    status.spreadsheet_match,
                    status.preview_pdf_match,
                    self._print_packet_match_for_status(status),
                    self._assembly_packet_match_for_status(status),
                    self._cut_list_match_for_status(status),
                    status.inventor_outputs,
                    status.status_summary,
                    self._punch_code_text_for_status(status),
                    self._note_text_for_status(status),
                    is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings),
                )
                for status in visible_statuses
            ),
        )

    def _render_current_statuses(self) -> None:
        previous_kit_name = self._current_status().kit_name if self._current_status() is not None else ""
        visible_statuses = filter_kit_statuses(
            self._all_statuses,
            self.settings,
            show_hidden=self.show_hidden_kits_checkbox.isChecked(),
        )
        self._current_statuses = visible_statuses
        signature = self._kit_table_signature(visible_statuses)
        if signature == self._kit_table_render_signature and self.kit_table.rowCount() == len(visible_statuses):
            self._refresh_flow_dependent_status_cells()
            return
        self._kit_table_render_signature = signature
        self._updating_kit_table = True
        self.kit_table.setUpdatesEnabled(False)
        try:
            self.kit_table.setRowCount(len(visible_statuses))
            for row, status in enumerate(visible_statuses):
                self._populate_status_row(row, status)
        finally:
            self.kit_table.setUpdatesEnabled(True)
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

    def _on_show_hidden_kits_toggled(self) -> None:
        self._render_current_statuses()
        self._reload_current_flow_for_hidden_state()

    def _reload_current_flow_for_hidden_state(self) -> None:
        truck_number = self.current_truck_number().strip()
        if not truck_number:
            return
        context = self._active_truck_switch
        truck_key = truck_number.casefold()
        run_id = (
            context.run_id
            if context is not None and context.truck_number.casefold() == truck_key
            else self._truck_switch_run_id
        )
        if self._load_flow_for_truck(truck_number, run_id=run_id):
            self._refresh_flow_dependent_status_cells()

    def _hidden_foreground_for_status(self, status: KitStatus) -> QColor | None:
        return QColor("#6C757D") if is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings) else None

    def _make_release_item_for_status(
        self,
        status: KitStatus,
        *,
        hidden_foreground: QColor | None,
    ) -> QTableWidgetItem:
        green = QColor("#D8F3DC")
        yellow = QColor("#FFF3BF")
        red = QColor("#F8D7DA")
        release_text = self._release_text_for_status(status)
        release_color = red
        if release_text in {"Complete", "Released"}:
            release_color = green
        elif release_text == "Not released":
            release_color = yellow
        return self._make_item(release_text, background=release_color, foreground=hidden_foreground)

    def _make_flow_item_for_status(
        self,
        status: KitStatus,
        *,
        hidden_foreground: QColor | None,
    ) -> QTableWidgetItem:
        green = QColor("#D8F3DC")
        yellow = QColor("#FFF3BF")
        red = QColor("#F8D7DA")
        blue = QColor("#D6E4FF")
        neutral = QColor("#E9ECEF")
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
        return flow_item

    def _refresh_flow_dependent_status_cells(self) -> None:
        if self.kit_table.rowCount() != len(self._current_statuses):
            self._render_current_statuses()
            return
        self._updating_kit_table = True
        self.kit_table.setUpdatesEnabled(False)
        try:
            for row, status in enumerate(self._current_statuses):
                hidden_foreground = self._hidden_foreground_for_status(status)
                self.kit_table.setItem(
                    row,
                    self.RELEASE_COLUMN,
                    self._make_release_item_for_status(status, hidden_foreground=hidden_foreground),
                )
                self.kit_table.setItem(
                    row,
                    self.FLOW_COLUMN,
                    self._make_flow_item_for_status(status, hidden_foreground=hidden_foreground),
                )
        finally:
            self.kit_table.setUpdatesEnabled(True)
            self._updating_kit_table = False

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
            return

        raw_text = item.text()
        if raw_text.strip():
            self.settings.notes_by_kit[kit_key] = raw_text
        else:
            self.settings.notes_by_kit.pop(kit_key, None)
        save_settings(self.settings)
        self.log(f"Saved notes for {status.paths.display_name}.")

    def _on_kit_table_item_activated(self, item: QTableWidgetItem) -> None:
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
            if self._print_packet_match_for_status(status).chosen_path is not None:
                self._open_print_packet_for_status(status)
            return
        if item.column() == self.ASSEMBLY_PACKET_COLUMN:
            if self._assembly_packet_match_for_status(status).chosen_path is not None:
                self._open_assembly_packet_for_status(status)
            return
        if item.column() == self.CUT_LIST_COLUMN:
            if self._cut_list_match_for_status(status).chosen_path is not None:
                self._open_cut_list_for_status(status)

    @staticmethod
    def _status_color(match, *, green: QColor, yellow: QColor, red: QColor) -> QColor:
        """Pick the traffic-light color for a PdfMatch-shaped status field.

        Green: exactly one candidate was found and chosen. Yellow: a
        candidate was chosen from multiple, or candidates exist but none
        was chosen. Red: nothing was found at all.
        """
        if match.chosen_path is not None:
            return green if len(match.candidates) == 1 else yellow
        if match.candidates:
            return yellow
        return red

    def _populate_status_row(self, row: int, status: KitStatus) -> None:
        green = QColor("#D8F3DC")
        yellow = QColor("#FFF3BF")
        red = QColor("#F8D7DA")
        hidden = is_hidden_kit(status.paths.truck_number, status.kit_name, self.settings)
        hidden_foreground = self._hidden_foreground_for_status(status)

        nest_summary_color = self._status_color(status.preview_pdf_match, green=green, yellow=yellow, red=red)
        packet_match = self._print_packet_match_for_status(status)
        assembly_packet_match = self._assembly_packet_match_for_status(status)
        cut_list_match = self._cut_list_match_for_status(status)
        print_packet_color = self._status_color(packet_match, green=green, yellow=yellow, red=red)
        assembly_packet_color = self._status_color(assembly_packet_match, green=green, yellow=yellow, red=red)
        cut_list_color = self._status_color(cut_list_match, green=green, yellow=yellow, red=red)

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
            self._make_release_item_for_status(status, hidden_foreground=hidden_foreground),
            self._make_flow_item_for_status(status, hidden_foreground=hidden_foreground),
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
        packet_build_controller = getattr(self, "packet_build_controller", None)
        if packet_build_controller is not None:
            packet_build_controller.refresh_button_states()
        block_transfer_controller = getattr(self, "block_transfer_controller", None)
        if block_transfer_controller is not None:
            block_transfer_controller.refresh_button_state()
        full_flow_controller = getattr(self, "full_flow_controller", None)
        if full_flow_controller is not None:
            full_flow_controller.reapply_action_lock()

    def _refresh_show_hidden_trucks_button(self) -> None:
        self.truck_ordering_controller.refresh_show_hidden_button()

    def _refresh_truck_order_buttons(self) -> None:
        self.truck_ordering_controller.refresh_order_buttons()

    def _visible_truck_numbers(self) -> list[str]:
        return [
            self.truck_list.item(row).text().strip()
            for row in range(self.truck_list.count())
            if self.truck_list.item(row) is not None
        ]

    def _prewarm_visible_truck_caches(self) -> None:
        current_key = self.current_truck_number().casefold()
        settings_signature = self._settings_signature()
        started_status = False
        started_flow = False
        for truck_number in self._visible_truck_numbers():
            truck_key = truck_number.casefold()
            if not truck_key or truck_key == current_key or truck_key in self._prewarm_truck_keys:
                continue
            status_key = self._status_cache_key(truck_number)
            status_hit, _cached_statuses = self._status_cache_by_truck.get(status_key)
            if not status_hit and truck_key not in self._pending_status_by_truck:
                prewarm_status_future = self._status_executor.submit(collect_kit_statuses, truck_number, self.settings)
                prewarm_status_future.add_done_callback(self._notify_status_future_ready)
                self._pending_status_by_truck[truck_key] = (
                    truck_number,
                    settings_signature,
                    0,
                    prewarm_status_future,
                )
                started_status = True
            flow_token = self._flow_request_token(truck_number)
            if self._should_probe_flow_for_truck(truck_number):
                hidden_flow_kit_names = self._hidden_flow_kit_names_for_truck(truck_number)
                flow_key = self._flow_cache_key(truck_number, flow_token)
                flow_hit, _cached_flow = self._flow_cache_by_truck.get(flow_key)
                if not flow_hit and truck_key not in self._pending_flow_by_truck:
                    prewarm_flow_future = self._flow_executor.submit(
                        load_cached_flow_truck_insight,
                        truck_number,
                        hidden_flow_kit_names=hidden_flow_kit_names,
                    )
                    prewarm_flow_future.add_done_callback(self._notify_flow_future_ready)
                    self._pending_flow_by_truck[truck_key] = (
                        truck_number,
                        flow_token,
                        0,
                        prewarm_flow_future,
                    )
                    started_flow = True
            self._prewarm_truck_keys.add(truck_key)
        if started_status:
            self._status_watch_timer.start()
        if started_flow:
            self._flow_watch_timer.start()

    def _persist_truck_order(self) -> None:
        self.truck_ordering_controller.persist_truck_order()

    def _move_selected_truck(self, direction: int) -> None:
        self.truck_ordering_controller.move_selected_truck(direction)

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
        self._reload_current_flow_for_hidden_state()

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
                "Enter a job number in the F##### or P##### format, for example F55985 or P56113.",
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
        kit_names = scaffold_kit_names_for_truck(truck_text, self.settings)
        if not kit_names:
            QMessageBox.warning(
                self,
                "Add Truck",
                f"Could not find any kit folders under:\n{fabrication_truck_dir}",
            )
            return

        for kit_name in kit_names:
            try:
                result = create_kit_scaffold(truck_text, kit_name, self.settings)
                created_count += len(result.created_paths)
            except Exception as exc:
                errors.append(f"{kit_name}: {exc}")

        truck_order = normalize_truck_order_entries(self.settings.truck_order)
        if truck_text.casefold() not in {truck.casefold() for truck in truck_order}:
            self.settings.truck_order = truck_order + [truck_text]
            save_settings(self.settings)

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
        self._set_current_statuses([], cache=False)
        self._queue_status_refresh_for_truck(truck_number)
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
        self._set_current_statuses([], cache=False)
        self._queue_status_refresh_for_truck(truck_number)
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
        release_root = release_root_for_job(truck_number, self.settings)
        if release_root is None or not release_root.exists():
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
            QMessageBox.information(self, "Open BOM", "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                self,
                "Open BOM",
                "This kit does not have exactly one BOM candidate in the W folder.",
            )
            return
        self._open_path_with_message(spreadsheet_path)

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
        packet_match = self._print_packet_match_for_status(status)
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
        packet_match = self._assembly_packet_match_for_status(status)
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
        packet_match = self._cut_list_match_for_status(status)
        packet_path = packet_match.chosen_path
        if packet_path is None:
            QMessageBox.warning(
                self,
                "Open Cut List",
                "Could not find a Cut List PDF on the L side for this kit.",
            )
            return
        self._open_path_with_message(packet_path)


    def _refresh_packet_statuses(self, status: KitStatus) -> None:
        if status.paths.truck_number.casefold() == self.current_truck_number().casefold():
            self._queue_status_refresh_for_truck(status.paths.truck_number)
            self.packet_build_controller.refresh_button_states()

    def _queue_status_refresh_for_truck(self, truck_number: str) -> bool:
        truck_text = str(truck_number or "").strip()
        if not truck_text:
            return False
        truck_key = truck_text.casefold()
        self._invalidate_status_for_truck(truck_text)
        if truck_key in self._pending_status_by_truck:
            return False
        context = self._active_truck_switch
        run_id = (
            context.run_id
            if context is not None and context.truck_number.casefold() == truck_key
            else self._truck_switch_run_id
        )
        settings_signature = self._settings_signature()
        refresh_future = self._status_executor.submit(collect_kit_statuses, truck_text, self.settings, use_cache=False)
        refresh_future.add_done_callback(self._notify_status_future_ready)
        self._pending_status_by_truck[truck_key] = (
            truck_text,
            settings_signature,
            run_id,
            refresh_future,
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
        self.packet_build_controller.build_print_packet()

    def build_selected_assembly_packet(self) -> None:
        self.packet_build_controller.build_assembly_packet()

    def build_selected_cut_list_packet(self) -> None:
        self.packet_build_controller.build_cut_list_packet()

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

    def run_selected_full_flow(self) -> None:
        self.full_flow_controller.start_selected()

    def run_selected_inventor_flow(self) -> None:
        self.inventor_controller.start_selected()

    def send_selected_block_files_to_machine(self) -> None:
        self.block_transfer_controller.send_selected()

    def import_selected_csv_to_radan(self) -> None:
        self.radan_import_controller.import_selected()

    def closeEvent(self, event) -> None:
        if not self.full_flow_controller.can_close():
            QMessageBox.warning(
                self,
                "Full Flow Running",
                "Full Flow is still running. The Explorer window will stay open until it finishes or stops.",
            )
            event.ignore()
            return
        if not self.radan_import_controller.can_close():
            QMessageBox.warning(
                self,
                "RADAN Import Running",
                "A RADAN CSV import helper is still running. The Explorer window will stay open until it finishes.",
            )
            self.radan_import_controller.raise_running_dialog()
            event.ignore()
            return
        super().closeEvent(event)

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
        self._hot_reload_controller.poll_request()

    def _read_hot_reload_request(self) -> dict[str, str | int | float | list[str]]:
        return self._hot_reload_controller.read_request()

    def _clear_hot_reload_banner(self) -> None:
        self._hot_reload_controller.clear_banner()

    def _accept_hot_reload_from_banner(self) -> None:
        self._hot_reload_controller.accept_from_banner()

    def _cancel_hot_reload_from_banner(self) -> None:
        self._hot_reload_controller.cancel_from_banner()

    def _write_hot_reload_response(self, action: str) -> None:
        self._hot_reload_controller.write_response(action)
