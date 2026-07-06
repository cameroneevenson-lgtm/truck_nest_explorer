from __future__ import annotations

import base64
from concurrent.futures import Future
import hashlib
import json
import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

RADAN_DIR = PROJECT_DIR.parent / "radan_kitter"
if str(RADAN_DIR) not in sys.path:
    sys.path.insert(0, str(RADAN_DIR))

import full_flow_service
import background_job
import inventor_service
import rpd_io  # type: ignore[import-not-found]
from flow_bridge import (
    FlowKitInsight,
    FlowTruckInsight,
    flow_kit_insight_for_explorer_kit,
    flow_probe_cache_token,
    load_flow_truck_insight,
    map_explorer_kit_to_flow_kit,
    normalize_flow_insight_for_local_release,
    parse_flow_probe_payload,
)
from flow_schedule_probe import include_kit_in_embedded_gantt, split_overlay_rows_for_embedded_gantt
from models import (
    canonicalize_client_numbers_by_truck,
    canonicalize_notes_by_kit,
    ExplorerSettings,
    KitPaths,
    KitStatus,
    PdfMatch,
    SpreadsheetMatch,
    build_hidden_kit_key,
    canonicalize_hidden_kit_entries,
    canonicalize_punch_codes_by_kit,
    materialize_legacy_punch_codes_for_kit,
    resolve_punch_code_text,
)
from performance_metrics import BoundedTTLCache, performance_snapshot, reset_performance_metrics
from packet_build_service import (
    PacketBuildReadinessError,
    apply_assembly_context_to_sym_comments,
    assembly_comment_shorthand,
    build_assembly_packet,
    build_cut_list_packet,
    prepare_packet_build_context,
    scan_assembly_bom_context,
    validate_print_packet_readiness,
    write_assembly_bom_context_csv,
)
from settings_store import load_settings, save_settings
from services import (
    DEFAULT_RADAN_CSV_IMPORT_ENTRY,
    InventorToRadanInlineNeedsUi,
    add_odd_job_to_truck,
    assert_w_drive_write_allowed,
    build_kit_paths,
    build_launch_command,
    build_kit_status,
    cached_path_exists,
    clear_performance_caches,
    collect_kit_statuses,
    create_kit_scaffold,
    detect_assembly_packet_pdf,
    detect_cut_list_packet_pdf,
    detect_print_packet_pdf,
    detect_preview_pdf,
    detect_spreadsheet,
    discover_trucks,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    invalidate_status_cache_for_truck,
    is_hidden_kit,
    is_hidden_truck,
    is_owned_inventor_output,
    is_w_drive_path,
    launch_tool,
    launch_radan_csv_import,
    move_inventor_outputs_to_project,
    odd_job_names_for_truck,
    radan_csv_missing_symbols,
    radan_csv_import_lock_status,
    run_inventor_to_radan_inline,
    release_text_for_status,
    resolve_existing_inventor_csv,
    restore_truck_visibility,
    sort_truck_numbers_by_fabrication_order,
)

TEST_TMP_ROOT = PROJECT_DIR / "_tmp_tests"


@contextmanager
def workspace_tempdir() -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_dir = TEST_TMP_ROOT / uuid.uuid4().hex
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def write_pdf(path: Path, *, text: str, width: float, height: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    try:
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), text, fontsize=18)
        doc.save(str(path))
    finally:
        doc.close()


