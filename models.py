from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re

DEFAULT_RELEASE_ROOT = r"L:\BATTLESHIELD\F-LARGE FLEET"
DEFAULT_P_RELEASE_ROOT = r"L:\BATTLESHIELD\P-SMALL FLEET"
DEFAULT_FABRICATION_ROOT = r"W:\LASER\For Battleshield Fabrication"
DEFAULT_DASHBOARD_LAUNCHER = r"C:\Tools\fabrication_flow_dashboard\run_app.bat"
DEFAULT_RADAN_KITTER_LAUNCHER = r"C:\Tools\radan_kitter\radan_kitter.bat"
DEFAULT_INVENTOR_TO_RADAN_ENTRY = r"C:\Tools\inventor_to_radan\inventor_to_radan.bat"
DEFAULT_SUPPORT_FOLDERS = ("_bak", "_out", "_kits")
DEFAULT_KIT_TEMPLATES = [
    "PAINT PACK",
    "INTERIOR PACK",
    "EXTERIOR PACK",
    "CONSOLE PACK",
    "CHASSIS PACK",
    "PUMP HOUSE => PUMP PACK\\PUMP HOUSE",
    "PUMP COVERING => PUMP PACK\\COVERING",
    "PUMP MOUNTS => PUMP PACK\\MOUNTS",
    "PUMP BRACKETS => PUMP PACK\\BRACKETS",
    "STEP PACK",
    "OPERATIONAL PANELS => PUMP PACK\\OPERATIONAL PANELS",
]
TRUCK_NUMBER_PATTERN = re.compile(r"^[A-Z]\d{5}$", re.IGNORECASE)
HIDDEN_KIT_SEPARATOR = "::"
KIT_NAME_ALIASES = {
    "CONSOLE": "CONSOLE PACK",
    "OPS PANELS": "OPERATIONAL PANELS",
    "PUMP COVERINGS": "PUMP COVERING",
    "STEPS PACK": "STEP PACK",
    "STEPS": "STEP PACK",
}


@dataclass(frozen=True)
class KitMapping:
    display_name: str
    kit_name: str
    fabrication_relative_path: str


def _normalize_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("/", "\\")
    parts = [segment.strip() for segment in text.split("\\") if segment.strip()]
    return "\\".join(parts)


def canonicalize_kit_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return KIT_NAME_ALIASES.get(text.upper(), text)


def kit_name_variants(value: object) -> tuple[str, ...]:
    canonical_name = canonicalize_kit_name(value)
    if not canonical_name:
        return ()
    variants = [canonical_name]
    for alias_name, mapped_name in KIT_NAME_ALIASES.items():
        if mapped_name.casefold() != canonical_name.casefold():
            continue
        if alias_name.casefold() == canonical_name.casefold():
            continue
        variants.append(alias_name)
    return tuple(variants)


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


def normalize_truck_order_entries(values: list[object] | None) -> list[str]:
    return normalize_hidden_truck_entries(values)


def canonicalize_client_numbers_by_truck(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}

    cleaned: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        truck_number = normalize_hidden_truck_number(raw_key)
        client_number = str(raw_value or "").strip()
        if not truck_number or not client_number:
            continue
        cleaned[truck_number] = client_number
    return cleaned


def normalize_odd_jobs_by_truck(values: object) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        return {}

    cleaned: dict[str, list[str]] = {}
    for raw_truck, raw_jobs in values.items():
        truck_number = normalize_hidden_truck_number(raw_truck)
        if not truck_number:
            continue
        if isinstance(raw_jobs, str):
            source_jobs = [raw_jobs]
        elif isinstance(raw_jobs, list):
            source_jobs = raw_jobs
        else:
            continue

        jobs: list[str] = []
        seen: set[str] = set()
        for raw_job in source_jobs:
            job_name = canonicalize_kit_name(raw_job)
            if not job_name:
                continue
            key = job_name.casefold()
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job_name)
        if jobs:
            cleaned[truck_number] = jobs
    return cleaned


def build_hidden_kit_key(truck_number: object, kit_name: object) -> str:
    truck_text = normalize_hidden_truck_number(truck_number)
    kit_text = canonicalize_kit_name(kit_name)
    if not truck_text or not kit_text:
        return ""
    return f"{truck_text}{HIDDEN_KIT_SEPARATOR}{kit_text}"


