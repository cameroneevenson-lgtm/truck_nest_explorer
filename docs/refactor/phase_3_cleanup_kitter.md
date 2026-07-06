# Phase 3: Worker Cleanup, Domain Cleanup, and Kitter Change

## Recommended Codex Mode

Use **Implement mode** after Phases 1 and 2 have been reviewed and their tests pass.

This phase changes both `truck_nest_explorer` and the sibling `radan_kitter` repository. Both repositories must be available in the same workspace before implementation begins.

Complete Phases 1 and 2 (and their tests) before starting this phase.

## Codex Execution Guidance

Run this phase as a separate Codex task. Do not combine it with a later phase.

Before editing code:
1. Inspect the current implementation and relevant tests.
2. Confirm that all prerequisite phases are present and passing.
3. Record the requested baseline measurements.

During implementation:
- Keep the diff limited to this phase.
- Run focused tests after each substantial migration.
- Do not begin the next phase.

At completion, report:
- Files added, changed, and removed
- Important ownership or architecture changes
- Obsolete symbols removed
- Tests run and their exact results
- Required line-count or performance measurements
- Remaining risks or follow-up work

## Tasks

### 1. Finish Generic Worker Adoption

Phase 1 already created `truck_nest_explorer/background_job.py` and introduced:

- `BackgroundJobSignals`
- `BackgroundJobWorker`

Do not recreate, rename, or independently reimplement that worker.

First verify that the Phase 1 implementation preserves:

- `progress`, `done`, and `error` signals
- `request_stop()`
- `should_cancel()`
- `emit_progress()`
- Exception-to-traceback handling

Confirm that `InventorController` and `FullFlowController` use the existing generic worker.

Then migrate remaining assembly and cut-list background tasks where practical and behavior-preserving.

Remove any remaining references to:

- `PacketJobSignals`
- `PacketJobWorker`

If the existing Phase 1 worker has a demonstrated defect, fix it in place and add a regression test. Do not replace it merely because this phase revisits worker adoption.

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

## Cross-Repository Rules

For changes in `radan_kitter`:

- Preserve the default behavior of `prepare_kits`
- Keep `write_part_kit_comments=True` as the default
- Use semantic identifiers at service and workflow levels
- Keep numeric RADAN attribute details isolated to low-level `sym_io`
- Add or update tests in `radan_kitter` before updating the `truck_nest_explorer` call site
- Do not add a fallback that retries the old function signature

## Phase 3 Scope Boundary

Do not:
- Rework the controller architecture completed in Phase 2 unless needed for the stated cleanup
- Reintroduce watcher-based Inventor behavior
- Add compatibility aliases for deleted entry points
- Begin Phase 4 caching or asynchronous truck-switch work
- Rename the semantic option to anything containing `attr109`, `attr_109`, or another numeric storage detail

## Required Final Report

Report:
- Baseline, post-Phase-1, post-Phase-2, and final `main_window.py` line counts
- Files added, changed, and removed in both repositories
- Confirmation that all listed obsolete symbols are absent
- Confirmation that Full Flow passes `write_part_kit_comments=False`
- Confirmation that numeric RADAN storage details are isolated
- Test results for both repositories
- Compile results
- Remaining cross-repository risks

## Completion Gate

This phase is complete only when:
- Its focused tests pass
- The relevant full test suite passes
- Required obsolete symbols are absent
- Required measurements are recorded
- No work from the next phase has been started
- Update this Markdown file with the results above after running the phase.

## Phase 3 Results

Completed on 2026-07-06.

### Line Counts

- `main_window.py` original baseline physical line count: 3816 lines.
- `main_window.py` post-Phase-1 physical line count: 3484 lines.
- `main_window.py` post-Phase-2 physical line count: 3086 lines.
- `main_window.py` final post-Phase-3 physical line count: 3086 lines.
- Phase 3 did not change `main_window.py`.

### Files Added

`radan_kitter`:

- `tests/test_sym_io.py`

No `truck_nest_explorer` source files were added.

### Files Changed

`truck_nest_explorer`:

- `full_flow_service.py`
- `packet_build_service.py`
- `tests/test_services.py`
- `docs/refactor/phase_3_cleanup_kitter.md`

`radan_kitter`:

- `README.md`
- `kit_service.py`
- `sym_io.py`
- `tests/test_kit_service.py`

`radan_kitter/_runtime/runtime_trace.jsonl` was already dirty before this phase and remained a generated runtime-file modification after test runs.

