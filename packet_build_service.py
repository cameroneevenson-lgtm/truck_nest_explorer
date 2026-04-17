from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from typing import Callable, Optional, Sequence

from models import ExplorerSettings

DEFAULT_PACKET_OUT_DIR = "_out"
ASSEMBLY_PACKET_PREFIX = "AssemblyPacket_TABLOID"
TABLOID_WIDTH_POINTS = 11.0 * 72.0
TABLOID_HEIGHT_POINTS = 17.0 * 72.0
TABLOID_TOLERANCE_POINTS = 18.0
ASSEMBLY_PACKET_RENDER_DPI = 300


@dataclass(frozen=True)
class PacketBuildContext:
    parts: tuple[object, ...]
    resolve_asset_fn: Callable[[str, str], Optional[str]]
    assembly_source_pdfs: tuple[Path, ...]
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


def _make_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize_path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


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
        or stem_words.endswith("nest summary")
        or " print packet " in f" {stem_words} "
        or " printpacket " in f" {stem_words} "
        or " assembly packet " in f" {stem_words} "
        or " assemblypacket " in f" {stem_words} "
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


def _is_tabloid_pdf(path: Path) -> bool:
    return bool(_tabloid_page_indices(path))


def _tabloid_page_indices(path: Path) -> tuple[int, ...]:
    try:
        fitz = _fitz_module()
        with fitz.open(str(path)) as doc:
            indices: list[int] = []
            for index in range(doc.page_count):
                rect = doc[index].rect
                if _is_tabloid_size(float(rect.width), float(rect.height)):
                    indices.append(index)
            return tuple(indices)
    except Exception:
        return ()


def _iter_pdf_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []

    discovered: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        if any(part.casefold() == DEFAULT_PACKET_OUT_DIR.casefold() for part in current_dir.parts):
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
            if not _is_tabloid_pdf(pdf_path):
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
    resolve_asset_fn = rk_assets.resolve_asset_fast
    assembly_search_roots = _assembly_search_roots(fabrication_dir, rpd_path.parent)
    assembly_source_pdfs = collect_unused_tabloid_pdfs(
        parts,
        search_roots=assembly_search_roots,
        resolve_asset_fn=resolve_asset_fn,
    )
    return PacketBuildContext(
        parts=tuple(parts),
        resolve_asset_fn=resolve_asset_fn,
        assembly_source_pdfs=assembly_source_pdfs,
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
            progress_cb(0, 0, "Assembly packet | No unused tabloid PDFs found")
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
                progress_cb(index - 1, total, f"Assembly packet | Flattening {pdf_path.name}")
            page_indices = _tabloid_page_indices(pdf_path)
            if not page_indices:
                continue
            with fitz.open(str(pdf_path)) as src:
                for page_index in page_indices:
                    page = src[page_index]
                    rect = page.rect
                    pix = page.get_pixmap(dpi=ASSEMBLY_PACKET_RENDER_DPI, alpha=False)
                    dst_page = dst.new_page(width=float(rect.width), height=float(rect.height))
                    dst_page.insert_image(dst_page.rect, stream=pix.tobytes("png"))
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