def resolve_punch_code_text(
    values: dict[str, str],
    truck_number: object,
    kit_name: object,
) -> str:
    key = build_hidden_kit_key(truck_number, kit_name)
    if key and key in values:
        return str(values.get(key) or "")
    legacy_key = canonicalize_kit_name(kit_name)
    if not legacy_key:
        return ""
    return str(values.get(legacy_key) or "")


def materialize_legacy_punch_codes_for_kit(
    values: dict[str, str],
    truck_numbers: list[object] | None,
    kit_name: object,
) -> dict[str, str]:
    canonical_kit_name = canonicalize_kit_name(kit_name)
    updated = dict(values)
    if not canonical_kit_name or canonical_kit_name not in updated:
        return updated

    legacy_value = str(updated.pop(canonical_kit_name) or "")
    if not legacy_value.strip():
        return updated

    for truck_number in truck_numbers or []:
        key = build_hidden_kit_key(truck_number, canonical_kit_name)
        if not key or key in updated:
            continue
        updated[key] = legacy_value
    return updated


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
    for alias_name, canonical_name in KIT_NAME_ALIASES.items():
        kit_name_lookup[alias_name.casefold()] = canonical_name

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
    for alias_name, canonical_name in KIT_NAME_ALIASES.items():
        kit_name_lookup[alias_name.casefold()] = canonical_name

    cleaned: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key_text = str(raw_key or "").strip()
        value_text = str(raw_value or "")
        if not key_text or not value_text.strip():
            continue
        if HIDDEN_KIT_SEPARATOR in key_text:
            truck_part, kit_part = key_text.split(HIDDEN_KIT_SEPARATOR, 1)
            canonical_kit_name = kit_name_lookup.get(kit_part.strip().casefold(), kit_part.strip())
            canonical_key = build_hidden_kit_key(truck_part, canonical_kit_name)
            if not canonical_key:
                continue
        else:
            canonical_key = kit_name_lookup.get(key_text.casefold(), key_text)
        cleaned[canonical_key] = value_text
    return cleaned


def canonicalize_notes_by_kit(
    values: object,
    kit_template_values: list[object] | None,
) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}

    kit_name_lookup: dict[str, str] = {}
    for mapping in build_kit_mappings(kit_template_values):
        kit_name_lookup[mapping.display_name.casefold()] = mapping.kit_name
        kit_name_lookup[mapping.kit_name.casefold()] = mapping.kit_name
    for alias_name, canonical_name in KIT_NAME_ALIASES.items():
        kit_name_lookup[alias_name.casefold()] = canonical_name

    cleaned: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key_text = str(raw_key or "").strip()
        value_text = str(raw_value or "")
        if not key_text or not value_text.strip():
            continue
        if HIDDEN_KIT_SEPARATOR in key_text:
            truck_part, kit_part = key_text.split(HIDDEN_KIT_SEPARATOR, 1)
            canonical_kit_name = kit_name_lookup.get(kit_part.strip().casefold(), kit_part.strip())
            canonical_key = build_hidden_kit_key(truck_part, canonical_kit_name)
            if not canonical_key:
                continue
        else:
            canonical_key = kit_name_lookup.get(key_text.casefold(), key_text)
        cleaned[canonical_key] = value_text
    return cleaned


@dataclass
class ExplorerSettings:
    release_root: str = DEFAULT_RELEASE_ROOT
    fabrication_root: str = DEFAULT_FABRICATION_ROOT
    dashboard_launcher: str = DEFAULT_DASHBOARD_LAUNCHER
    radan_kitter_launcher: str = DEFAULT_RADAN_KITTER_LAUNCHER
    inventor_to_radan_entry: str = DEFAULT_INVENTOR_TO_RADAN_ENTRY
    rpd_template_path: str = ""
    template_replacements_text: str = ""
    punch_codes_text: str = ""
    punch_codes_by_kit: dict[str, str] = field(default_factory=dict)
    notes_by_kit: dict[str, str] = field(default_factory=dict)
    client_numbers_by_truck: dict[str, str] = field(default_factory=dict)
    odd_jobs_by_truck: dict[str, list[str]] = field(default_factory=dict)
    create_support_folders: bool = True
    kit_templates: list[str] = field(default_factory=lambda: list(DEFAULT_KIT_TEMPLATES))
    truck_order: list[str] = field(default_factory=list)
    hidden_trucks: list[str] = field(default_factory=list)
    hidden_kits: list[str] = field(default_factory=list)


