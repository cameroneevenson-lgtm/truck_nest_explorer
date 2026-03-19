from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

DEFAULT_RELEASE_ROOT = r"L:\BATTLESHIELD\F-LARGE FLEET"
DEFAULT_FABRICATION_ROOT = r"W:\LASER\For Battleshield Fabrication"
DEFAULT_RADAN_KITTER_LAUNCHER = r"C:\Tools\radan_kitter\radan_kitter.bat"
DEFAULT_INVENTOR_TO_RADAN_ENTRY = r"C:\Tools\inventor_to_radan\inventor_to_radan.py"
DEFAULT_SUPPORT_FOLDERS = ("_bak", "_out", "_kits")
DEFAULT_KIT_TEMPLATES = [
    "BODY | PAINT PACK",
    "PUMPHOUSE",
    "CONSOLE PACK",
    "INTERIOR PACK",
    "EXTERIOR PACK",
    "PUMP COVERINGS",
    "GRAND REMOUS TWO",
]
TRUCK_NUMBER_PATTERN = re.compile(r"^F\d{5}$", re.IGNORECASE)
HIDDEN_KIT_SEPARATOR = "::"


@dataclass(frozen=True)
class KitMapping:
    display_name: str
    kit_name: str
    fabrication_relative_path: str


def _normalize_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("/", "\\")
    parts = [segment.strip() for segment in text.split("\\") if segment.strip()]
    return "\\".join(parts)


def parse_kit_mapping_entry(value: object) -> KitMapping | None:
    text = str(value or "").strip()
    if not text:
        return None

    if "=>" in text:
        name_part, fabrication_relative_path = [part.strip() for part in text.split("=>", 1)]
    else:
        name_part = text
        fabrication_relative_path = ""

    if "|" in name_part:
        display_name, kit_name = [part.strip() for part in name_part.split("|", 1)]
    else:
        display_name = str(name_part or "").strip()
        kit_name = display_name

    display_name = str(display_name or "").strip()
    kit_name = str(kit_name or "").strip()
    fabrication_relative_path = _normalize_relative_path(fabrication_relative_path or kit_name)
    if not display_name:
        display_name = kit_name
    if not display_name:
        return None
    if not kit_name:
        return None
    if not fabrication_relative_path:
        fabrication_relative_path = kit_name
    return KitMapping(
        display_name=display_name,
        kit_name=kit_name,
        fabrication_relative_path=fabrication_relative_path,
    )


def format_kit_mapping_entry(mapping: KitMapping) -> str:
    if mapping.display_name.casefold() == mapping.kit_name.casefold():
        name_text = mapping.kit_name
    else:
        name_text = f"{mapping.display_name} | {mapping.kit_name}"
    if mapping.fabrication_relative_path.casefold() == mapping.kit_name.casefold():
        return name_text
    return f"{name_text} => {mapping.fabrication_relative_path}"


def build_kit_mappings(values: list[object] | None) -> list[KitMapping]:
    cleaned: list[KitMapping] = []
    seen_display_names: set[str] = set()
    seen_kit_names: set[str] = set()
    source_values = values or list(DEFAULT_KIT_TEMPLATES)
    for raw in source_values:
        mapping = parse_kit_mapping_entry(raw)
        if mapping is None:
            continue
        display_key = mapping.display_name.casefold()
        kit_key = mapping.kit_name.casefold()
        if display_key in seen_display_names or kit_key in seen_kit_names:
            continue
        seen_display_names.add(display_key)
        seen_kit_names.add(kit_key)
        cleaned.append(mapping)
    if cleaned:
        return cleaned
    return [parse_kit_mapping_entry(value) for value in DEFAULT_KIT_TEMPLATES if parse_kit_mapping_entry(value) is not None]  # type: ignore[list-item]


def normalize_kit_template_entries(values: list[object] | None) -> list[str]:
    return [format_kit_mapping_entry(mapping) for mapping in build_kit_mappings(values)]


