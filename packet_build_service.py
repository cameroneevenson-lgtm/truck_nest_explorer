from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from types import SimpleNamespace
from typing import Callable, Optional, Sequence
import xml.etree.ElementTree as ET

from models import DEFAULT_P_RELEASE_ROOT, ExplorerSettings

DEFAULT_PACKET_OUT_DIR = "_out"
ASSEMBLY_PACKET_PREFIX = "AssemblyPacket_TABLOID"
CUT_LIST_PACKET_PREFIX = "CutList"
TABLOID_WIDTH_POINTS = 11.0 * 72.0
TABLOID_HEIGHT_POINTS = 17.0 * 72.0
ARCH_D_WIDTH_POINTS = 34.0 * 72.0
ARCH_D_HEIGHT_POINTS = 22.0 * 72.0
TABLOID_TOLERANCE_POINTS = 18.0
IGNORED_PACKET_SOURCE_DIR_NAMES = {
    DEFAULT_PACKET_OUT_DIR.casefold(),
    "_bak",
    "_kits",
    "design data",
    "old",
    "oldversions",
    "template",
    "templates",
}
IGNORED_PACKET_SOURCE_DIR_PREFIXES = ("additional work", "addtional work")
REVISION_SUFFIX_PATTERN = re.compile(r"(?:[\s-]+R\d+[A-Z]?)$", re.IGNORECASE)
RPD_QUANTITY_TAGS = ("Qty", "QTY", "Quantity", "Count", "Num", "Number", "Instances")


@dataclass(frozen=True)
class PacketBuildContext:
    parts: tuple[object, ...]
    resolve_asset_fn: Callable[[str, str], Optional[str]]
    assembly_source_pdfs: tuple[Path, ...]
    cut_list_source_pdfs: tuple[Path, ...]
    assembly_search_roots: tuple[Path, ...]


def _ensure_radan_kitter_on_path() -> None:
    radan_dir = Path(__file__).resolve().parents[1] / "radan_kitter"
    radan_dir_text = str(radan_dir)
    if radan_dir_text not in sys.path:
        sys.path.append(radan_dir_text)


def _load_radan_kitter_modules():
    _ensure_radan_kitter_on_path()
    import assets as rk_assets  # type: ignore[import-not-found]
    import packet_runtime as rk_packet_runtime  # type: ignore[import-not-found]
    import rpd_io as rk_rpd_io  # type: ignore[import-not-found]

    return rk_assets, rk_packet_runtime, rk_rpd_io


def _load_radan_kitter_sym_io():
    _ensure_radan_kitter_on_path()
    import sym_io as rk_sym_io  # type: ignore[import-not-found]

    return rk_sym_io


def _fitz_module():
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError("PyMuPDF (fitz) is required to build assembly packets.") from exc
    return fitz


@dataclass(frozen=True)
class AssemblyPacketBuildResult:
    packet_path: str
    source_documents: int
    output_pages: int
    skipped: bool = False
    source_pdfs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssemblyBomReference:
    part_name: str
    assembly_name: str
    assembly_pdf_path: str
    page_number: int
    bom_qty: int
    evidence: str


@dataclass(frozen=True)
class AssemblyBomContextResult:
    assembly_pdf_count: int
    checked_part_count: int
    references: tuple[AssemblyBomReference, ...]
    read_errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AssemblySymCommentUpdateResult:
    updated_count: int
    skipped_count: int
    missing_count: int
    errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _PartAlias:
    part_name: str
    alias: str
    pattern: re.Pattern[str]


class PacketBuildReadinessError(RuntimeError):
    """Raised when an RPD is not populated enough for a packet build."""


def _make_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize_path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _local_xml_name(tag: str) -> str:
    return re.sub(r"^\{.*\}", "", str(tag or "")).strip()


def _find_child_text_by_local_name(el: ET.Element, tag_names: Sequence[str]) -> str:
    wanted = {str(name or "").casefold() for name in tag_names if str(name or "").strip()}
    for child in list(el):
        if _local_xml_name(str(child.tag)).casefold() not in wanted:
            continue
        text = str(child.text or "").strip()
        if text:
            return text
    return ""


def _parse_positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception as exc:
        raise PacketBuildReadinessError(f"{field_name} is not a valid positive integer: {value!r}") from exc
    if parsed <= 0:
        raise PacketBuildReadinessError(f"{field_name} must be greater than zero: {value!r}")
    return parsed


def _part_key_from_symbol_path(path_text: str) -> str:
    return Path(str(path_text or "").strip()).stem.casefold()


def _part_display_name(part: object) -> str:
    value = str(getattr(part, "part", "") or "").strip()
    if value:
        return value
    return Path(str(getattr(part, "sym", "") or "").strip()).stem.strip()


def _part_sym_path(part: object) -> Path | None:
    sym_text = str(getattr(part, "sym", "") or "").strip()
    if not sym_text:
        return None
    return Path(sym_text)


def _part_alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)


def _part_aliases(part_name: str) -> tuple[str, ...]:
    clean = str(part_name or "").strip()
    if not clean:
        return ()
    aliases = [clean]
    match = re.match(r"^(F\d{3,})[-_\s]+(.+)$", clean, flags=re.IGNORECASE)
    if match:
        suffix = match.group(2).strip()
        aliases.append(suffix)
        aliases.append(f"{match.group(1)} {suffix}")
    seen: set[str] = set()
    out: list[str] = []
    for alias in aliases:
        key = alias.casefold()
        if not alias or key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return tuple(out)