def write_pdf_pages(path: Path, *, pages: list[tuple[str, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    try:
        for text, width, height in pages:
            page = doc.new_page(width=width, height=height)
            page.insert_text((72, 72), text, fontsize=18)
        doc.save(str(path))
    finally:
        doc.close()


def copy_inventor_inline_runner(tool_dir: Path) -> None:
    shutil.copyfile(PROJECT_DIR.parent / "inventor_to_radan" / "inline_runner.py", tool_dir / "inline_runner.py")


def write_simple_rpd(path: Path, *, sym_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://www.radan.com/ns/project">
  <Parts>
    <Part>
      <ID>1</ID>
      <Symbol>{sym_path}</Symbol>
      <Qty>2</Qty>
    </Part>
  </Parts>
</Project>
""",
        encoding="utf-8",
    )


def write_rpd_parts(path: Path, rows: list[tuple[str, int | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, (sym_path, qty) in enumerate(rows, start=1):
        qty_xml = "" if qty is None else f"\n      <Number>{qty}</Number>"
        parts.append(
            f"""    <Part>
      <ID>{index}</ID>
      <Symbol>{sym_path}</Symbol>{qty_xml}
    </Part>"""
        )
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<Project xmlns=\"http://www.radan.com/ns/project\">\n"
        "  <Parts>\n"
        + "\n".join(parts)
        + "\n  </Parts>\n"
        "</Project>\n",
        encoding="utf-8",
    )


class TruckNestExplorerServicesTests(unittest.TestCase):
    def test_default_dashboard_launcher_targets_fabrication_flow_dashboard(self) -> None:
        settings = ExplorerSettings()
        self.assertEqual(settings.dashboard_launcher, r"C:\Tools\fabrication_flow_dashboard\run_app.bat")

    def test_default_radan_csv_import_entry_is_headless_helper(self) -> None:
        self.assertEqual(DEFAULT_RADAN_CSV_IMPORT_ENTRY.name, "import_parts_csv_headless.py")

    def test_build_launch_command_uses_cmd_for_batch_files(self) -> None:
        command = build_launch_command(r"C:\Tools\fabrication_flow_dashboard\run_app.bat")
        self.assertEqual(command, ["cmd.exe", "/c", r"C:\Tools\fabrication_flow_dashboard\run_app.bat"])

    def test_build_launch_command_uses_shared_venv_for_python_files(self) -> None:
        with workspace_tempdir() as temp_dir:
            fake_python = temp_dir / "Scripts" / "python.exe"
            fake_python.parent.mkdir()
            fake_python.write_text("", encoding="utf-8")
            script_path = temp_dir / "tool.py"

            with patch("services.DEFAULT_VENV_PYTHON", fake_python):
                command = build_launch_command(script_path)

            self.assertEqual(command, [str(fake_python), str(script_path)])

    def test_build_launch_command_requires_shared_venv_for_python_files(self) -> None:
        with workspace_tempdir() as temp_dir:
            missing_python = temp_dir / "Scripts" / "python.exe"
            script_path = temp_dir / "tool.py"

            with patch("services.DEFAULT_VENV_PYTHON", missing_python):
                with self.assertRaisesRegex(FileNotFoundError, "Shared venv Python was not found"):
                    build_launch_command(script_path)

    def test_launch_tool_starts_dashboard_launcher_in_its_own_folder(self) -> None:
        with workspace_tempdir() as temp_dir:
            launcher_path = temp_dir / "run_app.bat"
            launcher_path.write_text("@echo off\r\necho launched\r\n", encoding="utf-8")

            with patch("services.subprocess.Popen") as popen_mock:
                launch_tool(launcher_path)

            popen_mock.assert_called_once()
            self.assertEqual(popen_mock.call_args.args[0], ["cmd.exe", "/c", str(launcher_path)])
            self.assertEqual(popen_mock.call_args.kwargs["cwd"], str(temp_dir))
            self.assertIs(popen_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(popen_mock.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(popen_mock.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertIn("creationflags", popen_mock.call_args.kwargs)

    def test_map_explorer_kit_to_flow_kit_uses_built_in_schedule_rollups(self) -> None:
        self.assertEqual(map_explorer_kit_to_flow_kit("PAINT PACK"), "Body")
        self.assertEqual(map_explorer_kit_to_flow_kit("CHASSIS PACK"), "Chassis")
        self.assertEqual(map_explorer_kit_to_flow_kit("PUMP HOUSE"), "Pumphouse")
        self.assertEqual(map_explorer_kit_to_flow_kit("PUMP MOUNTS"), "Pump Mounts")
        self.assertEqual(map_explorer_kit_to_flow_kit("PUMP COVERINGS"), "Pump Covering")
        self.assertEqual(map_explorer_kit_to_flow_kit("PUMP BRACKETS"), "Pump Brackets")
        self.assertEqual(map_explorer_kit_to_flow_kit("console"), "Console")
        self.assertEqual(map_explorer_kit_to_flow_kit("OPERATIONAL PANELS"), "Operational Panels")
        self.assertEqual(map_explorer_kit_to_flow_kit("OPS PANELS"), "Operational Panels")
        self.assertEqual(map_explorer_kit_to_flow_kit("STEPS PACK"), "Step Pack")
        self.assertEqual(map_explorer_kit_to_flow_kit("STEPS"), "Step Pack")
        self.assertEqual(map_explorer_kit_to_flow_kit("STEP PACK"), "Step Pack")

    def test_embedded_gantt_excludes_small_kit_lanes(self) -> None:
        self.assertTrue(include_kit_in_embedded_gantt("Body"))
        self.assertTrue(include_kit_in_embedded_gantt("Console"))
        self.assertFalse(include_kit_in_embedded_gantt("Chassis"))
        self.assertFalse(include_kit_in_embedded_gantt("Pump Mounts"))
        self.assertFalse(include_kit_in_embedded_gantt("Pump Coverings"))
        self.assertFalse(include_kit_in_embedded_gantt("Steps"))
        self.assertFalse(include_kit_in_embedded_gantt("Operational Panels"))

    def test_split_overlay_rows_for_embedded_gantt_keeps_small_kit_status_lookup(self) -> None:
        rows = [
            SimpleNamespace(row_label="F55334 | Pump Covering", status_key="red"),
            SimpleNamespace(row_label="F55334 | Exterior", status_key="yellow"),
        ]

        embedded_rows, rows_by_kit_name = split_overlay_rows_for_embedded_gantt(rows)

        self.assertEqual([row.row_label for row in embedded_rows], ["F55334 | Exterior"])
        self.assertEqual(rows_by_kit_name["pump covering"].status_key, "red")
        self.assertEqual(rows_by_kit_name["exterior"].status_key, "yellow")

    def test_flow_kit_insight_for_explorer_kit_uses_probe_payload_and_fails_safe(self) -> None:
        gantt_png = b"fake-png"
        truck_insight = parse_flow_probe_payload(
            {
                "available": True,
                "truck_number": "F56139",
                "summary_text": "Flow plan 2026-03-02 | Active flow kits 4",
                "gantt_png_base64": base64.b64encode(gantt_png).decode("ascii"),
                "kits": [
                    {
                        "flow_kit_name": "Body",
                        "display_text": "Unreleased | Hold 0.6w",
                        "tooltip_text": "Flow kit: Body",
                        "status_key": "yellow",
                        "pdf_link": r"docs\body.pdf",
                    },
                    {
                        "flow_kit_name": "Pump Mounts",
                        "display_text": "Weld | On track",
                        "tooltip_text": "Flow kit: Pump Mounts",
                        "status_key": "green",
                    }
                ],
            }
        )

        body_insight = flow_kit_insight_for_explorer_kit("PAINT PACK", truck_insight)
        self.assertEqual(body_insight.display_text, "Unreleased | Hold 0.6w")
        self.assertEqual(body_insight.status_key, "yellow")
        self.assertEqual(truck_insight.gantt_png_bytes, gantt_png)
        self.assertEqual(
            Path(body_insight.pdf_link),
            (PROJECT_DIR.parent / "fabrication_flow_dashboard" / "docs" / "body.pdf").resolve(),
        )

        inactive_pump_insight = flow_kit_insight_for_explorer_kit("PUMP HOUSE", truck_insight)
        self.assertEqual(inactive_pump_insight.display_text, "Inactive")
        self.assertEqual(inactive_pump_insight.status_key, "missing")

        tracked_pump_subkit = flow_kit_insight_for_explorer_kit("PUMP MOUNTS", truck_insight)
        self.assertEqual(tracked_pump_subkit.display_text, "Weld | On track")
        self.assertEqual(tracked_pump_subkit.status_key, "green")

        inactive_step_pack = flow_kit_insight_for_explorer_kit("STEP PACK", truck_insight)
        self.assertEqual(inactive_step_pack.display_text, "Inactive")
        self.assertEqual(inactive_step_pack.status_key, "missing")

        untracked_insight = flow_kit_insight_for_explorer_kit("UNKNOWN KIT", truck_insight)
        self.assertEqual(untracked_insight.display_text, "Not tracked")
        self.assertEqual(untracked_insight.status_key, "missing")

    def test_load_flow_truck_insight_rejects_payload_for_different_truck(self) -> None:
        with workspace_tempdir() as temp_root:
            flow_dir = temp_root / "flow"
            flow_dir.mkdir()
            probe_path = temp_root / "flow_schedule_probe.py"
            probe_path.write_text("# probe placeholder\n", encoding="utf-8")

            def runner(command, **kwargs):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "available": True,
                            "truck_number": "F55334",
                            "summary_text": "Flow plan 2026-02-09",
                            "kits": [
                                {
                                    "flow_kit_name": "Body",
                                    "display_text": "Complete",
                                    "status_key": "green",
                                }
                            ],
                        }
                    ),
                    stderr="",
                )

            with patch("flow_bridge.FLOW_APP_DIR", flow_dir), patch("flow_bridge.FLOW_PROBE_PATH", probe_path):
                insight = load_flow_truck_insight("F54410", runner=runner)

        self.assertFalse(insight.available)
        self.assertEqual(insight.truck_number, "F54410")
        self.assertEqual(insight.issue, "truck_mismatch")
        self.assertEqual(insight.kit_insights_by_flow_name, {})
        self.assertIn("F55334", insight.summary_text)
        self.assertIn("F54410", insight.summary_text)

    def test_load_flow_truck_insight_accepts_matching_payload_case_insensitively(self) -> None:
        with workspace_tempdir() as temp_root:
            flow_dir = temp_root / "flow"
            flow_dir.mkdir()
            probe_path = temp_root / "flow_schedule_probe.py"
            probe_path.write_text("# probe placeholder\n", encoding="utf-8")

            def runner(command, **kwargs):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "available": True,
                            "truck_number": "F55334",
                            "summary_text": "Flow plan 2026-02-09",
                            "kits": [
                                {
                                    "flow_kit_name": "Body",
                                    "display_text": "Complete",
                                    "status_key": "green",
                                }
                            ],
                        }
                    ),
                    stderr="",
                )

            with patch("flow_bridge.FLOW_APP_DIR", flow_dir), patch("flow_bridge.FLOW_PROBE_PATH", probe_path):
                insight = load_flow_truck_insight("f55334", runner=runner)

        self.assertTrue(insight.available)
        self.assertEqual(insight.truck_number, "F55334")
        self.assertEqual(
            insight.kit_insights_by_flow_name["body"].display_text,
            "Complete",
        )

    def test_load_flow_truck_insight_hides_probe_console_on_windows(self) -> None:
        captured_kwargs: dict[str, object] = {}
        with workspace_tempdir() as temp_root:
            flow_dir = temp_root / "flow"
            flow_dir.mkdir()
            probe_path = temp_root / "flow_schedule_probe.py"
            probe_path.write_text("# probe placeholder\n", encoding="utf-8")

            def runner(command, **kwargs):
                captured_kwargs.update(kwargs)
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "available": True,
                            "truck_number": "F55334",
                            "summary_text": "Flow plan 2026-02-09",
                            "kits": [],
                        }
                    ),
                    stderr="",
                )

            with patch("flow_bridge.FLOW_APP_DIR", flow_dir), patch("flow_bridge.FLOW_PROBE_PATH", probe_path):
                insight = load_flow_truck_insight("F55334", runner=runner)

        self.assertTrue(insight.available)
        if os.name == "nt":
            self.assertIn("startupinfo", captured_kwargs)
            self.assertIn("creationflags", captured_kwargs)
        else:
            self.assertNotIn("startupinfo", captured_kwargs)

    def test_normalize_flow_insight_for_local_release_prefers_local_unreleased_signal(self) -> None:
        truck_insight = parse_flow_probe_payload(
            {
                "available": True,
                "truck_number": "F56139",
                "kits": [
                    {
                        "flow_kit_name": "Body",
                        "display_text": "Bend | On track",
                        "tooltip_text": "Flow kit: Body\nHead: Bend",
                        "status_key": "green",
                    },
                    {
                        "flow_kit_name": "Console",
                        "display_text": "Weld | Late",
                        "tooltip_text": "Flow kit: Console\nHead: Weld",
                        "status_key": "red",
                    },
                ],
            }
        )

        body_insight = normalize_flow_insight_for_local_release(
            flow_kit_insight_for_explorer_kit("PAINT PACK", truck_insight),
            fabrication_folder_exists=True,
            fabrication_has_files=False,
        )
        self.assertEqual(body_insight.display_text, "Not released")
        self.assertEqual(body_insight.status_key, "yellow")
        self.assertIn("Local release: Not released", body_insight.tooltip_text)
        self.assertIn("Flow status: Bend | On track", body_insight.tooltip_text)

        console_insight = normalize_flow_insight_for_local_release(
            flow_kit_insight_for_explorer_kit("CONSOLE PACK", truck_insight),
            fabrication_folder_exists=False,
            fabrication_has_files=False,
        )
        self.assertEqual(console_insight.display_text, "W missing")
        self.assertEqual(console_insight.status_key, "red")
        self.assertIn("Flow status: Weld | Late", console_insight.tooltip_text)

        already_unreleased = normalize_flow_insight_for_local_release(
            parse_flow_probe_payload(
                {
                    "available": True,
                    "truck_number": "F56139",
                    "kits": [
                        {
                            "flow_kit_name": "Body",
                            "display_text": "Unreleased | Hold 0.6w",
                            "tooltip_text": "Flow kit: Body",
                            "status_key": "yellow",
                        }
                    ],
                }
            ).kit_insights_by_flow_name["body"],
            fabrication_folder_exists=True,
            fabrication_has_files=False,
        )
        self.assertEqual(already_unreleased.display_text, "Unreleased | Hold 0.6w")
        self.assertEqual(already_unreleased.status_key, "yellow")

    def test_build_kit_paths_matches_expected_layout(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
        )

        paths = build_kit_paths("F55334", "PAINT PACK", settings)

        self.assertEqual(
            str(paths.rpd_path),
            r"L:\BATTLESHIELD\F-LARGE FLEET\F55334\PAINT PACK\F55334 PAINT PACK\F55334 PAINT PACK.rpd",
        )
        self.assertEqual(
            str(paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\PAINT PACK",
        )

    def test_build_kit_paths_accepts_plural_pump_coverings_alias(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
        )

        paths = build_kit_paths("F55334", "PUMP COVERINGS", settings)

        self.assertEqual(paths.kit_name, "PUMP COVERING")
        self.assertEqual(
            str(paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\PUMP PACK\COVERING",
        )

    def test_build_kit_paths_accepts_step_aliases(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
        )

        steps_pack_paths = build_kit_paths("F55334", "STEPS PACK", settings)
        steps_paths = build_kit_paths("F55334", "STEPS", settings)

        self.assertEqual(steps_pack_paths.kit_name, "STEP PACK")
        self.assertEqual(steps_paths.kit_name, "STEP PACK")
        self.assertEqual(
            str(steps_pack_paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\STEP PACK",
        )
        self.assertEqual(
            str(steps_paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\STEP PACK",
        )

    def test_build_kit_paths_accepts_console_and_ops_panel_aliases(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            console_project_dir = release_root / "F55974" / "CONSOLE" / "F55974 CONSOLE"
            ops_project_dir = release_root / "F55974" / "OPS PANELS" / "F55974 OPS PANELS"
            console_project_dir.mkdir(parents=True)
            ops_project_dir.mkdir(parents=True)

            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
            )

            console_paths = build_kit_paths("F55974", "console", settings)
            ops_panel_paths = build_kit_paths("F55974", "OPS PANELS", settings)

            self.assertEqual(console_paths.kit_name, "CONSOLE PACK")
            self.assertEqual(console_paths.release_kit_dir, console_project_dir.parent)
            self.assertEqual(console_paths.project_dir, console_project_dir)
            self.assertEqual(
                str(console_paths.fabrication_kit_dir),
                str(fabrication_root / "F55974" / "CONSOLE PACK"),
            )
            self.assertEqual(ops_panel_paths.kit_name, "OPERATIONAL PANELS")
            self.assertEqual(ops_panel_paths.release_kit_dir, ops_project_dir.parent)
            self.assertEqual(ops_panel_paths.project_dir, ops_project_dir)
            self.assertEqual(
                str(ops_panel_paths.fabrication_kit_dir),
                str(fabrication_root / "F55974" / "PUMP PACK" / "OPERATIONAL PANELS"),
            )

    def test_build_kit_paths_prefers_existing_plural_coverings_release_path(self) -> None:
        with workspace_tempdir() as temp_dir:
            release_root = temp_dir / "release"
            fabrication_root = temp_dir / "fabrication"
            legacy_project_dir = release_root / "F55985" / "PUMP COVERINGS" / "F55985 PUMP COVERINGS"
            legacy_project_dir.mkdir(parents=True, exist_ok=True)
            legacy_rpd_path = legacy_project_dir / "F55985 PUMP COVERINGS.rpd"
            legacy_rpd_path.write_text("<Project />", encoding="utf-8")

            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
            )

            paths = build_kit_paths("F55985", "PUMP COVERING", settings)

            self.assertEqual(paths.kit_name, "PUMP COVERING")
            self.assertEqual(paths.release_kit_dir, legacy_project_dir.parent)
            self.assertEqual(paths.project_dir, legacy_project_dir)
            self.assertEqual(paths.project_name, "F55985 PUMP COVERINGS")
            self.assertEqual(paths.rpd_path, legacy_rpd_path)

    def test_build_kit_paths_supports_dashboard_alias_for_radan_name(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
            kit_templates=["BODY | PAINT PACK"],
        )

        paths = build_kit_paths("F55334", "BODY", settings)

        self.assertEqual(paths.display_name, "BODY")
        self.assertEqual(
            str(paths.release_kit_dir),
            r"L:\BATTLESHIELD\F-LARGE FLEET\F55334\PAINT PACK",
        )
        self.assertEqual(
            str(paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\PAINT PACK",
        )
        self.assertEqual(
            str(paths.rpd_path),
            r"L:\BATTLESHIELD\F-LARGE FLEET\F55334\PAINT PACK\F55334 PAINT PACK\F55334 PAINT PACK.rpd",
        )

    def test_build_kit_paths_supports_nested_w_mapping(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
            kit_templates=["CONSOLE PACK => LASER\\CONSOLE\\PACK"],
        )

        paths = build_kit_paths("F55334", "CONSOLE PACK", settings)

        self.assertEqual(
            str(paths.release_kit_dir),
            r"L:\BATTLESHIELD\F-LARGE FLEET\F55334\CONSOLE PACK",
        )
        self.assertEqual(
            str(paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\LASER\CONSOLE\PACK",
        )

    def test_build_kit_paths_flattens_pump_subkit_on_l_but_maps_into_pump_pack_on_w(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
            kit_templates=["PUMP BRACKETS => PUMP PACK\\BRACKETS"],
        )

        paths = build_kit_paths("F55334", "PUMP BRACKETS", settings)

        self.assertEqual(
            str(paths.release_kit_dir),
            r"L:\BATTLESHIELD\F-LARGE FLEET\F55334\PUMP BRACKETS",
        )
        self.assertEqual(
            str(paths.fabrication_kit_dir),
            r"W:\LASER\For Battleshield Fabrication\F55334\PUMP PACK\BRACKETS",
        )

    def test_collect_kit_statuses_respects_configured_canonical_order(self) -> None:
        settings = ExplorerSettings(
            release_root=r"L:\BATTLESHIELD\F-LARGE FLEET",
            fabrication_root=r"W:\LASER\For Battleshield Fabrication",
            kit_templates=[
                "PAINT PACK",
                "PUMP HOUSE => PUMP PACK\\PUMP HOUSE",
                "PUMP MOUNTS => PUMP PACK\\MOUNTS",
                "STEP PACK",
            ],
        )

        statuses = collect_kit_statuses("F55334", settings)

        self.assertEqual(
            [status.kit_name for status in statuses],
            ["PAINT PACK", "PUMP HOUSE", "PUMP MOUNTS", "STEP PACK"],
        )

    def test_bounded_ttl_cache_tracks_hits_misses_expiration_and_size(self) -> None:
        clock = [100.0]
        reset_performance_metrics()
        cache: BoundedTTLCache[str] = BoundedTTLCache(
            "unit_cache",
            max_size=2,
            positive_ttl_seconds=10.0,
            negative_ttl_seconds=1.0,
            clock=lambda: clock[0],
        )

        hit, _value = cache.get("a")
        self.assertFalse(hit)
        cache.set("a", "alpha")
        hit, value = cache.get("a")
        self.assertTrue(hit)
        self.assertEqual(value, "alpha")

        cache.set("missing", "nope", negative=True)
        clock[0] += 1.1
        hit, _value = cache.get("missing")
        self.assertFalse(hit)

        cache.set("b", "bravo")
        cache.set("c", "charlie")
        self.assertEqual(len(cache), 2)
        self.assertFalse(cache.get("a")[0])
        self.assertTrue(cache.invalidate("b"))

        snapshot = performance_snapshot()
        self.assertGreaterEqual(snapshot.cache_hits.get("unit_cache", 0), 1)
        self.assertGreaterEqual(snapshot.cache_misses.get("unit_cache", 0), 3)
        self.assertGreaterEqual(snapshot.cache_invalidations.get("unit_cache", 0), 1)

    def test_negative_filesystem_cache_expires_and_detects_recovery(self) -> None:
        clock = [50.0]
        reset_performance_metrics()
        cache: BoundedTTLCache[object] = BoundedTTLCache(
            "fs_test",
            max_size=8,
            positive_ttl_seconds=10.0,
            negative_ttl_seconds=1.0,
            clock=lambda: clock[0],
        )
        with workspace_tempdir() as temp_root:
            path = temp_root / "recover.txt"

            self.assertFalse(cached_path_exists(path, cache=cache))
            path.write_text("ready", encoding="utf-8")
            self.assertFalse(cached_path_exists(path, cache=cache))

            clock[0] += 1.1
            self.assertTrue(cached_path_exists(path, cache=cache))

    def test_collect_kit_statuses_uses_cache_and_truck_invalidation(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            w_folder = fabrication_root / "F55334" / "PAINT PACK"
            w_folder.mkdir(parents=True)
            (w_folder / "TruckBom.xlsx").write_text("bom", encoding="utf-8")
            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
                kit_templates=["PAINT PACK"],
            )

            clear_performance_caches()
            reset_performance_metrics()
            first = collect_kit_statuses("F55334", settings)
            first_snapshot = performance_snapshot()
            second = collect_kit_statuses("F55334", settings)
            second_snapshot = performance_snapshot()

            self.assertEqual([status.kit_name for status in first], ["PAINT PACK"])
            self.assertEqual([status.kit_name for status in second], ["PAINT PACK"])
            self.assertEqual(first_snapshot.filesystem_checks, second_snapshot.filesystem_checks)
            self.assertGreaterEqual(second_snapshot.cache_hits.get("kit_status", 0), 1)

            invalidate_status_cache_for_truck("F55334", settings)
            collect_kit_statuses("F55334", settings)
            invalidated_snapshot = performance_snapshot()
            self.assertGreater(
                invalidated_snapshot.cache_misses.get("kit_status", 0),
                second_snapshot.cache_misses.get("kit_status", 0),
            )

    def test_flow_probe_payload_reports_database_query_count(self) -> None:
        reset_performance_metrics()
        with workspace_tempdir() as temp_root:
            flow_dir = temp_root / "flow"
            flow_dir.mkdir()
            probe_path = temp_root / "flow_schedule_probe.py"
            probe_path.write_text("# probe placeholder\n", encoding="utf-8")

            payload = {
                "available": True,
                "truck_number": "F55334",
                "summary_text": "Flow ready",
                "metrics": {"database_queries": 7},
                "kits": [],
            }

            def runner(*_args, **_kwargs):
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with patch("flow_bridge.FLOW_APP_DIR", flow_dir), patch("flow_bridge.FLOW_PROBE_PATH", probe_path):
                insight = load_flow_truck_insight("F55334", runner=runner)

        self.assertEqual(insight.database_query_count, 7)
        self.assertEqual(performance_snapshot().database_queries, 7)

    def test_create_kit_scaffold_clones_template_and_replaces_tokens(self) -> None:
        with workspace_tempdir() as temp_root:
            template_root = temp_root / "Template"
            template_root.mkdir(parents=True)
            (template_root / "nests").mkdir()
            (template_root / "remnants").mkdir()
            template_path = template_root / "Template.rpd"
            template_path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<RadanProject xmlns="http://www.radan.com/ns/project">
  <JobName>Template</JobName>
  <OriginalName>Template.rpd</OriginalName>
  <Truck>BLANK_TRUCK</Truck>
  <RadanSchedule>
    <JobDetails>
      <JobName>Template</JobName>
      <NestFolder>C:\\Example\\Template\\nests</NestFolder>
      <RemnantSaveFolder>C:\\Example\\Template\\remnants</RemnantSaveFolder>
    </JobDetails>
  </RadanSchedule>
  <Parts />
</RadanProject>
""",
                encoding="utf-8",
            )

            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
                rpd_template_path=str(template_path),
                template_replacements_text=(
                    "BLANK_PROJECT => {project_name}\n"
                    "BLANK_TRUCK => {truck_number}"
                ),
            )

            result = create_kit_scaffold("F55334", "PAINT PACK", settings)
            self.assertEqual(result.template_mode, "template_clone")
            self.assertIsNotNone(result.paths.rpd_path)
            self.assertTrue(result.paths.rpd_path.exists())

            content = result.paths.rpd_path.read_text(encoding="utf-8")
            self.assertIn("F55334 PAINT PACK", content)
            self.assertIn("F55334", content)
            self.assertIn("F55334 PAINT PACK.rpd", content)
            self.assertIn(str(result.paths.project_dir / "nests"), content)
            self.assertIn(str(result.paths.project_dir / "remnants"), content)
            self.assertTrue((result.paths.project_dir / "nests").exists())
            self.assertTrue((result.paths.project_dir / "remnants").exists())

            _tree, parts, _debug = rpd_io.load_rpd(str(result.paths.rpd_path))
            self.assertEqual(parts, [])

    def test_detect_spreadsheet_ignores_generated_radan_csv(self) -> None:
        with workspace_tempdir() as folder:
            (folder / "KitExport_Radan.csv").write_text("generated", encoding="utf-8")
            source = folder / "TruckBom.xlsx"
            source.write_text("placeholder", encoding="utf-8")

            match = detect_spreadsheet(folder)

            self.assertTrue(match.is_unique)
            self.assertEqual(match.chosen_path, source)

    def test_build_kit_status_uses_files_in_w_folder_as_release_signal(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            empty_folder = fabrication_root / "F55334" / "PAINT PACK"
            empty_folder.mkdir(parents=True)

            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
            )

            empty_status = build_kit_status("F55334", "PAINT PACK", settings)
            self.assertTrue(empty_status.fabrication_folder_exists)
            self.assertFalse(empty_status.fabrication_has_files)
            self.assertTrue(empty_status.rpd_exists)
            self.assertTrue(empty_status.paths.rpd_path is not None and empty_status.paths.rpd_path.exists())
            self.assertIn("Not released", empty_status.status_summary)
            self.assertIn("BOM missing", empty_status.status_summary)
            self.assertNotIn("Spreadsheet missing", empty_status.status_summary)
            self.assertNotIn("RPD missing", empty_status.status_summary)

            released_file = empty_folder / "TruckBom.xlsx"
            released_file.write_text("bom", encoding="utf-8")

            released_status = build_kit_status("F55334", "PAINT PACK", settings)
            self.assertTrue(released_status.fabrication_has_files)
            self.assertIn("Released", released_status.status_summary)
            self.assertNotIn("BOM missing", released_status.status_summary)
            self.assertNotIn("Spreadsheet ready", released_status.status_summary)
            self.assertNotIn("BOM ready", released_status.status_summary)

    def test_release_text_for_status_prefers_flow_complete(self) -> None:
        self.assertEqual(
            release_text_for_status(
                fabrication_folder_exists=True,
                fabrication_has_files=True,
                flow_display_text="Complete",
            ),
            "Complete",
        )
        self.assertEqual(
            release_text_for_status(
                fabrication_folder_exists=True,
                fabrication_has_files=True,
                flow_display_text="Weld | Late",
            ),
            "Released",
        )
        self.assertEqual(
            release_text_for_status(
                fabrication_folder_exists=True,
                fabrication_has_files=False,
            ),
            "Not released",
        )
        self.assertEqual(
            release_text_for_status(
                fabrication_folder_exists=False,
                fabrication_has_files=False,
            ),
            "W missing",
        )

    def test_detect_preview_pdf_finds_matching_nest_summary_on_l(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.release_kit_dir is not None
            assert paths.project_dir is not None
            paths.project_dir.mkdir(parents=True)
            packet_pdf = paths.project_dir / "PrintPacket_QTY_20260319.pdf"
            summary_dir = paths.project_dir / "_out"
            summary_dir.mkdir(parents=True)
            nest_pdf = summary_dir / "F55334 PAINT PACK nest summary.pdf"
            packet_pdf.write_text("pdf", encoding="utf-8")
            nest_pdf.write_text("pdf", encoding="utf-8")

            match = detect_preview_pdf(paths)

            self.assertEqual(match.chosen_path, nest_pdf)
            self.assertEqual(tuple(path.name for path in match.candidates), (nest_pdf.name,))

    def test_detect_preview_pdf_ignores_overly_deep_nest_summary(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            deep_dir = paths.project_dir / "_out" / "archive" / "20260319"
            deep_dir.mkdir(parents=True)
            deep_pdf = deep_dir / "F55334 PAINT PACK nest summary.pdf"
            deep_pdf.write_text("pdf", encoding="utf-8")

            match = detect_preview_pdf(paths)

            self.assertIsNone(match.chosen_path)
            self.assertEqual(match.issue, "pdf_missing")

    def test_detect_print_packet_pdf_finds_print_packet_without_using_nest_summary(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.release_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            packet_pdf = paths.project_dir / "PrintPacket_QTY_20260319.pdf"
            summary_pdf = paths.project_dir / "F55334 PAINT PACK nest summary.pdf"
            packet_pdf.write_text("pdf", encoding="utf-8")
            summary_pdf.write_text("pdf", encoding="utf-8")

            match = detect_print_packet_pdf(paths)

            self.assertEqual(match.chosen_path, packet_pdf)
            self.assertEqual(tuple(path.name for path in match.candidates), (packet_pdf.name,))

    def test_detect_print_packet_pdf_reuses_filesystem_cache(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            paths.project_dir.mkdir(parents=True)
            packet_pdf = paths.project_dir / "PrintPacket_QTY_20260319.pdf"
            packet_pdf.write_text("pdf", encoding="utf-8")
            cache: BoundedTTLCache[object] = BoundedTTLCache(
                "packet_pdf_test",
                max_size=32,
                positive_ttl_seconds=30.0,
                negative_ttl_seconds=2.0,
            )

            reset_performance_metrics()
            first = detect_print_packet_pdf(paths, fs_cache=cache)
            first_snapshot = performance_snapshot()
            second = detect_print_packet_pdf(paths, fs_cache=cache)
            second_snapshot = performance_snapshot()

            self.assertEqual(first.chosen_path, packet_pdf)
            self.assertEqual(second.chosen_path, packet_pdf)
            self.assertEqual(first_snapshot.filesystem_checks, second_snapshot.filesystem_checks)
            self.assertGreater(
                second_snapshot.cache_hits.get("packet_pdf_test", 0),
                first_snapshot.cache_hits.get("packet_pdf_test", 0),
            )

    def test_detect_assembly_packet_pdf_finds_generated_assembly_packet(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            paths.project_dir.mkdir(parents=True)
            assembly_pdf = paths.project_dir / "AssemblyPacket_TABLOID_20260417_101500.pdf"
            packet_pdf = paths.project_dir / "PrintPacket_QTY_20260417_101500.pdf"
            assembly_pdf.write_text("pdf", encoding="utf-8")
            packet_pdf.write_text("pdf", encoding="utf-8")

            match = detect_assembly_packet_pdf(paths)

            self.assertEqual(match.chosen_path, assembly_pdf)
            self.assertEqual(tuple(path.name for path in match.candidates), (assembly_pdf.name,))

    def test_detect_cut_list_packet_pdf_finds_generated_cut_list(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            paths.project_dir.mkdir(parents=True)
            cut_list_pdf = paths.project_dir / "CutList_20260428_101500.pdf"
            assembly_pdf = paths.project_dir / "AssemblyPacket_TABLOID_20260417_101500.pdf"
            cut_list_pdf.write_text("pdf", encoding="utf-8")
            assembly_pdf.write_text("pdf", encoding="utf-8")

            match = detect_cut_list_packet_pdf(paths)

            self.assertEqual(match.chosen_path, cut_list_pdf)
            self.assertEqual(tuple(path.name for path in match.candidates), (cut_list_pdf.name,))

    def test_prepare_packet_build_context_collects_unused_tabloid_assembly_pdfs(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            paths.fabrication_kit_dir.mkdir(parents=True)

            sym_path = paths.fabrication_kit_dir / "PART-1.sym"
            part_pdf = paths.fabrication_kit_dir / "PART-1.pdf"
            assembly_iam = paths.fabrication_kit_dir / "Assembly-Overview.iam"
            assembly_pdf = paths.fabrication_kit_dir / "Assembly-Overview.pdf"
            note_pdf = paths.fabrication_kit_dir / "Traveler.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            assembly_iam.write_text("iam", encoding="utf-8")
            write_pdf(part_pdf, text="PART", width=612, height=792)
            write_pdf(assembly_pdf, text="ASSEMBLY", width=792, height=1224)
            write_pdf(note_pdf, text="LETTER", width=612, height=792)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(len(context.parts), 1)
            self.assertEqual(context.assembly_source_pdfs, (assembly_pdf,))

    def test_validate_print_packet_readiness_rejects_empty_rpd(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "empty.rpd"
            rpd_path.write_text(
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
                "<Project xmlns=\"http://www.radan.com/ns/project\"><Parts /></Project>\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PacketBuildReadinessError, "no part rows"):
                validate_print_packet_readiness(rpd_path=rpd_path, parts=())

    def test_validate_print_packet_readiness_rejects_missing_explicit_qty(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "missing_qty.rpd"
            sym_path = temp_root / "Part A.sym"
            sym_path.write_text("sym", encoding="utf-8")
            write_rpd_parts(rpd_path, [(str(sym_path), None)])
            _tree, parts, _debug = rpd_io.load_rpd(str(rpd_path))

            with self.assertRaisesRegex(PacketBuildReadinessError, "without explicit quantities"):
                validate_print_packet_readiness(rpd_path=rpd_path, parts=parts)

    def test_validate_print_packet_readiness_compares_radan_csv_quantities(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "ready.rpd"
            csv_path = temp_root / "TruckBom_Radan.csv"
            sym_a = temp_root / "Part A.sym"
            sym_b = temp_root / "Part B.sym"
            sym_a.write_text("sym", encoding="utf-8")
            sym_b.write_text("sym", encoding="utf-8")
            write_rpd_parts(rpd_path, [(str(sym_a), 2), (str(sym_b), 3)])
            csv_path.write_text(
                f"{temp_root / 'Part A.dxf'},2,Aluminum,0.125,in,AIR\n"
                f"{temp_root / 'Part B.dxf'},3,Aluminum,0.125,in,AIR\n",
                encoding="utf-8",
            )
            _tree, parts, _debug = rpd_io.load_rpd(str(rpd_path))

            self.assertIsNone(validate_print_packet_readiness(rpd_path=rpd_path, parts=parts, expected_csv_path=csv_path))

            csv_path.write_text(
                f"{temp_root / 'Part A.dxf'},2,Aluminum,0.125,in,AIR\n"
                f"{temp_root / 'Part B.dxf'},4,Aluminum,0.125,in,AIR\n",
                encoding="utf-8",
            )
            warning = validate_print_packet_readiness(rpd_path=rpd_path, parts=parts, expected_csv_path=csv_path)

            self.assertIsNotNone(warning)
            self.assertIn("does not match", warning or "")
            self.assertIn("quantity mismatch", warning or "")

    def test_validate_print_packet_readiness_warns_when_rpd_parts_were_removed(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "ready.rpd"
            csv_path = temp_root / "TruckBom_Radan.csv"
            sym_a = temp_root / "Part A.sym"
            sym_a.write_text("sym", encoding="utf-8")
            write_rpd_parts(rpd_path, [(str(sym_a), 2)])
            csv_path.write_text(
                f"{temp_root / 'Part A.dxf'},2,Aluminum,0.125,in,AIR\n"
                f"{temp_root / 'Part B.dxf'},3,Aluminum,0.125,in,AIR\n",
                encoding="utf-8",
            )
            _tree, parts, _debug = rpd_io.load_rpd(str(rpd_path))

            warning = validate_print_packet_readiness(rpd_path=rpd_path, parts=parts, expected_csv_path=csv_path)

            self.assertIsNotNone(warning)
            self.assertIn("intentionally removed", warning or "")
            self.assertIn("missing part b", warning or "")

    def test_prepare_packet_build_context_collects_iam_backed_assembly_drawings(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            paths.fabrication_kit_dir.mkdir(parents=True)

            sym_path = paths.fabrication_kit_dir / "PART-1.sym"
            part_pdf = paths.fabrication_kit_dir / "PART-1.pdf"
            assembly_iam = paths.fabrication_kit_dir / "F55334-B-100.iam"
            assembly_pdf = paths.fabrication_kit_dir / "F55334-B-100.pdf"
            revised_assembly_iam = paths.fabrication_kit_dir / "F55334-BODY.iam"
            revised_assembly_pdf = paths.fabrication_kit_dir / "F55334-BODY R1.pdf"
            cut_part_ipt = paths.fabrication_kit_dir / "B-1001.ipt"
            cut_part_pdf = paths.fabrication_kit_dir / "B-1001.pdf"
            loose_assembly_pdf = paths.fabrication_kit_dir / "Assembly-Loose.pdf"
            sample_template_pdf = paths.fabrication_kit_dir / "Templates" / "Sample Assembly Template.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            assembly_iam.write_text("iam", encoding="utf-8")
            revised_assembly_iam.write_text("iam", encoding="utf-8")
            cut_part_ipt.write_text("ipt", encoding="utf-8")
            write_pdf(part_pdf, text="PART", width=612, height=792)
            write_pdf(assembly_pdf, text="ASSEMBLY", width=2448, height=1584)
            write_pdf(revised_assembly_pdf, text="REVISED ASSEMBLY", width=2448, height=1584)
            write_pdf(cut_part_pdf, text="CUT PART", width=2448, height=1584)
            write_pdf(loose_assembly_pdf, text="LOOSE ASSEMBLY", width=2448, height=1584)
            write_pdf(sample_template_pdf, text="SAMPLE", width=792, height=1224)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(
                context.assembly_source_pdfs,
                (assembly_pdf, revised_assembly_pdf),
            )

    def test_prepare_packet_build_context_collects_nonlaser_token_cut_list_pdfs(self) -> None:
        with workspace_tempdir() as temp_root:
            inventor_tool_dir = temp_root / "inventor_to_radan"
            inventor_tool_dir.mkdir(parents=True)
            (inventor_tool_dir / "nonlaser_tokens.csv").write_text("Token\nACH\nAST\n", encoding="utf-8")
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
                inventor_to_radan_entry=str(inventor_tool_dir / "inventor_to_radan.bat"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            paths.fabrication_kit_dir.mkdir(parents=True)

            sym_path = paths.fabrication_kit_dir / "PART-1.sym"
            part_pdf = paths.fabrication_kit_dir / "PART-1.pdf"
            ach_ipt = paths.fabrication_kit_dir / "ACH 3X2X.25-32-1.ipt"
            ach_pdf = paths.fabrication_kit_dir / "ACH 3X2X.25-32-1.pdf"
            revised_ast_ipt = paths.fabrication_kit_dir / "AST SC 2X.25-49.75-1.ipt"
            revised_ast_pdf = paths.fabrication_kit_dir / "AST SC 2X.25-49.75-1 R1.pdf"
            laser_ipt = paths.fabrication_kit_dir / "B-100.ipt"
            laser_pdf = paths.fabrication_kit_dir / "B-100.pdf"
            assembly_iam = paths.fabrication_kit_dir / "ACH Assembly.iam"
            assembly_pdf = paths.fabrication_kit_dir / "ACH Assembly.pdf"
            template_ipt = paths.fabrication_kit_dir / "Templates" / "ACH Template.ipt"
            template_pdf = paths.fabrication_kit_dir / "Templates" / "ACH Template.pdf"
            generated_pdf = paths.fabrication_kit_dir / "_out" / "CutList_20260428_101500.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            ach_ipt.write_text("ipt", encoding="utf-8")
            revised_ast_ipt.write_text("ipt", encoding="utf-8")
            laser_ipt.write_text("ipt", encoding="utf-8")
            assembly_iam.write_text("iam", encoding="utf-8")
            template_ipt.parent.mkdir(parents=True)
            template_ipt.write_text("ipt", encoding="utf-8")
            generated_pdf.parent.mkdir(parents=True)
            write_pdf(part_pdf, text="PART", width=612, height=792)
            write_pdf(ach_pdf, text="ACH CUT", width=1584, height=1224)
            write_pdf(revised_ast_pdf, text="AST CUT", width=1584, height=1224)
            write_pdf(laser_pdf, text="LASER", width=1584, height=1224)
            write_pdf(assembly_pdf, text="ASSEMBLY", width=1584, height=1224)
            write_pdf(template_pdf, text="TEMPLATE", width=1584, height=1224)
            write_pdf(generated_pdf, text="GENERATED", width=1584, height=1224)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(context.cut_list_source_pdfs, (ach_pdf, revised_ast_pdf))

    def test_prepare_packet_build_context_uses_subtree_lookup_for_pump_house_print_packet_pdfs(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
                kit_templates=["PUMP HOUSE => PUMP PACK\\PUMP HOUSE"],
            )
            paths = build_kit_paths("F55334", "PUMP HOUSE", settings)
            assert paths.release_kit_dir is not None
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.release_kit_dir.mkdir(parents=True, exist_ok=True)
            paths.project_dir.mkdir(parents=True, exist_ok=True)
            paths.fabrication_kit_dir.mkdir(parents=True, exist_ok=True)

            sym_path = paths.release_kit_dir / "F55334-PH-1.sym"
            nested_pdf_dir = paths.fabrication_kit_dir / "Parts" / "Nested"
            part_pdf = nested_pdf_dir / "F55334-PH-1.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            write_pdf(part_pdf, text="PUMP HOUSE PART", width=612, height=792)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(context.resolve_asset_fn(str(sym_path), ".pdf"), str(part_pdf))
            self.assertEqual(context.assembly_source_pdfs, ())

    def test_prepare_packet_build_context_collects_unused_tabloid_assembly_pdfs_from_project_dir(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            paths.fabrication_kit_dir.mkdir(parents=True)

            sym_path = paths.fabrication_kit_dir / "PART-1.sym"
            part_pdf = paths.fabrication_kit_dir / "PART-1.pdf"
            assembly_iam = paths.project_dir / "Assembly-Overview.iam"
            assembly_pdf = paths.project_dir / "Assembly-Overview.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            assembly_iam.write_text("iam", encoding="utf-8")
            write_pdf(part_pdf, text="PART", width=612, height=792)
            write_pdf(assembly_pdf, text="ASSEMBLY", width=792, height=1224)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(context.assembly_source_pdfs, (assembly_pdf,))

    def test_prepare_packet_build_context_ignores_generated_packet_artifacts(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )
            paths = build_kit_paths("F55334", "PAINT PACK", settings)
            assert paths.project_dir is not None
            assert paths.rpd_path is not None
            assert paths.fabrication_kit_dir is not None
            paths.project_dir.mkdir(parents=True)
            paths.fabrication_kit_dir.mkdir(parents=True)

            sym_path = paths.fabrication_kit_dir / "PART-1.sym"
            part_pdf = paths.fabrication_kit_dir / "PART-1.pdf"
            assembly_iam = paths.fabrication_kit_dir / "Assembly-Overview.iam"
            assembly_pdf = paths.fabrication_kit_dir / "Assembly-Overview.pdf"
            generated_out_dir = paths.fabrication_kit_dir / "_out"
            generated_out_dir.mkdir(parents=True)
            generated_print_packet = generated_out_dir / "PrintPacket_QTY_20260417_101500.pdf"
            generated_assembly_packet = generated_out_dir / "AssemblyPacket_TABLOID_20260417_101500.pdf"
            sym_path.write_text("sym", encoding="utf-8")
            assembly_iam.write_text("iam", encoding="utf-8")
            write_pdf(part_pdf, text="PART", width=612, height=792)
            write_pdf(assembly_pdf, text="ASSEMBLY", width=792, height=1224)
            write_pdf(generated_print_packet, text="GENERATED PRINT", width=792, height=1224)
            write_pdf(generated_assembly_packet, text="GENERATED ASSEMBLY", width=792, height=1224)
            write_simple_rpd(paths.rpd_path, sym_path=sym_path)

            context = prepare_packet_build_context(
                rpd_path=paths.rpd_path,
                fabrication_dir=paths.fabrication_kit_dir,
                settings=settings,
            )

            self.assertEqual(context.assembly_source_pdfs, (assembly_pdf,))

    def test_build_assembly_packet_combines_unused_tabloid_pdfs_as_is(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "release" / "F55334 PAINT PACK.rpd"
            rpd_path.parent.mkdir(parents=True, exist_ok=True)
            rpd_path.write_text("<Project />", encoding="utf-8")
            assembly_pdf_a = temp_root / "fab" / "Assembly-A.pdf"
            assembly_pdf_b = temp_root / "fab" / "Assembly-B.pdf"
            write_pdf(assembly_pdf_a, text="ASSEMBLY A", width=792, height=1224)
            write_pdf(assembly_pdf_b, text="ASSEMBLY B", width=1224, height=792)

            result = build_assembly_packet(
                rpd_path=rpd_path,
                source_pdfs=(assembly_pdf_a, assembly_pdf_b),
            )

            self.assertTrue(result.packet_path.endswith(".pdf"))
            self.assertEqual(result.source_documents, 2)
            self.assertEqual(result.output_pages, 2)
            with fitz.open(result.packet_path) as doc:
                self.assertEqual(doc.page_count, 2)
                self.assertEqual((round(doc[0].rect.width), round(doc[0].rect.height)), (792, 1224))
                self.assertEqual((round(doc[1].rect.width), round(doc[1].rect.height)), (1224, 792))
                self.assertIn("ASSEMBLY A", doc[0].get_text())
                self.assertIn("ASSEMBLY B", doc[1].get_text())

    def test_build_assembly_packet_includes_only_tabloid_pages_from_mixed_source_pdf(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "release" / "F55334 PAINT PACK.rpd"
            rpd_path.parent.mkdir(parents=True, exist_ok=True)
            rpd_path.write_text("<Project />", encoding="utf-8")
            mixed_pdf = temp_root / "fab" / "Assembly-Mixed.pdf"
            write_pdf_pages(
                mixed_pdf,
                pages=[
                    ("LETTER COVER", 612, 792),
                    ("TABLOID DRAWING", 792, 1224),
                ],
            )

            result = build_assembly_packet(
                rpd_path=rpd_path,
                source_pdfs=(mixed_pdf,),
            )

            self.assertEqual(result.source_documents, 1)
            self.assertEqual(result.output_pages, 1)
            with fitz.open(result.packet_path) as doc:
                self.assertEqual(doc.page_count, 1)
                self.assertIn("TABLOID DRAWING", doc[0].get_text())
                self.assertNotIn("LETTER COVER", doc[0].get_text())

    def test_build_assembly_packet_includes_large_assembly_drawing_pages(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "release" / "F55334 PAINT PACK.rpd"
            rpd_path.parent.mkdir(parents=True, exist_ok=True)
            rpd_path.write_text("<Project />", encoding="utf-8")
            assembly_pdf = temp_root / "fab" / "F55334-B-100.pdf"
            write_pdf(assembly_pdf, text="LARGE ASSEMBLY", width=2448, height=1584)

            result = build_assembly_packet(
                rpd_path=rpd_path,
                source_pdfs=(assembly_pdf,),
            )

            self.assertEqual(result.source_documents, 1)
            self.assertEqual(result.output_pages, 1)
            with fitz.open(result.packet_path) as doc:
                self.assertEqual(doc.page_count, 1)
                self.assertEqual((round(doc[0].rect.width), round(doc[0].rect.height)), (2448, 1584))
                self.assertIn("LARGE ASSEMBLY", doc[0].get_text())

    def test_scan_assembly_bom_context_maps_laser_parts_and_ignores_nonlaser_rows(self) -> None:
        with workspace_tempdir() as temp_root:
            parts = [
                SimpleNamespace(sym=str(temp_root / "F55334-B-1001.sym"), part="F55334-B-1001"),
                SimpleNamespace(sym=str(temp_root / "F55334-B-1002.sym"), part="F55334-B-1002"),
            ]
            assembly_a = temp_root / "fab" / "F55334-BODY.pdf"
            assembly_b = temp_root / "fab" / "F55334-DOOR.pdf"
            write_pdf(
                assembly_a,
                text=(
                    "BOM\n"
                    "ITEM PART NUMBER DESCRIPTION QTY\n"
                    "1 F55334-B-1001 LASER BRACKET 2\n"
                    "2 ACH 3X2X.25 NON LASER ANGLE 4\n"
                    "3 F55334-B-1002 LASER GUSSET 1\n"
                ),
                width=2448,
                height=1584,
            )
            write_pdf(
                assembly_b,
                text=(
                    "BOM\n"
                    "ITEM PART NUMBER DESCRIPTION QTY\n"
                    "1 F55334-B-1001 LASER BRACKET 1\n"
                    "2 BOLT 3/8 NON LASER 12\n"
                ),
                width=2448,
                height=1584,
            )

            result = scan_assembly_bom_context(
                parts=parts,
                source_pdfs=(assembly_a, assembly_b),
            )

            refs = [(ref.part_name, ref.assembly_name) for ref in result.references]
            self.assertEqual(
                refs,
                [
                    ("F55334-B-1001", "F55334-BODY"),
                    ("F55334-B-1001", "F55334-DOOR"),
                    ("F55334-B-1002", "F55334-BODY"),
                ],
            )
            self.assertEqual(result.assembly_pdf_count, 2)
            self.assertEqual(result.checked_part_count, 2)
            self.assertEqual(result.read_errors, ())
            self.assertEqual([ref.bom_qty for ref in result.references], [2, 1, 1])
            self.assertNotIn("ACH", "\n".join(ref.part_name for ref in result.references))

    def test_write_assembly_bom_context_csv_persists_many_to_many_mapping(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "job" / "job.rpd"
            rpd_path.parent.mkdir(parents=True)
            rpd_path.write_text("<Project />", encoding="utf-8")
            assembly_pdf = temp_root / "fab" / "F55334-BODY.pdf"
            write_pdf(
                assembly_pdf,
                text="1 F55334-B-1001 LASER BRACKET 2",
                width=2448,
                height=1584,
            )
            result = scan_assembly_bom_context(
                parts=[SimpleNamespace(sym=str(temp_root / "F55334-B-1001.sym"), part="F55334-B-1001")],
                source_pdfs=(assembly_pdf,),
            )

            report_path = write_assembly_bom_context_csv(rpd_path=rpd_path, result=result)

            text = report_path.read_text(encoding="utf-8")
            self.assertIn("part_name,assembly_name,assembly_pdf_path,page_number,bom_qty,evidence", text)
            self.assertIn("F55334-B-1001,F55334-BODY", text)

    def test_assembly_comment_shorthand_uses_last_hyphen_token(self) -> None:
        self.assertEqual(assembly_comment_shorthand("F55334-BODY"), "BODY")
        self.assertEqual(assembly_comment_shorthand("F55334-DOOR R2"), "DOOR")
        self.assertEqual(assembly_comment_shorthand("F55334-B-100"), "100")

    def test_apply_assembly_context_to_sym_comments_appends_shorthands(self) -> None:
        with workspace_tempdir() as temp_root:
            sym_path = temp_root / "F55334-B-1001.sym"
            sym_path.write_text(
                '<Symbol><Attr num="109" name="Comments" desc="Comments about this file." type="s" value="Walls"></Attr></Symbol>',
                encoding="utf-8",
            )
            parts = [SimpleNamespace(sym=str(sym_path), part="F55334-B-1001")]
            context = scan_assembly_bom_context(
                parts=parts,
                source_pdfs=(),
            )
            context = type(context)(
                assembly_pdf_count=context.assembly_pdf_count,
                checked_part_count=context.checked_part_count,
                references=(
                    SimpleNamespace(
                        part_name="F55334-B-1001",
                        assembly_name="F55334-BODY",
                        assembly_pdf_path="",
                        page_number=1,
                        bom_qty=1,
                        evidence="",
                    ),
                    SimpleNamespace(
                        part_name="F55334-B-1001",
                        assembly_name="F55334-DOOR",
                        assembly_pdf_path="",
                        page_number=1,
                        bom_qty=1,
                        evidence="",
                    ),
                ),
                read_errors=(),
            )

            backup_dir = temp_root / "_bak" / "assembly_comments"
            result = apply_assembly_context_to_sym_comments(parts=parts, result=context, backup_dir=backup_dir)
            second_result = apply_assembly_context_to_sym_comments(parts=parts, result=context)

            text = sym_path.read_text(encoding="utf-8")
            self.assertEqual(result.updated_count, 1)
            self.assertEqual(second_result.updated_count, 0)
            self.assertIn('value="Walls | ASM: BODY, DOOR"', text)
            self.assertIn('value="Walls"', (backup_dir / sym_path.name).read_text(encoding="utf-8"))

    def test_apply_assembly_context_to_sym_comments_uses_scan_result(self) -> None:
        with workspace_tempdir() as temp_root:
            sym_path = temp_root / "F55334-B-1001.sym"
            sym_path.write_text(
                '<Symbol><Attr num="109" name="Comments" desc="Comments about this file." type="s"></Attr></Symbol>',
                encoding="utf-8",
            )
            assembly_pdf = temp_root / "fab" / "F55334-BODY.pdf"
            write_pdf(
                assembly_pdf,
                text="1 F55334-B-1001 LASER BRACKET 2",
                width=2448,
                height=1584,
            )
            parts = [SimpleNamespace(sym=str(sym_path), part="F55334-B-1001")]
            context = scan_assembly_bom_context(parts=parts, source_pdfs=(assembly_pdf,))

            result = apply_assembly_context_to_sym_comments(parts=parts, result=context)

            self.assertEqual(result.updated_count, 1)
            self.assertIn('value="ASM: BODY"', sym_path.read_text(encoding="utf-8"))

    def test_build_cut_list_packet_combines_one_copy_of_each_source_pdf(self) -> None:
        with workspace_tempdir() as temp_root:
            rpd_path = temp_root / "release" / "F55334 PAINT PACK.rpd"
            rpd_path.parent.mkdir(parents=True, exist_ok=True)
            rpd_path.write_text("<Project />", encoding="utf-8")
            cut_pdf_a = temp_root / "fab" / "ACH 3X2X.25-32-1.pdf"
            cut_pdf_b = temp_root / "fab" / "AST SC 2X.25-49.75-1 R1.pdf"
            write_pdf(cut_pdf_a, text="ACH CUT", width=1584, height=1224)
            write_pdf_pages(
                cut_pdf_b,
                pages=[
                    ("AST CUT PAGE 1", 1584, 1224),
                    ("AST CUT PAGE 2", 1584, 1224),
                ],
            )

            result = build_cut_list_packet(
                rpd_path=rpd_path,
                source_pdfs=(cut_pdf_a, cut_pdf_b),
            )

            self.assertTrue(Path(result.packet_path).name.startswith("CutList_"))
            self.assertEqual(result.source_documents, 2)
            self.assertEqual(result.output_pages, 3)
            with fitz.open(result.packet_path) as doc:
                self.assertEqual(doc.page_count, 3)
                self.assertIn("ACH CUT", doc[0].get_text())
                self.assertIn("AST CUT PAGE 1", doc[1].get_text())
                self.assertIn("AST CUT PAGE 2", doc[2].get_text())

    def test_move_inventor_outputs_to_project_places_files_in_l_project_folder(self) -> None:
        with workspace_tempdir() as temp_root:
            w_folder = temp_root / "W" / "F55334" / "PAINT PACK"
            l_project = temp_root / "L" / "F55334" / "PAINT PACK" / "F55334 PAINT PACK"
            w_folder.mkdir(parents=True)
            l_project.mkdir(parents=True)

            spreadsheet = w_folder / "TruckBom.xlsx"
            spreadsheet.write_text("bom", encoding="utf-8")
            source_csv = w_folder / "TruckBom_Radan.csv"
            source_report = w_folder / "TruckBom_report.txt"
            source_csv.write_text("csv", encoding="utf-8")
            source_report.write_text("report", encoding="utf-8")

            outputs, moved = move_inventor_outputs_to_project(spreadsheet, l_project)

            self.assertEqual(
                tuple(path.name for path in moved),
                ("TruckBom_Radan.csv", "TruckBom_report.txt"),
            )
            self.assertTrue(outputs.target_csv_path.exists())
            self.assertTrue(outputs.target_report_path.exists())
            self.assertFalse(source_csv.exists())
            self.assertFalse(source_report.exists())

    def test_w_drive_guard_allows_only_owned_inventor_outputs(self) -> None:
        spreadsheet = Path(r"W:\LASER\TruckBom.xlsx")

        self.assertTrue(is_w_drive_path(r"W:\LASER\TruckBom.xlsx"))
        self.assertFalse(is_w_drive_path(r"L:\BATTLESHIELD\TruckBom_Radan.csv"))
        self.assertTrue(is_owned_inventor_output(r"W:\LASER\TruckBom_Radan.csv", spreadsheet_path=spreadsheet))
        self.assertTrue(is_owned_inventor_output(r"W:\LASER\TruckBom_report.txt", spreadsheet_path=spreadsheet))

        assert_w_drive_write_allowed(
            r"W:\LASER\TruckBom_Radan.csv",
            operation="move Inventor output",
            allow_owned_inventor_output=True,
            spreadsheet_path=spreadsheet,
        )
        with self.assertRaisesRegex(RuntimeError, "Refusing to write cleaned DXF on W:"):
            assert_w_drive_write_allowed(r"W:\LASER\cleaned.dxf", operation="write cleaned DXF")
        with self.assertRaises(RuntimeError):
            assert_w_drive_write_allowed(
                r"W:\LASER\Other_Radan.csv",
                operation="move Inventor output",
                allow_owned_inventor_output=True,
                spreadsheet_path=spreadsheet,
            )

    def test_resolve_existing_inventor_csv_prefers_l_project_output(self) -> None:
        with workspace_tempdir() as temp_root:
            w_folder = temp_root / "W" / "F55334" / "PAINT PACK"
            l_project = temp_root / "L" / "F55334" / "PAINT PACK" / "F55334 PAINT PACK"
            w_folder.mkdir(parents=True)
            l_project.mkdir(parents=True)

            spreadsheet = w_folder / "TruckBom.xlsx"
            spreadsheet.write_text("bom", encoding="utf-8")
            source_csv = w_folder / "TruckBom_Radan.csv"
            target_csv = l_project / "TruckBom_Radan.csv"
            source_csv.write_text("w csv", encoding="utf-8")
            target_csv.write_text("l csv", encoding="utf-8")

            self.assertEqual(resolve_existing_inventor_csv(spreadsheet, l_project), target_csv)

    def test_resolve_existing_inventor_csv_falls_back_to_w_output(self) -> None:
        with workspace_tempdir() as temp_root:
            w_folder = temp_root / "W" / "F55334" / "PAINT PACK"
            l_project = temp_root / "L" / "F55334" / "PAINT PACK" / "F55334 PAINT PACK"
            w_folder.mkdir(parents=True)
            l_project.mkdir(parents=True)

            spreadsheet = w_folder / "TruckBom.xlsx"
            spreadsheet.write_text("bom", encoding="utf-8")
            source_csv = w_folder / "TruckBom_Radan.csv"
            source_csv.write_text("w csv", encoding="utf-8")

            self.assertEqual(resolve_existing_inventor_csv(spreadsheet, l_project), source_csv)

    def test_run_inventor_to_radan_inline_loads_sibling_module_for_batch_entry(self) -> None:
        with workspace_tempdir() as temp_root:
            tool_dir = temp_root / "inventor_to_radan"
            tool_dir.mkdir()
            copy_inventor_inline_runner(tool_dir)
            spreadsheet = temp_root / "bom.csv"
            spreadsheet.write_text("Part Number,Description,Qty\n", encoding="utf-8")
            batch = tool_dir / "inventor_to_radan.bat"
            batch.write_text("@echo off\n", encoding="utf-8")
            (tool_dir / "bom_reader.py").write_text("ADDED_COUNT = 3\n", encoding="utf-8")
            (tool_dir / "inventor_to_radan.py").write_text(
                "import bom_reader\n"
                "from types import SimpleNamespace\n"
                "def convert_bom_to_radan_csv(path, *, allow_prompts, show_summary):\n"
                "    if allow_prompts or show_summary:\n"
                "        raise AssertionError('inline mode should not prompt')\n"
                "    return SimpleNamespace(added_count=bom_reader.ADDED_COUNT, bom_path=path)\n",
                encoding="utf-8",
            )

            result = run_inventor_to_radan_inline(batch, spreadsheet)

            self.assertEqual(result.added_count, 3)
            self.assertEqual(result.bom_path, str(spreadsheet))

    def test_run_inventor_to_radan_inline_prefers_inventor_dialog_package(self) -> None:
        saved_path = list(sys.path)
        saved_dialog_modules = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if name == "dialogs" or name.startswith("dialogs.")
        }
        try:
            for name in saved_dialog_modules:
                sys.modules.pop(name, None)

            with workspace_tempdir() as temp_root:
                foreign_root = temp_root / "foreign"
                foreign_dialogs = foreign_root / "dialogs"
                foreign_dialogs.mkdir(parents=True)
                (foreign_dialogs / "__init__.py").write_text("ORIGIN = 'foreign'\n", encoding="utf-8")
                sys.path.insert(0, str(foreign_root))
                foreign_module = __import__("dialogs")
                self.assertEqual(foreign_module.ORIGIN, "foreign")

                tool_dir = temp_root / "inventor_to_radan"
                tool_dialogs = tool_dir / "dialogs"
                tool_dialogs.mkdir(parents=True)
                copy_inventor_inline_runner(tool_dir)
                spreadsheet = temp_root / "bom.csv"
                spreadsheet.write_text("Part Number,Description,Qty\n", encoding="utf-8")
                entry = tool_dir / "inventor_to_radan.py"
                (tool_dialogs / "__init__.py").write_text("", encoding="utf-8")
                (tool_dialogs / "missing_dxf_dialog.py").write_text("VALUE = 'inventor'\n", encoding="utf-8")
                entry.write_text(
                    "from dialogs.missing_dxf_dialog import VALUE\n"
                    "from types import SimpleNamespace\n"
                    "def convert_bom_to_radan_csv(path, *, allow_prompts, show_summary):\n"
                    "    return SimpleNamespace(dialog_value=VALUE)\n",
                    encoding="utf-8",
                )

                result = run_inventor_to_radan_inline(entry, spreadsheet)

                self.assertEqual(result.dialog_value, "inventor")
                self.assertIs(sys.modules.get("dialogs"), foreign_module)
                self.assertNotIn("dialogs.missing_dxf_dialog", sys.modules)
        finally:
            sys.path[:] = saved_path
            for name in [name for name in sys.modules if name == "dialogs" or name.startswith("dialogs.")]:
                sys.modules.pop(name, None)
            sys.modules.update(saved_dialog_modules)

    def test_run_inventor_to_radan_inline_wraps_prompt_required_signal(self) -> None:
        with workspace_tempdir() as temp_root:
            tool_dir = temp_root / "inventor_to_radan"
            tool_dir.mkdir()
            copy_inventor_inline_runner(tool_dir)
            spreadsheet = temp_root / "bom.csv"
            spreadsheet.write_text("Part Number,Description,Qty\n", encoding="utf-8")
            entry = tool_dir / "inventor_to_radan.py"
            entry.write_text(
                "class InventorToRadanNeedsUi(RuntimeError):\n"
                "    def __init__(self):\n"
                "        self.missing_dxf_items = [{'desc': 'X'}]\n"
                "        self.missing_rules = ['Y']\n"
                "        super().__init__('needs ui')\n"
                "def convert_bom_to_radan_csv(path, *, allow_prompts, show_summary):\n"
                "    raise InventorToRadanNeedsUi()\n",
                encoding="utf-8",
            )

            with self.assertRaises(InventorToRadanInlineNeedsUi) as raised:
                run_inventor_to_radan_inline(entry, spreadsheet)

            self.assertEqual(raised.exception.missing_dxf_count, 1)
            self.assertEqual(raised.exception.missing_rule_count, 1)

    def test_inventor_service_requires_exactly_one_bom(self) -> None:
        status = SimpleNamespace(
            spreadsheet_match=SimpleNamespace(chosen_path=None, candidates=()),
            paths=SimpleNamespace(project_dir=Path("project")),
        )

        with self.assertRaisesRegex(inventor_service.InventorValidationError, "exactly one BOM"):
            inventor_service.run_inventor_for_status(status, ExplorerSettings())

    def test_inventor_service_reports_ambiguous_bom(self) -> None:
        status = SimpleNamespace(
            spreadsheet_match=SimpleNamespace(
                chosen_path=None,
                candidates=(Path("one.xlsx"), Path("two.xlsx")),
            ),
            paths=SimpleNamespace(project_dir=Path("project")),
        )

        with self.assertRaisesRegex(inventor_service.InventorValidationError, "multiple BOM candidates"):
            inventor_service.run_inventor_for_status(status, ExplorerSettings())

    def test_inventor_service_validates_project_dir_and_entry(self) -> None:
        with workspace_tempdir() as temp_root:
            bom = temp_root / "BOM.xlsx"
            bom.write_text("bom", encoding="utf-8")
            status = SimpleNamespace(
                spreadsheet_match=SimpleNamespace(chosen_path=bom, candidates=(bom,)),
                paths=SimpleNamespace(project_dir=None),
            )

            with self.assertRaisesRegex(inventor_service.InventorValidationError, "L-side project folder"):
                inventor_service.run_inventor_for_status(status, ExplorerSettings(inventor_to_radan_entry=""))

            missing_project = temp_root / "missing_project"
            status.paths.project_dir = missing_project
            with self.assertRaisesRegex(inventor_service.InventorValidationError, "does not exist"):
                inventor_service.run_inventor_for_status(status, ExplorerSettings(inventor_to_radan_entry=""))

            project = temp_root / "project"
            project.mkdir()
            status.paths.project_dir = project
            with self.assertRaisesRegex(inventor_service.InventorValidationError, "not configured"):
                inventor_service.run_inventor_for_status(status, ExplorerSettings(inventor_to_radan_entry=""))

            with self.assertRaisesRegex(inventor_service.InventorValidationError, "does not exist"):
                inventor_service.run_inventor_for_status(
                    status,
                    ExplorerSettings(inventor_to_radan_entry=str(temp_root / "missing.bat")),
                )

    def test_inventor_service_moves_output_once_and_returns_typed_result(self) -> None:
        with workspace_tempdir() as temp_root:
            bom = temp_root / "W" / "BOM.xlsx"
            bom.parent.mkdir()
            bom.write_text("bom", encoding="utf-8")
            project = temp_root / "L"
            project.mkdir()
            entry = temp_root / "inventor_to_radan.bat"
            entry.write_text("@echo off\r\n", encoding="utf-8")
            outputs = inventor_service.inventor_output_paths(bom, project)
            target_csv = outputs.target_csv_path
            target_report = outputs.target_report_path
            self.assertIsNotNone(target_csv)
            self.assertIsNotNone(target_report)
            target_report.write_text("report", encoding="utf-8")
            status = SimpleNamespace(
                spreadsheet_match=SimpleNamespace(chosen_path=bom, candidates=(bom,)),
                paths=SimpleNamespace(project_dir=project),
            )

            with (
                patch(
                    "inventor_service.run_inventor_to_radan_inline",
                    return_value=SimpleNamespace(added_count="7"),
                ) as run_mock,
                patch(
                    "inventor_service.move_inventor_outputs_to_project",
                    return_value=(outputs, (target_csv, target_report)),
                ) as move_mock,
            ):
                result = inventor_service.run_inventor_for_status(
                    status,
                    ExplorerSettings(inventor_to_radan_entry=str(entry)),
                )

            run_mock.assert_called_once_with(entry, bom)
            move_mock.assert_called_once_with(bom, project)
            self.assertEqual(result.added_count, 7)
            self.assertEqual(result.report_path, target_report)
            self.assertEqual(result.discard_paths, (target_csv, target_report))

    def test_inventor_service_translates_inline_needs_ui(self) -> None:
        with workspace_tempdir() as temp_root:
            bom = temp_root / "BOM.xlsx"
            bom.write_text("bom", encoding="utf-8")
            project = temp_root / "project"
            project.mkdir()
            entry = temp_root / "inventor_to_radan.bat"
            entry.write_text("@echo off\r\n", encoding="utf-8")
            status = SimpleNamespace(
                spreadsheet_match=SimpleNamespace(chosen_path=bom, candidates=(bom,)),
                paths=SimpleNamespace(project_dir=project),
            )

            with (
                patch(
                    "inventor_service.run_inventor_to_radan_inline",
                    side_effect=InventorToRadanInlineNeedsUi("needs ui"),
                ),
                patch("inventor_service.move_inventor_outputs_to_project") as move_mock,
            ):
                with self.assertRaises(inventor_service.InventorNeedsUserAction):
                    inventor_service.run_inventor_for_status(
                        status,
                        ExplorerSettings(inventor_to_radan_entry=str(entry)),
                    )

            move_mock.assert_not_called()

    def test_inventor_review_accept_does_not_delete_output(self) -> None:
        from PySide6.QtWidgets import QDialog
        import dialogs.inventor_report_review_dialog as review_module

        with workspace_tempdir() as temp_root:
            report = temp_root / "BOM_report.txt"
            report.write_text("ok", encoding="utf-8")
            result = inventor_service.InventorRunResult(
                spreadsheet_path=temp_root / "BOM.xlsx",
                project_dir=temp_root,
                entry_path=temp_root / "inventor_to_radan.bat",
                moved_paths=(report,),
                report_path=report,
                discard_paths=(report,),
            )

            class FakeDialog:
                rejected_without_ack = False

                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def exec(self) -> int:
                    return int(QDialog.Accepted)

            with (
                patch.object(review_module, "InventorReportReviewDialog", FakeDialog),
                patch.object(review_module, "discard_inventor_result") as discard_mock,
            ):
                outcome = review_module.review_inventor_result(None, result)

            self.assertEqual(outcome.state, review_module.InventorReviewState.ACCEPTED)
            discard_mock.assert_not_called()

    def test_inventor_discard_deletes_only_eligible_generated_files(self) -> None:
        with workspace_tempdir() as temp_root:
            csv_path = temp_root / "BOM_Radan.csv"
            report_path = temp_root / "BOM_report.txt"
            pdf_path = temp_root / "BOM.pdf"
            for path in (csv_path, report_path, pdf_path):
                path.write_text("data", encoding="utf-8")
            result = inventor_service.InventorRunResult(
                spreadsheet_path=temp_root / "BOM.xlsx",
                project_dir=temp_root,
                entry_path=temp_root / "inventor_to_radan.bat",
                moved_paths=(csv_path, report_path, pdf_path),
                report_path=report_path,
                discard_paths=(csv_path, report_path, pdf_path),
            )

            discard_result = inventor_service.discard_inventor_result(result)

            self.assertEqual(discard_result.failed_deletes, ())
            self.assertEqual(set(discard_result.deleted_paths), {csv_path, report_path})
            self.assertFalse(csv_path.exists())
            self.assertFalse(report_path.exists())
            self.assertTrue(pdf_path.exists())

    def test_radan_csv_missing_symbols_reports_missing_sym_files(self) -> None:
        with workspace_tempdir() as temp_root:
            output_folder = temp_root / "symbols"
            output_folder.mkdir()
            (output_folder / "Part A.sym").write_text("sym", encoding="utf-8")
            csv_path = temp_root / "parts_Radan.csv"
            csv_path.write_text(
                f"{temp_root / 'Part A.dxf'},1,Aluminum,0.125,in,AIR\n"
                f"{temp_root / 'Part B.dxf'},1,Aluminum,0.125,in,AIR\n",
                encoding="utf-8",
            )

            missing = radan_csv_missing_symbols(csv_path, output_folder)

            self.assertEqual(missing, (output_folder / "Part B.sym",))

    def test_radan_csv_missing_symbols_can_limit_importable_rows(self) -> None:
        with workspace_tempdir() as temp_root:
            output_folder = temp_root / "symbols"
            output_folder.mkdir()
            (output_folder / "Part A.sym").write_text("sym", encoding="utf-8")
            csv_path = temp_root / "parts_Radan.csv"
            csv_path.write_text(
                "\n"
                f"{temp_root / 'Part A.dxf'},1,Aluminum,0.125,in,AIR\n"
                f"{temp_root / 'Part B.dxf'},1,Aluminum,0.125,in,AIR\n",
                encoding="utf-8",
            )

            missing = radan_csv_missing_symbols(csv_path, output_folder, max_parts=1)

            self.assertEqual(missing, ())

    def test_radan_csv_import_lock_status_reports_live_pid(self) -> None:
        with workspace_tempdir() as temp_root:
            project_path = temp_root / "job.rpd"
            project_path.write_text("<Project />", encoding="utf-8")
            digest = hashlib.sha1(str(project_path.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
            lock_path = Path(os.environ.get("TEMP", str(project_path.parent))) / f"radan_csv_import_{digest}.lock"
            lock_path.write_text("1234", encoding="ascii")
            try:
                with patch("services._process_exists", return_value=True):
                    running, found_lock_path, process_id = radan_csv_import_lock_status(project_path)
            finally:
                lock_path.unlink(missing_ok=True)

            self.assertTrue(running)
            self.assertEqual(found_lock_path, lock_path)
            self.assertEqual(process_id, 1234)

    def test_launch_radan_csv_import_starts_helper_console(self) -> None:
        with workspace_tempdir() as temp_root:
            entry_path = temp_root / "import_parts_csv_live.py"
            csv_path = temp_root / "TruckBom_Radan.csv"
            project_path = temp_root / "TruckBom.rpd"
            log_path = temp_root / "import.log"
            output_folder = temp_root / "out"
            entry_path.write_text("print('helper')", encoding="utf-8")
            csv_path.write_text("csv", encoding="utf-8")
            project_path.write_text("<Project />", encoding="utf-8")
            output_folder.mkdir()

            with patch("services.subprocess.Popen") as popen_mock:
                launch_radan_csv_import(
                    csv_path,
                    output_folder,
                    project_path=project_path,
                    log_path=log_path,
                    entry_path=entry_path,
                    allow_visible_radan=True,
                    rebuild_symbols=True,
                    lab_symbol_writer=True,
                    d_record_view_height_threshold_guard=True,
                    preprocess_dxf_outer_profile=True,
                    preprocess_dxf_tolerance=0.002,
                    assign_project_colors=True,
                    project_update_method="radan-nst",
                    refresh_project_sheets=True,
                    max_parts=10,
                )

            command = popen_mock.call_args.args[0]
            self.assertIn(str(entry_path), command)
            self.assertIn(str(csv_path), command)
            self.assertIn(str(output_folder), command)
            self.assertIn(str(project_path), command)
            self.assertIn(str(log_path), command)
            self.assertNotIn("--kitter-launcher", command)
            self.assertIn("--allow-visible-radan", command)
            self.assertIn("--rebuild-symbols", command)
            self.assertIn("--lab-symbol-writer", command)
            self.assertNotIn("--native-sym-experimental", command)
            self.assertIn("--d-record-view-height-threshold-guard", command)
            self.assertIn("--preprocess-dxf-outer-profile", command)
            self.assertIn("--preprocess-dxf-tolerance", command)
            self.assertIn("0.002", command)
            self.assertIn("--assign-project-colors", command)
            self.assertIn("--project-update-method", command)
            self.assertIn("radan-nst", command)
            self.assertIn("--refresh-project-sheets", command)
            self.assertIn("--max-parts", command)
            self.assertIn("10", command)
            self.assertEqual(popen_mock.call_args.kwargs["cwd"], str(temp_root))
            self.assertIs(popen_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
            stdout_arg = popen_mock.call_args.kwargs["stdout"]
            self.assertEqual(Path(stdout_arg.name), log_path)
            self.assertIs(popen_mock.call_args.kwargs["stderr"], stdout_arg)

    def test_launch_radan_csv_import_allows_cleaned_dxf_preprocess_without_synthetic_mode(self) -> None:
        with workspace_tempdir() as temp_root:
            entry_path = temp_root / "import_parts_csv_headless.py"
            csv_path = temp_root / "TruckBom_Radan.csv"
            project_path = temp_root / "TruckBom.rpd"
            output_folder = temp_root / "out"
            entry_path.write_text("print('helper')", encoding="utf-8")
            csv_path.write_text("csv", encoding="utf-8")
            project_path.write_text("<Project />", encoding="utf-8")
            output_folder.mkdir()

            with patch("services.subprocess.Popen") as popen_mock:
                launch_radan_csv_import(
                    csv_path,
                    output_folder,
                    project_path=project_path,
                    entry_path=entry_path,
                    rebuild_symbols=True,
                    preprocess_dxf_outer_profile=True,
                    preprocess_dxf_tolerance=0.002,
                )

            command = popen_mock.call_args.args[0]
            self.assertIn("--preprocess-dxf-outer-profile", command)
            self.assertIn("--preprocess-dxf-tolerance", command)
            self.assertIn("0.002", command)
            self.assertIn("--rebuild-symbols", command)
            self.assertNotIn("--native-sym-experimental", command)
            self.assertNotIn("--d-record-view-height-threshold-guard", command)
            self.assertNotIn("--assign-project-colors", command)

    def test_discover_trucks_uses_release_root_only(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            registry_path = temp_root / "truck_registry.csv"
            (release_root / "F55334").mkdir(parents=True)
            (release_root / "F59999").mkdir(parents=True)
            (release_root / "Templates").mkdir(parents=True)
            (release_root / "_runtime").mkdir(parents=True)
            (release_root / "F5533").mkdir(parents=True)
            (fabrication_root / "F55335").mkdir(parents=True)
            registry_path.write_text(
                "truck_number,day_zero,is_active,notes\n"
                "F55334,2026-02-09,1,Whole truck\n",
                encoding="utf-8",
            )

            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
            )

            with patch("services.FLOW_TRUCK_REGISTRY_PATH", registry_path):
                trucks = discover_trucks(settings)

            self.assertEqual(trucks, ["F55334"])

    def test_create_kit_scaffold_rejects_unregistered_f_job_when_registry_exists(self) -> None:
        with workspace_tempdir() as temp_root:
            registry_path = temp_root / "truck_registry.csv"
            registry_path.write_text(
                "truck_number,day_zero,is_active,notes\n"
                "F55334,2026-02-09,1,Whole truck\n",
                encoding="utf-8",
            )
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
            )

            with patch("services.FLOW_TRUCK_REGISTRY_PATH", registry_path):
                with self.assertRaisesRegex(RuntimeError, "not listed as an active whole-truck job"):
                    create_kit_scaffold("F59999", "PAINT PACK", settings)

    def test_find_fabrication_truck_dir_matches_case_insensitively(self) -> None:
        with workspace_tempdir() as temp_root:
            fabrication_root = temp_root / "fab"
            wanted = fabrication_root / "f55334"
            wanted.mkdir(parents=True)
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(fabrication_root),
            )

            found = find_fabrication_truck_dir("F55334", settings)

            self.assertEqual(found, wanted)

    def test_filter_truck_numbers_hides_persisted_completed_trucks(self) -> None:
        settings = ExplorerSettings(hidden_trucks=["f55334", "bad-value"])

        visible = filter_truck_numbers(["F55333", "F55334", "F55335"], settings)
        all_trucks = filter_truck_numbers(["F55333", "F55334", "F55335"], settings, show_hidden=True)

        self.assertEqual(visible, ["F55333", "F55335"])
        self.assertEqual(all_trucks, ["F55333", "F55334", "F55335"])
        self.assertTrue(is_hidden_truck("F55334", settings))
        self.assertFalse(is_hidden_truck("F55335", settings))

    def test_restore_truck_visibility_unhides_truck_and_its_kits(self) -> None:
        settings = ExplorerSettings(
            hidden_trucks=["F55333", "f55334"],
            hidden_kits=[
                "F55334::BODY",
                "F55334::PUMPHOUSE",
                "F55335::BODY",
            ],
            kit_templates=["BODY | PAINT PACK", "PUMPHOUSE"],
        )

        removed_truck, removed_kit_count = restore_truck_visibility("f55334", settings)

        self.assertTrue(removed_truck)
        self.assertEqual(removed_kit_count, 2)
        self.assertEqual(settings.hidden_trucks, ["F55333"])
        self.assertEqual(settings.hidden_kits, ["F55335::PAINT PACK"])

    def test_sort_truck_numbers_uses_saved_fabrication_order(self) -> None:
        settings = ExplorerSettings(truck_order=["F55335", "f55333", "bad-value"])

        ordered = sort_truck_numbers_by_fabrication_order(
            ["F55334", "F55333", "F55336", "F55335"],
            settings,
        )

        self.assertEqual(ordered, ["F55335", "F55333", "F55334", "F55336"])

    def test_canonicalize_hidden_kits_maps_dashboard_alias_to_radan_name(self) -> None:
        hidden_kits = canonicalize_hidden_kit_entries(
            ["F55334::BODY", "F55334::PAINT PACK"],
            ["BODY | PAINT PACK", "PUMPHOUSE"],
        )

        self.assertEqual(hidden_kits, ["F55334::PAINT PACK"])

    def test_canonicalize_punch_codes_map_uses_canonical_kit_name(self) -> None:
        punch_codes = canonicalize_punch_codes_by_kit(
            {
                "BODY": "P01 = Body vent",
                "f55334::BODY": "P02 = Truck-specific body vent",
                "PAINT PACK": "P01 = Paint pack vent",
                "PUMPHOUSE": "P77 = Pump slot",
                "bad-value::PUMPHOUSE": "ignore",
                "": "ignore",
            },
            ["BODY | PAINT PACK", "PUMPHOUSE"],
        )

        self.assertEqual(
            punch_codes,
            {
                "PAINT PACK": "P01 = Paint pack vent",
                "F55334::PAINT PACK": "P02 = Truck-specific body vent",
                "PUMPHOUSE": "P77 = Pump slot",
            },
        )

    def test_resolve_punch_code_text_prefers_truck_specific_entry(self) -> None:
        punch_codes = {
            "PAINT PACK": "P01 = Shared paint pack vent",
            "F55334::PAINT PACK": "P99 = Truck-specific vent",
        }

        self.assertEqual(
            resolve_punch_code_text(punch_codes, "F55334", "PAINT PACK"),
            "P99 = Truck-specific vent",
        )
        self.assertEqual(
            resolve_punch_code_text(punch_codes, "F55335", "PAINT PACK"),
            "P01 = Shared paint pack vent",
        )

    def test_materialize_legacy_punch_codes_expands_shared_value_to_other_trucks(self) -> None:
        punch_codes = materialize_legacy_punch_codes_for_kit(
            {
                "PAINT PACK": "P01 = Shared paint pack vent",
                "F55334::PAINT PACK": "P99 = Truck-specific vent",
            },
            ["F55334", "F55335"],
            "PAINT PACK",
        )

        self.assertEqual(
            punch_codes,
            {
                "F55334::PAINT PACK": "P99 = Truck-specific vent",
                "F55335::PAINT PACK": "P01 = Shared paint pack vent",
            },
        )

    def test_canonicalize_notes_map_uses_canonical_kit_name(self) -> None:
        notes = canonicalize_notes_by_kit(
            {
                "BODY": "General body note",
                "f55334::BODY": "Truck-specific body note",
                "PAINT PACK": "General paint note",
                "bad-value::PUMPHOUSE": "ignore",
                "": "ignore",
            },
            ["BODY | PAINT PACK", "PUMPHOUSE"],
        )

        self.assertEqual(
            notes,
            {
                "PAINT PACK": "General paint note",
                "F55334::PAINT PACK": "Truck-specific body note",
            },
        )

    def test_canonicalize_client_numbers_by_truck_normalizes_keys(self) -> None:
        client_numbers = canonicalize_client_numbers_by_truck(
            {
                "f55334": "12345",
                "F55335": "  67890  ",
                "bad-value": "ignore",
                "F55336": "   ",
            }
        )

        self.assertEqual(
            client_numbers,
            {
                "F55334": "12345",
                "F55335": "67890",
            },
        )

    def test_filter_kit_statuses_hides_persisted_completed_kits(self) -> None:
        with workspace_tempdir() as temp_root:
            settings = ExplorerSettings(
                release_root=str(temp_root / "release"),
                fabrication_root=str(temp_root / "fab"),
                kit_templates=["BODY | PAINT PACK", "PUMPHOUSE"],
                hidden_kits=[
                    build_hidden_kit_key("F55334", "PAINT PACK"),
                    "bad-value",
                ],
            )

            statuses = [
                create_kit_scaffold("F55334", "BODY", settings).paths,
                create_kit_scaffold("F55334", "PUMPHOUSE", settings).paths,
            ]
            kit_statuses = [
                detect_status_from_paths(paths)
                for paths in statuses
            ]

            visible = filter_kit_statuses(kit_statuses, settings)
            all_statuses = filter_kit_statuses(kit_statuses, settings, show_hidden=True)

            self.assertEqual([status.kit_name for status in visible], ["PUMPHOUSE"])
        self.assertEqual([status.kit_name for status in all_statuses], ["PAINT PACK", "PUMPHOUSE"])
        self.assertTrue(is_hidden_kit("F55334", "PAINT PACK", settings))
        self.assertFalse(is_hidden_kit("F55334", "PUMPHOUSE", settings))

    def test_save_and_load_settings_round_trip_notes_by_kit(self) -> None:
        with workspace_tempdir() as temp_root:
            runtime_dir = temp_root / "runtime"
            settings_path = runtime_dir / "settings.json"
            settings = ExplorerSettings(
                notes_by_kit={
                    "f55334::BODY": "Truck-specific body note",
                    "PAINT PACK": "Shared paint note",
                    "bad-value::PUMPHOUSE": "ignore",
                },
                kit_templates=["BODY | PAINT PACK", "PUMPHOUSE"],
                odd_jobs_by_truck={"f55334": ["Loose Brackets", "loose brackets", ""]},
            )

            with patch("settings_store.RUNTIME_DIR", runtime_dir), patch("settings_store.SETTINGS_PATH", settings_path):
                save_settings(settings)
                loaded = load_settings()

            self.assertEqual(
                loaded.notes_by_kit,
                {
                    "F55334::PAINT PACK": "Truck-specific body note",
                    "PAINT PACK": "Shared paint note",
                },
            )
            self.assertEqual(loaded.odd_jobs_by_truck, {"F55334": ["Loose Brackets"]})

    def test_odd_jobs_are_truck_specific_extra_kit_rows(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
                kit_templates=["BODY | PAINT PACK"],
            )

            self.assertTrue(add_odd_job_to_truck(settings, "F55334", "Loose Brackets"))
            self.assertFalse(add_odd_job_to_truck(settings, "F55334", "Loose Brackets"))
            self.assertEqual(odd_job_names_for_truck("F55334", settings), ["Loose Brackets"])

            statuses = collect_kit_statuses("F55334", settings)

            self.assertEqual([status.kit_name for status in statuses], ["PAINT PACK", "Loose Brackets"])
            odd_status = statuses[-1]
            self.assertTrue(odd_status.rpd_exists)
            self.assertTrue(odd_status.paths.release_kit_dir is not None and odd_status.paths.release_kit_dir.exists())

    def test_odd_job_rejects_canonical_kit_name(self) -> None:
        settings = ExplorerSettings(kit_templates=["BODY | PAINT PACK"])

        with self.assertRaises(ValueError):
            add_odd_job_to_truck(settings, "F55334", "PAINT PACK")

    def test_full_flow_skips_kitter_rf_for_non_paint_pack(self) -> None:
        status = SimpleNamespace(kit_name="PUMPHOUSE", paths=SimpleNamespace(rpd_path=Path("job.rpd")))
        progress: list[str] = []
        inventor = inventor_service.InventorRunResult(
            spreadsheet_path=Path("bom.xlsx"),
            project_dir=Path("."),
            entry_path=Path("inventor_to_radan.bat"),
            moved_paths=(),
            report_path=Path("report.txt"),
            discard_paths=(),
        )
        packet_result = full_flow_service.PacketFlowResult(
            packet_paths=(),
            print_pages=0,
            print_missing=0,
            assembly_pages=0,
            cut_list_pages=0,
        )

        with (
            patch(
                "full_flow_service.run_csv_import_for_status",
                return_value=full_flow_service.CsvImportResult(log_path=Path("import.log"), return_code=0),
            ),
            patch(
                "full_flow_service.run_kitter_rf_assignment_for_project",
                side_effect=AssertionError("RF should not run for non-Paint Pack"),
            ) as rf_mock,
            patch("full_flow_service.build_all_packets_for_status", return_value=packet_result),
        ):
            result = full_flow_service.run_full_flow_after_inventor_review(
                status,
                ExplorerSettings(),
                inventor=inventor,
                runtime_dir=Path("."),
                progress_cb=progress.append,
            )

        self.assertFalse(rf_mock.called)
        self.assertEqual(result.rf_assignment.model_source, "skipped")
        self.assertIn("only runs for PAINT PACK", result.rf_assignment.skipped_reason or "")
        self.assertTrue(any("skipping Kitter RF" in message for message in progress))

    def test_full_flow_runs_kitter_rf_for_paint_pack(self) -> None:
        status = SimpleNamespace(kit_name="PAINT PACK", paths=SimpleNamespace(rpd_path=Path("paint.rpd")))
        inventor = inventor_service.InventorRunResult(
            spreadsheet_path=Path("bom.xlsx"),
            project_dir=Path("."),
            entry_path=Path("inventor_to_radan.bat"),
            moved_paths=(),
            report_path=Path("report.txt"),
            discard_paths=(),
        )
        packet_result = full_flow_service.PacketFlowResult(
            packet_paths=(),
            print_pages=0,
            print_missing=0,
            assembly_pages=0,
            cut_list_pages=0,
        )
        rf_result = full_flow_service.RfAssignmentResult(
            predicted_count=12,
            skipped_count=0,
            model_source="model",
            kit_count=3,
            backup_path=Path("paint.bak"),
        )

        with (
            patch(
                "full_flow_service.run_csv_import_for_status",
                return_value=full_flow_service.CsvImportResult(log_path=Path("import.log"), return_code=0),
            ),
            patch("full_flow_service.run_kitter_rf_assignment_for_project", return_value=rf_result) as rf_mock,
            patch("full_flow_service.build_all_packets_for_status", return_value=packet_result),
        ):
            result = full_flow_service.run_full_flow_after_inventor_review(
                status,
                ExplorerSettings(),
                inventor=inventor,
                runtime_dir=Path("."),
            )

        rf_mock.assert_called_once()
        self.assertEqual(result.rf_assignment.predicted_count, 12)
        self.assertIsNone(result.rf_assignment.skipped_reason)

    def test_full_flow_rf_prepares_kits_without_part_comment_writes(self) -> None:
        with workspace_tempdir() as temp_root:
            project_path = temp_root / "paint.rpd"
            project_path.write_text("<RadanProject />", encoding="utf-8")
            donor_path = temp_root / "donor.sym"
            donor_path.write_text("donor", encoding="utf-8")
            part = SimpleNamespace(
                part="F55334-B-01",
                sym=str(temp_root / "F55334-B-01.sym"),
                kit_label="",
                kit_text="",
                priority="9",
            )
            progress: list[str] = []
            prepare_calls: list[dict[str, object]] = []

            def prepare_kits(parts, **kwargs):
                prepare_calls.append(dict(kwargs))
                self.assertIs(kwargs.get("write_part_kit_comments"), False)
                for row in parts:
                    row.kit_label = str(row.kit_label).strip().upper()
                    row.kit_text = f"{kwargs['kits_dirname']}/{row.kit_label}.sym"
                    row.priority = kwargs["kit_to_priority"].get(row.kit_label, row.priority)
                progress_cb = kwargs.get("progress_cb")
                if progress_cb is not None:
                    progress_cb(0, 1, "Preparing kits")
                    progress_cb(1, 1, "Building kit: BODY")
                return 1

            fake_kit_service = SimpleNamespace(
                prepare_kits=prepare_kits,
                write_rpd_with_backup=lambda tree, parts, *, rpd_path, bak_dirname: str(temp_root / "backup.rpd"),
            )
            fake_modules = {
                "assets": SimpleNamespace(
                    configure_release_mapping=lambda *args, **kwargs: None,
                    resolve_asset_fast=lambda sym, suffix: str(temp_root / f"{sym}{suffix}"),
                ),
                "config": SimpleNamespace(
                    GLOBAL_DATASET_PATH="dataset",
                    RF_MODEL_PATH="model",
                    RF_META_PATH="meta",
                    RF_FEATURES=("feature",),
                    CANON_KITS=["BODY"],
                    BALANCE_KIT="BALANCE",
                    DONOR_TEMPLATE_PATH=str(donor_path),
                    BAK_DIRNAME="_bak",
                    KITS_DIRNAME="_kits",
                    KIT_TO_PRIORITY={"BODY": "2"},
                ),
                "kit_service": fake_kit_service,
                "rf_service": SimpleNamespace(
                    run_rf_suggestions=lambda parts, **kwargs: ([("BODY", 0.91)], "fake-model")
                ),
                "rpd_io": SimpleNamespace(load_rpd=lambda path: (object(), [part], {})),
                "packet_service": SimpleNamespace(),
            }

            with patch("full_flow_service._load_radan_kitter_modules", return_value=fake_modules):
                result = full_flow_service.run_kitter_rf_assignment_for_project(
                    project_path,
                    ExplorerSettings(),
                    progress_cb=progress.append,
                )

            self.assertEqual(result.predicted_count, 1)
            self.assertEqual(result.kit_count, 1)
            self.assertEqual(part.priority, "2")
            self.assertEqual(part.kit_text, "_kits/BODY.sym")
            self.assertEqual(len(prepare_calls), 1)
            self.assertTrue(any("skipping RF kit-label part comments" in message for message in progress))
            self.assertTrue(any("prepare kits 1/1 Building kit: BODY" in message for message in progress))

    def test_full_flow_packet_build_still_inserts_assembly_context(self) -> None:
        status = SimpleNamespace(
            spreadsheet_match=SimpleNamespace(chosen_path=None),
            paths=SimpleNamespace(
                rpd_path=Path("paint.rpd"),
                fabrication_kit_dir=Path("fab"),
            ),
        )
        part = SimpleNamespace(sym="F55334-B-01", part="F55334-B-01")
        context = SimpleNamespace(
            parts=[part],
            resolve_asset_fn=lambda sym, suffix: None,
            assembly_source_pdfs=(),
            cut_list_source_pdfs=(),
        )
        assembly_context = SimpleNamespace(references=(), read_errors=())
        sym_result = SimpleNamespace(updated_count=1)
        print_packet = (Path("print.pdf"), 3, 0)
        assembly_result = SimpleNamespace(packet_path=Path("assembly.pdf"), output_pages=2)
        cut_list_result = SimpleNamespace(packet_path=Path("cut.pdf"), output_pages=1)

        with (
            patch.object(Path, "exists", return_value=True),
            patch("full_flow_service.prepare_packet_build_context", return_value=context),
            patch("full_flow_service.validate_print_packet_readiness", return_value=""),
            patch(
                "full_flow_service._load_radan_kitter_modules",
                return_value={"packet_service": SimpleNamespace(build_packet=lambda *args, **kwargs: print_packet)},
            ),
            patch("full_flow_service.scan_assembly_bom_context", return_value=assembly_context),
            patch("full_flow_service.apply_assembly_context_to_sym_comments", return_value=sym_result) as sym_mock,
            patch("full_flow_service.write_assembly_bom_context_csv", return_value=Path("assembly_context.csv")),
            patch("full_flow_service.build_assembly_packet", return_value=assembly_result),
            patch("full_flow_service.build_cut_list_packet", return_value=cut_list_result),
        ):
            result = full_flow_service.build_all_packets_for_status(status, ExplorerSettings())

        sym_mock.assert_called_once()
        self.assertEqual(result.sym_comment_updated_count, 1)

    def test_full_flow_import_progress_prefers_log_detail(self) -> None:
        with workspace_tempdir() as temp_root:
            log_path = temp_root / "import.log"
            log_path.write_text(
                "\n".join(
                    [
                        "[10:00:00] helper started",
                        "{",
                        '  "part_count": 3,',
                        "[10:00:01] Converted 2/3: F55334-B-02 (123 bytes, 1.2s)",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                full_flow_service.latest_import_progress_message(log_path),
                "Converted 2/3: F55334-B-02 (123 bytes, 1.2s)",
            )

    def test_headless_nester_drg_change_detection_ignores_old_files(self) -> None:
        old_path = Path(r"C:\jobs\old.drg")
        updated_path = Path(r"C:\jobs\updated.drg")
        new_path = Path(r"C:\jobs\new.drg")
        before = {
            os.path.normcase(str(old_path.resolve())): (100, 10),
            os.path.normcase(str(updated_path.resolve())): (100, 10),
        }
        after = {
            os.path.normcase(str(old_path.resolve())): (100, 10),
            os.path.normcase(str(updated_path.resolve())): (200, 10),
            os.path.normcase(str(new_path.resolve())): (50, 5),
        }

        changed = full_flow_service.changed_drg_paths(before, after)

        self.assertEqual(
            [path.name for path in changed],
            ["new.drg", "updated.drg"],
        )

    def test_radan_kitter_imports_ignore_preloaded_generic_modules(self) -> None:
        fake_config = ModuleType("config")
        fake_assets = ModuleType("assets")
        original_config = sys.modules.get("config")
        original_assets = sys.modules.get("assets")
        sys.modules["config"] = fake_config
        sys.modules["assets"] = fake_assets
        try:
            modules = full_flow_service._load_radan_kitter_modules(ExplorerSettings())
        finally:
            if original_config is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = original_config
            if original_assets is None:
                sys.modules.pop("assets", None)
            else:
                sys.modules["assets"] = original_assets

        self.assertIsNot(modules["config"], fake_config)
        self.assertIsNot(modules["assets"], fake_assets)
        self.assertIn("radan_kitter", str(getattr(modules["config"], "__file__", "")))
        self.assertIn("radan_kitter", str(getattr(modules["assets"], "__file__", "")))

    def test_full_flow_controller_action_lock_restores_exact_states(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget, QWidget
        from PySide6.QtWidgets import QAbstractItemView
        from controllers.full_flow_controller import _ActionLock

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        full_flow_button = QPushButton("Run Full Flow", window)
        enabled_button = QPushButton("Enabled", window)
        disabled_button = QPushButton("Disabled", window)
        disabled_button.setEnabled(False)
        table = QTableWidget(window)
        original_triggers = QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        table.setEditTriggers(original_triggers)
        lock = _ActionLock(
            widgets=(full_flow_button, enabled_button, disabled_button),
            editable_table=table,
            full_flow_button=full_flow_button,
        )
        try:
            lock.acquire()
            self.assertFalse(full_flow_button.isEnabled())
            self.assertFalse(enabled_button.isEnabled())
            self.assertFalse(disabled_button.isEnabled())
            self.assertEqual(full_flow_button.text(), "Running Full Flow...")
            self.assertEqual(table.editTriggers(), QAbstractItemView.NoEditTriggers)

            enabled_button.setEnabled(True)
            table.setEditTriggers(QAbstractItemView.EditKeyPressed)
            lock.reapply()
            self.assertFalse(enabled_button.isEnabled())
            self.assertEqual(table.editTriggers(), QAbstractItemView.NoEditTriggers)

            lock.release()
            lock.release()
            self.assertTrue(full_flow_button.isEnabled())
            self.assertTrue(enabled_button.isEnabled())
            self.assertFalse(disabled_button.isEnabled())
            self.assertEqual(full_flow_button.text(), "Run Full Flow")
            self.assertEqual(table.editTriggers(), original_triggers)
        finally:
            window.close()
            app.processEvents()

    def test_full_flow_controller_double_run_guard_cleanup_and_close_state(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableWidget, QWidget
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.full_flow_button = QPushButton("Run Full Flow", window)
        table = QTableWidget(window)
        controller = FullFlowController(
            window,
            mutating_widgets=(window.full_flow_button,),
            editable_table=table,
        )
        status = SimpleNamespace(
            kit_name="PAINT PACK",
            paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
        )
        controller._context = FullFlowRunContext(1, status, False, FullFlowPhase.INVENTOR)
        try:
            self.assertFalse(controller.can_close())
            with patch.object(QMessageBox, "information") as info_mock:
                controller.start_selected()
            info_mock.assert_called_once()
            self.assertTrue(controller._finish_run(1, "done"))
            self.assertFalse(controller._finish_run(1, "done again"))
            self.assertTrue(controller.can_close())
        finally:
            window.close()
            app.processEvents()

    def test_main_window_close_event_blocks_while_full_flow_active(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from controllers.full_flow_controller import FullFlowPhase, FullFlowRunContext
        import main_window

        app = QApplication.instance() or QApplication([])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=ExplorerSettings()),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            event = SimpleNamespace(ignored=False, ignore=lambda: setattr(event, "ignored", True))
            status = SimpleNamespace(
                kit_name="PAINT PACK",
                paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
            )
            try:
                window.full_flow_controller._context = FullFlowRunContext(1, status, False, FullFlowPhase.INVENTOR)
                with patch.object(QMessageBox, "warning") as warning_mock:
                    window.closeEvent(event)
                warning_mock.assert_called_once()
                self.assertTrue(event.ignored)
            finally:
                window.full_flow_controller._context = None
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_truck_switch_stale_status_and_flow_results_are_ignored(self) -> None:
        from PySide6.QtWidgets import QApplication
        import main_window

        app = QApplication.instance() or QApplication([])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=ExplorerSettings()),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F11111")
                window.truck_list.addItem("F22222")
                window.truck_list.setCurrentRow(1)
                window.truck_list.blockSignals(False)
                window._active_truck_switch = main_window.TruckSwitchRunContext("F22222", 2)
                window._truck_switch_run_id = 2
                sentinel_statuses = [object()]
                window._all_statuses = list(sentinel_statuses)
                window._current_flow_truck_insight = FlowTruckInsight(
                    available=True,
                    truck_number="F22222",
                    summary_text="current",
                )

                reset_performance_metrics()
                status_future: Future[list[object]] = Future()
                status_future.set_result([])
                window._pending_status_by_truck["f11111"] = (
                    "F11111",
                    window._settings_signature(),
                    1,
                    status_future,
                )
                window._poll_pending_status_future()

                flow_future: Future[FlowTruckInsight] = Future()
                flow_future.set_result(
                    FlowTruckInsight(
                        available=True,
                        truck_number="F11111",
                        summary_text="stale",
                    )
                )
                window._pending_flow_by_truck["f11111"] = ("F11111", "old-token", 1, flow_future)
                window._poll_pending_flow_future()

                self.assertEqual(window._all_statuses, sentinel_statuses)
                self.assertEqual(window._current_flow_truck_insight.truck_number, "F22222")
                self.assertEqual(performance_snapshot().stale_results_ignored, 2)
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_flow_completion_updates_status_cells_without_repopulating_table(self) -> None:
        from PySide6.QtWidgets import QApplication
        import main_window

        app = QApplication.instance() or QApplication([])
        settings = ExplorerSettings(kit_templates=["PAINT PACK"])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=settings),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            status = KitStatus(
                kit_name="PAINT PACK",
                paths=KitPaths(
                    truck_number="F55334",
                    display_name="F55334 PAINT PACK",
                    kit_name="PAINT PACK",
                    fabrication_relative_path="PAINT PACK",
                    project_name="F55334 PAINT PACK",
                    release_truck_dir=None,
                    release_kit_dir=None,
                    project_dir=None,
                    rpd_path=None,
                    support_dirs=(),
                    fabrication_truck_dir=None,
                    fabrication_kit_dir=None,
                ),
                release_folder_exists=True,
                project_folder_exists=False,
                rpd_exists=False,
                rpd_size_bytes=0,
                fabrication_folder_exists=True,
                fabrication_has_files=True,
                spreadsheet_match=SpreadsheetMatch(chosen_path=None, candidates=()),
                preview_pdf_match=PdfMatch(chosen_path=None, candidates=()),
                inventor_outputs=None,
                status_summary="Released",
            )
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F55334")
                window.truck_list.setCurrentRow(0)
                window.truck_list.blockSignals(False)
                window._current_flow_truck_insight = FlowTruckInsight(
                    available=False,
                    truck_number="F55334",
                    summary_text="Flow: loading...",
                )
                window._set_current_statuses([status], cache=False)
                self.assertEqual(window.kit_table.rowCount(), 1)
                self.assertEqual(window.kit_table.item(0, window.RELEASE_COLUMN).text(), "Released")

                flow_future: Future[FlowTruckInsight] = Future()
                flow_future.set_result(
                    FlowTruckInsight(
                        available=True,
                        truck_number="F55334",
                        summary_text="Flow: ready",
                        kit_insights_by_flow_name={
                            "body": FlowKitInsight(
                                flow_kit_name="Body",
                                display_text="Complete",
                                status_key="green",
                            )
                        },
                    )
                )
                window._active_truck_switch = main_window.TruckSwitchRunContext("F55334", 1)
                window._truck_switch_run_id = 1
                window._pending_flow_by_truck["f55334"] = ("F55334", flow_probe_cache_token(), 1, flow_future)

                with patch.object(window, "_populate_status_row", wraps=window._populate_status_row) as populate_mock:
                    window._poll_pending_flow_future()

                populate_mock.assert_not_called()
                self.assertEqual(window.kit_table.rowCount(), 1)
                self.assertEqual(window.kit_table.item(0, window.RELEASE_COLUMN).text(), "Complete")
                self.assertEqual(window.kit_table.item(0, window.FLOW_COLUMN).text(), "Complete")
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_same_truck_reselection_keeps_existing_table_rows(self) -> None:
        from PySide6.QtWidgets import QApplication
        import main_window

        app = QApplication.instance() or QApplication([])
        settings = ExplorerSettings(kit_templates=["PAINT PACK"])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=settings),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            status = KitStatus(
                kit_name="PAINT PACK",
                paths=KitPaths(
                    truck_number="F55334",
                    display_name="F55334 PAINT PACK",
                    kit_name="PAINT PACK",
                    fabrication_relative_path="PAINT PACK",
                    project_name="F55334 PAINT PACK",
                    release_truck_dir=None,
                    release_kit_dir=None,
                    project_dir=None,
                    rpd_path=None,
                    support_dirs=(),
                    fabrication_truck_dir=None,
                    fabrication_kit_dir=None,
                ),
                release_folder_exists=True,
                project_folder_exists=False,
                rpd_exists=False,
                rpd_size_bytes=0,
                fabrication_folder_exists=True,
                fabrication_has_files=True,
                spreadsheet_match=SpreadsheetMatch(chosen_path=None, candidates=()),
                preview_pdf_match=PdfMatch(chosen_path=None, candidates=()),
                inventor_outputs=None,
                status_summary="Released",
            )
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F55334")
                window.truck_list.setCurrentRow(0)
                window.truck_list.blockSignals(False)
                window._current_flow_truck_insight = FlowTruckInsight(
                    available=False,
                    truck_number="F55334",
                    summary_text="Flow: loading...",
                )
                window._set_current_statuses([status], cache=False)
                window._status_cache_by_truck.set(window._status_cache_key("F55334"), [status])

                with (
                    patch.object(window, "_load_flow_for_truck", return_value=True),
                    patch.object(window, "_populate_status_row", wraps=window._populate_status_row) as populate_mock,
                ):
                    window._on_truck_changed()

                populate_mock.assert_not_called()
                self.assertEqual(window.kit_table.rowCount(), 1)
                self.assertEqual(window.kit_table.item(0, 0).text(), "F55334 PAINT PACK")
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def _make_packet_button_status(self, temp_root: Path, *, create_packets: bool = False) -> KitStatus:
        project_dir = temp_root / "release" / "F55334" / "PAINT PACK"
        fabrication_dir = temp_root / "fab" / "F55334" / "PAINT PACK"
        project_dir.mkdir(parents=True, exist_ok=True)
        fabrication_dir.mkdir(parents=True, exist_ok=True)
        rpd_path = project_dir / "F55334 PAINT PACK.rpd"
        rpd_path.write_text("<rpd />", encoding="utf-8")
        if create_packets:
            (project_dir / "PrintPacket_QTY_20260417_101500.pdf").write_text("pdf", encoding="utf-8")
            (project_dir / "AssemblyPacket_TABLOID_20260417_101500.pdf").write_text("pdf", encoding="utf-8")
            (project_dir / "CutList_20260417_101500.pdf").write_text("pdf", encoding="utf-8")
        return KitStatus(
            kit_name="PAINT PACK",
            paths=KitPaths(
                truck_number="F55334",
                display_name="F55334 PAINT PACK",
                kit_name="PAINT PACK",
                fabrication_relative_path="PAINT PACK",
                project_name="F55334 PAINT PACK",
                release_truck_dir=project_dir.parent,
                release_kit_dir=project_dir,
                project_dir=project_dir,
                rpd_path=rpd_path,
                support_dirs=(),
                fabrication_truck_dir=fabrication_dir.parent,
                fabrication_kit_dir=fabrication_dir,
            ),
            release_folder_exists=True,
            project_folder_exists=True,
            rpd_exists=True,
            rpd_size_bytes=rpd_path.stat().st_size,
            fabrication_folder_exists=True,
            fabrication_has_files=True,
            spreadsheet_match=SpreadsheetMatch(chosen_path=None, candidates=()),
            preview_pdf_match=PdfMatch(chosen_path=None, candidates=()),
            inventor_outputs=None,
            status_summary="Released",
        )

    def test_packet_build_buttons_disable_when_packets_already_exist(self) -> None:
        from PySide6.QtWidgets import QApplication
        import main_window

        app = QApplication.instance() or QApplication([])
        settings = ExplorerSettings(kit_templates=["PAINT PACK"])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=settings),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            status = self._make_packet_button_status(temp_root, create_packets=True)
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F55334")
                window.truck_list.setCurrentRow(0)
                window.truck_list.blockSignals(False)
                window._set_current_statuses([status], cache=False)

                self.assertFalse(window.build_print_packet_button.isEnabled())
                self.assertFalse(window.build_assembly_packet_button.isEnabled())
                self.assertFalse(window.build_cut_list_button.isEnabled())
                self.assertEqual(window.build_print_packet_button.text(), "Print Packet Ready")
                self.assertEqual(window.build_assembly_packet_button.text(), "Assembly Packet Ready")
                self.assertEqual(window.build_cut_list_button.text(), "Cut List Ready")
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_packet_build_handlers_skip_existing_packets_before_prepare(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox
        import main_window

        app = QApplication.instance() or QApplication([])
        settings = ExplorerSettings(kit_templates=["PAINT PACK"])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=settings),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            status = self._make_packet_button_status(temp_root, create_packets=True)
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F55334")
                window.truck_list.setCurrentRow(0)
                window.truck_list.blockSignals(False)
                window._set_current_statuses([status], cache=False)

                with (
                    patch("main_window.prepare_packet_build_context") as prepare_mock,
                    patch.object(QMessageBox, "information") as information_mock,
                ):
                    window.build_selected_print_packet()
                    window.build_selected_assembly_packet()
                    window.build_selected_cut_list_packet()

                prepare_mock.assert_not_called()
                self.assertEqual(information_mock.call_count, 3)
                self.assertFalse(window._packet_build_running)
                self.assertFalse(window._active_packet_build_keys)
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_packet_build_guard_blocks_second_click_until_released(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox
        import main_window

        app = QApplication.instance() or QApplication([])
        settings = ExplorerSettings(kit_templates=["PAINT PACK"])
        with (
            workspace_tempdir() as temp_root,
            patch("main_window.load_settings", return_value=settings),
            patch("main_window.QTimer.singleShot", lambda *args, **kwargs: None),
        ):
            window = main_window.MainWindow(runtime_dir=temp_root)
            status = self._make_packet_button_status(temp_root, create_packets=False)
            try:
                window.truck_list.blockSignals(True)
                window.truck_list.addItem("F55334")
                window.truck_list.setCurrentRow(0)
                window.truck_list.blockSignals(False)
                window._set_current_statuses([status], cache=False)

                guard = window._begin_packet_build_guard("Build Print Packet", status, "print")
                self.assertIsNotNone(guard)
                self.assertTrue(window._packet_build_running)
                self.assertFalse(window.build_print_packet_button.isEnabled())

                with patch.object(QMessageBox, "information") as information_mock:
                    blocked_guard = window._begin_packet_build_guard("Build Print Packet", status, "print")

                self.assertIsNone(blocked_guard)
                information_mock.assert_called_once()
                window._finish_packet_build_guard(guard)
                self.assertFalse(window._packet_build_running)
                self.assertTrue(window.build_print_packet_button.isEnabled())
            finally:
                window._truck_executor.shutdown(wait=False, cancel_futures=True)
                window._status_executor.shutdown(wait=False, cancel_futures=True)
                window._flow_executor.shutdown(wait=False, cancel_futures=True)
                window.close()
                app.processEvents()

    def test_full_flow_controller_accepted_review_continues_to_post_review(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget, QWidget
        from controllers import full_flow_controller
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext
        from dialogs.inventor_report_review_dialog import InventorReviewOutcome

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.full_flow_button = QPushButton("Run Full Flow", window)
        controller = FullFlowController(window, mutating_widgets=(window.full_flow_button,), editable_table=QTableWidget(window))
        status = SimpleNamespace(
            kit_name="PAINT PACK",
            paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
        )
        inventor = inventor_service.InventorRunResult(
            spreadsheet_path=Path("bom.xlsx"),
            project_dir=Path("."),
            entry_path=Path("inventor_to_radan.bat"),
            moved_paths=(),
            report_path=Path("report.txt"),
            discard_paths=(),
        )
        context = FullFlowRunContext(1, status, False, FullFlowPhase.REPORT_REVIEW, inventor_result=inventor)
        controller._context = context
        try:
            with (
                patch(
                    "controllers.full_flow_controller.review_inventor_result",
                    return_value=InventorReviewOutcome(state=full_flow_controller.InventorReviewState.ACCEPTED),
                ),
                patch.object(controller, "_start_post_review") as start_mock,
            ):
                controller._review_report(context)
            start_mock.assert_called_once_with(context)
            self.assertTrue(controller.is_running)
        finally:
            window.close()
            app.processEvents()

    def test_full_flow_controller_discarded_and_cancelled_review_stop(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableWidget, QWidget
        from controllers import full_flow_controller
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext
        from dialogs.inventor_report_review_dialog import InventorReviewOutcome

        app = QApplication.instance() or QApplication([])

        def make_controller():
            window = QWidget()
            window.full_flow_button = QPushButton("Run Full Flow", window)
            window._queue_status_refresh_for_truck = lambda truck_number: True
            window.log = lambda message: None
            controller = FullFlowController(
                window,
                mutating_widgets=(window.full_flow_button,),
                editable_table=QTableWidget(window),
            )
            status = SimpleNamespace(
                kit_name="PAINT PACK",
                paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
            )
            inventor = inventor_service.InventorRunResult(
                spreadsheet_path=Path("bom.xlsx"),
                project_dir=Path("."),
                entry_path=Path("inventor_to_radan.bat"),
                moved_paths=(),
                report_path=Path("report.txt"),
                discard_paths=(),
            )
            context = FullFlowRunContext(1, status, False, FullFlowPhase.REPORT_REVIEW, inventor_result=inventor)
            controller._context = context
            return window, controller, context

        for outcome in (
            InventorReviewOutcome(
                state=full_flow_controller.InventorReviewState.DISCARDED,
                discard_result=SimpleNamespace(deleted_paths=(Path("report.txt"),), failed_deletes=()),
            ),
            InventorReviewOutcome(
                state=full_flow_controller.InventorReviewState.CANCELLED,
                message="review cancelled",
            ),
        ):
            window, controller, context = make_controller()
            try:
                with (
                    patch("controllers.full_flow_controller.review_inventor_result", return_value=outcome),
                    patch.object(controller, "_start_post_review", side_effect=AssertionError("must not continue")),
                    patch.object(QMessageBox, "information"),
                    patch.object(QMessageBox, "critical"),
                    patch.object(QMessageBox, "warning"),
                ):
                    controller._review_report(context)
                self.assertFalse(controller.is_running)
            finally:
                window.close()
                app.processEvents()

    def test_full_flow_controller_stale_signals_are_ignored(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableWidget, QWidget
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.full_flow_button = QPushButton("Run Full Flow", window)
        controller = FullFlowController(window, mutating_widgets=(window.full_flow_button,), editable_table=QTableWidget(window))
        status = SimpleNamespace(
            kit_name="PAINT PACK",
            paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
        )
        controller._context = FullFlowRunContext(2, status, False, FullFlowPhase.INVENTOR)
        try:
            with patch.object(QMessageBox, "critical") as critical_mock:
                controller._on_worker_error(1, "stale traceback")
                controller._on_inventor_done(1, {"state": "error", "message": "stale error"})
                controller._on_post_review_done(1, {"state": "error", "message": "stale error"})
            critical_mock.assert_not_called()
            self.assertTrue(controller.is_running)
            self.assertEqual(controller._context.run_id, 2)
        finally:
            window.close()
            app.processEvents()

    def test_full_flow_controller_user_action_and_worker_errors_unlock_cleanly(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableWidget, QWidget
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext, _ActionLock

        app = QApplication.instance() or QApplication([])

        def locked_controller(run_id: int):
            window = QWidget()
            window.full_flow_button = QPushButton("Run Full Flow", window)
            other_button = QPushButton("Other", window)
            table = QTableWidget(window)
            controller = FullFlowController(
                window,
                mutating_widgets=(window.full_flow_button, other_button),
                editable_table=table,
            )
            status = SimpleNamespace(
                kit_name="PAINT PACK",
                paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=Path("paint.rpd")),
            )
            controller._context = FullFlowRunContext(run_id, status, False, FullFlowPhase.INVENTOR)
            lock = _ActionLock(
                widgets=(window.full_flow_button, other_button),
                editable_table=table,
                full_flow_button=window.full_flow_button,
            )
            lock.acquire()
            controller._action_lock = lock
            return window, controller, other_button

        window, controller, other_button = locked_controller(1)
        try:
            with patch.object(QMessageBox, "information") as info_mock:
                controller._on_inventor_done(1, {"state": "needs_user_action", "message": "needs input"})
            info_mock.assert_called_once()
            self.assertFalse(controller.is_running)
            self.assertTrue(window.full_flow_button.isEnabled())
            self.assertTrue(other_button.isEnabled())
            self.assertEqual(window.full_flow_button.text(), "Run Full Flow")
        finally:
            window.close()
            app.processEvents()

        window, controller, other_button = locked_controller(2)
        try:
            with patch.object(QMessageBox, "critical") as critical_mock:
                controller._on_worker_error(2, "traceback")
            critical_mock.assert_called_once()
            self.assertFalse(controller.is_running)
            self.assertTrue(window.full_flow_button.isEnabled())
            self.assertTrue(other_button.isEnabled())
            self.assertEqual(window.full_flow_button.text(), "Run Full Flow")
        finally:
            window.close()
            app.processEvents()

    def test_full_flow_controller_nester_failure_still_opens_project(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTableWidget, QWidget
        from controllers.full_flow_controller import FullFlowController, FullFlowPhase, FullFlowRunContext

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.full_flow_button = QPushButton("Run Full Flow", window)
        window._queue_status_refresh_for_truck = lambda truck_number: True
        window.log = lambda message: None
        controller = FullFlowController(window, mutating_widgets=(window.full_flow_button,), editable_table=QTableWidget(window))
        project_path = Path("paint.rpd")
        status = SimpleNamespace(
            kit_name="PAINT PACK",
            paths=SimpleNamespace(display_name="F55334 PAINT PACK", truck_number="F55334", rpd_path=project_path),
        )
        inventor = inventor_service.InventorRunResult(
            spreadsheet_path=Path("bom.xlsx"),
            project_dir=Path("."),
            entry_path=Path("inventor_to_radan.bat"),
            moved_paths=(Path("paint.csv"),),
            report_path=Path("report.txt"),
            discard_paths=(),
        )
        result = full_flow_service.FullFlowResult(
            project_path=project_path,
            inventor=inventor,
            csv_import=full_flow_service.CsvImportResult(log_path=Path("import.log"), return_code=0),
            rf_assignment=full_flow_service.RfAssignmentResult(
                predicted_count=4,
                skipped_count=0,
                model_source="model",
                kit_count=1,
                backup_path=None,
            ),
            packets=full_flow_service.PacketFlowResult(
                packet_paths=(),
                print_pages=1,
                print_missing=0,
                assembly_pages=2,
                cut_list_pages=3,
            ),
        )
        context = FullFlowRunContext(
            1,
            status,
            True,
            FullFlowPhase.NESTER,
            inventor_result=inventor,
            full_flow_result=result,
        )
        controller._context = context
        try:
            with (
                patch("controllers.full_flow_controller.open_path") as open_mock,
                patch.object(QMessageBox, "warning"),
                patch.object(QMessageBox, "information"),
            ):
                controller._on_nester_done(1, {"state": "error", "message": "nester boom"})
            open_mock.assert_called_once_with(project_path)
            self.assertFalse(controller.is_running)
        finally:
            window.close()
            app.processEvents()

    def test_inventor_controller_restores_button_state_after_user_action_stop(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget
        from controllers.inventor_controller import InventorController

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.launch_inventor_button = QPushButton("Run Inventor Tool", window)
        window.settings = ExplorerSettings()
        window._current_status = lambda: SimpleNamespace(paths=SimpleNamespace(truck_number="F55334"))
        window._ensure_saved_settings = lambda: None
        window.current_truck_number = lambda: "F55334"
        window._set_current_statuses = lambda statuses: None
        window.log = lambda message: None
        controller = InventorController(window)
        try:
            with (
                patch.object(InventorController, "_start_worker", lambda self, worker: None),
                patch.object(QMessageBox, "information") as info_mock,
            ):
                controller.start_selected()
                self.assertFalse(window.launch_inventor_button.isEnabled())
                self.assertEqual(window.launch_inventor_button.text(), "Running Inventor...")

                controller._on_worker_done({"state": "needs_user_action", "message": "needs operator input"})

            self.assertTrue(window.launch_inventor_button.isEnabled())
            self.assertEqual(window.launch_inventor_button.text(), "Run Inventor Tool")
            info_mock.assert_called_once()
        finally:
            window.close()
            app.processEvents()

    def test_inventor_watcher_path_symbols_are_absent(self) -> None:
        text = (PROJECT_DIR / "main_window.py").read_text(encoding="utf-8")

        for symbol in (
            "PendingInventorJob",
            "_pending_inventor_job",
            "_inventor_watch_timer",
            "_inventor_output_signature",
            "_poll_pending_inventor_job",
            "_finish_pending_inventor_job",
            "Watching Inventor",
            "output watch",
        ):
            self.assertNotIn(symbol, text)
        self.assertNotIn("run_inventor_to_radan_inline", text)
        self.assertNotIn("move_inventor_outputs_to_project", text)

    def test_full_flow_main_window_symbols_are_absent(self) -> None:
        text = (PROJECT_DIR / "main_window.py").read_text(encoding="utf-8")

        for symbol in (
            "_full_flow_running",
            "_full_flow_worker",
            "_full_flow_disabled_widget_states",
            "_full_flow_table_edit_triggers",
            "_full_flow_mutating_widgets",
            "_lock_full_flow_actions",
            "_reapply_full_flow_action_lock",
            "_unlock_full_flow_actions",
            "_start_full_flow_worker",
            "_create_full_flow_progress_dialog",
            "_confirm_close_radan_for_full_flow",
            "run_full_flow_after_inventor_review",
            "run_headless_nester",
            "InventorNeedsUserAction",
            "InventorServiceError",
            "run_inventor_for_status",
            "review_inventor_result",
        ):
            self.assertNotIn(symbol, text)

        self.assertIn("self.full_flow_controller.start_selected()", text)

    def test_generic_background_worker_is_used(self) -> None:
        import main_window
        from controllers import full_flow_controller, inventor_controller

        self.assertIs(main_window.BackgroundJobWorker, background_job.BackgroundJobWorker)
        self.assertIs(inventor_controller.BackgroundJobWorker, background_job.BackgroundJobWorker)
        self.assertIs(full_flow_controller.BackgroundJobWorker, background_job.BackgroundJobWorker)
        text = (PROJECT_DIR / "main_window.py").read_text(encoding="utf-8")
        self.assertNotIn("PacketJobWorker", text)
        self.assertNotIn("PacketJobSignals", text)

    def test_phase_3_obsolete_symbols_are_absent_from_production_source(self) -> None:
        obsolete_symbols = (
            "run_inventor_inline_for_status",
            "_prepare_full_flow_kits_without_" + "attr" + "109",
            "run_full_flow_before_nester",
            "_create_full_flow_progress_dialog",
            "_start_full_flow_worker",
            "_review_full_flow_inventor_report",
            "PendingInventorJob",
            "PacketJobSignals",
            "PacketJobWorker",
        )
        forbidden_workflow_tokens = ("attr" + "109", "attr_" + "109")
        production_files = [
            path
            for path in PROJECT_DIR.rglob("*.py")
            if "tests" not in path.parts
            and "docs" not in path.parts
            and "__pycache__" not in path.parts
        ]

        for path in production_files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for symbol in obsolete_symbols:
                    self.assertNotIn(symbol, text)
                lowered = text.casefold()
                for token in forbidden_workflow_tokens:
                    self.assertNotIn(token, lowered)


def detect_status_from_paths(paths):
    return type(
        "StatusStub",
        (),
        {
            "kit_name": paths.kit_name,
            "paths": paths,
        },
    )()


if __name__ == "__main__":
    unittest.main()
