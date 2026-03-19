# Truck Nest Explorer

A PySide6 desktop tool for browsing truck kit folders, creating L-side kit scaffolds, running `inventor_to_radan` from W-side spreadsheets, moving the generated RADAN output back into the matching L-side project folder, and launching `radan_kitter`.

It also includes:

- on-demand opening of the best matching Nest Summary PDF found in the L-side project area
- editable per-kit punch-code notes directly in the kit table
- saved client numbers per truck
- persistent hide/unhide controls for completed trucks and kits that should stay on L

The release root, fabrication root, template project, and launcher paths are now treated as built-in static values instead of editable UI settings.

## Workflow this app supports

1. Create the L-side `.rpd` folders and files for a truck.
2. Run `inventor_to_radan` against the single spreadsheet found in the matching W-side kit folder.
3. Generate the RADAN output in the W-side kit folder, then move the `*_Radan.csv` and report into the appropriate L-side project folder.
4. Switch to RADAN and load that CSV from L.
5. Save in RADAN.
6. Run `radan_kitter` on the saved `.rpd`.
7. Return to RADAN for completion.

## Folder convention

The scaffold logic follows this release-side layout:

```text
<release_root>\<truck_number>\<kit_name>\<truck_number> <kit_name>\<truck_number> <kit_name>.rpd
```

Example:

```text
L:\BATTLESHIELD\F-LARGE FLEET\F55334\PAINT PACK\F55334 PAINT PACK\F55334 PAINT PACK.rpd
```

The fabrication-side layout is:

```text
<fabrication_root>\<truck_number>\<kit_name>
```

Example:

```text
W:\LASER\For Battleshield Fabrication\F55334\PAINT PACK
```

If L and W use the same kit name, enter just the kit name:

```text
PAINT PACK
```

If W is nested differently, use this format:

```text
KIT NAME => W\nested\relative\path
```

Example:

```text
CONSOLE PACK => LASER\CONSOLE\PACK
```

## Template cloning

The explorer now uses the bundled template at `Template\Template.rpd` automatically.

When it creates a new project file, it rewrites the template so the project is named for that truck and kit instead of `Template`.

- The output filename becomes `<truck> <kit>.rpd`
- XML values like `Template` and `Template.rpd` are rewritten to the new project name
- `NestFolder` and `RemnantSaveFolder` are pointed at the new L-side project folder
- The blank `nests` and `remnants` folders are created alongside the project file

If the bundled template is unavailable, the app writes a minimal XML placeholder `.rpd` that `radan_kitter` can parse.

## Spreadsheet detection

The app looks for a single spreadsheet candidate inside the fabrication-side kit folder.

Supported input files:

- `.xlsx`
- `.xls`
- `.csv`

Ignored generated files:

- `*_Radan.csv`

If the folder contains zero spreadsheet candidates or more than one, the app surfaces that instead of guessing.

## Nest Summary access

The actual nest summary PDF is opened on demand instead of previewed in a small pane.

- It searches on the L side only.
- It looks for a filename based on the RPD name with `nest summary` appended.
- It only searches a shallow folder hierarchy under the L-side kit folder.
- A print packet PDF is ignored even if it is nearby.
- Use `Open Nest Summary` or double-click the Nest Summary cell to open it.
- Use `Open Print Packet` when you want the kitter packet PDF instead.

Example target filename:

```text
F55334 PAINT PACK nest summary.pdf
```

## Punch codes

Punch codes are edited directly in the kit table.

- Double-click the `Punch Code` cell for a kit to edit it.
- Notes are saved per kit, not as one shared note.
- Notes save when you commit the cell edit.
- The content is stored in the app settings file under `_runtime\settings.json`.

## Hide completed work

Use `Hide Truck` or `Hide Selected Kits` when completed work should remain on L but stop cluttering the active list.

- Hidden trucks stay on disk and are omitted from the left sidebar by default.
- Hidden kits stay in the selected truck folder but are omitted from the kit table by default.
- Use the `Show Hidden (n)` truck-header toggle or `Show hidden kits` to bring them back into view and unhide them.

## Truck client numbers

Use `Set Client Number` on the selected truck to store the client number for that truck.

- Client numbers are saved in `_runtime\settings.json`
- The selected-truck heading shows the saved client number
- The truck filter matches on either the truck number or the client number

## Truck fabrication order

Use the `↑` and `↓` buttons beside the truck list to manually arrange trucks in the actual fabrication order.

- The order is saved in `_runtime\settings.json`.
- New trucks that are not yet placed manually are appended after the saved order.

## Inventor entry

For the intended shop flow, point `Inventor Launcher` at `inventor_to_radan.bat`.

The explorer waits for that launcher to finish, then moves the generated RADAN files from `W` into the matching `L` project folder.

## Canonical kit list

The app now uses this built-in canonical order and does not expose kit-list editing in the UI:

- `PAINT PACK`
- `INTERIOR PACK`
- `EXTERIOR PACK`
- `CONSOLE PACK`
- `CHASSIS PACK`
- `PUMP HOUSE => PUMP PACK\PUMP HOUSE`
- `PUMP COVERING => PUMP PACK\COVERING`
- `PUMP MOUNTS => PUMP PACK\MOUNTS`
- `PUMP BRACKETS => PUMP PACK\BRACKETS`
- `STEP PACK`
- `OPERATIONAL PANELS => PUMP PACK\OPERATIONAL PANELS`

## Run

### Option 1: launcher

```powershell
cd c:\Tools\truck_nest_explorer
.\truck_nest_explorer.bat
```

### Option 2: direct Python

```powershell
cd c:\Tools\truck_nest_explorer
C:\Tools\.venv\Scripts\python.exe app.py
```

### Option 3: hot reload during development

```powershell
cd c:\Tools\truck_nest_explorer
.\dev_run.bat
```

This mirrors the dashboard-style dev launcher:

- watches `.py` files
- shows an in-app hot reload banner
- lets you click `Accept Reload` immediately
- auto-reloads after the timeout unless you click `Cancel Reload`
