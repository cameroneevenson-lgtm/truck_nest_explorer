from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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
    normalize_hidden_kit_entries,
    normalize_hidden_truck_entries,
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
TRUCK_FOLDER_PATTERN = re.compile(r"^F\d{5}$", re.IGNORECASE)


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
    wanted = clean_text(kit_name)
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

    project_name = f"{truck_text} {kit_text}".strip()
    release_root = _path_from_setting(settings.release_root)
    fabrication_root = _path_from_setting(settings.fabrication_root)

    release_truck_dir = release_root / truck_text if release_root else None
    release_kit_dir = release_truck_dir / kit_text if release_truck_dir else None
    project_dir = release_kit_dir / project_name if release_kit_dir else None
    rpd_path = project_dir / f"{project_name}.rpd" if project_dir else None

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
    for child in root.iterdir():
        if child.is_dir() and TRUCK_FOLDER_PATTERN.fullmatch(child.name):
            names.add(child.name)
    return sorted(names, key=natural_sort_key)


def find_fabrication_truck_dir(truck_number: str, settings: ExplorerSettings) -> Path | None:
    wanted = clean_text(truck_number)
    if not wanted:
        return None
    fabrication_root = _path_from_setting(settings.fabrication_root)
    if fabrication_root is None or not fabrication_root.exists():
        return None

    for child in fabrication_root.iterdir():
        if not child.is_dir():
            continue
        if child.name.casefold() == wanted.casefold():
            return child
    return None


def _is_generated_spreadsheet(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(pattern) for pattern in IGNORED_SPREADSHEET_PATTERNS)


def detect_spreadsheet(folder: Path | None) -> SpreadsheetMatch:
    if folder is None:
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="root_not_configured")
    if not folder.exists():
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="folder_missing")

    candidates = tuple(
        sorted(
            (
                path
                for path in folder.iterdir()
                if path.is_file()
                and path.suffix.casefold() in SUPPORTED_SPREADSHEET_SUFFIXES
                and not _is_generated_spreadsheet(path)
            ),
            key=lambda path: natural_sort_key(path.name),
        )
    )
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

    text = re.sub(r'encoding="[^"]+"', 'encoding="utf-8"', text, count=1)
    output_path.write_text(text, encoding="utf-8")
    return "template_clone"


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
            children = list(folder.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_file():
                discovered.append((child, depth))
                continue
            if child.is_dir() and depth < max_depth:
                stack.append((child, depth + 1))
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
        if not _is_print_packet_pdf(child):
            continue
        candidates.append((depth, child))

    candidates.sort(key=lambda item: (item[0], natural_sort_key(str(item[1].relative_to(search_root)))))
    paths_only = tuple(path for _depth, path in candidates)
    if not paths_only:
        return PdfMatch(chosen_path=None, candidates=(), issue="pdf_missing")
    return PdfMatch(chosen_path=paths_only[0], candidates=paths_only)


def build_kit_status(truck_number: str, kit_name: str, settings: ExplorerSettings) -> KitStatus:
    paths = build_kit_paths(truck_number, kit_name, settings)
    release_folder_exists = bool(paths.release_kit_dir and paths.release_kit_dir.exists())
    project_folder_exists = bool(paths.project_dir and paths.project_dir.exists())
    rpd_exists = bool(paths.rpd_path and paths.rpd_path.exists())
    rpd_size_bytes = paths.rpd_path.stat().st_size if rpd_exists and paths.rpd_path is not None else 0
    fabrication_folder_exists = bool(paths.fabrication_kit_dir and paths.fabrication_kit_dir.exists())
    spreadsheet_match = detect_spreadsheet(paths.fabrication_kit_dir)
    preview_pdf_match = detect_preview_pdf(paths)
    outputs = None
    if spreadsheet_match.chosen_path is not None:
        outputs = inventor_output_paths(spreadsheet_match.chosen_path, paths.project_dir)

    summary_parts: list[str] = []
    summary_parts.append("RPD ready" if rpd_exists else "RPD missing")
    if spreadsheet_match.is_unique:
        summary_parts.append("Spreadsheet ready")
    elif spreadsheet_match.issue == "multiple_spreadsheets":
        summary_parts.append("Spreadsheet ambiguous")
    elif fabrication_folder_exists:
        summary_parts.append("Spreadsheet missing")
    else:
        summary_parts.append("W folder missing")
    if preview_pdf_match.chosen_path is not None:
        summary_parts.append("Nest Summary")
    if outputs is not None and outputs.target_csv_path is not None and outputs.target_csv_path.exists():
        summary_parts.append("Import CSV on L")

    return KitStatus(
        kit_name=kit_name,
        paths=paths,
        release_folder_exists=release_folder_exists,
        project_folder_exists=project_folder_exists,
        rpd_exists=rpd_exists,
        rpd_size_bytes=rpd_size_bytes,
        fabrication_folder_exists=fabrication_folder_exists,
        spreadsheet_match=spreadsheet_match,
        preview_pdf_match=preview_pdf_match,
        inventor_outputs=outputs,
        status_summary=" | ".join(summary_parts),
    )


def collect_kit_statuses(truck_number: str, settings: ExplorerSettings) -> list[KitStatus]:
    statuses = [
        build_kit_status(truck_number, mapping.kit_name, settings)
        for mapping in configured_kit_mappings(settings)
    ]
    return sorted(statuses, key=lambda status: natural_sort_key(status.paths.display_name))


def open_path(path: Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    os.startfile(str(target))  # type: ignore[attr-defined]


def launch_launcher(launcher_path: Path | str, argument_path: Path | str) -> None:
    launcher = Path(str(launcher_path))
    argument = Path(str(argument_path))
    if not launcher.exists():
        raise FileNotFoundError(str(launcher))
    if not argument.exists():
        raise FileNotFoundError(str(argument))

    subprocess.Popen(
        ["cmd.exe", "/c", str(launcher), str(argument)],
        cwd=str(launcher.parent),
    )


def _python_executable() -> str:
    if DEFAULT_VENV_PYTHON.exists():
        return str(DEFAULT_VENV_PYTHON)
    return sys.executable


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

    return subprocess.run(
        command,
        cwd=str(entry.parent),
        text=True,
        capture_output=True,
    )


def copy_inventor_outputs_to_project(
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

    copied: list[Path] = []
    shutil.copyfile(outputs.source_csv_path, outputs.target_csv_path)
    copied.append(outputs.target_csv_path)

    if outputs.source_report_path.exists():
        shutil.copyfile(outputs.source_report_path, outputs.target_report_path)
        copied.append(outputs.target_report_path)

    return outputs, tuple(copied)