def normalize_hidden_truck_number(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if not TRUCK_NUMBER_PATTERN.fullmatch(text):
        return ""
    return text


def normalize_hidden_truck_entries(values: list[object] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        truck_number = normalize_hidden_truck_number(raw)
        if not truck_number:
            continue
        key = truck_number.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(truck_number)
    return cleaned


def build_hidden_kit_key(truck_number: object, kit_name: object) -> str:
    truck_text = normalize_hidden_truck_number(truck_number)
    kit_text = str(kit_name or "").strip()
    if not truck_text or not kit_text:
        return ""
    return f"{truck_text}{HIDDEN_KIT_SEPARATOR}{kit_text}"


def normalize_hidden_kit_entries(values: list[object] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = str(raw or "").strip()
        if not text or HIDDEN_KIT_SEPARATOR not in text:
            continue
        truck_part, kit_part = text.split(HIDDEN_KIT_SEPARATOR, 1)
        key = build_hidden_kit_key(truck_part, kit_part)
        if not key:
            continue
        dedupe_key = key.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(key)
    return cleaned


def canonicalize_hidden_kit_entries(
    values: list[object] | None,
    kit_template_values: list[object] | None,
) -> list[str]:
    kit_name_lookup: dict[str, str] = {}
    for mapping in build_kit_mappings(kit_template_values):
        kit_name_lookup[mapping.display_name.casefold()] = mapping.kit_name
        kit_name_lookup[mapping.kit_name.casefold()] = mapping.kit_name

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in normalize_hidden_kit_entries(values):
        truck_part, kit_part = value.split(HIDDEN_KIT_SEPARATOR, 1)
        canonical_kit_name = kit_name_lookup.get(kit_part.strip().casefold(), kit_part.strip())
        key = build_hidden_kit_key(truck_part, canonical_kit_name)
        if not key:
            continue
        dedupe_key = key.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(key)
    return cleaned


def canonicalize_punch_codes_by_kit(
    values: object,
    kit_template_values: list[object] | None,
) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}

    kit_name_lookup: dict[str, str] = {}
    for mapping in build_kit_mappings(kit_template_values):
        kit_name_lookup[mapping.display_name.casefold()] = mapping.kit_name
        kit_name_lookup[mapping.kit_name.casefold()] = mapping.kit_name

    cleaned: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key_text = str(raw_key or "").strip()
        value_text = str(raw_value or "")
        if not key_text or not value_text.strip():
            continue
        canonical_key = kit_name_lookup.get(key_text.casefold(), key_text)
        cleaned[canonical_key] = value_text
    return cleaned


@dataclass
class ExplorerSettings:
    release_root: str = DEFAULT_RELEASE_ROOT
    fabrication_root: str = DEFAULT_FABRICATION_ROOT
    radan_kitter_launcher: str = DEFAULT_RADAN_KITTER_LAUNCHER
    inventor_to_radan_entry: str = DEFAULT_INVENTOR_TO_RADAN_ENTRY
    rpd_template_path: str = ""
    template_replacements_text: str = ""
    punch_codes_text: str = ""
    punch_codes_by_kit: dict[str, str] = field(default_factory=dict)
    create_support_folders: bool = True
    kit_templates: list[str] = field(default_factory=lambda: list(DEFAULT_KIT_TEMPLATES))
    hidden_trucks: list[str] = field(default_factory=list)
    hidden_kits: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class KitPaths:
    truck_number: str
    display_name: str
    kit_name: str
    fabrication_relative_path: str
    project_name: str
    release_truck_dir: Path | None
    release_kit_dir: Path | None
    project_dir: Path | None
    rpd_path: Path | None
    support_dirs: tuple[Path, ...]
    fabrication_truck_dir: Path | None
    fabrication_kit_dir: Path | None


@dataclass(frozen=True)
class SpreadsheetMatch:
    chosen_path: Path | None
    candidates: tuple[Path, ...]
    issue: str = ""

    @property
    def is_unique(self) -> bool:
        return self.chosen_path is not None and len(self.candidates) == 1


@dataclass(frozen=True)
class PdfMatch:
    chosen_path: Path | None
    candidates: tuple[Path, ...]
    issue: str = ""

    @property
    def is_unique(self) -> bool:
        return self.chosen_path is not None and len(self.candidates) == 1


@dataclass(frozen=True)
class InventorOutputPaths:
    source_csv_path: Path
    source_report_path: Path
    target_csv_path: Path | None
    target_report_path: Path | None


@dataclass(frozen=True)
class KitStatus:
    kit_name: str
    paths: KitPaths
    release_folder_exists: bool
    project_folder_exists: bool
    rpd_exists: bool
    rpd_size_bytes: int
    fabrication_folder_exists: bool
    spreadsheet_match: SpreadsheetMatch
    preview_pdf_match: PdfMatch
    inventor_outputs: InventorOutputPaths | None
    status_summary: str


@dataclass(frozen=True)
class ScaffoldResult:
    paths: KitPaths
    created_paths: tuple[Path, ...]
    notes: tuple[str, ...]
    template_mode: str
