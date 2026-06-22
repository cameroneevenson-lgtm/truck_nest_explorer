# Phase 2: Extract Full Flow Controller (Highest Impact)

## Recommended Codex Workflow

Use two passes in the same Codex thread:

1. **Plan mode**
2. Review the plan
3. **Implement mode**

The planning pass must not modify files.

This is the largest and highest-impact phase. Complete Phase 1 and its tests before starting this phase.

## Planning Pass Requirements

Before implementation, inspect the completed Phase 1 code and produce a concrete, file-by-file migration plan covering:

- Every current Full Flow entry point
- All callbacks, closures, workers, signals, and timers involved
- Every `MainWindow` field that will move
- Worker ownership and Qt thread-affinity boundaries
- The report-review transition
- Every terminal success, discard, user-action, and error path
- Action-lock acquisition, refresh reapplication, and release
- Idempotent cleanup and duplicate-finish prevention
- `run_id` handling for stale worker signals
- Application-close behavior
- Existing tests that must change
- New tests required by this document

The plan must identify likely stale-signal, double-cleanup, UI-lock, and nested-callback hazards.

Do not edit code during the planning pass.

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

## Goal
Extract **all** Full Flow orchestration from `main_window.py` into a dedicated `FullFlowController`.

`MainWindow` should become a thin delegate after this phase.

## Tasks

### 1. Create `FullFlowController`
Create `truck_nest_explorer/controllers/full_flow_controller.py`.

`FullFlowController` must own the complete UI-level Full Flow sequence:

- Duplicate-run guard
- Selected-kit and RPD validation
- Confirmation prompt + optional headless-nester checkbox
- Progress dialog lifecycle
- Action locking (move ownership here)
- Worker ownership and lifecycle
- Inventor phase (via shared service)
- Report-review transition (via shared reviewer)
- Post-review phase
- Packet opening
- Optional nester phase
- RADAN-close confirmation
- Project opening
- Status refresh
- Logging and final summary
- Application-close prevention while active
- Every success, discard, user-action, and error path

`MainWindow` must only delegate:
```python
def run_selected_full_flow(self) -> None:
    self.full_flow_controller.start_selected()
```

Do **not** leave Full Flow jobs, phase callbacks, progress closures, or action-lock logic in `MainWindow`.

### 2. Full Flow State (inside the controller)
Use a simple phase enum and run context:

```python
class FullFlowPhase(Enum):
    IDLE = "idle"
    INVENTOR = "inventor"
    REPORT_REVIEW = "report_review"
    POST_REVIEW = "post_review"
    NESTER = "nester"
    FINALIZING = "finalizing"

@dataclass
class FullFlowRunContext:
    run_id: int
    status: KitStatus
    run_nester: bool
    phase: FullFlowPhase
    inventor_result: InventorRunResult | None = None
    full_flow_result: FullFlowResult | None = None
    opened_packet_count: int = 0
    finished: bool = False
```

Use named methods (`_start_inventor`, `_on_inventor_done`, `_review_report`, etc.) instead of a giant nested callback tree.

### 3. Progress Dialog
Keep a focused private `_FullFlowProgressDialog` inside `full_flow_controller.py`.

It should provide:
- Current-status label
- Timestamped read-only log with auto-scroll
- Disabled close while active
- Enabled Dismiss button after `finish()`

Remove `MainWindow._create_full_flow_progress_dialog`.

### 4. Move Action Locking into `FullFlowController`
Create a small private `_ActionLock` helper inside the controller (do not create a separate file).

The lock must:
- Receive widgets and the editable table explicitly from `MainWindow`
- Snapshot original enabled states
- Disable all file-mutating controls and table editing
- Change Full Flow button text while running
- Survive UI refreshes
- Restore exact original states and table edit triggers
- Be safe to release more than once

Expose:
- `is_running` property
- `reapply_action_lock()`
- `can_close()`

`MainWindow.closeEvent` must consult `FullFlowController.can_close()`.

After migration, **delete** these obsolete `MainWindow` members:
- `_full_flow_running`
- `_full_flow_worker`
- `_full_flow_disabled_widget_states`
- `_full_flow_table_edit_triggers`
- `_full_flow_mutating_widgets`
- `_lock_full_flow_actions`
- `_reapply_full_flow_action_lock`
- `_unlock_full_flow_actions`
- `_start_full_flow_worker`
- `_confirm_close_radan_for_full_flow`

### 5. Idempotent Cleanup + Stale Signal Protection
Implement one terminal cleanup path that:
- Can safely run twice
- Restores controls exactly once
- Clears worker references
- Makes progress dialog dismissible
- Prevents duplicate completion dialogs or status refreshes
- Uses `run_id` to ignore stale worker signals from previous runs
- Ensures report discard cannot continue into post-review work
- Ensures worker errors / `InventorNeedsUserAction` / packet or nester failures cannot leave the UI locked

### 6. Preserve Full Flow Sequence and Behavior
Preserve the exact current sequence and guarantees:
- No concurrent file-mutating actions while Full Flow is active
- No Qt dialogs or widget updates from worker threads
- RF only runs for PAINT PACK
- Assembly context still updates part-symbol comments
- Project still opens after nester failure
- No automatic deletion of output after ordinary errors

### 7. Phase 2 Validation & Measurement
Add tests covering:
- Double-run guard
- Accepted vs discarded review behavior
- Action lock snapshots and restores exact states
- Controls disabled before the run remain disabled
- Table edit triggers restore correctly
- Cleanup is idempotent
- Stale worker signals are ignored
- `InventorNeedsUserAction` and worker errors stop and unlock cleanly
- Nester failure still opens the project
- Close is blocked while active

After Phase 2:
1. Measure `main_window.py` line count again.
2. Report baseline → post-Phase-1 → post-Phase-2 counts and reduction percentage.
3. Confirm `MainWindow` now only delegates Full Flow to the controller.

Do not proceed to Phase 3 until Phase 2 tests pass.

## Implementation Scope Boundary

Implement only the approved Phase 2 plan.

Do not:
- Begin the Kitter API changes from Phase 3
- Recreate or rename the Phase 1 background worker without a demonstrated defect
- Begin caching or truck-switch optimization
- Leave a second compatibility path in `MainWindow`
- Leave nested callback trees when named phase methods can be used

## Required Final Report

Report:
- Baseline, post-Phase-1, and post-Phase-2 `main_window.py` line counts
- Percentage reduction from baseline
- Files added, changed, and removed
- The final controller phase/state model
- Confirmation that cleanup is idempotent
- Confirmation that stale signals are ignored
- Confirmation that controls restore to their exact original state
- Focused and full test results
- Remaining Qt lifecycle or sequencing risks

## Completion Gate

This phase is complete only when:
- Its focused tests pass
- The relevant full test suite passes
- Required obsolete symbols are absent
- Required measurements are recorded
- No work from the next phase has been started