def _build_part_aliases(parts: Sequence[object]) -> tuple[_PartAlias, ...]:
    aliases: list[_PartAlias] = []
    seen: set[tuple[str, str]] = set()
    for part in parts:
        part_name = _part_display_name(part)
        for alias in _part_aliases(part_name):
            key = (part_name.casefold(), alias.casefold())
            if key in seen:
                continue
            seen.add(key)
            aliases.append(_PartAlias(part_name=part_name, alias=alias, pattern=_part_alias_pattern(alias)))
    aliases.sort(key=lambda item: len(item.alias), reverse=True)
    return tuple(aliases)


def _text_line_evidence(text: str, pattern: re.Pattern[str]) -> str:
    for raw_line in str(text or "").replace("\r", "\n").splitlines():
        line = " ".join(raw_line.split())
        if line and pattern.search(line):
            return line[:260]
    match = pattern.search(str(text or ""))
    if not match:
        return ""
    start = max(0, match.start() - 100)
    end = min(len(text), match.end() + 100)
    return " ".join(text[start:end].split())[:260]


def _quantity_from_bom_evidence(evidence: str, alias: str) -> int:
    text = str(evidence or "").strip()
    if not text:
        return 0
    match = re.search(r"\bqty\b\s*[:#-]?\s*(\d{1,4})\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    alias_match = re.search(re.escape(str(alias or "").strip()), text, flags=re.IGNORECASE) if str(alias or "").strip() else None
    after_alias: list[int] = []
    before_alias: list[int] = []
    anywhere: list[int] = []
    for number in re.finditer(r"\b(\d{1,4})\b", text):
        value = int(number.group(1))
        if value <= 0:
            continue
        anywhere.append(value)
        if alias_match is None:
            continue
        # Part numbers carry digits; do not treat those digits as BOM quantities.
        if number.start() >= alias_match.start() and number.end() <= alias_match.end():
            continue
        if number.start() >= alias_match.end():
            after_alias.append(value)
        elif number.end() <= alias_match.start():
            before_alias.append(value)
    if after_alias:
        return after_alias[-1]
    if before_alias:
        return before_alias[-1]
    return anywhere[-1] if anywhere else 0


def assembly_comment_shorthand(assembly_name: str) -> str:
    # Full raw assembly name (the source drawing PDF's filename stem) -
    # deliberately not shortened to a last-hyphen token or revision-stripped
    # anymore, so the .sym comment and packet note both show the real name.
    return str(assembly_name or "").strip()


def _read_text_fallback(path: Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _write_text_utf8(path: Path, text: str) -> None:
    path = Path(path)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(text.encode("utf-8"))
    os.replace(tmp_path, path)


def _backup_sym_before_comment_update(sym_path: Path, backup_dir: Path | None) -> None:
    if backup_dir is None:
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    candidate = backup_dir / sym_path.name
    if candidate.exists():
        digest = hashlib.sha1(str(sym_path.resolve()).casefold().encode("utf-8")).hexdigest()[:8]
        candidate = backup_dir / f"{sym_path.stem}.{digest}{sym_path.suffix}"
    candidate.write_bytes(sym_path.read_bytes())


def _append_assembly_shorthands_to_comment(existing_comment: str, shorthands: Sequence[str]) -> str:
    clean_shorthands: list[str] = []
    seen: set[str] = set()
    for shorthand in shorthands:
        clean = str(shorthand or "").strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean_shorthands.append(clean)
    if not clean_shorthands:
        return str(existing_comment or "").strip()

    comment = str(existing_comment or "").strip()
    marker_match = re.search(r"(?:^|\s\|\s)ASM:\s*([^|]*)", comment, flags=re.IGNORECASE)
    existing_assemblies: list[str] = []
    base_comment = comment
    if marker_match:
        existing_assemblies = [item.strip() for item in re.split(r"[,;/]", marker_match.group(1)) if item.strip()]
        base_comment = (comment[: marker_match.start()] + comment[marker_match.end() :]).strip(" |")

    combined: list[str] = []
    seen_combined: set[str] = set()
    for item in [*existing_assemblies, *clean_shorthands]:
        key = item.casefold()
        if key in seen_combined:
            continue
        seen_combined.add(key)
        combined.append(item)

    assembly_text = "ASM: " + ", ".join(combined)
    if not base_comment:
        return assembly_text
    return f"{base_comment} | {assembly_text}"


def _shorthands_by_part(result: AssemblyBomContextResult) -> dict[str, list[str]]:
    shorthands_by_part: dict[str, list[str]] = {}
    for ref in result.references:
        shorthand = assembly_comment_shorthand(ref.assembly_name)
        if shorthand:
            shorthands_by_part.setdefault(ref.part_name.casefold(), []).append(shorthand)
    return shorthands_by_part


def assembly_notes_by_part(parts: Sequence[object], result: AssemblyBomContextResult) -> dict[str, str]:
    """Per-part assembly note text (joined, comma-separated), keyed by
    _part_display_name(part).casefold() - the same matching used by
    apply_assembly_context_to_sym_comments, exposed separately so callers
    can also stamp the note on the print packet (PartRow.assembly_note)
    without re-scanning or duplicating the .sym comment write."""
    shorthands_by_part = _shorthands_by_part(result)
    notes: dict[str, str] = {}
    for part in parts:
        part_key = _part_display_name(part).casefold()
        shorthands = shorthands_by_part.get(part_key)
        if shorthands:
            notes[part_key] = ", ".join(shorthands)
    return notes


def apply_assembly_notes_to_parts(parts: Sequence[object], result: AssemblyBomContextResult) -> None:
    """Set part.assembly_note in place for each part with a match, so the
    print packet build (packet_service.build_packet -> PartRow.assembly_note)
    picks it up. Must run before the print packet is built, since building
    it also un-annotated is what leaves the note off today."""
    notes_by_part = assembly_notes_by_part(parts, result)
    for part in parts:
        if not hasattr(part, "assembly_note"):
            continue
        part_key = _part_display_name(part).casefold()
        part.assembly_note = notes_by_part.get(part_key, "")


def apply_assembly_context_to_sym_comments(
    *,
    parts: Sequence[object],
    result: AssemblyBomContextResult,
    backup_dir: Path | None = None,
) -> AssemblySymCommentUpdateResult:
    rk_sym_io = _load_radan_kitter_sym_io()
    sym_by_part: dict[str, Path] = {}
    for part in parts:
        part_name = _part_display_name(part)
        sym_path = _part_sym_path(part)
        if part_name and sym_path is not None:
            sym_by_part.setdefault(part_name.casefold(), sym_path)

    shorthands_by_part = _shorthands_by_part(result)

    updated = 0
    skipped = 0
    missing = 0
    errors: list[tuple[str, str]] = []
    for part_key, shorthands in sorted(shorthands_by_part.items(), key=lambda item: _natural_sort_key(item[0])):
        sym_path = sym_by_part.get(part_key)
        if sym_path is None:
            skipped += 1
            continue
        if not sym_path.exists():
            missing += 1
            continue
        try:
            text = _read_text_fallback(sym_path)
            updated_comment = _append_assembly_shorthands_to_comment(
                rk_sym_io.part_comment_from_text(text),
                shorthands,
            )
            updated_text, found_comment = rk_sym_io.set_part_comment_text(text, updated_comment)
            if not found_comment:
                skipped += 1
                continue
            if updated_text != text:
                _backup_sym_before_comment_update(sym_path, backup_dir)
                _write_text_utf8(sym_path, updated_text)
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append((str(sym_path), str(exc)))
    return AssemblySymCommentUpdateResult(
        updated_count=updated,
        skipped_count=skipped,
        missing_count=missing,
        errors=tuple(errors),
    )


def _aggregate_quantities(rows: Sequence[tuple[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for part_key, quantity in rows:
        key = str(part_key or "").strip().casefold()
        if not key:
            continue
        totals[key] = totals.get(key, 0) + int(quantity)
    return totals


def _read_radan_csv_quantities(csv_path: Path) -> dict[str, int]:
    rows: list[tuple[str, int]] = []
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.reader(handle), start=1):
            if not row or all(not cell.strip() for cell in row):
                continue
            dxf_text = row[0].strip()
            if not dxf_text:
                continue
            if len(row) < 2 or not row[1].strip():
                raise PacketBuildReadinessError(f"RADAN CSV row {index} is missing its quantity column.")
            rows.append((_part_key_from_symbol_path(dxf_text), _parse_positive_int(row[1], field_name=f"RADAN CSV row {index} quantity")))
    return _aggregate_quantities(rows)


def _read_explicit_rpd_quantities(rpd_path: Path) -> dict[str, int]:
    tree = ET.parse(rpd_path)
    rows: list[tuple[str, int]] = []
    missing_symbol_rows = 0
    missing_quantity_rows: list[str] = []
    for el in tree.getroot().iter():
        if _local_xml_name(str(el.tag)).casefold() != "part":
            continue
        symbol_text = _find_child_text_by_local_name(el, ("Symbol",))
        if not symbol_text:
            missing_symbol_rows += 1
            continue
        part_key = _part_key_from_symbol_path(symbol_text)
        quantity_text = _find_child_text_by_local_name(el, RPD_QUANTITY_TAGS)
        if not quantity_text:
            missing_quantity_rows.append(Path(symbol_text).stem or symbol_text)
            continue
        rows.append((part_key, _parse_positive_int(quantity_text, field_name=f"RPD quantity for {Path(symbol_text).stem}")))

    if missing_symbol_rows:
        raise PacketBuildReadinessError(f"The RPD has {missing_symbol_rows} part row(s) without a Symbol path.")
    if missing_quantity_rows:
        sample = ", ".join(missing_quantity_rows[:8])
        suffix = "" if len(missing_quantity_rows) <= 8 else f", and {len(missing_quantity_rows) - 8} more"
        raise PacketBuildReadinessError(
            "The RPD has part rows without explicit quantities. "
            f"Finish the RADAN CSV import before building the print packet. Missing quantity: {sample}{suffix}"
        )
    return _aggregate_quantities(rows)


def _quantity_mismatch_message(expected: dict[str, int], actual: dict[str, int], expected_csv_path: Path) -> str:
    missing = sorted(set(expected) - set(actual), key=_natural_sort_key)
    extra = sorted(set(actual) - set(expected), key=_natural_sort_key)
    mismatched = sorted(
        (key for key in set(expected) & set(actual) if int(expected[key]) != int(actual[key])),
        key=_natural_sort_key,
    )
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing[:8]) + ("" if len(missing) <= 8 else f", and {len(missing) - 8} more"))
    if extra:
        details.append("unexpected " + ", ".join(extra[:8]) + ("" if len(extra) <= 8 else f", and {len(extra) - 8} more"))
    if mismatched:
        sample = ", ".join(f"{key} CSV={expected[key]} RPD={actual[key]}" for key in mismatched[:8])
        details.append("quantity mismatch " + sample + ("" if len(mismatched) <= 8 else f", and {len(mismatched) - 8} more"))
    detail_text = "; ".join(details) if details else "part quantities do not match"
    return (
        "The selected RPD does not match the generated RADAN CSV. "
        "That can be OK if you intentionally removed or changed parts in RADAN.\n\n"
        f"{expected_csv_path.name} has {len(expected)} part(s), but the saved RPD has {len(actual)} populated part(s): "
        f"{detail_text}."
    )


def validate_print_packet_readiness(
    *,
    rpd_path: Path,
    parts: Sequence[object],
    expected_csv_path: Path | None = None,
) -> str | None:
    """Fail fast for unusable RPDs; return a warning for CSV/RPD drift."""

    if not parts:
        raise PacketBuildReadinessError(
            "The selected RPD has no part rows yet. Finish the RADAN CSV import before building the print packet."
        )

    actual_quantities = _read_explicit_rpd_quantities(Path(rpd_path))
    if not actual_quantities:
        raise PacketBuildReadinessError(
            "The selected RPD has no populated part quantities yet. Finish the RADAN CSV import before building the print packet."
        )

    if expected_csv_path is None or not Path(expected_csv_path).exists():
        return None

    try:
        expected_quantities = _read_radan_csv_quantities(Path(expected_csv_path))
    except PacketBuildReadinessError as exc:
        return (
            "The saved RPD can be used, but the generated RADAN CSV could not be checked.\n\n"
            f"{Path(expected_csv_path).name}: {exc}"
        )
    if expected_quantities and actual_quantities != expected_quantities:
        return _quantity_mismatch_message(expected_quantities, actual_quantities, Path(expected_csv_path))
    return None


def _natural_sort_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _normalize_pdf_name_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _looks_generated_pdf_artifact(path: Path) -> bool:
    stem_words = _normalize_pdf_name_words(path.stem)
    return (
        stem_words.startswith("print packet")
        or stem_words.startswith("printpacket")
        or stem_words.startswith("assembly packet")
        or stem_words.startswith("assemblypacket")
        or stem_words.startswith("cut list")
        or stem_words.startswith("cutlist")
        or stem_words.endswith("nest summary")
        or " print packet " in f" {stem_words} "
        or " printpacket " in f" {stem_words} "
        or " assembly packet " in f" {stem_words} "
        or " assemblypacket " in f" {stem_words} "
        or " cut list " in f" {stem_words} "
        or " cutlist " in f" {stem_words} "
        or " nest summary " in f" {stem_words} "
    )


def _sorted_relative_key(path: Path, root: Path) -> tuple[int, list[object]]:
    try:
        relative = path.relative_to(root)
        relative_text = str(relative)
        depth = max(0, len(relative.parts) - 1)
    except ValueError:
        relative_text = str(path)
        depth = 99
    return depth, _natural_sort_key(relative_text)


def _is_tabloid_size(width_points: float, height_points: float) -> bool:
    dims = sorted((float(width_points), float(height_points)))
    target = sorted((TABLOID_WIDTH_POINTS, TABLOID_HEIGHT_POINTS))
    return all(abs(dims[index] - target[index]) <= TABLOID_TOLERANCE_POINTS for index in range(2))


def _is_drawing_sheet_size(width_points: float, height_points: float) -> bool:
    dims = sorted((float(width_points), float(height_points)))
    targets = (
        sorted((TABLOID_WIDTH_POINTS, TABLOID_HEIGHT_POINTS)),
        sorted((ARCH_D_WIDTH_POINTS, ARCH_D_HEIGHT_POINTS)),
    )
    return any(
        all(abs(dims[index] - target[index]) <= TABLOID_TOLERANCE_POINTS for index in range(2))
        for target in targets
    )


def _is_tabloid_pdf(path: Path) -> bool:
    return bool(_tabloid_page_indices(path))


def _tabloid_page_indices(path: Path) -> tuple[int, ...]:
    return _page_indices_matching_size(path, _is_tabloid_size)


def _drawing_page_indices(path: Path) -> tuple[int, ...]:
    return _page_indices_matching_size(path, _is_drawing_sheet_size)


def _page_indices_matching_size(path: Path, matches_size_fn: Callable[[float, float], bool]) -> tuple[int, ...]:
    try:
        fitz = _fitz_module()
        with fitz.open(str(path)) as doc:
            indices: list[int] = []
            for index in range(doc.page_count):
                rect = doc[index].rect
                if matches_size_fn(float(rect.width), float(rect.height)):
                    indices.append(index)
            return tuple(indices)
    except Exception:
        return ()


def _is_ignored_packet_source_path(path: Path) -> bool:
    for part in path.parts:
        part_key = part.casefold()
        if part_key in IGNORED_PACKET_SOURCE_DIR_NAMES:
            return True
        if any(part_key.startswith(prefix) for prefix in IGNORED_PACKET_SOURCE_DIR_PREFIXES):
            return True
    return False


def _iter_pdf_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []

    discovered: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        if _is_ignored_packet_source_path(current_dir):
            continue
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.casefold() != ".pdf":
                continue
            discovered.append(path)
    return discovered


def _assembly_search_roots(*roots: Path | None) -> tuple[Path, ...]:
    collected: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root is None:
            continue
        path = Path(root)
        key = _normalize_path_key(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        collected.append(path)
    return tuple(collected)


def _matched_part_pdf_keys(
    parts: Sequence[object],
    *,
    resolve_asset_fn: Callable[[str, str], Optional[str]],
) -> set[str]:
    matched: set[str] = set()
    for part in parts:
        sym_path = str(getattr(part, "sym", "") or "").strip()
        if not sym_path:
            continue
        pdf_path = resolve_asset_fn(sym_path, ".pdf")
        if not pdf_path or not os.path.exists(pdf_path):
            continue
        matched.add(_normalize_path_key(pdf_path))
    return matched


def _revision_base_stem(stem: str) -> str:
    base = REVISION_SUFFIX_PATTERN.sub("", str(stem or "")).strip(" -")
    return base if base and base != stem else ""


def _asset_stem(path_text: str) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    return PureWindowsPath(text).stem or Path(text).stem


def _pdf_part_stem_key(stem: str) -> str:
    base = _revision_base_stem(stem) or str(stem or "").strip()
    return re.sub(r"[^a-z0-9]+", "", base.casefold())


def _find_part_pdf_in_fabrication_dir(sym_path: str, fabrication_dir: Path | None) -> str | None:
    if fabrication_dir is None:
        return None

    root = Path(fabrication_dir)
    if not root.exists():
        return None

    target_stem = _asset_stem(sym_path)
    target_key = _pdf_part_stem_key(target_stem)
    if not target_key:
        return None

    candidates: list[tuple[int, tuple[int, list[object]], Path]] = []
    for pdf_path in _iter_pdf_paths(root):
        if _looks_generated_pdf_artifact(pdf_path):
            continue
        if _pdf_part_stem_key(pdf_path.stem) != target_key:
            continue
        candidate_base = _revision_base_stem(pdf_path.stem) or pdf_path.stem
        rank = 0 if pdf_path.stem.casefold() == target_stem.casefold() else 1
        if candidate_base.casefold() != target_stem.casefold():
            rank = 2
        candidates.append((rank, _sorted_relative_key(pdf_path, root), pdf_path))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return str(candidates[0][2])


def _kit_packet_asset_resolver(
    base_resolve_asset_fn: Callable[[str, str], Optional[str]],
    *,
    fabrication_dir: Path | None,
) -> Callable[[str, str], Optional[str]]:
    def _resolve(sym_path: str, ext: str) -> Optional[str]:
        resolved = base_resolve_asset_fn(sym_path, ext)
        if resolved and os.path.exists(resolved):
            return resolved

        clean_ext = str(ext or "").strip().lower()
        if clean_ext and not clean_ext.startswith("."):
            clean_ext = "." + clean_ext
        if clean_ext == ".pdf":
            fallback = _find_part_pdf_in_fabrication_dir(sym_path, fabrication_dir)
            if fallback:
                return fallback

        return resolved

    return _resolve


def _inventor_sibling_exists(path: Path, suffix: str) -> bool:
    if path.with_suffix(suffix).exists():
        return True
    base_stem = _revision_base_stem(path.stem)
    return bool(base_stem and path.with_name(f"{base_stem}{suffix}").exists())


def _has_assembly_inventor_source(path: Path) -> bool:
    return _inventor_sibling_exists(path, ".iam")


def _inventor_to_radan_dir(settings: ExplorerSettings) -> Path:
    entry_text = str(settings.inventor_to_radan_entry or "").strip()
    if entry_text:
        entry = Path(entry_text)
        if entry.suffix.casefold() == ".py":
            return entry.parent
        return entry.parent
    return Path(__file__).resolve().parents[1] / "inventor_to_radan"


def _load_nonlaser_tokens(settings: ExplorerSettings) -> frozenset[str]:
    token_csv = _inventor_to_radan_dir(settings) / "nonlaser_tokens.csv"
    if not token_csv.exists():
        return frozenset()
    tokens: set[str] = set()
    try:
        with token_csv.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                token = str(row.get("Token") or "").strip()
                if token:
                    tokens.add(token.casefold())
    except OSError:
        return frozenset()
    return frozenset(tokens)


def _first_filename_token(path: Path) -> str:
    parts = str(path.stem or "").strip().split()
    return parts[0].casefold() if parts else ""


def _has_cut_list_inventor_source(path: Path) -> bool:
    return _inventor_sibling_exists(path, ".ipt") and not _has_assembly_inventor_source(path)


def collect_cut_list_pdfs(
    *,
    search_roots: Sequence[Path],
    settings: ExplorerSettings,
) -> tuple[Path, ...]:
    tokens = _load_nonlaser_tokens(settings)
    if not search_roots or not tokens:
        return ()

    candidates: list[tuple[int, tuple[int, list[object]], Path]] = []
    seen: set[str] = set()
    for root_index, root in enumerate(search_roots):
        for pdf_path in _iter_pdf_paths(root):
            key = _normalize_path_key(pdf_path)
            if key in seen:
                continue
            seen.add(key)
            if _looks_generated_pdf_artifact(pdf_path):
                continue
            if _first_filename_token(pdf_path) not in tokens:
                continue
            if not _has_cut_list_inventor_source(pdf_path):
                continue
            candidates.append((root_index, _sorted_relative_key(pdf_path, root), pdf_path))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return tuple(path for _root_index, _relative_key, path in candidates)


def _configure_asset_lookup(rk_assets, settings: ExplorerSettings) -> None:
    release_root = os.path.normpath(str(settings.release_root or "").strip()) if str(settings.release_root or "").strip() else ""
    fabrication_root = (
        os.path.normpath(str(settings.fabrication_root or "").strip())
        if str(settings.fabrication_root or "").strip()
        else ""
    )

    eng_release_map: list[tuple[str, str]] = []
    if release_root and fabrication_root:
        eng_release_map.append((release_root, fabrication_root))
        release_parent = os.path.dirname(release_root.rstrip("\\/"))
        if release_parent:
            p_release_root = os.path.normpath(
                os.path.join(release_parent, PureWindowsPath(DEFAULT_P_RELEASE_ROOT).name)
            )
            if p_release_root.lower() != release_root.lower():
                eng_release_map.append((p_release_root, fabrication_root))
        if release_parent and release_parent.lower() != release_root.lower():
            eng_release_map.append((release_parent, fabrication_root))

    rk_assets.configure_release_mapping(
        w_release_root=fabrication_root or None,
        eng_release_map=eng_release_map or None,
    )


def collect_unused_tabloid_pdfs(
    parts: Sequence[object],
    *,
    search_roots: Sequence[Path],
    resolve_asset_fn: Callable[[str, str], Optional[str]],
) -> tuple[Path, ...]:
    if not search_roots:
        return ()

    matched_pdf_keys = _matched_part_pdf_keys(parts, resolve_asset_fn=resolve_asset_fn)
    candidates: list[tuple[int, tuple[int, list[object]], Path]] = []
    seen: set[str] = set()
    for root_index, root in enumerate(search_roots):
        for pdf_path in _iter_pdf_paths(root):
            key = _normalize_path_key(pdf_path)
            if key in seen or key in matched_pdf_keys:
                continue
            seen.add(key)
            if _looks_generated_pdf_artifact(pdf_path):
                continue
            if not _has_assembly_inventor_source(pdf_path):
                continue
            if not _drawing_page_indices(pdf_path):
                continue
            candidates.append((root_index, _sorted_relative_key(pdf_path, root), pdf_path))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return tuple(path for _root_index, _relative_key, path in candidates)


def prepare_packet_build_context(
    *,
    rpd_path: Path,
    fabrication_dir: Path | None,
    settings: ExplorerSettings,
    include_assembly_sources: bool = True,
    include_cut_list_sources: bool = True,
) -> PacketBuildContext:
    if not rpd_path.exists():
        raise FileNotFoundError(str(rpd_path))

    rk_assets, _rk_packet_runtime, rk_rpd_io = _load_radan_kitter_modules()
    _configure_asset_lookup(rk_assets, settings)

    _tree, parts, _debug = rk_rpd_io.load_rpd(str(rpd_path))
    # Packet builds are explicit user actions, so prefer the subtree-capable
    # resolver over the preview-optimized fast path. This covers cases where
    # W-side PDFs live under kit-specific subfolders such as PUMP PACK\PUMP HOUSE.
    resolve_asset_fn = _kit_packet_asset_resolver(
        rk_assets.resolve_asset,
        fabrication_dir=fabrication_dir,
    )
    assembly_search_roots = _assembly_search_roots(fabrication_dir, rpd_path.parent)
    assembly_source_pdfs = (
        collect_unused_tabloid_pdfs(
            parts,
            search_roots=assembly_search_roots,
            resolve_asset_fn=resolve_asset_fn,
        )
        if include_assembly_sources
        else ()
    )
    cut_list_source_pdfs = (
        collect_cut_list_pdfs(
            search_roots=assembly_search_roots,
            settings=settings,
        )
        if include_cut_list_sources
        else ()
    )
    return PacketBuildContext(
        parts=tuple(parts),
        resolve_asset_fn=resolve_asset_fn,
        assembly_source_pdfs=assembly_source_pdfs,
        cut_list_source_pdfs=cut_list_source_pdfs,
        assembly_search_roots=assembly_search_roots,
    )


def create_main_packet_worker(
    *,
    context: PacketBuildContext,
    rpd_path: Path,
    out_dirname: str = DEFAULT_PACKET_OUT_DIR,
    render_mode: str = "vector",
):
    _rk_assets, rk_packet_runtime, _rk_rpd_io = _load_radan_kitter_modules()
    return rk_packet_runtime.PacketBuildWorker(
        parts=list(context.parts),
        rpd_path=str(rpd_path),
        out_dirname=str(out_dirname or DEFAULT_PACKET_OUT_DIR),
        resolve_asset_fn=context.resolve_asset_fn,
        render_mode=str(render_mode or "vector").strip().lower() or "vector",
    )


def review_pdf_assets_for_action(
    *,
    parent,
    action_name: str,
    context: PacketBuildContext,
    rpd_path: Path,
    out_dirname: str = DEFAULT_PACKET_OUT_DIR,
) -> bool:
    _ensure_radan_kitter_on_path()
    import pdf_asset_review as rk_pdf_asset_review  # type: ignore[import-not-found]

    return bool(
        rk_pdf_asset_review.review_pdf_assets_for_action(
            parent=parent,
            action_name=action_name,
            parts=list(context.parts),
            rpd_path=str(rpd_path),
            resolve_asset_fn=context.resolve_asset_fn,
            out_dirname=str(out_dirname or DEFAULT_PACKET_OUT_DIR),
        )
    )


def scan_assembly_bom_context(
    *,
    parts: Sequence[object],
    source_pdfs: Sequence[Path],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_cancel_cb: Optional[Callable[[], bool]] = None,
) -> AssemblyBomContextResult:
    aliases = _build_part_aliases(parts)
    pdfs = tuple(Path(path) for path in source_pdfs if Path(path).exists())
    references: list[AssemblyBomReference] = []
    read_errors: list[tuple[str, str]] = []
    seen_refs: set[tuple[str, str, int]] = set()
    fitz = _fitz_module()

    def _should_cancel() -> bool:
        if should_cancel_cb is None:
            return False
        try:
            return bool(should_cancel_cb())
        except Exception:
            return False

    for pdf_index, pdf_path in enumerate(pdfs, start=1):
        if progress_cb is not None:
            progress_cb(pdf_index - 1, len(pdfs), f"Assembly context | Scanning {pdf_path.name}")
        if _should_cancel():
            break
        try:
            with fitz.open(str(pdf_path)) as doc:
                for page_index in range(doc.page_count):
                    text = str(doc[page_index].get_text("text") or "")
                    for alias in aliases:
                        if not alias.pattern.search(text):
                            continue
                        key = (alias.part_name.casefold(), _normalize_path_key(pdf_path), page_index + 1)
                        if key in seen_refs:
                            continue
                        evidence = _text_line_evidence(text, alias.pattern)
                        references.append(
                            AssemblyBomReference(
                                part_name=alias.part_name,
                                assembly_name=pdf_path.stem,
                                assembly_pdf_path=str(pdf_path),
                                page_number=page_index + 1,
                                bom_qty=_quantity_from_bom_evidence(evidence, alias.alias),
                                evidence=evidence,
                            )
                        )
                        seen_refs.add(key)
        except Exception as exc:
            read_errors.append((str(pdf_path), str(exc)))
        if progress_cb is not None:
            progress_cb(pdf_index, len(pdfs), f"Assembly context | Scanned {pdf_path.name}")

    references.sort(
        key=lambda item: (
            item.part_name.casefold(),
            item.assembly_name.casefold(),
            int(item.page_number),
        )
    )
    return AssemblyBomContextResult(
        assembly_pdf_count=len(pdfs),
        checked_part_count=len({_part_display_name(part).casefold() for part in parts if _part_display_name(part)}),
        references=tuple(references),
        read_errors=tuple(read_errors),
    )


def write_assembly_bom_context_csv(
    *,
    rpd_path: Path,
    result: AssemblyBomContextResult,
    out_dirname: str = DEFAULT_PACKET_OUT_DIR,
) -> Path:
    out_dir = Path(rpd_path).parent / str(out_dirname or DEFAULT_PACKET_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"AssemblyContext_{_make_stamp()}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["part_name", "assembly_name", "assembly_pdf_path", "page_number", "bom_qty", "evidence"])
        for ref in result.references:
            writer.writerow(
                [
                    ref.part_name,
                    ref.assembly_name,
                    ref.assembly_pdf_path,
                    int(ref.page_number),
                    int(ref.bom_qty),
                    ref.evidence,
                ]
            )
    return report_path


def build_assembly_packet(
    *,
    rpd_path: Path,
    source_pdfs: Sequence[Path],
    out_dirname: str = DEFAULT_PACKET_OUT_DIR,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_cancel_cb: Optional[Callable[[], bool]] = None,
) -> AssemblyPacketBuildResult:
    valid_sources = tuple(path for path in source_pdfs if Path(path).exists())
    total = len(valid_sources)
    source_pdf_text = tuple(str(path) for path in valid_sources)
    if total == 0:
        if progress_cb is not None:
            progress_cb(0, 0, "Assembly packet | No .iam-backed drawing PDFs found")
        return AssemblyPacketBuildResult(
            packet_path="",
            source_documents=0,
            output_pages=0,
            skipped=False,
            source_pdfs=(),
        )

    def _should_cancel() -> bool:
        if should_cancel_cb is None:
            return False
        try:
            return bool(should_cancel_cb())
        except Exception:
            return False

    if _should_cancel():
        if progress_cb is not None:
            progress_cb(0, total, "Assembly packet | Skipped after cancel request")
        return AssemblyPacketBuildResult(
            packet_path="",
            source_documents=total,
            output_pages=0,
            skipped=True,
            source_pdfs=source_pdf_text,
        )

    fitz = _fitz_module()
    out_dir = rpd_path.parent / str(out_dirname or DEFAULT_PACKET_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ASSEMBLY_PACKET_PREFIX}_{_make_stamp()}.pdf"

    output_pages = 0
    dst = fitz.open()
    try:
        for index, pdf_path in enumerate(valid_sources, start=1):
            if _should_cancel():
                if progress_cb is not None:
                    progress_cb(index - 1, total, "Assembly packet | Skipped after cancel request")
                return AssemblyPacketBuildResult(
                    packet_path="",
                    source_documents=total,
                    output_pages=output_pages,
                    skipped=True,
                    source_pdfs=source_pdf_text,
                )
            if progress_cb is not None:
                progress_cb(index - 1, total, f"Assembly packet | Adding {pdf_path.name}")
            page_indices = _drawing_page_indices(pdf_path)
            if not page_indices:
                continue
            with fitz.open(str(pdf_path)) as src:
                for page_index in page_indices:
                    dst.insert_pdf(src, from_page=page_index, to_page=page_index)
                    output_pages += 1

        if output_pages <= 0:
            return AssemblyPacketBuildResult(
                packet_path="",
                source_documents=total,
                output_pages=0,
                skipped=False,
                source_pdfs=source_pdf_text,
            )

        dst.save(str(out_path), deflate=True, garbage=3)
    finally:
        try:
            dst.close()
        except Exception:
            pass

    if progress_cb is not None:
        progress_cb(total, total, "Assembly packet | Complete")

    return AssemblyPacketBuildResult(
        packet_path=str(out_path),
        source_documents=total,
        output_pages=output_pages,
        skipped=False,
        source_pdfs=source_pdf_text,
    )


_CUT_LIST_NOTE_COLOR = (0.25, 1.00, 0.25)  # same fluorescent green as the print packet's QTY/ASM boxes
_CUT_LIST_NOTE_TEXT_COLOR = (0, 0, 0)
_CUT_LIST_NOTE_FONT_SIZE = 22.0
_CUT_LIST_NOTE_STROKE_WIDTH = 4.0
_CUT_LIST_NOTE_MARGIN = 18.0
_CUT_LIST_NOTE_PAD_X = 12.0
_CUT_LIST_NOTE_BOX_H = 36.0


def _cut_list_assembly_notes(
    valid_sources: Sequence[Path],
    assembly_source_pdfs: Sequence[Path],
) -> dict[str, str]:
    """Cut list items have no PartRow/.sym - each source PDF's own filename
    stem stands in for its part identity, matched the same way laser parts
    are (BOM text search across the assembly drawing PDFs)."""
    if not assembly_source_pdfs:
        return {}
    pseudo_parts = [SimpleNamespace(sym=str(path), part=path.stem) for path in valid_sources]
    assembly_context = scan_assembly_bom_context(parts=pseudo_parts, source_pdfs=assembly_source_pdfs)
    return assembly_notes_by_part(pseudo_parts, assembly_context)


def _stamp_cut_list_assembly_note(page, note_text: str, *, fitz_module) -> None:
    # No QTY box exists on cut list pages (unlike the print packet) to
    # position beside, so this note stands alone in the same bottom-left
    # corner QTY normally occupies.
    text = f"ASM: {note_text}"
    rect = page.rect
    x1 = _CUT_LIST_NOTE_MARGIN
    y1 = rect.height - _CUT_LIST_NOTE_MARGIN - _CUT_LIST_NOTE_BOX_H
    y2 = y1 + _CUT_LIST_NOTE_BOX_H
    font_size = _CUT_LIST_NOTE_FONT_SIZE
    text_w = fitz_module.get_text_length(text, fontname="helv", fontsize=font_size)
    max_text_w = max(20.0, rect.width - x1 - _CUT_LIST_NOTE_MARGIN - (2 * _CUT_LIST_NOTE_PAD_X))
    if text_w > max_text_w:
        font_size = max(7.0, font_size * (max_text_w / text_w))
        text_w = fitz_module.get_text_length(text, fontname="helv", fontsize=font_size)
    x2 = x1 + text_w + (2 * _CUT_LIST_NOTE_PAD_X)
    page.draw_rect(
        fitz_module.Rect(x1, y1, x2, y2),
        color=_CUT_LIST_NOTE_COLOR,
        fill=None,
        width=_CUT_LIST_NOTE_STROKE_WIDTH,
        stroke_opacity=0.94,
    )
    page.insert_text(
        fitz_module.Point(x1 + _CUT_LIST_NOTE_PAD_X, y1 + _CUT_LIST_NOTE_BOX_H * 0.72),
        text,
        fontsize=font_size,
        color=_CUT_LIST_NOTE_TEXT_COLOR,
        fontname="helv",
    )


def build_cut_list_packet(
    *,
    rpd_path: Path,
    source_pdfs: Sequence[Path],
    assembly_source_pdfs: Sequence[Path] = (),
    out_dirname: str = DEFAULT_PACKET_OUT_DIR,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_cancel_cb: Optional[Callable[[], bool]] = None,
) -> AssemblyPacketBuildResult:
    valid_sources = tuple(path for path in source_pdfs if Path(path).exists())
    total = len(valid_sources)
    source_pdf_text = tuple(str(path) for path in valid_sources)
    if total == 0:
        if progress_cb is not None:
            progress_cb(0, 0, "Cut list | No non-laser part PDFs found")
        return AssemblyPacketBuildResult(
            packet_path="",
            source_documents=0,
            output_pages=0,
            skipped=False,
            source_pdfs=(),
        )

    def _should_cancel() -> bool:
        if should_cancel_cb is None:
            return False
        try:
            return bool(should_cancel_cb())
        except Exception:
            return False

    if _should_cancel():
        if progress_cb is not None:
            progress_cb(0, total, "Cut list | Skipped after cancel request")
        return AssemblyPacketBuildResult(
            packet_path="",
            source_documents=total,
            output_pages=0,
            skipped=True,
            source_pdfs=source_pdf_text,
        )

    fitz = _fitz_module()
    out_dir = rpd_path.parent / str(out_dirname or DEFAULT_PACKET_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{CUT_LIST_PACKET_PREFIX}_{_make_stamp()}.pdf"
    notes_by_part = _cut_list_assembly_notes(valid_sources, assembly_source_pdfs)

    output_pages = 0
    dst = fitz.open()
    try:
        for index, pdf_path in enumerate(valid_sources, start=1):
            if _should_cancel():
                if progress_cb is not None:
                    progress_cb(index - 1, total, "Cut list | Skipped after cancel request")
                return AssemblyPacketBuildResult(
                    packet_path="",
                    source_documents=total,
                    output_pages=output_pages,
                    skipped=True,
                    source_pdfs=source_pdf_text,
                )
            if progress_cb is not None:
                progress_cb(index - 1, total, f"Cut list | Adding {pdf_path.name}")
            try:
                with fitz.open(str(pdf_path)) as src:
                    if src.page_count <= 0:
                        continue
                    note = notes_by_part.get(pdf_path.stem.casefold())
                    first_new_index = dst.page_count
                    dst.insert_pdf(src)
                    output_pages += int(src.page_count)
                    if note:
                        for page_index in range(first_new_index, dst.page_count):
                            try:
                                _stamp_cut_list_assembly_note(dst[page_index], note, fitz_module=fitz)
                            except Exception:
                                pass
            except Exception:
                continue

        if output_pages <= 0:
            return AssemblyPacketBuildResult(
                packet_path="",
                source_documents=total,
                output_pages=0,
                skipped=False,
                source_pdfs=source_pdf_text,
            )

        dst.save(str(out_path), deflate=True, garbage=3)
    finally:
        try:
            dst.close()
        except Exception:
            pass

    if progress_cb is not None:
        progress_cb(total, total, "Cut list | Complete")

    return AssemblyPacketBuildResult(
        packet_path=str(out_path),
        source_documents=total,
        output_pages=output_pages,
        skipped=False,
        source_pdfs=source_pdf_text,
    )
