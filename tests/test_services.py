from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

RADAN_DIR = PROJECT_DIR.parent / "radan_kitter"
if str(RADAN_DIR) not in sys.path:
    sys.path.insert(0, str(RADAN_DIR))

import rpd_io  # type: ignore[import-not-found]
from models import (
    ExplorerSettings,
    build_hidden_kit_key,
    canonicalize_hidden_kit_entries,
    canonicalize_punch_codes_by_kit,
)
from services import (
    build_kit_paths,
    copy_inventor_outputs_to_project,
    create_kit_scaffold,
    detect_print_packet_pdf,
    detect_preview_pdf,
    detect_spreadsheet,
    discover_trucks,
    filter_kit_statuses,
    filter_truck_numbers,
    find_fabrication_truck_dir,
    is_hidden_kit,
    is_hidden_truck,
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


class TruckNestExplorerServicesTests(unittest.TestCase):
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

    def test_create_kit_scaffold_clones_template_and_replaces_tokens(self) -> None:
        with workspace_tempdir() as temp_root:
            template_path = temp_root / "blank_template.rpd"
            template_path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://www.radan.com/ns/project">
  <Name>BLANK_PROJECT</Name>
  <Truck>BLANK_TRUCK</Truck>
  <Parts />
</Project>
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

    def test_copy_inventor_outputs_to_project_places_files_in_l_project_folder(self) -> None:
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

            outputs, copied = copy_inventor_outputs_to_project(spreadsheet, l_project)

            self.assertEqual(
                tuple(path.name for path in copied),
                ("TruckBom_Radan.csv", "TruckBom_report.txt"),
            )
            self.assertTrue(outputs.target_csv_path.exists())
            self.assertTrue(outputs.target_report_path.exists())

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
                "PAINT PACK": "P01 = Paint pack vent",
                "PUMPHOUSE": "P77 = Pump slot",
                "": "ignore",
            },
            ["BODY | PAINT PACK", "PUMPHOUSE"],
        )

        self.assertEqual(
            punch_codes,
            {
                "PAINT PACK": "P01 = Paint pack vent",
                "PUMPHOUSE": "P77 = Pump slot",
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
