# Truck Nest Explorer

A PySide6 desktop tool for browsing truck kit folders, creating L-side kit scaffolds, running `inventor_to_radan` from W-side spreadsheets, copying the generated import CSV back into the matching L-side project folder, and launching `radan_kitter`.

It also includes:

- built-in preview of the best matching nest PDF found in the L-side project area
- a persistent punch-code notes panel for quick reference
- persistent hide/unhide controls for completed trucks and kits that should stay on L

## Workflow this app supports

1. Create the L-side `.rpd` folders and files for a truck.
2. Run `inventor_to_radan` against the single spreadsheet found in the matching W-side kit folder.
3. Copy the generated `*_Radan.csv` and report into the appropriate created L-side project folder.
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

If you want a friendlier dashboard label than the RADAN/project name, use this format:

```text
DASHBOARD NAME | RADAN NAME
```

Example:

```text
BODY | PAINT PACK
```

If W is nested differently too, extend it like this:

```text
DASHBOARD NAME | RADAN NAME => W\nested\relative\path
```

Example:

```text
BODY | PAINT PACK => NESTS\PAINT PACK
```

## Template cloning

Set `Blank Template RPD` in the app to a canonical blank `.rpd` file.

Optional string replacement rules are line-based:

```text
FIND THIS => {project_name}
TRUCK_TOKEN => {truck_number}
KIT_TOKEN => {kit_name}
```

Available replacement placeholders:

- `{truck_number}`
- `{kit_name}`
- `{project_name}`
- `{rpd_stem}`

If no template is configured yet, the app writes a minimal XML placeholder `.rpd` that `radan_kitter` can parse.

## Spreadsheet detection

The app looks for a single spreadsheet candidate inside the fabrication-side kit folder.

Supported input files:

- `.xlsx`
- `.xls`
- `.csv`

Ignored generated files:

- `*_Radan.csv`

If the folder contains zero spreadsheet candidates or more than one, the app surfaces that instead of guessing.

## Nest Summary preview

The preview pane is for the actual nest summary PDF, not the kitter print packet.

- It searches on the L side only.
- It looks for a filename based on the RPD name with `nest summary` appended.
- It only searches a shallow folder hierarchy under the L-side kit folder.
- A print packet PDF is ignored even if it is nearby.
- Use the `Open Print Packet` button in the kit actions row when you want the kitter packet PDF instead.

Example target filename:

```text
F55334 PAINT PACK nest summary.pdf
```

The pane renders the matching Nest Summary PDF in-app and lets you page through it.

## Punch codes

The right-side punch-code panel now follows the selected kit.

- Use it to keep quick punch-code references nearby while working on that kit.
- Notes are saved per kit, not as one shared note.
- Kit aliases still map to the same stored note.
  Example: `BODY` uses the `PAINT PACK` punch-code entry.
- Notes auto-save while you type.
- The content is stored in the app settings file under `_runtime\settings.json`.

## Hide completed work

Use `Hide Truck` or `Hide Selected Kits` when completed work should remain on L but stop cluttering the active list.

- Hidden trucks stay on disk and are omitted from the left sidebar by default.
- Hidden kits stay in the selected truck folder but are omitted from the kit table by default.
- Turn on `Show hidden trucks` or `Show hidden kits` to bring them back into view and unhide them.

## Dashboard kit aliases

The explorer can show a simpler dashboard name while keeping the underlying RADAN-facing name unchanged.

- Example: show `BODY` in the explorer, while the actual project/RPD/W-side name remains `PAINT PACK`.
- Hover the kit name in the table to see the underlying RADAN name when an alias is active.

## Inventor entry

For the cleanest W-to-L handoff, point `Inventor Entry` at `inventor_to_radan.py`.

The app can also run a `.bat` or `.cmd`, but if that launcher pauses at the end you will need to close its console before the explorer can continue the copy-to-L step.

## Default kit templates

The app starts with these editable defaults:

- `BODY | PAINT PACK`
- `PUMPHOUSE`
- `CONSOLE PACK`
- `INTERIOR PACK`
- `EXTERIOR PACK`
- `PUMP COVERINGS`
- `GRAND REMOUS TWO`

Replace these with your canonical list once you provide it.

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
