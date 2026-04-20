from __future__ import annotations

import base64
import sys
from types import SimpleNamespace
import unittest
from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid
from unittest.mock import patch

import fitz

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

RADAN_DIR = PROJECT_DIR.parent / "radan_kitter"
if str(RADAN_DIR) not in sys.path:
    sys.path.insert(0, str(RADAN_DIR))

import rpd_io  # type: ignore[import-not-found]
from flow_bridge import (
    flow_kit_insight_for_explorer_kit,
    map_explorer_kit_to_flow_kit,
    normalize_flow_insight_for_local_release,
    parse_flow_probe_payload,
)
from flow_schedule_probe import include_kit_in_embedded_gantt, split_overlay_rows_for_embedded_gantt
from models import (
    canonicalize_client_numbers_by_truck,
    canonicalize_notes_by_kit,
    ExplorerSettings,
    build_hidden_kit_key,
    canonicalize_hidden_kit_entries,
    canonicalize_punch_codes_by_kit,
    materialize_legacy_punch_codes_for_kit,
    resolve_punch_code_text,
)
from packet_build_service import build_assembly_packet, prepare_packet_build_context
from settings_store import load_settings, save_settings
from services import (
    build_kit_paths,
    build_launch_command,
    build_kit_status,
    collect_kit_statuses,
    create_kit_scaffold,
    detect_assembly_packet_pdf,
    detect_print_packet_pdf,
    detect_preview_pdf,
    detect_spreadsheet,
    discover_trucks,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    is_hidden_kit,
    is_hidden_truck,
    launch_tool,
    move_inventor_outputs_to_project,
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


class TruckNestExplorerServicesTests(unittest.TestCase):
    def test_default_dashboard_launcher_targets_fabrication_flow_dashboard(self) -> None:
        settings = ExplorerSettings()
        self.assertEqual(settings.dashboard_launcher, r"C:\Tools\fabrication_flow_dashboard\run_app.bat")

    def test_build_launch_command_uses_cmd_for_batch_files(self) -> None:
        command = build_launch_command(r"C:\Tools\fabrication_flow_dashboard\run_app.bat")
        self.assertEqual(command, ["cmd.exe", "/c", r"C:\Tools\fabrication_flow_dashboard\run_app.bat"])

    def test_launch_tool_starts_dashboard_launcher_in_its_own_folder(self) -> None:
        with workspace_tempdir() as temp_dir:
            launcher_path = temp_dir / "run_app.bat"
            launcher_path.write_text("@echo off\r\necho launched\r\n", encoding="utf-8")

            with patch("services.subprocess.Popen") as popen_mock:
                launch_tool(launcher_path)

            popen_mock.assert_called_once_with(
                ["cmd.exe", "/c", str(launcher_path)],
                cwd=str(temp_dir),
            )

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
            self.assertIn("Not released", empty_status.status_summary)
            self.assertNotIn("Spreadsheet missing", empty_status.status_summary)

            released_file = empty_folder / "TruckBom.xlsx"
            released_file.write_text("bom", encoding="utf-8")

            released_status = build_kit_status("F55334", "PAINT PACK", settings)
            self.assertTrue(released_status.fabrication_has_files)
            self.assertIn("Released", released_status.status_summary)
            self.assertIn("Spreadsheet ready", released_status.status_summary)

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
            assembly_pdf = paths.fabrication_kit_dir / "Assembly-Overview.pdf"
            note_pdf = paths.fabrication_kit_dir / "Traveler.pdf"
            sym_path.write_text("sym", encoding="utf-8")
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
            assembly_pdf = paths.project_dir / "Assembly-Overview.pdf"
            sym_path.write_text("sym", encoding="utf-8")
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
            assembly_pdf = paths.fabrication_kit_dir / "Assembly-Overview.pdf"
            generated_out_dir = paths.fabrication_kit_dir / "_out"
            generated_out_dir.mkdir(parents=True)
            generated_print_packet = generated_out_dir / "PrintPacket_QTY_20260417_101500.pdf"
            generated_assembly_packet = generated_out_dir / "AssemblyPacket_TABLOID_20260417_101500.pdf"
            sym_path.write_text("sym", encoding="utf-8")
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
                image_counts = [len(doc[index].get_images(full=True)) for index in range(doc.page_count)]
                darkest_pixels = [min(doc[index].get_pixmap().samples) for index in range(doc.page_count)]
            self.assertEqual(image_counts, [1, 1])
            self.assertLess(darkest_pixels[0], 250)
            self.assertLess(darkest_pixels[1], 250)

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
                self.assertEqual(len(doc[0].get_images(full=True)), 1)
                self.assertLess(min(doc[0].get_pixmap().samples), 250)

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

    def test_discover_trucks_uses_release_root_only(self) -> None:
        with workspace_tempdir() as temp_root:
            release_root = temp_root / "release"
            fabrication_root = temp_root / "fab"
            (release_root / "F55334").mkdir(parents=True)
            (release_root / "Templates").mkdir(parents=True)
            (release_root / "_runtime").mkdir(parents=True)
            (release_root / "F5533").mkdir(parents=True)
            (fabrication_root / "F55335").mkdir(parents=True)

            settings = ExplorerSettings(
                release_root=str(release_root),
                fabrication_root=str(fabrication_root),
            )

            trucks = discover_trucks(settings)

            self.assertEqual(trucks, ["F55334"])

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