def truck_number_has_tracked_data(settings: ExplorerSettings, truck_number: object) -> bool:
    """Whether this truck number appears anywhere in settings - trucks have
    no other persisted identity in this app (see explicit_truck_numbers in
    services.py, which draws from the same fields), so this is the
    collision check for rename_truck_number_in_settings: renaming onto a
    truck number that already has data here would silently merge two
    trucks' settings together."""
    key = normalize_hidden_truck_number(truck_number)
    if not key:
        return False
    if key in {normalize_hidden_truck_number(entry) for entry in settings.truck_order}:
        return True
    if key in {normalize_hidden_truck_number(entry) for entry in settings.hidden_trucks}:
        return True
    if key in {normalize_hidden_truck_number(k) for k in settings.client_numbers_by_truck}:
        return True
    if key in {normalize_hidden_truck_number(k) for k in settings.odd_jobs_by_truck}:
        return True
    for mapping in (settings.punch_codes_by_kit, settings.notes_by_kit, settings.hidden_kits):
        entries = mapping.keys() if isinstance(mapping, dict) else mapping
        for entry in entries:
            entry_text = str(entry or "")
            if HIDDEN_KIT_SEPARATOR not in entry_text:
                continue
            truck_part, _kit_part = entry_text.split(HIDDEN_KIT_SEPARATOR, 1)
            if normalize_hidden_truck_number(truck_part) == key:
                return True
    return False


def rename_truck_number_in_settings(
    settings: ExplorerSettings,
    old_truck_number: object,
    new_truck_number: object,
) -> ExplorerSettings:
    """Relabels every truck-number-keyed entry in settings from
    old_truck_number to new_truck_number - trucks are renumbered upstream
    of this app before release, for reasons outside its control, and this
    app's own tracked data (client, notes, punch codes, hidden state,
    order) needs to follow the new number. Never touches the filesystem -
    this app must never rename L:/W: folders. Returns settings unchanged
    if old/new are equal or either fails to normalize; callers should use
    truck_number_has_tracked_data(settings, new_truck_number) beforehand to
    guard against silently merging two trucks' data together."""
    old_key = normalize_hidden_truck_number(old_truck_number)
    new_key = normalize_hidden_truck_number(new_truck_number)
    if not old_key or not new_key or old_key == new_key:
        return settings

    def _rename_direct_key(values: dict[str, object]) -> dict[str, object]:
        renamed = dict(values)
        for existing_key in list(renamed.keys()):
            if normalize_hidden_truck_number(existing_key) == old_key:
                renamed[new_key] = renamed.pop(existing_key)
        return renamed

    def _rename_in_list(values: list[str]) -> list[str]:
        return [new_key if normalize_hidden_truck_number(v) == old_key else v for v in values]

    def _rename_composite_key(key_text: str) -> str:
        if HIDDEN_KIT_SEPARATOR not in key_text:
            return key_text
        truck_part, kit_part = key_text.split(HIDDEN_KIT_SEPARATOR, 1)
        if normalize_hidden_truck_number(truck_part) != old_key:
            return key_text
        return build_hidden_kit_key(new_key, kit_part) or key_text

    def _rename_composite_dict(values: dict[str, str]) -> dict[str, str]:
        return {_rename_composite_key(key): value for key, value in values.items()}

    def _rename_composite_list(values: list[str]) -> list[str]:
        return [_rename_composite_key(entry) for entry in values]

    return replace(
        settings,
        client_numbers_by_truck=_rename_direct_key(settings.client_numbers_by_truck),
        odd_jobs_by_truck=_rename_direct_key(settings.odd_jobs_by_truck),
        truck_order=_rename_in_list(settings.truck_order),
        hidden_trucks=_rename_in_list(settings.hidden_trucks),
        punch_codes_by_kit=_rename_composite_dict(settings.punch_codes_by_kit),
        notes_by_kit=_rename_composite_dict(settings.notes_by_kit),
        hidden_kits=_rename_composite_list(settings.hidden_kits),
    )


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
    fabrication_has_files: bool
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
