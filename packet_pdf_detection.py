"""PDF and BOM (spreadsheet) detection/classification rules.

This is the single source of truth for "what kind of generated packet PDF is
this" (print packet / assembly packet / cut list / nest summary). It used to
be duplicated between services.py and packet_build_service.py; both now
import the classification rules from here instead of reimplementing them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fs_cache import cached_path_exists, natural_sort_key
from models import KitPaths, PdfMatch, SpreadsheetMatch
from performance_metrics import BoundedTTLCache, GLOBAL_METRICS, normalize_cache_path

SUPPORTED_SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".csv"}
IGNORED_SPREADSHEET_PATTERNS = ("_radan.csv",)
SUPPORTED_PREVIEW_SUFFIXES = {".pdf"}
MAX_NEST_SUMMARY_DEPTH = 2


def _is_generated_spreadsheet(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(pattern) for pattern in IGNORED_SPREADSHEET_PATTERNS)


def detect_spreadsheet(
    folder: Path | None,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> SpreadsheetMatch:
    if folder is None:
        return SpreadsheetMatch(chosen_path=None, candidates=(), issue="root_not_configured")
    cache_obj = fs_cache
    key = ("spreadsheet", normalize_cache_path(folder))
    if cache_obj is not None:
        hit, value = cache_obj.get(key)
        if hit and isinstance(value, SpreadsheetMatch):
            return value
    if not cached_path_exists(folder, cache=cache_obj):
        result = SpreadsheetMatch(chosen_path=None, candidates=(), issue="folder_missing")
        if cache_obj is not None:
            cache_obj.set(key, result, negative=True)
        return result

    discovered: list[Path] = []
    try:
        GLOBAL_METRICS.record_filesystem_check()
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
        result = SpreadsheetMatch(chosen_path=None, candidates=(), issue="folder_missing")
        if cache_obj is not None:
            cache_obj.set(key, result, negative=True)
        return result

    candidates = tuple(sorted(discovered, key=lambda path: natural_sort_key(path.name)))
    if len(candidates) == 1:
        result = SpreadsheetMatch(chosen_path=candidates[0], candidates=candidates)
        if cache_obj is not None:
            cache_obj.set(key, result, negative=False)
        return result
    if not candidates:
        result = SpreadsheetMatch(chosen_path=None, candidates=(), issue="spreadsheet_missing")
        if cache_obj is not None:
            cache_obj.set(key, result, negative=True)
        return result
    result = SpreadsheetMatch(chosen_path=None, candidates=candidates, issue="multiple_spreadsheets")
    if cache_obj is not None:
        cache_obj.set(key, result, negative=True)
    return result


def _normalize_pdf_name_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _shallow_descendant_files(
    root: Path,
    *,
    max_depth: int,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> list[tuple[Path, int]]:
    cache_obj = fs_cache
    key = ("shallow_descendant_files", normalize_cache_path(root), int(max_depth))
    if cache_obj is not None:
        hit, value = cache_obj.get(key)
        if hit and isinstance(value, tuple):
            return list(value)
    if not cached_path_exists(root, cache=cache_obj):
        if cache_obj is not None:
            cache_obj.set(key, tuple(), negative=True)
        return []

    discovered: list[tuple[Path, int]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            GLOBAL_METRICS.record_filesystem_check()
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
    if cache_obj is not None:
        cache_obj.set(key, tuple(discovered), negative=not discovered)
    return discovered


def _collect_preview_pdf_candidates(
    paths: KitPaths,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> tuple[Path, ...]:
    search_root = paths.release_kit_dir or paths.project_dir
    if search_root is None or not cached_path_exists(search_root, cache=fs_cache):
        return ()

    rpd_stem = paths.rpd_path.stem if paths.rpd_path is not None else paths.project_name
    expected_stem = _normalize_pdf_name_words(f"{rpd_stem} nest summary")

    candidates: list[tuple[int, Path]] = []
    for child, depth in _shallow_descendant_files(
        search_root,
        max_depth=MAX_NEST_SUMMARY_DEPTH,
        fs_cache=fs_cache,
    ):
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


def _is_nest_summary_pdf(path: Path) -> bool:
    stem_words = _normalize_pdf_name_words(path.stem)
    return stem_words.endswith("nest summary") or " nest summary " in f" {stem_words} "


def is_generated_packet_pdf_artifact(path: Path) -> bool:
    """True if `path` looks like any packet PDF this app generates.

    Used to exclude already-built packet/nest-summary PDFs when scanning a
    folder for *source* PDFs to pull into a new packet build. Formerly
    duplicated (and slightly out of sync) as
    packet_build_service._looks_generated_pdf_artifact.
    """
    return (
        _is_print_packet_pdf(path)
        or _is_assembly_packet_pdf(path)
        or _is_cut_list_packet_pdf(path)
        or _is_nest_summary_pdf(path)
    )


def _detect_named_packet_pdf(
    paths: KitPaths,
    *,
    matches_fn,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> PdfMatch:
    cache_obj = fs_cache
    if paths.project_dir is None:
        return PdfMatch(chosen_path=None, candidates=(), issue="project_not_configured")
    if not cached_path_exists(paths.project_dir, cache=cache_obj):
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")

    search_root = paths.release_kit_dir or paths.project_dir
    if search_root is None or not cached_path_exists(search_root, cache=cache_obj):
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")
    cache_key = (
        "named_packet_pdf",
        getattr(matches_fn, "__name__", repr(matches_fn)),
        normalize_cache_path(paths.project_dir),
        normalize_cache_path(search_root),
    )
    if cache_obj is not None:
        hit, value = cache_obj.get(cache_key)
        if hit and isinstance(value, PdfMatch):
            return value

    candidates: list[tuple[int, Path]] = []
    for child, depth in _shallow_descendant_files(
        search_root,
        max_depth=MAX_NEST_SUMMARY_DEPTH,
        fs_cache=cache_obj,
    ):
        if child.suffix.casefold() not in SUPPORTED_PREVIEW_SUFFIXES:
            continue
        if not matches_fn(child):
            continue
        candidates.append((depth, child))

    candidates.sort(key=lambda item: (item[0], natural_sort_key(str(item[1].relative_to(search_root)))))
    paths_only = tuple(path for _depth, path in candidates)
    if not paths_only:
        result = PdfMatch(chosen_path=None, candidates=(), issue="pdf_missing")
        if cache_obj is not None:
            cache_obj.set(cache_key, result, negative=True)
        return result
    result = PdfMatch(chosen_path=paths_only[-1], candidates=paths_only)
    if cache_obj is not None:
        cache_obj.set(cache_key, result, negative=False)
    return result


def detect_preview_pdf(
    paths: KitPaths,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> PdfMatch:
    if paths.project_dir is None:
        return PdfMatch(chosen_path=None, candidates=(), issue="project_not_configured")
    if not cached_path_exists(paths.project_dir, cache=fs_cache):
        return PdfMatch(chosen_path=None, candidates=(), issue="project_missing")

    candidates = _collect_preview_pdf_candidates(paths, fs_cache=fs_cache)
    if not candidates:
        return PdfMatch(chosen_path=None, candidates=(), issue="pdf_missing")
    return PdfMatch(chosen_path=candidates[0], candidates=candidates)


def detect_print_packet_pdf(
    paths: KitPaths,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_print_packet_pdf, fs_cache=fs_cache)


def detect_assembly_packet_pdf(
    paths: KitPaths,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_assembly_packet_pdf, fs_cache=fs_cache)


def detect_cut_list_packet_pdf(
    paths: KitPaths,
    *,
    fs_cache: BoundedTTLCache[object] | None = None,
) -> PdfMatch:
    return _detect_named_packet_pdf(paths, matches_fn=_is_cut_list_packet_pdf, fs_cache=fs_cache)
