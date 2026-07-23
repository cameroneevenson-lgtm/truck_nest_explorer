"""Inventor-to-RADAN subprocess launching, RADAN session detection, and
RADAN CSV import-lock detection.

Note: `_python_executable`/`DEFAULT_VENV_PYTHON`/`_hidden_process_kwargs`
below intentionally mirror the copies in services.py rather than importing
them from there. services.py imports this module (to re-export its public
functions for backward compatibility), so this module importing back from
services.py would create a circular import. The mirrored helpers are tiny
and self-contained.
"""
from __future__ import annotations

import csv
import ctypes
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_VENV_PYTHON = Path(r"C:\Tools\.venv\Scripts\python.exe")
DEFAULT_RADAN_CSV_IMPORT_ENTRY = Path(r"C:\Tools\radan_automation\import_parts_csv_headless.py")

# Single switch for the whole app: run RADAN automation on a private Win32 desktop
# (radan_automation/radan_isolated_desktop.py) so open RADAN sessions are not disturbed.
# Same logon session and same instance count as the default path, so no extra licence seat.
#
# ON by default since 2026-07-23. Measured on this machine: a 12-part conversion batch ran
# with two Nest Editor sessions open and the watched session recorded zero disturbance
# events across 1145 samples (RADAN_DESKTOP_ISOLATION_FINDINGS_20260723.md). This is what
# retires the "close RADAN before nesting" prompts.
#
# Set TRUCK_NEST_RADAN_ISOLATED_DESKTOP=0 to fall back to the legacy shared-desktop path,
# which still exists and still shows those prompts.
RADAN_ISOLATED_DESKTOP = os.environ.get("TRUCK_NEST_RADAN_ISOLATED_DESKTOP", "1").strip() not in {
    "0",
    "false",
    "False",
    "no",
}


class InventorToRadanInlineNeedsUi(RuntimeError):
    def __init__(self, message: str, *, missing_dxf_count: int = 0, missing_rule_count: int = 0) -> None:
        self.missing_dxf_count = missing_dxf_count
        self.missing_rule_count = missing_rule_count
        super().__init__(message)


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


def _inventor_to_radan_module_path(entry_path: Path) -> Path:
    if entry_path.suffix.casefold() == ".py":
        return entry_path
    return entry_path.parent / "inventor_to_radan.py"


def _inventor_to_radan_inline_runner_path(module_path: Path) -> Path:
    return module_path.parent / "inline_runner.py"


def _load_inventor_to_radan_inline_runner(runner_path: Path) -> object:
    if not runner_path.exists():
        raise FileNotFoundError(f"Could not find inline Inventor-to-RADAN runner: {runner_path}")

    module_name = "_truck_nest_inventor_to_radan_inline_runner"
    spec = importlib.util.spec_from_file_location(module_name, runner_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load inline Inventor-to-RADAN runner: {runner_path}")

    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_module is not None:
            sys.modules[module_name] = previous_module
        else:
            sys.modules.pop(module_name, None)


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

    runner = _load_inventor_to_radan_inline_runner(_inventor_to_radan_inline_runner_path(module_path))
    run_inline = getattr(runner, "run_inline", None)
    if not callable(run_inline):
        raise RuntimeError(f"{module_path.parent / 'inline_runner.py'} does not expose run_inline().")

    try:
        return run_inline(entry, spreadsheet, allow_prompts=False, show_summary=False)
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
        **_hidden_process_kwargs(),
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


def launch_radan_csv_import(
    csv_path: Path | str,
    output_folder: Path | str,
    *,
    project_path: Path | str | None = None,
    log_path: Path | str | None = None,
    entry_path: Path | str = DEFAULT_RADAN_CSV_IMPORT_ENTRY,
    allow_visible_radan: bool = False,
    isolated_desktop: bool = False,
    rebuild_symbols: bool = False,
    lab_symbol_writer: bool = False,
    native_sym_experimental: bool = False,
    d_record_view_height_threshold_guard: bool = False,
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
    if isolated_desktop:
        command.append("--isolated-desktop")
    if rebuild_symbols:
        command.append("--rebuild-symbols")
    if lab_symbol_writer or native_sym_experimental:
        command.append("--lab-symbol-writer")
    if d_record_view_height_threshold_guard:
        command.append("--d-record-view-height-threshold-guard")
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
                **_hidden_process_kwargs(),
            )
        finally:
            log_stream.close()
    return subprocess.Popen(
        command,
        cwd=str(entry.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_hidden_process_kwargs(),
    )
