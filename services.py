from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from models import (
    DEFAULT_SUPPORT_FOLDERS,
    DEFAULT_P_RELEASE_ROOT,
    ExplorerSettings,
    InventorOutputPaths,
    KitMapping,
    KitPaths,
    KitStatus,
    ScaffoldResult,
    build_hidden_kit_key,
    build_kit_mappings,
    canonicalize_kit_name,
    canonicalize_hidden_kit_entries,
    kit_name_variants,
    normalize_hidden_kit_entries,
    normalize_hidden_truck_number,
    normalize_hidden_truck_entries,
    normalize_truck_order_entries,
)
from performance_metrics import BoundedTTLCache, GLOBAL_METRICS, normalize_cache_path, settings_cache_signature

# fs_cache.py holds the generic filesystem-metadata caching primitives so
# packet_pdf_detection.py and w_block_transfer.py can use them without
# importing services.py (which would create a circular import, since this
# module imports from both of those for re-export). Several names below are
# re-exported for backward compatibility with existing
# `from services import ...` call sites (tests, controllers, other modules)
# even though services.py itself no longer calls them directly - noqa'd
# rather than dropped, per the established convention in this repo (see
# commit "Remove unused threading import; keep BackgroundJobWorker
# re-export").
from fs_cache import (
    FILE_METADATA_CACHE,
    FILESYSTEM_CACHE_NEGATIVE_TTL_SECONDS,
    FILESYSTEM_CACHE_POSITIVE_TTL_SECONDS,  # noqa: F401 - re-exported
    cached_path_exists,
    cached_path_size,
    clean_text,
    invalidate_filesystem_cache_for_path,  # noqa: F401 - re-exported
    invalidate_filesystem_cache_for_paths,
    natural_sort_key,
)
from packet_pdf_detection import (
    IGNORED_SPREADSHEET_PATTERNS,  # noqa: F401 - re-exported
    MAX_NEST_SUMMARY_DEPTH,  # noqa: F401 - re-exported
    SUPPORTED_PREVIEW_SUFFIXES,  # noqa: F401 - re-exported
    SUPPORTED_SPREADSHEET_SUFFIXES,  # noqa: F401 - re-exported
    detect_assembly_packet_pdf,  # noqa: F401 - re-exported; used by main_window.py/packet_build_controller.py
    detect_cut_list_packet_pdf,  # noqa: F401 - re-exported; used by main_window.py/packet_build_controller.py
    detect_preview_pdf,
    detect_print_packet_pdf,  # noqa: F401 - re-exported; used by main_window.py/packet_build_controller.py
    detect_spreadsheet,
)
from inventor_bridge import (
    DEFAULT_RADAN_CSV_IMPORT_ENTRY,  # noqa: F401 - re-exported; used by tests/test_services.py
    InventorToRadanInlineNeedsUi,  # noqa: F401 - re-exported; used by inventor_service.py
    launch_radan_csv_import,  # noqa: F401 - re-exported; used by radan_import_controller.py
    radan_csv_import_lock_status,  # noqa: F401 - re-exported; used by full_flow_service.py etc.
    radan_csv_missing_symbols,  # noqa: F401 - re-exported; used by radan_import_controller.py
    run_inventor_to_radan_inline,  # noqa: F401 - re-exported; used by inventor_service.py
    visible_radan_sessions,  # noqa: F401 - re-exported; used by full_flow_controller.py
)
from w_block_transfer import (
    BlockFileMatch,  # noqa: F401 - re-exported (public return-type surface)
    BlockFileTransferPlan,  # noqa: F401 - re-exported (public return-type surface)
    BlockFileTransferResult,  # noqa: F401 - re-exported (public return-type surface)
    DEFAULT_BLOCK_FILES_ROOT,  # noqa: F401 - re-exported
    DEFAULT_MACHINE_EIA_ROOT,  # noqa: F401 - re-exported
    DEFAULT_P_MACHINE_EIA_ROOT,  # noqa: F401 - re-exported
    build_project_block_transfer_plan,  # noqa: F401 - re-exported; used by block_transfer_controller.py
    machine_block_project_dir,  # noqa: F401 - re-exported (public return-type surface)
    machine_block_root_for_release_root,  # noqa: F401 - re-exported; used by tests/test_services.py
    send_project_block_files_to_machine,  # noqa: F401 - re-exported; used by block_transfer_controller.py
)

