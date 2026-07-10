# Phase 6: Targeted, Evidence-Based Optimization

Complete Phases 1–5 and their tests before starting this phase.

## Relationship to Phase 5

Phase 6 is driven exclusively by the completed Phase 5 residual hot-path
audit.

Phase 6 may implement any candidate that Phase 5 explicitly ranked as
`pursue` and supported with reproducible measurements.

Candidates should be implemented in descending order of expected value,
considering:

- User-visible impact
- Measurement confidence
- Correctness risk
- Implementation scope
- Dependencies between candidates
- Testability
- Rollback difficulty

A higher-risk candidate requires an explicit planning and approval gate before
implementation. Phase 6 must not exclude a material candidate solely because
it is not classified as low risk.

Phase 6 must not introduce optimizations that were not identified and ranked
in Phase 5.

## Recommended Codex Workflow

Use this workflow:

1. **Planning Pass**
   - Read the Phase 5 results.
   - Identify every candidate marked `pursue`.
   - Rank candidates by expected value, risk, and dependency order.
   - Produce a staged implementation plan.
   - Do not edit production code during this pass.

2. **Candidate Implementation Passes**
   - Implement one approved candidate at a time.
   - Measure before and after.
   - Run focused and cumulative tests.
   - Keep or revert the change based on evidence.

3. **Final Validation Pass**
   - Re-run the relevant Phase 4/5 performance scenarios.
   - Confirm cumulative performance gains.
   - Confirm no correctness regressions.
   - Record final results.

Do not combine unrelated candidates into one implementation diff.

## Goal

Implement all Phase 5-confirmed optimizations that are worth pursuing,
while maintaining correctness, safety, reviewability, and test coverage.

The goal is not to optimize every category in theory. The goal is to
optimize every **material, measured, approved** hot path.

## Candidate Categories

Phase 6 may include any of the following categories if, and only if, Phase 5
identified a material candidate in that category:

- **Database**
  - Missing SQLite indexes
  - Repeated queries
  - Inefficient query plans
  - Unnecessary refresh queries

- **Network/filesystem**
  - Duplicate existence or metadata checks
  - Broad scans
  - Blocking mapped-drive access
  - Redundant packet or preview checks

- **Cache behavior**
  - Weak cache keys
  - Excessive invalidation
  - Premature expiry
  - Poor negative-cache behavior
  - Unnecessary cache misses

- **UI/threading**
  - Expensive work remaining on the Qt UI thread
  - Inefficient worker handoffs
  - Result-application bottlenecks
  - UI blocking during refresh or truck switching

- **Python hot paths**
  - Expensive loops
  - Repeated transformations
  - Unnecessary model rebuilding
  - Costly serialization or parsing

- **Watchers/events**
  - Folder-watcher event storms
  - Redundant refreshes
  - Missing debounce
  - Over-broad invalidation

- **Additional measured hot paths**
  - Any other reproducible, material bottleneck identified by Phase 5

A category with no material Phase 5 finding must be skipped.

## Staged Candidate Workflow

Treat each approved Phase 5 candidate as a separate optimization unit.

For each candidate:

1. Record its Phase 5 evidence and baseline measurement.
2. Identify affected files, correctness invariants, and rollback plan.
3. Implement only that candidate and its required supporting changes.
4. Run focused correctness tests.
5. Run the same performance scenario and measurement harness used for the
   baseline.
6. Compare before and after results.
7. Keep the change only when it produces a meaningful, reproducible
   improvement without unacceptable correctness or maintainability cost.
8. Run cumulative regression tests before beginning the next candidate.
9. Record the result as accepted, revised, deferred, or rejected.

Do not combine unrelated candidates into one implementation diff.

## Important Rules

- Only implement changes that were explicitly listed as `pursue` candidates
  in the Phase 5 Phase 6 candidate table.
- Each implemented change must have before/after measurements using the same
  harness as Phase 4/5.
- Every change must be accompanied by relevant correctness tests.
- Each change must preserve existing data-deletion and filesystem safety
  rules.
- An optimization that does not produce a meaningful, reproducible
  improvement must be reverted or explicitly justified as necessary for
  another approved candidate.
- Higher-risk candidates require a separate explicit approval gate before
  implementation.
- Do not weaken stale-result, cache-invalidation, or mapped-drive failure
  behavior to gain speed.
- Do not replace measured bottlenecks with assumptions.
- Do not make "while we're here" changes.
- Keep all changes narrowly scoped and reviewable.

## Planning Pass Requirements

The planning pass must produce:

- Exact Phase 5 commit or results document used as input
- Full list of Phase 5 candidates
- Which candidates are `pursue`, `defer`, or `reject`
- Proposed implementation order
- Dependency graph between candidates, if any
- Candidate-by-candidate risk assessment
- Candidate-by-candidate test plan
- Candidate-by-candidate measurement plan
- Rollback plan for each candidate
- Files expected to change
- Explicit list of categories skipped because Phase 5 did not justify them

