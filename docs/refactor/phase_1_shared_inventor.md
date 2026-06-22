# Phase 1: Shared Inventor Implementation + Remove Watcher Path + Generic Worker

## Recommended Codex Mode

Use **Implement mode** with high or extra-high reasoning.

This document is implementation-ready. Codex must inspect the existing implementation before editing, but it should not produce a separate planning-only response.

Refactor the current checkouts of `truck_nest_explorer` and the sibling `radan_kitter` repository.

This is an implementation task. Inspect the current code first. We own both repositories and have no legacy compatibility requirements.

## Important Safety Rules
- Remove obsolete source code, imports, fields, methods, and tests.
- Do **not** delete project data, RPDs, BOMs, PDFs, DXFs, symbols, logs, CSVs, reports, or user-generated files.
- Generated CSV/report files may only be deleted through the explicit “Discard CSV/Report” operator action.

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

## Baseline Inspection (do this first)
1. Measure and record the current line count of `main_window.py`.
2. Run the existing test suites for both repositories.
3. Identify all current Inventor code paths (standalone and Full Flow).
4. Identify every MainWindow field, timer, worker, and helper related to Inventor execution and watching.
5. Confirm the current explicit CSV/report discard behavior.

## Tasks for Phase 1

### 0. Extract Generic Worker (first)
Create `truck_nest_explorer/background_job.py` and move the generic worker implementation out of `main_window.py`.

Rename to:
- `BackgroundJobSignals`
- `BackgroundJobWorker`

Preserve all existing signals and methods (`request_stop`, `should_cancel`, `emit_progress`, traceback handling).

This worker will be used by `InventorController` and later `FullFlowController`.

Remove the old `PacketJob*` classes from `main_window.py` after migration.

### 1. Create `inventor_service.py`
Create `truck_nest_explorer/inventor_service.py` as the **single non-UI** Inventor-to-RADAN implementation.

It must own:
- Validating exactly one BOM candidate
- Validating L-side project directory and configured Inventor entry
- Calling `run_inventor_to_radan_inline`
- Translating `InventorToRadanInlineNeedsUi` into `InventorNeedsUserAction`
- Moving generated output into the L-side project exactly once
- Locating and validating the generated report
- Identifying generated CSV/TXT eligible for explicit discard
- Returning typed `InventorRunResult` and `InventorDiscardResult`
- Raising typed errors instead of showing Qt dialogs

The service must **not**:
- Import `MainWindow`
- Create Qt widgets or show `QMessageBox`
- Perform report review
- Automatically delete output after ordinary errors

After migration, **delete** `run_inventor_inline_for_status` from `full_flow_service.py`. Do not leave an alias.

### 2. Create `InventorController`
Create `truck_nest_explorer/controllers/inventor_controller.py`.

`InventorController` owns the standalone “Run Inventor Tool” UI flow:
- Duplicate-run guard
- Acquiring selected status
- Button state management
- Running the shared service in a background worker (use the new `BackgroundJobWorker`)
- Progress display
- Receiving typed result on UI thread
- Running shared report review
- Handling accepted / discarded / user-action-required / error outcomes
- Status refresh and logging

It must **not** contain the actual Inventor conversion or output movement logic.

`MainWindow` should only delegate:
```python
def run_selected_inventor_flow(self) -> None:
    self.inventor_controller.start_selected()
```

### 3. Share Report Review
Extend `dialogs/inventor_report_review_dialog.py` with one shared review helper used by both `InventorController` and `FullFlowController`.

Provide:
- `InventorReviewState` enum
- `InventorReviewOutcome` dataclass
- `review_inventor_result(parent, result)` function

The helper must:
- Run on the Qt UI thread
- Use `result.report_path` directly
- Call `discard_inventor_result` only after explicit discard confirmation
- Return typed outcome

Both standalone Inventor and Full Flow must use this same helper.

### 4. Remove the Watcher-Based Inventor Path
Completely remove the old timer/watcher implementation, including:
- `PendingInventorJob`
- `_pending_inventor_job`
- `_inventor_watch_timer`
- `_inventor_output_signature`
- `_poll_pending_inventor_job`
- `_finish_pending_inventor_job`
- All output-stability polling and delayed movement logic
- Duplicate calls to `run_inventor_to_radan_inline` and `move_inventor_outputs_to_project`
- MainWindow’s duplicated report-review block

When `InventorNeedsUserAction` is raised:
- Stop cleanly
- Restore UI
- Show clear message
- Do **not** fall back to another watcher workflow

### 5. Phase 1 Validation
Add focused tests covering:
- Exactly one BOM required
- Missing/ambiguous BOM raises typed error
- Project directory and entry validation
- Output moved exactly once
- `InventorToRadanInlineNeedsUi` translated correctly
- Accepted review does not delete output
- Explicit discard only deletes eligible generated files
- `InventorController` restores button state
- No timer/watcher path remains
- Generic worker is used

Run relevant tests. Then measure `main_window.py` line count and record the reduction.

Do not proceed to Phase 2 until Phase 1 tests pass.

## Phase 1 Scope Boundary

Phase 1 may introduce the generic background worker needed by later phases, but it must not extract the complete Full Flow controller.

Do not:
- Begin the Phase 2 Full Flow migration
- Move unrelated assembly or cut-list jobs unless required to remove a broken dependency
- Introduce caching or performance work from Phase 4
- Modify `radan_kitter` unless a current Phase 1 dependency strictly requires it

The Phase 1 implementation should leave clear extension points for the later `FullFlowController` without implementing that controller early.

## Required Final Report

Report:
- Baseline and final `main_window.py` line counts
- Files added, changed, and removed
- Confirmation that the watcher-based Inventor path is gone
- Confirmation that generated output is deleted only after explicit discard confirmation
- Focused and full test results
- Any unresolved Qt-threading or filesystem risks

## Completion Gate

This phase is complete only when:
- Its focused tests pass
- The relevant full test suite passes
- Required obsolete symbols are absent
- Required measurements are recorded
- No work from the next phase has been started