MINIMAL_RPD_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://www.radan.com/ns/project">
  <Name>{project_name}</Name>
  <Parts />
</Project>
"""
DEFAULT_VENV_PYTHON = Path(r"C:\Tools\.venv\Scripts\python.exe")
FLOW_TRUCK_REGISTRY_PATH = Path(r"C:\Tools\fabrication_flow_dashboard\truck_registry.csv")
TRUCK_FOLDER_PATTERN = re.compile(r"^[A-Z]\d{5}$", re.IGNORECASE)
OWNED_INVENTOR_OUTPUT_SUFFIXES = ("_Radan.csv", "_report.txt")
KIT_STATUS_CACHE_TTL_SECONDS = 30.0

KIT_STATUS_CACHE: BoundedTTLCache[tuple[KitStatus, ...]] = BoundedTTLCache(
    "kit_status",
    max_size=128,
    positive_ttl_seconds=KIT_STATUS_CACHE_TTL_SECONDS,
    negative_ttl_seconds=FILESYSTEM_CACHE_NEGATIVE_TTL_SECONDS,
)


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


def _status_cache_key(truck_number: str, settings: ExplorerSettings) -> tuple[str, str, str]:
    return ("kit_status", settings_cache_signature(settings), clean_text(truck_number).casefold())


def invalidate_status_cache_for_truck(truck_number: str, settings: ExplorerSettings | None = None) -> None:
    truck_key = clean_text(truck_number).casefold()
    if not truck_key:
        return
    if settings is not None:
        KIT_STATUS_CACHE.invalidate(_status_cache_key(truck_key, settings))
        return
    KIT_STATUS_CACHE.invalidate_where(
        lambda key, _value: isinstance(key, tuple)
        and len(key) == 3
        and key[0] == "kit_status"
        and str(key[2]).casefold() == truck_key
    )


def clear_performance_caches() -> None:
    FILE_METADATA_CACHE.clear()
    KIT_STATUS_CACHE.clear()


def configured_kit_mappings(settings: ExplorerSettings) -> list[KitMapping]:
    return build_kit_mappings(settings.kit_templates)


def is_standard_truck_number(truck_number: str) -> bool:
    truck = normalize_hidden_truck_number(truck_number)
    return bool(truck and truck.startswith("F"))


def odd_job_names_for_truck(truck_number: str, settings: ExplorerSettings) -> list[str]:
    truck = normalize_hidden_truck_number(truck_number)
    if not truck:
        return []
    jobs = settings.odd_jobs_by_truck.get(truck, [])
    cleaned: list[str] = []
    seen: set[str] = {mapping.kit_name.casefold() for mapping in configured_kit_mappings(settings)}
    for raw_job in jobs:
        job_name = canonicalize_kit_name(raw_job)
        if not job_name:
            continue
        key = job_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(job_name)
    return cleaned


def scaffold_kit_names_for_truck(
    truck_number: str,
    settings: ExplorerSettings,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> list[str]:
    if is_standard_truck_number(truck_number):
        kit_names = [mapping.kit_name for mapping in configured_kit_mappings(settings)]
    else:
        kit_names = discovered_fabrication_kit_names_for_job(truck_number, settings, fs_cache=fs_cache)
    kit_names.extend(odd_job_names_for_truck(truck_number, settings))
    return kit_names


def add_odd_job_to_truck(settings: ExplorerSettings, truck_number: str, kit_name: str) -> bool:
    truck = normalize_hidden_truck_number(truck_number)
    job_name = canonicalize_kit_name(kit_name)
    if not truck:
        raise ValueError("Truck number is required.")
    if not job_name:
        raise ValueError("Odd job name is required.")

    canonical_keys = {mapping.kit_name.casefold() for mapping in configured_kit_mappings(settings)}
    canonical_keys.update(mapping.display_name.casefold() for mapping in configured_kit_mappings(settings))
    if job_name.casefold() in canonical_keys:
        raise ValueError("That name is already a canonical kit.")

    jobs = list(settings.odd_jobs_by_truck.get(truck, []))
    if job_name.casefold() in {job.casefold() for job in jobs}:
        return False
    jobs.append(job_name)
    updated = dict(settings.odd_jobs_by_truck)
    updated[truck] = jobs
    settings.odd_jobs_by_truck = updated
    return True


def _truthy_registry_value(value: object) -> bool:
    text = clean_text(value).casefold()
    if not text:
        return True
    return text not in {"0", "false", "no", "n", "inactive", "archived"}


def active_registered_truck_numbers(registry_path: Path | str | None = None) -> set[str]:
    path = Path(str(registry_path)) if registry_path is not None else FLOW_TRUCK_REGISTRY_PATH
    if not path.exists():
        return set()
    numbers: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                truck = normalize_hidden_truck_number(row.get("truck_number", ""))
                if not truck:
                    continue
                if not _truthy_registry_value(row.get("is_active", "1")):
                    continue
                numbers.add(truck.upper())
    except OSError:
        return set()
    return numbers


def is_release_truck_discoverable(
    truck_number: str,
    release_truck_dir: Path | str | None,
    settings: ExplorerSettings,
) -> bool:
    truck = normalize_hidden_truck_number(truck_number)
    if not truck or not TRUCK_FOLDER_PATTERN.fullmatch(truck):
        return False
    if release_truck_dir is None or not Path(str(release_truck_dir)).exists():
        return False
    registered_trucks = active_registered_truck_numbers()
    if registered_trucks and is_standard_truck_number(truck):
        return truck.upper() in registered_trucks
    return True


def assert_truck_scaffold_allowed(truck_number: str, settings: ExplorerSettings) -> None:
    truck = normalize_hidden_truck_number(truck_number)
    registered_trucks = active_registered_truck_numbers()
    if not registered_trucks or not is_standard_truck_number(truck) or truck.upper() in registered_trucks:
        return
    raise RuntimeError(
        f"Refusing to create kit scaffolds for {truck or truck_number}. "
        f"It is not listed as an active whole-truck job in {FLOW_TRUCK_REGISTRY_PATH}."
    )


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


def restore_truck_visibility(truck_number: str, settings: ExplorerSettings) -> tuple[bool, int]:
    truck_key = normalize_hidden_truck_number(truck_number)
    if not truck_key:
        return False, 0

    hidden_trucks = normalize_hidden_truck_entries(settings.hidden_trucks)
    visible_trucks = [
        value
        for value in hidden_trucks
        if value.casefold() != truck_key.casefold()
    ]
    removed_truck = len(visible_trucks) != len(hidden_trucks)

    hidden_kits = canonicalize_hidden_kit_entries(settings.hidden_kits, settings.kit_templates)
    hidden_kit_prefix = f"{truck_key}::".casefold()
    visible_kits = [
        value
        for value in hidden_kits
        if not value.casefold().startswith(hidden_kit_prefix)
    ]
    removed_kit_count = len(hidden_kits) - len(visible_kits)

    settings.hidden_trucks = visible_trucks
    settings.hidden_kits = visible_kits
    return removed_truck, removed_kit_count


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


def explicit_truck_numbers(settings: ExplorerSettings) -> list[str]:
    candidates: list[object] = []
    candidates.extend(settings.truck_order)
    candidates.extend(settings.hidden_trucks)
    candidates.extend(settings.client_numbers_by_truck.keys())
    candidates.extend(settings.odd_jobs_by_truck.keys())
    for mapping in (settings.punch_codes_by_kit, settings.notes_by_kit):
        for key in mapping.keys():
            key_text = str(key or "")
            if "::" not in key_text:
                continue
            candidates.append(key_text.split("::", 1)[0])
    return normalize_truck_order_entries(candidates)


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


def release_root_for_job(truck_number: str, settings: ExplorerSettings) -> Path | None:
    truck = normalize_hidden_truck_number(truck_number)
    if truck.startswith("P"):
        configured_root = _path_from_setting(settings.release_root)
        if configured_root is not None:
            parent = configured_root.parent
            p_root = parent / PureWindowsPath(DEFAULT_P_RELEASE_ROOT).name
            if p_root.exists():
                return p_root
        return Path(DEFAULT_P_RELEASE_ROOT)
    return _path_from_setting(settings.release_root)


def _release_roots_for_discovery(settings: ExplorerSettings) -> tuple[Path, ...]:
    root = _path_from_setting(settings.release_root)
    if root is None:
        return ()
    candidates = (root, root.parent / PureWindowsPath(DEFAULT_P_RELEASE_ROOT).name)
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalize_cache_path(candidate)
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return tuple(roots)


def _existing_named_child(
    parent: Path | None,
    candidate_names: tuple[str, ...],
    *,
    want_dir: bool,
    cache: BoundedTTLCache[object] | None = None,
) -> Path | None:
    if parent is None or not cached_path_exists(parent, cache=cache) or not candidate_names:
        return None

    wanted_by_key = {
        str(name or "").strip().casefold(): str(name or "").strip()
        for name in candidate_names
        if str(name or "").strip()
    }
    if not wanted_by_key:
        return None

    key = (
        "existing_named_child",
        normalize_cache_path(parent),
        tuple(sorted(wanted_by_key)),
        bool(want_dir),
    )
    cache_obj = cache
    if cache_obj is not None:
        hit, value = cache_obj.get(key)
        if hit:
            return Path(str(value)) if value else None

    try:
        GLOBAL_METRICS.record_filesystem_check()
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
                result = Path(entry.path)
                if cache_obj is not None:
                    cache_obj.set(key, result, negative=False)
                return result
    except OSError:
        if cache_obj is not None:
            cache_obj.set(key, None, negative=True)
        return None
    if cache_obj is not None:
        cache_obj.set(key, None, negative=True)
    return None


def build_kit_paths(
    truck_number: str,
    kit_name: str,
    settings: ExplorerSettings,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> KitPaths:
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
    release_root = release_root_for_job(truck_text, settings)
    fabrication_root = _path_from_setting(settings.fabrication_root)

    release_truck_dir = release_root / truck_text if release_root else None
    release_kit_dir = release_truck_dir / kit_text if release_truck_dir else None
    existing_release_kit_dir = _existing_named_child(
        release_truck_dir,
        kit_name_candidates,
        want_dir=True,
        cache=fs_cache,
    )
    if existing_release_kit_dir is not None:
        release_kit_dir = existing_release_kit_dir

    project_dir = release_kit_dir / project_name if release_kit_dir else None
    existing_project_dir = _existing_named_child(
        release_kit_dir,
        project_name_candidates,
        want_dir=True,
        cache=fs_cache,
    )
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
            cache=fs_cache,
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
    for root in _release_roots_for_discovery(settings):
        if not cached_path_exists(root):
            continue
        try:
            GLOBAL_METRICS.record_filesystem_check()
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir():
                            continue
                        truck = normalize_hidden_truck_number(entry.name)
                        if not is_standard_truck_number(truck):
                            continue
                        if is_release_truck_discoverable(truck, Path(entry.path), settings):
                            names.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            pass

    for truck in explicit_truck_numbers(settings):
        if find_fabrication_truck_dir(truck, settings) is not None:
            names.add(truck)
    return sorted(names, key=natural_sort_key)


def find_fabrication_truck_dir(truck_number: str, settings: ExplorerSettings) -> Path | None:
    wanted = clean_text(truck_number)
    if not wanted:
        return None
    fabrication_root = _path_from_setting(settings.fabrication_root)
    if fabrication_root is None or not cached_path_exists(fabrication_root):
        return None

    try:
        GLOBAL_METRICS.record_filesystem_check()
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


def discovered_fabrication_kit_names_for_job(
    truck_number: str,
    settings: ExplorerSettings,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> list[str]:
    fabrication_truck_dir = find_fabrication_truck_dir(truck_number, settings)
    if fabrication_truck_dir is None or not cached_path_exists(fabrication_truck_dir, cache=fs_cache):
        return []
    names: list[str] = []
    seen: set[str] = set()
    try:
        GLOBAL_METRICS.record_filesystem_check()
        with os.scandir(fabrication_truck_dir) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                name = clean_text(entry.name)
                if not name or name.casefold() in {folder.casefold() for folder in DEFAULT_SUPPORT_FOLDERS}:
                    continue
                key = name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                names.append(name)
    except OSError:
        return []
    return sorted(names, key=natural_sort_key)


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
    assert_truck_scaffold_allowed(truck_number, settings)
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

    invalidate_status_cache_for_truck(truck_number, settings)
    invalidate_filesystem_cache_for_paths(tuple(created))
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


def fabrication_kit_dir_ready(fabrication_kit_dir: Path | None) -> bool:
    """Is the W-side fabrication kit folder present on disk right now?

    This is the single readiness predicate for "can we build/open packets
    for this kit" - it used to be reimplemented independently as the same
    inline expression in main_window.py (four times, across
    _recommended_action_for_status/_available_actions_for_status) and in
    controllers/packet_build_controller.py's prepare_context (negated, as
    a pre-build guard). A deliberately live filesystem check (not the
    cached KitStatus.fabrication_folder_exists field) so callers that are
    about to act on the folder - or a UI rendering pass right after an
    action - see the current state.
    """
    return fabrication_kit_dir is not None and fabrication_kit_dir.exists()


def fabrication_folder_has_files(
    folder: Path | None,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> bool:
    if folder is None:
        return False
    cache_obj = fs_cache
    key = ("fabrication_has_files", normalize_cache_path(folder))
    if cache_obj is not None:
        hit, value = cache_obj.get(key)
        if hit:
            return bool(value)
    if not cached_path_exists(folder, cache=cache_obj):
        if cache_obj is not None:
            cache_obj.set(key, False, negative=True)
        return False
    stack: list[Path] = [folder]
    while stack:
        current = stack.pop()
        try:
            GLOBAL_METRICS.record_filesystem_check()
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            if cache_obj is not None:
                                cache_obj.set(key, True, negative=False)
                            return True
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    if cache_obj is not None:
        cache_obj.set(key, False, negative=True)
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


def build_kit_status(
    truck_number: str,
    kit_name: str,
    settings: ExplorerSettings,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> KitStatus:
    cache_obj = fs_cache
    paths = build_kit_paths(truck_number, kit_name, settings, fs_cache=cache_obj)
    release_folder_exists = bool(paths.release_kit_dir and cached_path_exists(paths.release_kit_dir, cache=cache_obj))
    project_folder_exists = bool(paths.project_dir and cached_path_exists(paths.project_dir, cache=cache_obj))
    rpd_exists = bool(paths.rpd_path and cached_path_exists(paths.rpd_path, cache=cache_obj))
    rpd_size_bytes = cached_path_size(paths.rpd_path, cache=cache_obj) if rpd_exists else 0
    fabrication_folder_exists = bool(
        paths.fabrication_kit_dir and cached_path_exists(paths.fabrication_kit_dir, cache=cache_obj)
    )
    fabrication_has_files = fabrication_folder_has_files(paths.fabrication_kit_dir, fs_cache=cache_obj)
    spreadsheet_match = detect_spreadsheet(paths.fabrication_kit_dir, fs_cache=cache_obj)
    preview_pdf_match = detect_preview_pdf(paths, fs_cache=cache_obj)
    outputs = None
    if spreadsheet_match.chosen_path is not None:
        outputs = inventor_output_paths(spreadsheet_match.chosen_path, paths.project_dir)

    summary_parts: list[str] = []
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
    if spreadsheet_match.issue == "multiple_spreadsheets":
        summary_parts.append("BOM ambiguous")
    elif fabrication_folder_exists and spreadsheet_match.chosen_path is None:
        summary_parts.append("BOM missing")
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


def collect_kit_statuses(
    truck_number: str,
    settings: ExplorerSettings,
    *,
    use_cache: bool = True,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> list[KitStatus]:
    status_key = _status_cache_key(truck_number, settings)
    if use_cache:
        hit, cached = KIT_STATUS_CACHE.get(status_key)
        if hit and cached is not None:
            return list(cached)
    cache_obj = fs_cache or FILE_METADATA_CACHE
    kit_names = scaffold_kit_names_for_truck(truck_number, settings, fs_cache=cache_obj)
    statuses = [
        build_kit_status(truck_number, kit_name, settings, fs_cache=cache_obj)
        for kit_name in kit_names
    ]
    if use_cache:
        KIT_STATUS_CACHE.set(status_key, tuple(statuses), negative=not statuses)
    return statuses


def open_path(path: Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if os.name == "nt":
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_hidden_process_kwargs(),
        )
        return
    os.startfile(str(target))  # type: ignore[attr-defined]


def open_external_target(target: str | Path) -> None:
    text = clean_text(target).strip('"')
    if not text:
        raise FileNotFoundError("No external target was provided.")
    if "://" in text:
        if os.name == "nt":
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", text],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_hidden_process_kwargs(),
            )
            return
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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_hidden_process_kwargs(),
    )


def launch_launcher(launcher_path: Path | str, argument_path: Path | str) -> None:
    launch_tool(launcher_path, argument_path=argument_path)


def _python_executable() -> str:
    if DEFAULT_VENV_PYTHON.exists():
        return str(DEFAULT_VENV_PYTHON)
    raise FileNotFoundError(f"Shared venv Python was not found: {DEFAULT_VENV_PYTHON}")


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def _hidden_process_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {}
    startupinfo = _hidden_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    return kwargs


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

    invalidate_filesystem_cache_for_paths(tuple(moved))
    return outputs, tuple(moved)
