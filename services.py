from __future__ import annotations

import csv
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from models import (
    DEFAULT_SUPPORT_FOLDERS,
    ExplorerSettings,
    InventorOutputPaths,
    KitMapping,
    KitPaths,
    KitStatus,
    PdfMatch,
    ScaffoldResult,
    SpreadsheetMatch,
    build_hidden_kit_key,
    build_kit_mappings,
    canonicalize_kit_name,
    kit_name_variants,
    normalize_hidden_kit_entries,
    normalize_hidden_truck_entries,
    normalize_truck_order_entries,
)

SUPPORTED_SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".csv"}
IGNORED_SPREADSHEET_PATTERNS = ("_radan.csv",)
SUPPORTED_PREVIEW_SUFFIXES = {".pdf"}
MAX_NEST_SUMMARY_DEPTH = 2
MINIMAL_RPD_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://www.radan.com/ns/project">
  <Name>{project_name}</Name>
  <Parts />
</Project>
"""
DEFAULT_VENV_PYTHON = Path(r"C:\Tools\.venv\Scripts\python.exe")
DEFAULT_RADAN_CSV_IMPORT_ENTRY = Path(r"C:\Tools\radan_automation\import_parts_csv_headless.py")
TRUCK_FOLDER_PATTERN = re.compile(r"^F\d{5}$", re.IGNORECASE)
OWNED_INVENTOR_OUTPUT_SUFFIXES = ("_Radan.csv", "_report.txt")


class InventorToRadanInlineNeedsUi(RuntimeError):
    def __init__(self, message: str, *, missing_dxf_count: int = 0, missing_rule_count: int = 0) -> None:
        self.missing_dxf_count = missing_dxf_count
        self.missing_rule_count = missing_rule_count
        super().__init__(message)


def is_w_drive_path(path: Path | str) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    normalized = text.replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return PureWindowsPath(normalized).drive.casefold() == "w:"


def is_owned_inventor_output(path: Path | str, *, spreadsheet_path: Path | str | None = None) -> bool:
    candidate = Path(str(path))
    if spreadsheet_path is not None:
        spreadsheet = Path(str(spreadsheet_path))
        allowed_names = {
            f"{spreadsheet.stem}_Radan.csv".casefold(),
            f"{spreadsheet.stem}_report.txt".casefold(),
        }
        return candidate.name.casefold() in allowed_names
    name = candidate.name.casefold()
    return name.endswith(tuple(suffix.casefold() for suffix in OWNED_INVENTOR_OUTPUT_SUFFIXES))


def assert_w_drive_write_allowed(
    path: Path | str | None,
    *,
    operation: str,
    allow_owned_inventor_output: bool = False,
    spreadsheet_path: Path | str | None = None,
) -> None:
    if path is None or not is_w_drive_path(path):
        return
    if allow_owned_inventor_output and is_owned_inventor_output(path, spreadsheet_path=spreadsheet_path):
        return
    raise RuntimeError(
        f"Refusing to {operation} on W: path: {path}. "
        "W: is read-only except for moving/deleting Inventor-generated *_Radan.csv and *_report.txt handoff files."
    )


def clean_text(value: object) -> str:
    return str(value or "").strip()


def natural_sort_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def normalize_kit_templates(values: list[str]) -> list[str]:
    return [mapping.display_name for mapping in build_kit_mappings(values)]


def configured_kit_mappings(settings: ExplorerSettings) -> list[KitMapping]:
    return build_kit_mappings(settings.kit_templates)


def is_hidden_truck(truck_number: str, settings: ExplorerSettings) -> bool:
    wanted = clean_text(truck_number).casefold()
    if not wanted:
        return False
    return wanted in {value.casefold() for value in normalize_hidden_truck_entries(settings.hidden_trucks)}


def is_hidden_kit(truck_number: str, kit_name: str, settings: ExplorerSettings) -> bool:
    wanted = build_hidden_kit_key(truck_number, kit_name).casefold()
    if not wanted:
        return False
    return wanted in {value.casefold() for value in normalize_hidden_kit_entries(settings.hidden_kits)}


def filter_truck_numbers(
    truck_numbers: list[str],
    settings: ExplorerSettings,
    *,
    show_hidden: bool = False,
) -> list[str]:
    if show_hidden:
        return list(truck_numbers)
    return [truck_number for truck_number in truck_numbers if not is_hidden_truck(truck_number, settings)]


def sort_truck_numbers_by_fabrication_order(
    truck_numbers: list[str],
    settings: ExplorerSettings,
) -> list[str]:
    configured_order = normalize_truck_order_entries(settings.truck_order)
    order_index = {truck_number.casefold(): index for index, truck_number in enumerate(configured_order)}
    deduped: list[str] = []
    seen: set[str] = set()
    for truck_number in truck_numbers:
        key = clean_text(truck_number).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(truck_number)

    return sorted(
        deduped,
        key=lambda truck_number: (
            0 if truck_number.casefold() in order_index else 1,
            order_index.get(truck_number.casefold(), 0),
            natural_sort_key(truck_number),
        ),
    )


def filter_kit_statuses(
    statuses: list[KitStatus],
    settings: ExplorerSettings,
    *,
    show_hidden: bool = False,
) -> list[KitStatus]:
    if show_hidden:
        return list(statuses)
    return [
        status
        for status in statuses
        if not is_hidden_kit(status.paths.truck_number, status.kit_name, settings)
    ]


def resolve_kit_mapping(kit_name: str, settings: ExplorerSettings) -> KitMapping:
    wanted = canonicalize_kit_name(kit_name)
    for mapping in configured_kit_mappings(settings):
        if mapping.kit_name.casefold() == wanted.casefold():
            return mapping
        if mapping.display_name.casefold() == wanted.casefold():
            return mapping
    return KitMapping(
        display_name=wanted,
        kit_name=wanted,
        fabrication_relative_path=wanted,
    )


def _path_from_setting(value: str) -> Path | None:
    text = clean_text(value)
    if not text:
        return None
    return Path(text)


def _existing_named_child(parent: Path | None, candidate_names: tuple[str, ...], *, want_dir: bool) -> Path | None:
    if parent is None or not parent.exists() or not candidate_names:
        return None

    wanted_by_key = {
        str(name or "").strip().casefold(): str(name or "").strip()
        for name in candidate_names
        if str(name or "").strip()
    }
    if not wanted_by_key:
        return None

    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name.casefold() not in wanted_by_key:
                    continue
                try:
                    if want_dir and not entry.is_dir():
                        continue
                    if not want_dir and not entry.is_file():
                        continue
                except OSError:
                    continue
                return Path(entry.path)
    except OSError:
        return None
    return None


def build_kit_paths(truck_number: str, kit_name: str, settings: ExplorerSettings) -> KitPaths:
    truck_text = clean_text(truck_number)
    mapping = resolve_kit_mapping(kit_name, settings)
    display_text = clean_text(mapping.display_name)
    kit_text = clean_text(mapping.kit_name)
    if not truck_text:
        raise ValueError("Truck number cannot be empty.")
    if not display_text:
        raise ValueError("Dashboard kit name cannot be empty.")
    if not kit_text:
        raise ValueError("Kit name cannot be empty.")

    kit_name_candidates = kit_name_variants(kit_text) or (kit_text,)
    project_name_candidates = tuple(f"{truck_text} {name}".strip() for name in kit_name_candidates)
    project_name = project_name_candidates[0]
    release_root = _path_from_setting(settings.release_root)
    fabrication_root = _path_from_setting(settings.fabrication_root)

    release_truck_dir = release_root / truck_text if release_root else None
    release_kit_dir = release_truck_dir / kit_text if release_truck_dir else None
    existing_release_kit_dir = _existing_named_child(release_truck_dir, kit_name_candidates, want_dir=True)
    if existing_release_kit_dir is not None:
        release_kit_dir = existing_release_kit_dir

    project_dir = release_kit_dir / project_name if release_kit_dir else None
    existing_project_dir = _existing_named_child(release_kit_dir, project_name_candidates, want_dir=True)
    if existing_project_dir is not None:
        project_dir = existing_project_dir
        project_name = existing_project_dir.name

    rpd_path = project_dir / f"{project_name}.rpd" if project_dir else None
    rpd_name_candidates = tuple(f"{name}.rpd" for name in project_name_candidates)
    if project_dir is not None:
        existing_rpd_path = _existing_named_child(
            project_dir,
            (f"{project_name}.rpd",) + rpd_name_candidates,
            want_dir=False,
        )
        if existing_rpd_path is not None:
            rpd_path = existing_rpd_path

    fabrication_truck_dir = fabrication_root / truck_text if fabrication_root else None
    fabrication_relative_path = clean_text(mapping.fabrication_relative_path)
    fabrication_kit_dir = (
        fabrication_truck_dir / Path(fabrication_relative_path)
        if fabrication_truck_dir and fabrication_relative_path
        else fabrication_truck_dir
    )

    support_dirs: tuple[Path, ...] = ()
    if project_dir is not None:
        support_dirs = tuple(project_dir / folder_name for folder_name in DEFAULT_SUPPORT_FOLDERS)

    return KitPaths(
        truck_number=truck_text,
        display_name=display_text,
        kit_name=kit_text,
        fabrication_relative_path=fabrication_relative_path,
        project_name=project_name,
        release_truck_dir=release_truck_dir,
        release_kit_dir=release_kit_dir,
        project_dir=project_dir,
        rpd_path=rpd_path,
        support_dirs=support_dirs,
        fabrication_truck_dir=fabrication_truck_dir,
        fabrication_kit_dir=fabrication_kit_dir,
    )


def discover_trucks(settings: ExplorerSettings) -> list[str]:
    names: set[str] = set()
    root = _path_from_setting(settings.release_root)
    if root is None or not root.exists():
        return []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and TRUCK_FOLDER_PATTERN.fullmatch(entry.name):
                        names.add(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(names, key=natural_sort_key)


def find_fabrication_truck_dir(truck_number: str, settings: ExplorerSettings) -> Path | None:
    wanted = clean_text(truck_number)
    if not wanted:
        return None
    fabrication_root = _path_from_setting(settings.fabrication_root)
    if fabrication_root is None or not fabrication_root.exists():
        return None

    try:
        with os.scandir(fabrication_root) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if entry.name.casefold() == wanted.casefold():
                    return Path(entry.path)
    except OSError:
        return None
    return None


def _is_generated_spreadsheet(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(pattern) for pattern in IGNORED_SPREADSHEET_PATTERNS)


def detect_spreadsheet(folder: Path | None) -> SpreadsheetMatch:
    if folder is None:
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="root_not_configured")
    if not folder.exists():
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="folder_missing")

    discovered: list[Path] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                path = Path(entry.path)
                if path.suffix.casefold() not in SUPPORTED_SPREADSHEET_SUFFIXES:
                    continue
                if _is_generated_spreadsheet(path):
                    continue
                discovered.append(path)
    except OSError:
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="folder_missing")

    candidates = tuple(sorted(discovered, key=lambda path: natural_sort_key(path.name)))
    if len(candidates) == 1:
        return SpreadsheetMatch(chosen_path=candidates[0], candidates=candidates)
    if not candidates:
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="spreadsheet_missing")
    return SpreadsheetMatch(chosen_path=None, candidates=candidates, issue="multiple_spreadsheets")


def parse_replacement_rules(raw_text: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for line in str(raw_text or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=>" not in text:
            raise ValueError(f"Invalid replacement rule: {line}")
        find_text, replace_text = [part.strip() for part in text.split("=>", 1)]
        if not find_text:
            raise ValueError(f"Replacement rule is missing a search value: {line}")
        rules.append((find_text, replace_text))
    return rules


def _render_replacement(template: str, *, truck_number: str, kit_name: str, project_name: str) -> str:
    return template.format(
        truck_number=truck_number,
        kit_name=kit_name,
        project_name=project_name,
        rpd_stem=project_name,
    )


def _decode_template_bytes(data: bytes) -> tuple[str, str]:
    encodings = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252")
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("template", b"", 0, 1, "Unsupported template encoding")


def _write_template_clone(
    template_path: Path,
    output_path: Path,
    *,
    truck_number: str,
    kit_name: str,
    project_name: str,
    replacements_text: str,
) -> str:
    data = template_path.read_bytes()
    text, _encoding = _decode_template_bytes(data)
    rules = parse_replacement_rules(replacements_text)
    for find_text, replace_template in rules:
        rendered = _render_replacement(
            replace_template,
            truck_number=truck_number,
            kit_name=kit_name,
            project_name=project_name,
        )
        text = text.replace(find_text, rendered)

    text = _apply_template_project_defaults(
        text,
        output_path=output_path,
        project_name=project_name,
    )
    text = re.sub(r'encoding="[^"]+"', 'encoding="utf-8"', text, count=1)
    output_path.write_text(text, encoding="utf-8")
    return "template_clone"


def _replace_exact_xml_text(text: str, old_value: str, new_value: str) -> str:
    pattern = rf">(\s*){re.escape(old_value)}(\s*)<"
    return re.sub(pattern, lambda match: f">{match.group(1)}{new_value}{match.group(2)}<", text)


def _replace_xml_element_value(text: str, tag_name: str, new_value: str) -> str:
    pattern = rf"(<{tag_name}>)(.*?)(</{tag_name}>)"
    return re.sub(
        pattern,
        lambda match: f"{match.group(1)}{new_value}{match.group(3)}",
        text,
        flags=re.DOTALL,
    )


def _apply_template_project_defaults(
    text: str,
    *,
    output_path: Path,
    project_name: str,
) -> str:
    project_dir = output_path.parent
    text = _replace_exact_xml_text(text, "Template.rpd", output_path.name)
    text = _replace_exact_xml_text(text, "Template", project_name)
    text = _replace_xml_element_value(text, "JobName", project_name)
    text = _replace_xml_element_value(text, "NestFolder", str(project_dir / "nests"))
    text = _replace_xml_element_value(text, "RemnantSaveFolder", str(project_dir / "remnants"))
    return text


def _clone_template_subfolders(template_root: Path, project_dir: Path) -> tuple[Path, ...]:
    created: list[Path] = []
    for folder in sorted((path for path in template_root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts)):
        relative = folder.relative_to(template_root)
        if not relative.parts:
            continue
        target = project_dir / relative
        if target.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return tuple(created)


def _write_minimal_rpd(output_path: Path, *, project_name: str) -> str:
    output_path.write_text(MINIMAL_RPD_TEMPLATE.format(project_name=project_name), encoding="utf-8")
    return "minimal_placeholder"


def create_kit_scaffold(
    truck_number: str,
    kit_name: str,
    settings: ExplorerSettings,
) -> ScaffoldResult:
    paths = build_kit_paths(truck_number, kit_name, settings)
    if paths.release_truck_dir is None or paths.release_kit_dir is None or paths.project_dir is None:
        raise ValueError("Release root is not configured.")
    if paths.rpd_path is None:
        raise ValueError("Could not build the RPD path.")

    created: list[Path] = []
    notes: list[str] = []

    for folder in (
        paths.release_truck_dir,
        paths.release_kit_dir,
        paths.project_dir,
    ):
        if folder is None:
            continue
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(folder)

    if settings.create_support_folders:
        for folder in paths.support_dirs:
            if not folder.exists():
                folder.mkdir(parents=True, exist_ok=True)
                created.append(folder)

    template_mode = "existing_rpd"
    if not paths.rpd_path.exists():
        template_path = _path_from_setting(settings.rpd_template_path)
        if template_path is not None and template_path.exists():
            template_mode = _write_template_clone(
                template_path,
                paths.rpd_path,
                truck_number=paths.truck_number,
                kit_name=paths.kit_name,
                project_name=paths.project_name,
                replacements_text=settings.template_replacements_text,
            )
            notes.append(f"Cloned template RPD from {template_path}")
            created.extend(_clone_template_subfolders(template_path.parent, paths.project_dir))
        else:
            template_mode = _write_minimal_rpd(paths.rpd_path, project_name=paths.project_name)
            notes.append("Wrote a minimal placeholder RPD because no template file is configured.")
        created.append(paths.rpd_path)

    return ScaffoldResult(
        paths=paths,
        created_paths=tuple(created),
        notes=tuple(notes),
        template_mode=template_mode,
    )


def inventor_output_paths(spreadsheet_path: Path, project_dir: Path | None) -> InventorOutputPaths:
    source_csv_path = spreadsheet_path.with_name(f"{spreadsheet_path.stem}_Radan.csv")
    source_report_path = spreadsheet_path.with_name(f"{spreadsheet_path.stem}_report.txt")
    target_csv_path = project_dir / source_csv_path.name if project_dir is not None else None
    target_report_path = project_dir / source_report_path.name if project_dir is not None else None
    return InventorOutputPaths(
        source_csv_path=source_csv_path,
        source_report_path=source_report_path,
        target_csv_path=target_csv_path,
        target_report_path=target_report_path,
    )


def resolve_existing_inventor_csv(spreadsheet_path: Path, project_dir: Path | None) -> Path:
    outputs = inventor_output_paths(spreadsheet_path, project_dir)
    candidates = [
        path
        for path in (outputs.target_csv_path, outputs.source_csv_path)
        if path is not None
    ]
    for path in candidates:
        if path.exists():
            return path
    expected = "\n".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Inventor-to-RADAN CSV was not found. Expected one of:\n{expected}")


def _normalize_pdf_name_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _shallow_descendant_files(root: Path, *, max_depth: int) -> list[tuple[Path, int]]:
    if not root.exists():
        return []

    discovered: list[tuple[Path, int]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            discovered.append((Path(entry.path), depth))
                            continue
                        if entry.is_dir() and depth < max_depth:
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return discovered


def _collect_preview_pdf_candidates(paths: KitPaths) -> tuple[Path, ...]:
    search_root = paths.release_kit_dir or paths.project_dir
    if search_root is None or not search_root.exists():
        return ()

    rpd_stem = paths.rpd_path.stem if paths.rpd_path is not None else paths.project_name
    expected_stem = _normalize_pdf_name_words(f"{rpd_stem} nest summary")

    candidates: list[tuple[int, Path]] = []
    for child, depth in _shallow_descendant_files(search_root, max_depth=MAX_NEST_SUMMARY_DEPTH):
        if child.suffix.casefold() not in SUPPORTED_PREVIEW_SUFFIXES:
            continue
        child_stem = _normalize_pdf_name_words(child.stem)
        if child_stem != expected_stem:
            continue
        candidates.append((depth, child))

    candidates.sort(key=lambda item: (item[0], natural_sort_key(str(item[1].relative_to(search_root)))))
    return tuple(path for _depth, path in candidates)


def _is_print_packet_pdf(path: Path) -> bool:
    stem_words = _normalize_pdf_name_words(path.stem)
    return (
        stem_words.startswith("print packet")
        or stem_words.startswith("printpacket")
        or " print packet " in f" {stem_words} "
        or " printpacket " in f" {stem_words} "
    )


def _is_assembly_packet_pdf(path: Path) -> bool:
    stem_words = _normalize_pdf_name_words(path.stem)
    return (
        stem_words.startswith("assembly packet")
        or stem_words.startswith("assemblypacket")
        or stem_words.startswith("assembly drawings")
        or stem_words.startswith("assemblydrawings")
        or " assembly packet " in f" {stem_words} "
        or " assemblypacket " in f" {stem_words} "
        or " assembly drawings " in f" {stem_words} "
        or " assemblydrawings " in f" {stem_words} "
    )


def _is_cut_list_packet_pdf(path: Path) -> bool:
    stem_words = _normalize_pdf_name_words(path.stem)
    return (
        stem_words.startswith("cut list")
        or stem_words.startswith("cutlist")
        or " cut list " in f" {stem_words} "
        or " cutlist " in f" {stem_words} "
    )


def _detect_named_packet_pdf(
    paths: KitPaths,
    *,
    matches_fn,
) -> PdfMatch:
    if paths.project_dir is None:
        return PdfMatch(chosen_path=None, candidates=(), issue="project_not_configured")
    if not paths.project_dir.exists():
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")

    search_root = paths.release_kit_dir or paths.project_dir
    if search_root is None or not search_root.exists():
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")

    candidates: list[tuple[int, Path]] = []
    for child, depth in _shallow_descendant_files(search_root, max_depth=MAX_NEST_SUMMARY_DEPTH):
        if child.suffix.casefold() not in SUPPORTED_PREVIEW_SUFFIXES:
            continue
        if not matches_fn(child):
            continue
        candidates.append((depth, child))

    candidates.sort(key=lambda item: (item[0], natural_sort_key(str(item[1].relative_to(search_root)))))
    paths_only = tuple(path for _depth, path in candidates)
    if not paths_only:
        return PdfMatch(chosen_path=None, candidates=(), issue="pdf_missing")
    return PdfMatch(chosen_path=paths_only[-1], candidates=paths_only)


def detect_preview_pdf(paths: KitPaths) -> PdfMatch:
    if paths.project_dir is None:
        return PdfMatch(chosen_path=None, candidates=(), issue="project_not_configured")
    if not paths.project_dir.exists():
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")

    candidates = _collect_preview_pdf_candidates(paths)
    if not candidates:
        return PdfMatch(chosen_path=None, candidates=(), issue="pdf_missing")
    return PdfMatch(chosen_path=candidates[0], candidates=candidates)


def detect_print_packet_pdf(paths: KitPaths) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_print_packet_pdf)


def detect_assembly_packet_pdf(paths: KitPaths) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_assembly_packet_pdf)


def detect_cut_list_packet_pdf(paths: KitPaths) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_cut_list_packet_pdf)


def fabrication_folder_has_files(folder: Path | None) -> bool:
    if folder is None or not folder.exists():
        return False
    stack: list[Path] = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            return True
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def release_text_for_status(
    *,
    fabrication_folder_exists: bool,
    fabrication_has_files: bool,
    flow_display_text: str = "",
) -> str:
    display_key = clean_text(flow_display_text).casefold()
    if display_key == "complete":
        return "Complete"
    if fabrication_has_files:
        return "Released"
    if fabrication_folder_exists:
        return "Not released"
    return "W missing"


def build_kit_status(truck_number: str, kit_name: str, settings: ExplorerSettings) -> KitStatus:
    paths = build_kit_paths(truck_number, kit_name, settings)
    release_folder_exists = bool(paths.release_kit_dir and paths.release_kit_dir.exists())
    project_folder_exists = bool(paths.project_dir and paths.project_dir.exists())
    rpd_exists = bool(paths.rpd_path and paths.rpd_path.exists())
    rpd_size_bytes = paths.rpd_path.stat().st_size if rpd_exists and paths.rpd_path is not None else 0
    fabrication_folder_exists = bool(paths.fabrication_kit_dir and paths.fabrication_kit_dir.exists())
    fabrication_has_files = fabrication_folder_has_files(paths.fabrication_kit_dir)
    spreadsheet_match = detect_spreadsheet(paths.fabrication_kit_dir)
    preview_pdf_match = detect_preview_pdf(paths)
    outputs = None
    if spreadsheet_match.chosen_path is not None:
        outputs = inventor_output_paths(spreadsheet_match.chosen_path, paths.project_dir)

    summary_parts: list[str] = []
    summary_parts.append("RPD ready" if rpd_exists else "RPD missing")
    release_text = release_text_for_status(
        fabrication_folder_exists=fabrication_folder_exists,
        fabrication_has_files=fabrication_has_files,
    )
    if release_text == "Released":
        summary_parts.append("Released")
    elif release_text == "Not released":
        summary_parts.append("Not released")
    else:
        summary_parts.append("W folder missing")
    if spreadsheet_match.is_unique:
        summary_parts.append("Spreadsheet ready")
    elif spreadsheet_match.issue == "multiple_spreadsheets":
        summary_parts.append("Spreadsheet ambiguous")
    elif fabrication_has_files:
        summary_parts.append("Spreadsheet missing")
    if preview_pdf_match.chosen_path is not None:
        summary_parts.append("Nest Summary")

    return KitStatus(
        kit_name=kit_name,
        paths=paths,
        release_folder_exists=release_folder_exists,
        project_folder_exists=project_folder_exists,
        rpd_exists=rpd_exists,
        rpd_size_bytes=rpd_size_bytes,
        fabrication_folder_exists=fabrication_folder_exists,
        fabrication_has_files=fabrication_has_files,
        spreadsheet_match=spreadsheet_match,
        preview_pdf_match=preview_pdf_match,
        inventor_outputs=outputs,
        status_summary=" | ".join(summary_parts),
    )


def collect_kit_statuses(truck_number: str, settings: ExplorerSettings) -> list[KitStatus]:
    return [
        build_kit_status(truck_number, mapping.kit_name, settings)
        for mapping in configured_kit_mappings(settings)
    ]


def open_path(path: Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    os.startfile(str(target))  # type: ignore[attr-defined]


def open_external_target(target: str | Path) -> None:
    text = clean_text(target).strip('"')
    if not text:
        raise FileNotFoundError("No external target was provided.")
    if "://" in text:
        os.startfile(text)  # type: ignore[attr-defined]
        return
    open_path(Path(text))


def build_launch_command(
    entry_path: Path | str,
    argument_path: Path | str | None = None,
) -> list[str]:
    entry = Path(str(entry_path))
    suffix = entry.suffix.casefold()
    if suffix == ".py":
        command = [_python_executable(), str(entry)]
    elif suffix in {".bat", ".cmd"}:
        command = ["cmd.exe", "/c", str(entry)]
    else:
        command = [str(entry)]
    if argument_path is not None:
        command.append(str(Path(str(argument_path))))
    return command


def launch_tool(
    entry_path: Path | str,
    argument_path: Path | str | None = None,
) -> None:
    entry = Path(str(entry_path))
    if not entry.exists():
        raise FileNotFoundError(str(entry))
    if argument_path is not None:
        argument = Path(str(argument_path))
        if not argument.exists():
            raise FileNotFoundError(str(argument))

    subprocess.Popen(
        build_launch_command(entry, argument_path=argument_path),
        cwd=str(entry.parent),
    )


def launch_launcher(launcher_path: Path | str, argument_path: Path | str) -> None:
    launch_tool(launcher_path, argument_path=argument_path)


def _python_executable() -> str:
    if DEFAULT_VENV_PYTHON.exists():
        return str(DEFAULT_VENV_PYTHON)
    raise FileNotFoundError(f"Shared venv Python was not found: {DEFAULT_VENV_PYTHON}")


def _inventor_to_radan_module_path(entry_path: Path) -> Path:
    if entry_path.suffix.casefold() == ".py":
        return entry_path
    return entry_path.parent / "inventor_to_radan.py"


def run_inventor_to_radan_inline(entry_path: Path | str, spreadsheet_path: Path | str) -> object:
    entry = Path(str(entry_path))
    spreadsheet = Path(str(spreadsheet_path))
    if not entry.exists():
        raise FileNotFoundError(str(entry))
    if not spreadsheet.exists():
        raise FileNotFoundError(str(spreadsheet))

    module_path = _inventor_to_radan_module_path(entry)
    if not module_path.exists():
        raise FileNotFoundError(f"Could not find inline Inventor-to-RADAN module: {module_path}")

    module_name = "_truck_nest_inventor_to_radan_inline"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load inline Inventor-to-RADAN module: {module_path}")

    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_module is not None:
            sys.modules[module_name] = previous_module
        else:
            sys.modules.pop(module_name, None)

    converter = getattr(module, "convert_bom_to_radan_csv", None)
    if not callable(converter):
        raise RuntimeError(
            f"{module_path} does not expose convert_bom_to_radan_csv(). "
            "Use the external launcher for this version."
        )

    try:
        return converter(str(spreadsheet), allow_prompts=False, show_summary=False)
    except Exception as exc:
        if exc.__class__.__name__ != "InventorToRadanNeedsUi":
            raise
        missing_dxf_items = getattr(exc, "missing_dxf_items", ()) or ()
        missing_rules = getattr(exc, "missing_rules", ()) or ()
        parts: list[str] = []
        if missing_dxf_items:
            parts.append(f"{len(missing_dxf_items)} missing-DXF classification(s)")
        if missing_rules:
            parts.append(f"{len(missing_rules)} RADAN rule(s)")
        detail = " and ".join(parts) if parts else "user input"
        raise InventorToRadanInlineNeedsUi(
            f"Inline conversion needs {detail}.",
            missing_dxf_count=len(missing_dxf_items),
            missing_rule_count=len(missing_rules),
        ) from exc


def radan_csv_missing_symbols(
    csv_path: Path | str,
    output_folder: Path | str,
    *,
    max_parts: int | None = None,
) -> tuple[Path, ...]:
    if max_parts is not None and max_parts <= 0:
        raise ValueError("max_parts must be greater than zero when supplied.")
    csv_file = Path(str(csv_path))
    symbol_folder = Path(str(output_folder))
    missing: list[Path] = []
    importable_count = 0
    with csv_file.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            dxf_text = row[0].strip()
            if not dxf_text:
                continue
            importable_count += 1
            symbol_path = symbol_folder / f"{Path(dxf_text).stem}.sym"
            if not symbol_path.exists():
                missing.append(symbol_path)
            if max_parts is not None and importable_count >= max_parts:
                break
    return tuple(missing)


def visible_radan_sessions() -> tuple[tuple[int, str], ...]:
    command = (
        "$sessions = Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -like 'radraft*' -and $_.MainWindowHandle -ne 0 -and "
        "-not [string]::IsNullOrWhiteSpace($_.MainWindowTitle) } | "
        "Select-Object @{Name='ProcessId';Expression={$_.Id}}, @{Name='WindowTitle';Expression={$_.MainWindowTitle}}; "
        "$sessions | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return ()
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ()
    if isinstance(payload, dict):
        items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        return ()
    sessions: list[tuple[int, str]] = []
    for item in items:
        try:
            process_id = int(item.get("ProcessId"))
        except (AttributeError, TypeError, ValueError):
            continue
        title = str(item.get("WindowTitle") or "").strip()
        if title:
            sessions.append((process_id, title))
    return tuple(sessions)


def _process_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(process_id))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(int(process_id), 0)
    except OSError:
        return False
    return True


def radan_csv_import_lock_status(project_path: Path | str) -> tuple[bool, Path, int | None]:
    project = Path(str(project_path)).expanduser().resolve()
    digest = hashlib.sha1(str(project).casefold().encode("utf-8")).hexdigest()[:16]
    lock_path = Path(os.environ.get("TEMP", str(project.parent))) / f"radan_csv_import_{digest}.lock"
    if not lock_path.exists():
        return False, lock_path, None
    try:
        process_id = int(lock_path.read_text(encoding="ascii", errors="ignore").strip())
    except (OSError, ValueError):
        return False, lock_path, None
    return _process_exists(process_id), lock_path, process_id


def launch_inventor_to_radan(entry_path: Path | str, spreadsheet_path: Path | str) -> subprocess.Popen[object]:
    entry = Path(str(entry_path))
    spreadsheet = Path(str(spreadsheet_path))
    if not entry.exists():
        raise FileNotFoundError(str(entry))
    if not spreadsheet.exists():
        raise FileNotFoundError(str(spreadsheet))

    suffix = entry.suffix.casefold()
    if suffix == ".py":
        command = [_python_executable(), str(entry), str(spreadsheet)]
    elif suffix in {".bat", ".cmd"}:
        command = ["cmd.exe", "/c", str(entry), str(spreadsheet)]
    else:
        command = [str(entry), str(spreadsheet)]

    popen_kwargs: dict[str, object] = {
        "cwd": str(entry.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if suffix in {".bat", ".cmd"}:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(command, **popen_kwargs)


def launch_radan_csv_import(
    csv_path: Path | str,
    output_folder: Path | str,
    *,
    project_path: Path | str | None = None,
    log_path: Path | str | None = None,
    entry_path: Path | str = DEFAULT_RADAN_CSV_IMPORT_ENTRY,
    allow_visible_radan: bool = False,
    rebuild_symbols: bool = False,
    native_sym_experimental: bool = False,
    preprocess_dxf_outer_profile: bool = False,
    preprocess_dxf_tolerance: float | None = None,
    assign_project_colors: bool = False,
    project_update_method: str = "direct-xml",
    refresh_project_sheets: bool = False,
    max_parts: int | None = None,
) -> subprocess.Popen[object]:
    entry = Path(str(entry_path))
    csv = Path(str(csv_path))
    output = Path(str(output_folder))
    if not entry.exists():
        raise FileNotFoundError(str(entry))
    if not csv.exists():
        raise FileNotFoundError(str(csv))
    if not output.exists():
        raise FileNotFoundError(str(output))
    project = Path(str(project_path)) if project_path is not None else None
    if project is not None and not project.exists():
        raise FileNotFoundError(str(project))
    log = Path(str(log_path)) if log_path is not None else None

    command = [
        _python_executable(),
        str(entry),
        "--csv",
        str(csv),
        "--output-folder",
        str(output),
    ]
    if project is not None:
        command.extend(["--project", str(project)])
    if allow_visible_radan:
        command.append("--allow-visible-radan")
    if rebuild_symbols:
        command.append("--rebuild-symbols")
    if native_sym_experimental:
        command.append("--native-sym-experimental")
    if preprocess_dxf_outer_profile:
        command.append("--preprocess-dxf-outer-profile")
    if preprocess_dxf_tolerance is not None:
        command.extend(["--preprocess-dxf-tolerance", str(preprocess_dxf_tolerance)])
    if assign_project_colors:
        command.append("--assign-project-colors")
    if project_update_method:
        command.extend(["--project-update-method", str(project_update_method)])
    if refresh_project_sheets:
        command.append("--refresh-project-sheets")
    if max_parts is not None:
        if max_parts <= 0:
            raise ValueError("max_parts must be greater than zero when supplied.")
        command.extend(["--max-parts", str(max_parts)])
    if log is not None:
        command.extend(["--log-file", str(log)])
        log.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log.open("a", encoding="utf-8", buffering=1)
        try:
            return subprocess.Popen(
                command,
                cwd=str(entry.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=log_stream,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            log_stream.close()
    return subprocess.Popen(
        command,
        cwd=str(entry.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_inventor_to_radan(entry_path: Path | str, spreadsheet_path: Path | str) -> subprocess.CompletedProcess[str]:
    entry = Path(str(entry_path))
    spreadsheet = Path(str(spreadsheet_path))
    if not entry.exists():
        raise FileNotFoundError(str(entry))
    if not spreadsheet.exists():
        raise FileNotFoundError(str(spreadsheet))

    suffix = entry.suffix.casefold()
    if suffix == ".py":
        command = [_python_executable(), str(entry), str(spreadsheet)]
    elif suffix in {".bat", ".cmd"}:
        command = ["cmd.exe", "/c", str(entry), str(spreadsheet)]
    else:
        command = [str(entry), str(spreadsheet)]

    run_kwargs = {
        "cwd": str(entry.parent),
        "text": True,
    }
    if suffix in {".bat", ".cmd"}:
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        return subprocess.run(command, **run_kwargs)
    return subprocess.run(
        command,
        capture_output=True,
        **run_kwargs,
    )


def _move_or_replace(
    source_path: Path,
    target_path: Path,
    *,
    spreadsheet_path: Path | str | None = None,
    source_is_owned_inventor_output: bool = False,
) -> Path:
    assert_w_drive_write_allowed(
        source_path,
        operation="move/delete source file",
        allow_owned_inventor_output=source_is_owned_inventor_output,
        spreadsheet_path=spreadsheet_path,
    )
    assert_w_drive_write_allowed(target_path, operation="overwrite target file")
    if target_path.exists():
        target_path.unlink()
    shutil.move(str(source_path), str(target_path))
    return target_path


def move_inventor_outputs_to_project(
    spreadsheet_path: Path,
    project_dir: Path | None,
) -> tuple[InventorOutputPaths, tuple[Path, ...]]:
    outputs = inventor_output_paths(spreadsheet_path, project_dir)
    if project_dir is None:
        raise ValueError("Project folder is not available.")
    if not project_dir.exists():
        raise FileNotFoundError(str(project_dir))
    if outputs.target_csv_path is None or outputs.target_report_path is None:
        raise ValueError("Could not build the inventor output paths.")
    if not outputs.source_csv_path.exists():
        raise FileNotFoundError(str(outputs.source_csv_path))

    moved: list[Path] = []
    _move_or_replace(
        outputs.source_csv_path,
        outputs.target_csv_path,
        spreadsheet_path=spreadsheet_path,
        source_is_owned_inventor_output=True,
    )
    moved.append(outputs.target_csv_path)

    if outputs.source_report_path.exists():
        _move_or_replace(
            outputs.source_report_path,
            outputs.target_report_path,
            spreadsheet_path=spreadsheet_path,
            source_is_owned_inventor_output=True,
        )
        moved.append(outputs.target_report_path)

    return outputs, tuple(moved)