Do not edit production code during the planning pass.

## Scope Boundary

Do not:

- Implement optimizations not explicitly recommended in Phase 5
- Begin broad architectural refactoring
- Revisit Phase 1–4 structural work except as directly required by a Phase 5
  candidate
- Add new features
- Introduce new caching strategies without Phase 5 justification
- Change threading behavior without Phase 5 justification
- Change polling behavior without Phase 5 justification
- Change database schema or indexes without Phase 5 justification
- Optimize speculative hot paths
- Bundle unrelated candidates into one change
- Keep a change that fails to produce a meaningful measured gain unless it is
  explicitly required by another approved candidate

## Required Validation

For every implemented candidate:

- Run before/after performance measurements
- Use the same measurement harness and scenario definitions as Phase 4/5
  wherever possible
- Run focused correctness tests
- Run the relevant full test suite
- Confirm no regression in correctness or user-visible behavior
- Confirm no regression in stale-result protection
- Confirm no regression in cache invalidation behavior
- Confirm missing or slow mapped drives still do not block the UI thread
- Document the measured improvement
- Document whether the change was accepted, revised, deferred, or rejected

After all accepted candidates:

- Re-run the relevant Phase 4/5 performance scenarios
- Report cumulative before/after measurements
- Run cumulative regression tests
- Confirm no unapproved changes were included

## Required Final Report

Report:

- Phase 5 findings that justified each implemented candidate
- Candidate implementation order
- Candidates accepted, revised, deferred, or rejected
- Before and after measurements for each implemented optimization
- Cumulative before and after measurements
- Files changed
- Tests run and exact results
- Any changes reverted because measurements did not justify keeping them
- Any higher-risk candidates deferred for explicit approval
- Remaining risks
- Recommended follow-up work, if any

## Completion Gate

Phase 6 is complete only when:

- Every implemented change is backed by Phase 5 evidence
- Every implemented change has before/after measurements
- Every accepted change shows meaningful, reproducible improvement or is
  explicitly justified as necessary for another approved candidate
- Relevant focused tests pass
- Relevant full test suite passes
- Cumulative measurements are recorded
- No unmeasured or unauthorized changes were made
- No unrelated refactoring was included
- No previous phase documents were modified
- This Markdown file is updated with results

## Update This File

After running Phase 6, append an `## Execution Results` section containing:

- The Phase 5 candidate table used as input
- Candidate-by-candidate implementation results
- Candidate-by-candidate before/after measurements
- Cumulative measurements
- Test results
- Final accepted/deferred/rejected list
- Remaining risks and follow-up recommendations

Preserve the instructions above so the process remains reproducible.

## Execution Results

Completed on 2026-07-10.

### Phase 5 Candidate Table Used As Input

From `docs/refactor/phase_5_profiling_hot_paths.md` ("Execution Results" -> "Ranked Phase 6 Candidate Table"), commit `605a0ae32edebfc538f3b50833f77bad76b58f91`:

| Candidate | Recommendation |
|---|---|
| Replace/augment 120ms `QTimer` completion polling with a Qt-native worker-finished signal for status/flow futures | **Pursue** |
| Reduce per-kit cold-switch filesystem-check count for slow/network-drive cases | Defer (no real mapped-drive latency measurement available) |
| Memoize per-kit `_kit_table_signature` tuple construction | Reject (cost already sub-millisecond, not material) |
| Any change to packet-PDF detection caching | Reject (confirmed 0 incremental cost, not a bottleneck) |

Only the first candidate was implemented. The other three were explicitly out of scope per their Phase 5 recommendation, and were not touched.

### Candidate Implemented: Signal-based status/flow future completion

**Phase 5 evidence used:** cold-switch work measured at 5.02-6.48ms (median 5.33ms) while completion detection was gated behind an unchanged 120ms `QTimer`, a ~24x gap Phase 5 flagged as the highest-confidence residual hot path.

**Correctness invariant preserved:** the existing `TruckSwitchRunContext` run-id staleness guard and the `_poll_pending_status_future` / `_poll_pending_flow_future` methods that implement it were not modified at all. The change only adds a second, faster trigger for the same, unchanged poll methods.

**Implementation:**
- Added two class-level `Signal()` attributes on `MainWindow`: `_status_future_ready`, `_flow_future_ready`.
- Connected each to the existing (unmodified) `_poll_pending_status_future` / `_poll_pending_flow_future` slots alongside the existing 120ms `QTimer` connections - both triggers remain active.
- Added `_notify_status_future_ready` / `_notify_flow_future_ready` helper methods that emit these signals, wrapped in `try/except RuntimeError` to guard against a background task's done-callback firing after the window's C++ object has been torn down during shutdown.
- Attached `future.add_done_callback(...)` to all 5 existing `_status_executor`/`_flow_executor submit()` call sites (`_on_truck_changed`, `_load_flow_for_truck`, `_prewarm_visible_truck_caches` x2, `_queue_status_refresh_for_truck`). No call site's dispatch logic, cache lookups, or run-id/token bookkeeping was changed - only a callback was attached to the future each site already created.
- The 120ms timers are **unchanged and still running** as a safety net: if a signal is ever missed for any reason, the existing polling still guarantees completion is detected within one interval, exactly as before this change. Worst-case behavior is therefore unchanged; only the common case gets faster.