### Files Removed

No files were removed.

### Architecture And Ownership Changes

- `radan_kitter.kit_service.prepare_kits()` now accepts `write_part_kit_comments: bool = True`.
- Default Kitter behavior is preserved: part comments are written and part-symbol backups are created when `write_part_kit_comments=True`.
- When `write_part_kit_comments=False`, Kitter skips part-comment writes and comment-only part backups, while still normalizing kit labels, updating priorities and `kit_text`, validating the donor, grouping kits, generating kit symbols, running `refresh_kit_fn`, reporting progress, and returning the kit count.
- `radan_kitter.sym_io` now exposes semantic part-comment helpers:
  - `part_comment_from_text()`
  - `set_part_comment_text()`
  - `set_part_comment()`
- Numeric RADAN storage details are isolated to `radan_kitter/sym_io.py` and the single low-level format test in `radan_kitter/tests/test_sym_io.py`.
- `truck_nest_explorer.full_flow_service.run_kitter_rf_assignment_for_project()` now calls `rk_kit_service.prepare_kits(..., write_part_kit_comments=False)`.
- The duplicated Truck Nest Explorer Kitter clone was deleted.
- `truck_nest_explorer.packet_build_service` now reuses `radan_kitter.sym_io` semantic text helpers for assembly comment updates instead of owning duplicated file-format regexes.

### Obsolete Symbols Removed Or Confirmed Absent

Confirmed absent from production `truck_nest_explorer` source:

- `PendingInventorJob`
- `run_inventor_inline_for_status`
- `_prepare_full_flow_kits_without_attr109`
- `_create_full_flow_progress_dialog`
- `_start_full_flow_worker`
- `_review_full_flow_inventor_report`
- `PacketJobSignals`
- `PacketJobWorker`

`run_full_flow_before_nester` and its supporting `FullFlowNeedsUserAction` compatibility path were also removed.

The Phase 2 Full Flow locking methods remain absent from `MainWindow`.

### Kitter And Full Flow Confirmations

- `InventorController` and `FullFlowController` continue to use the Phase 1 `BackgroundJobWorker`.
- Assembly and cut-list background jobs in `MainWindow` already use `BackgroundJobWorker`.
- The print-packet builder still delegates to `radan_kitter.packet_runtime.PacketBuildWorker`; this is a Kitter domain worker and not the removed `PacketJobWorker`.
- Full Flow passes `write_part_kit_comments=False` to Kitter RF preparation.
- No fallback retry for an older `prepare_kits()` signature was added.
- No workflow-level production identifier contains `attr109` or `attr_109`.

### Validation Results

Focused tests:

- `radan_kitter`: `python -m pytest tests/test_kit_service.py tests/test_sym_io.py -q` -> 9 passed in 0.17s.
- `truck_nest_explorer`: `python -m pytest tests/test_services.py -q` -> 105 passed in 1.48s.

Full and relevant suite checks:

- `truck_nest_explorer`: `python -m pytest -q` -> 105 passed in 1.41s.
- `radan_kitter` checked-in test files: `$tests = Get-ChildItem -Path .\tests -Filter 'test_*.py' -File | ForEach-Object { $_.FullName }; python -m pytest @tests -q` -> 58 passed, 7 warnings in 2.33s.
- `radan_kitter` repo-root discovery: `python -m pytest -q` -> 10 pre-existing collection errors from inaccessible generated temp folders (`_smoke_uzutih1v`, `tmp4ttlqq61`, and `tests/_tmp...` / `tests/_tmp_probe...`).

Compile checks:

- `truck_nest_explorer`: `python -m py_compile main_window.py full_flow_service.py packet_build_service.py background_job.py controllers\inventor_controller.py controllers\full_flow_controller.py tests\test_services.py` -> passed.
- `radan_kitter`: `python -m py_compile kit_service.py sym_io.py tests\test_kit_service.py tests\test_sym_io.py` -> passed.

### Remaining Risks And Follow-Up

- `truck_nest_explorer` now depends on the updated `radan_kitter.sym_io` semantic text helpers for assembly comment updates, so these repository changes should be deployed together.
- `radan_kitter` repo-root pytest remains blocked by inaccessible generated temp folders until those folders are excluded or cleaned by an explicit operator action.
- Generated runtime traces in `radan_kitter/_runtime/runtime_trace.jsonl` remain dirty and were not deleted or reverted under the generated-data safety rule.
- No Phase 4 caching or Phase 5/6 optimization work was started.
