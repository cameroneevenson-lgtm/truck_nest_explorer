# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this app does

A PySide6 desktop tool for browsing truck kit folders, creating L-side kit scaffolds from `Template\Template.rpd`, running `inventor_to_radan` against W-side spreadsheets, moving the generated RADAN output back to the matching L-side project folder, and launching `radan_kitter`. It also builds print/assembly/cut-list packets directly from the saved `.rpd`, and tracks per-kit punch codes, per-truck client numbers, hide/unhide state, and manual fabrication order — all persisted in `_runtime\settings.json`.

See `README.md` for the full folder-convention rules (L vs. W side layout, `KIT NAME => nested\path` syntax), the canonical kit list, and Nest Summary / packet-building behavior — these are shop-floor conventions, not obvious from the code alone.

## Commands

Run (production launcher — re-execs into the shared venv if not already running from it):

```powershell
cd c:\Tools\truck_nest_explorer
.\truck_nest_explorer.bat
```

Direct Python:

```powershell
C:\Tools\.venv\Scripts\python.exe app.py
```

Hot reload during development (watches `.py` files, shows an in-app banner with Accept/Cancel before auto-reloading):

```powershell
.\dev_run.bat
```

Tests (single test file exists, `unittest`-based but runnable via pytest):

```powershell
C:\Tools\.venv\Scripts\python.exe -m pytest tests/test_services.py -q
C:\Tools\.venv\Scripts\python.exe -m pytest tests/test_services.py -k test_name -q
```

`tests/test_services.py` sets `QT_QPA_PLATFORM=offscreen` and inserts both this project's root and the sibling `radan_kitter` directory onto `sys.path` — tests exercise real cross-project imports (`full_flow_service`, etc.), not mocks.

## Architecture

**Always runs from `C:\Tools\.venv`.** `app.py` checks `sys.prefix`/`sys.executable` against the shared venv and re-execs itself under the correct interpreter if launched some other way — this is intentional, not a bug, since this app is one of several sibling apps sharing one venv (see `master_app`, which embeds this app's `MainWindow` directly).

**`main_window.py` is the Qt shell + controller host, not where business logic lives.** It builds the UI and owns state, then delegates workflow logic to `controllers/*Controller` classes, each constructed with a `window` back-reference (`self.inventor_controller = InventorController(self)`, etc.). When changing behavior for a specific workflow, find the matching controller first:

- `full_flow_controller.py` — the end-to-end "Inventor → RADAN → nester" run, including a modal progress dialog and phase state machine (`FullFlowPhase`)
- `inventor_controller.py` — invoking `inventor_to_radan` inline (headless) against a kit's spreadsheet
- `radan_import_controller.py` — importing generated RADAN CSVs back into the L-side project
- `packet_build_controller.py` — building print/assembly/cut-list packets from the saved `.rpd`
- `block_transfer_controller.py` — moving generated RADAN output from W back to the matching L-side folder
- `hot_reload_controller.py` — the in-app hot-reload accept/cancel banner, driven by `dev_hot_restart.py`

New workflow logic should follow this same pattern (a new `controllers/*_controller.py` taking `window` in its constructor) rather than growing `main_window.py` further — it was previously a ~3800-line god object and has been deliberately decomposed down to ~2300 lines via this pattern.

**Service-layer modules hold the actual file/domain logic**, independent of Qt:
- `services.py` — largest module; RADAN session detection, path resolution, packet opening/generation glue
- `full_flow_service.py` — the non-UI logic behind the full-flow run
- `packet_build_service.py` — PDF packet generation (print/assembly/cut-list) from `.rpd` + PDFs on W
- `inventor_service.py` — thin wrapper for invoking `inventor_to_radan`'s inline runner
- `flow_bridge.py` / `flow_schedule_probe.py` — integration points with `fabrication_flow_dashboard`'s schedule/state
- `models.py` — shared dataclasses (e.g. `KitStatus`)
- `settings_store.py` — reads/writes `_runtime\settings.json`

**Cross-repo coupling to be aware of:**
- Imports `inventor_to_radan`'s `inline_runner`/`convert_bom_to_radan_csv` for headless BOM conversion (`allow_prompts=False, show_summary=False`).
- Launches `radan_kitter` as an external process for its kit-prep workflow.
- `master_app` imports this project's `MainWindow` and embeds it inside a `QStackedWidget`, juggling `sys.path`/`sys.modules` to load it under an isolated namespace — changes to this app's module-level side effects or global state can affect that embedding.
- `flow_bridge.py`/`flow_schedule_probe.py` read state from `fabrication_flow_dashboard`.

**`docs/refactor/`** contains phase-by-phase notes (`phase_1_shared_inventor.md` through `phase_6_targeted_optimization.md`) documenting the history of prior refactors — check there before re-deriving why something is structured the way it is.