**Files changed:** `main_window.py` only (42 insertions, 8 deletions).

### Before/After Measurement

Using a new scenario added to the same Phase 5 measurement harness (temp-workspace fixture, offscreen `QApplication`, mocked flow probe - no real Inventor/RADAN/COM/mapped drive), isolating exactly this change on the same commit: one run drove completion detection using only `app.processEvents()` with the new signal connected (current behavior), and one run with the same two signals explicitly disconnected immediately after construction (reproducing pre-change behavior on identical code), 10 repetitions each:

| Condition | Runs | Median | Min-Max |
|---|---|---|---|
| Timer-only (signals disconnected, reproduces pre-Phase-6 behavior) | 10 | 139.1 ms | 119.9-159.4 ms |
| Signal + timer (current, post-Phase-6 behavior) | 10 | 18.2 ms | 16.6-28.4 ms |

**Result: median time from truck-switch dispatch to visible status dropped from 139.1ms to 18.2ms, a ~121ms (~7.6x) reduction**, for a cold switch whose actual underlying work is ~5.3ms. The remaining ~18ms in the "after" condition is attributable to this test harness's own `app.processEvents()` / `time.sleep(5ms)` poll granularity, not the application - a real running event loop (which processes queued signal emissions as soon as it is idle, without an artificial sleep) would be expected to reflect the underlying work time even more closely, though that was not separately isolated.

The timer-only figure (~139ms, consistently just past the 120ms mark) confirms the mechanism precisely: since `_status_watch_timer` is started at the same moment the future is submitted, and the underlying work reliably finishes well inside one interval, completion was essentially always detected on the *first* tick after submission - not, as loosely worded in the Phase 5 write-up, an average of half the interval. This is a stronger result than Phase 5 estimated, not a weaker one.

### Correctness Validation

- `python -m pytest -q` -> **138 passed, 28 subtests passed** (identical to the pre-Phase-6 baseline recorded in Phase 5's Execution Results - no regressions, no new tests were needed because no observable behavior changed, only its timing).
- Stale-result protection re-verified via the existing `test_truck_switch_stale_status_and_flow_results_are_ignored` and `test_full_flow_controller_stale_signals_are_ignored` tests, both still passing unmodified - these exercise `_poll_pending_status_future`/`_poll_pending_flow_future` directly, which this change does not alter.
- Cache invalidation re-verified via the existing `test_collect_kit_statuses_uses_cache_and_truck_invalidation` test, still passing unmodified.
- Missing/slow mapped-drive non-blocking behavior: re-confirmed qualitatively - the new done-callbacks fire on the same background executor threads Phase 4 already used; nothing was added to the UI thread's synchronous path.
- `git status` clean apart from the intended `main_window.py` diff; no unrelated files touched.

### Cumulative Measurements

| Scenario | Phase 5 baseline | Post-Phase-6 |
|---|---|---|
| Cold-switch underlying work (`collect_kit_statuses`) | 5.33 ms median | Unchanged (not touched by this candidate) |
| Cold-switch completion **visibility** (event-loop driven) | Not previously isolated; timer-only re-measurement here: 139.1 ms median | 18.2 ms median |
| Full test suite | 138 passed, 28 subtests | 138 passed, 28 subtests |

### Accepted / Deferred / Rejected

- **Accepted:** signal-based status/flow future completion. Produced a large, reproducible, low-risk improvement with an unchanged worst-case fallback.
- **Deferred:** per-kit cold-switch filesystem-check reduction. Still blocked on a real mapped-drive latency measurement, per Phase 5's recommendation; not started.
- **Rejected:** `_kit_table_signature` memoization and any packet-PDF caching change. Not started, per Phase 5's recommendation.

### Remaining Risks

- The done-callback's `try/except RuntimeError` guard around signal emission is a new failure mode surface (however narrow) that did not exist before this change, since completion notification can now originate from a background thread touching a `MainWindow`-owned signal during shutdown. This is exercised implicitly by the existing test suite's window-teardown pattern (executor `shutdown(wait=False, cancel_futures=True)` followed by `window.close()`) without incident, but was not specifically stress-tested for a task that completes exactly during teardown.
- The truck-list future (`_pending_truck_future` / `_truck_watch_timer`) was deliberately left on timer-only completion, matching Phase 5's candidate wording ("status/flow futures"). If a future Phase 5 pass measures a material gap there too, it would need its own candidate and approval - this Phase 6 pass did not touch it.

### Recommended Follow-Up

None required to close this Phase 6 pass. A future profiling pass could obtain a real mapped-drive latency sample to re-evaluate the deferred filesystem-check-count candidate, per Phase 5's stated condition for revisiting it.