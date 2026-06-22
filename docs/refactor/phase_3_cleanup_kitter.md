# Phase 3: Worker Cleanup, Domain Cleanup, and Kitter Change

Complete Phases 1 and 2 (and their tests) before starting this phase.

## Tasks

### 1. Move Generic Worker
Create `truck_nest_explorer/background_job.py`.

Move the generic worker implementation out of `main_window.py` and rename:

- `BackgroundJobSignals`
- `BackgroundJobWorker`

Preserve:
- `progress`, `done`, `error` signals
- `request_stop()`, `should_cancel()`, `emit_progress()`
- Exception-to-traceback handling

Use it for `InventorController`, `FullFlowController`, and existing assembly/cut-list tasks where practical.

Remove the old `PacketJobSignals` and `PacketJobWorker` from `main_window.py` after migration.

### 2. Clean Up `full_flow_service.py`
Keep `full_flow_service.py` as the focused non-UI Full Flow domain layer.

It may continue to own:
- `FullFlowResult` and related dataclasses
- `run_full_flow_after_inventor_review`
- CSV import completion
- Conditional RF behavior
- Packet composition
- Headless nester execution

It must **not** own:
- Inventor execution
- Report dialogs or review
- UI phase sequencing
- Action locking or progress dialogs
- Worker ownership
- Copied Kitter preparation logic
- Obsolete compatibility entry points

Delete `run_full_flow_before_nester` if it no longer has callers.
Delete `run_inventor_inline_for_status`.

### 3. Make Part-Comment Writing Optional in Kitter (Semantic API)
Modify `radan_kitter/kit_service.py`.

Add a semantic option:

```python
def prepare_kits(
    parts: List[PartRow],
    *,
    rpd_path: str,
    donor_template_path: str,
    bak_dirname: str,
    kits_dirname: str,
    kit_to_priority: Dict[str, str],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    refresh_kit_fn: Optional[Callable[[str], None]] = None,
    write_part_kit_comments: bool = True,
) -> int:
    ...
```

**Do not** use names containing `attr109`, `attr_109`, or similar.

Required behavior when `write_part_kit_comments=True`:
- Preserve current Kitter behavior (including part-symbol backups and comment writes via `sym_io`)

Required behavior when `write_part_kit_comments=False`:
- Do **not** create backups associated solely with part-comment writes
- Do **not** call `sym_io.set_part_comment`
- Still perform kit label normalization, priority updates, `kit_text` updates, donor validation, kit grouping, kit symbol generation, and `refresh_kit_fn`
- Still return the correct kit count with coherent progress

Update the low-level function in `radan_kitter/sym_io.py` to use a semantic name (e.g. `set_part_comment`) and document that it currently maps to RADAN attribute 109 internally.

### 4. Update Truck Nest Explorer Kitter Call Site
Full Flow must call:

```python
rk_kit_service.prepare_kits(
    ...,
    write_part_kit_comments=False,
)
```

**Delete** the duplicated implementation `_prepare_full_flow_kits_without_attr109` and all its copied logic (donor validation, kit grouping, backup handling, kit-symbol generation, etc.).

Do not retain any fallback that retries an older signature.

### 5. Kitter Tests
Add tests proving:
- `write_part_kit_comments` defaults to `True`
- `True` writes comments and creates expected backups
- `False` does not call `sym_io.set_part_comment` and does not create comment-related backups
- `False` still performs all other kit preparation steps correctly
- Donor validation and `refresh_kit_fn` still work in both modes
- Progress remains coherent and both modes return the correct kit count

Add one low-level `sym_io` test that may reference the numeric RADAN storage detail (because it is testing file format). Higher-level tests must use semantic behavior only.

### 6. Final Removals
After all behavior is implemented and tested, confirm these obsolete symbols are gone:
- `PendingInventorJob` and all related watcher fields/timers
- `run_inventor_inline_for_status`
- `_prepare_full_flow_kits_without_attr109`
- `_create_full_flow_progress_dialog`
- All Full Flow locking methods from `MainWindow`
- `_start_full_flow_worker`
- `_review_full_flow_inventor_report`

Confirm no workflow-level identifier anywhere contains `attr109` or `attr_109`. The only remaining numeric references should be inside the low-level `sym_io` implementation, its comments/docstrings, and one format-specific test.

### 7. Final Validation
Run the full test suites for both repositories:

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m compileall .
```

Also run `pytest` where configured.

Use mocks/fakes for Inventor, RADAN, COM, subprocesses, filesystem operations, etc. No test may require real Inventor/RADAN/COM/W:/L:.

## Final Report Requirements
After completing all phases, report:
- Baseline, post-Phase-1, post-Phase-2, and final `main_window.py` line counts + reductions
- Files added / changed / removed
- Confirmation that all listed obsolete symbols are gone
- Confirmation that `write_part_kit_comments=False` is used in Full Flow
- Confirmation that numeric RADAN storage details are isolated in `sym_io`
- Test results
- Any remaining risks or recommended follow-up work