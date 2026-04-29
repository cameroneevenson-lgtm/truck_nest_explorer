from __future__ import annotations

import csv
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from typing import Callable, Optional, Sequence
import xml.etree.ElementTree as ET

from models import ExplorerSettings

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
        "The RPD is not ready for a print packet yet. "
        f"Expected {len(expected)} part(s) from {expected_csv_path.name}, but found {len(actual)} populated RPD part(s): "
        f"{detail_text}. Finish the RADAN CSV import and save the project before building the packet."
    )


def validate_print_packet_readiness(
    *,
    rpd_path: Path,
    parts: Sequence[object],
    expected_csv_path: Path | None = None,
) -> None:
    """Fail fast when the RPD is empty, partially imported, or qtys are not populated."""

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
        return

    expected_quantities = _read_radan_csv_quantities(Path(expected_csv_path))
    if expected_quantities and actual_quantities != expected_quantities:
        raise PacketBuildReadinessError(
            _quantity_mismatch_message(expected_quantities, actual_quantities, Path(expected_csv_path))
        )


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
) -> PacketBuildContext:
    if not rpd_path.exists():
        raise FileNotFoundError(str(rpd_path))

    rk_assets, _rk_packet_runtime, rk_rpd_io = _load_radan_kitter_modules()
    _configure_asset_lookup(rk_assets, settings)

    _tree, parts, _debug = rk_rpd_io.load_rpd(str(rpd_path))
    # Packet builds are explicit user actions, so prefer the subtree-capable
    # resolver over the preview-optimized fast path. This covers cases where
    # W-side PDFs live under kit-specific subfolders such as PUMP PACK\PUMP HOUSE.
    resolve_asset_fn = rk_assets.resolve_asset
    assembly_search_roots = _assembly_search_roots(fabrication_dir, rpd_path.parent)
    assembly_source_pdfs = collect_unused_tabloid_pdfs(
        parts,
        search_roots=assembly_search_roots,
        resolve_asset_fn=resolve_asset_fn,
    )
    cut_list_source_pdfs = collect_cut_list_pdfs(
        search_roots=assembly_search_roots,
        settings=settings,
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


def build_cut_list_packet(
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
                    dst.insert_pdf(src)
                    output_pages += int(src.page_count)
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
