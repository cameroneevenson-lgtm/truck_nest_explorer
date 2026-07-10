# Phase 5: Post-Phase-4 Residual Hot-Path Audit

Complete Phases 1–4 and their tests before starting this phase.

## Relationship to Phase 4

Phase 4 already introduced or required:

- Truck-switch timing and lifecycle instrumentation
- Database query counts
- Filesystem existence and metadata-check counts
- Cache hit, miss, and invalidation counts
- Cold-cache and warm-cache truck-switch scenarios
- Rapid A → B → C stale-result validation
- UI-thread versus worker-boundary checks
- Before-and-after performance measurements

Phase 5 must **reuse those Phase 4 measurements, test harnesses, and instrumentation**. It must not create a second profiling framework or repeat Phase 4's cache, invalidation, asynchronous switching, or polling-reduction design work.

Phase 5 exists to answer a narrower question:

> After Phase 4 is complete, which measurable bottlenecks still remain, and is a Phase 6 optimization justified?

If a required metric is missing, extend the existing Phase 4 instrumentation minimally and document the gap. Do not replace the existing instrumentation.

## Recommended Codex Workflow

Use two passes in the same Codex thread:

1. **Plan and inspect**
2. **Measure and report**

During the first pass, inspect the completed Phase 4 implementation, instrumentation, tests, and final measurements. Produce a concise execution plan identifying the existing tools and scenarios that will be reused.

Do not propose production optimizations during the planning pass.

During the second pass, run the approved profiling scenarios and produce the required evidence. Source changes are allowed only for narrowly scoped, diagnostics-only gaps in the existing instrumentation.

## Goal

Validate that the Phase 4 gains remain measurable, identify and rank the residual hot paths in the post-Phase-4 implementation, and produce an evidence-based recommendation for Phase 6.

Do not assume that a Phase 6 is necessary.

## 1. Verify Phase 4 Prerequisites

Before profiling:

1. Confirm the Phase 4A and Phase 4B completion gates were satisfied.
2. Locate the Phase 4 instrumentation and performance test harnesses.
3. Locate the Phase 4 baseline and final measurement report.
4. Record the exact repository commit being profiled.
5. Record the relevant Python, Qt, operating-system, database, and filesystem test conditions.
6. Identify which measurements are:
   - Reused directly from Phase 4
   - Re-run for Phase 5
   - Newly added because Phase 4 lacked a required metric

If Phase 4 lacks reproducible measurements, stop and report the prerequisite gap rather than inventing numbers.

## 2. Re-run the Existing Phase 4 Scenarios

Reuse the existing Phase 4 harnesses for:

- Cold-cache truck switch
- Warm-cache truck switch
- Repeated switch to the same truck
- Rapid A → B → C switching
- Manual status refresh
- Folder-watcher-triggered invalidation and refresh
- Slow mapped-drive simulation
- Missing mapped-drive simulation

For each scenario, capture the metrics already supported by Phase 4:

- End-to-end wall-clock time
- Time spent in measured sub-operations
- Database query count
- Filesystem existence and metadata-check count
- Cache hit, miss, and invalidation count
- Stale-result count
- UI-thread blocking observations
- Worker start and completion timing

Use repeated runs where practical. Report the number of runs and use a robust summary such as median plus range or percentiles. Do not present a single noisy run as a definitive result.

## 3. Audit Residual Hot Paths

Rank the remaining measured costs rather than listing every timed operation.

Examine at least:

- Database queries that remain repeated or unexpectedly expensive
- Network filesystem checks that remain duplicated, broad, or blocking
- Cache misses caused by weak keys, premature expiry, or invalidation churn
- Work still performed on the Qt UI thread
- Worker startup, handoff, or result-application overhead
- Folder-watcher event storms or redundant refreshes
- Python-level loops or transformations that dominate measured runtime
- SQLite query plans or missing indexes when query timing identifies the database as a significant contributor

For each candidate hot path, report:

- Operation or code path
- Triggering scenario
- Measured time or count
- Share of the relevant end-to-end operation
- Reproduction method
- Confidence level
- Likely bottleneck category: database, network filesystem, cache, Python, Qt event loop, worker lifecycle, or external dependency

Correlation is not proof of causation. Clearly distinguish measured facts from hypotheses.

## 4. Rules for Additional Instrumentation

Prefer existing Phase 4 instrumentation.

Additional instrumentation is permitted only when:

- A material residual cost cannot be attributed with the existing metrics
- The change is small and diagnostics-only
- It is disabled or low-overhead in normal use
- It does not change application behavior
- It has deterministic test coverage where practical
- It reuses the existing metrics or tracing structure

Do not add a second global profiler, cache statistics system, or truck-switch lifecycle framework.

Temporary instrumentation must either be removed before completion or deliberately integrated into the existing Phase 4 diagnostics with a clear justification.

## 5. Phase 6 Decision

Produce a ranked Phase 6 candidate table containing:

- Candidate change
- Supporting measurement
- Expected impact
- Confidence
- Correctness risk
- Implementation scope
- Required tests
- Recommendation: pursue, defer, or reject

A Phase 6 should be recommended only when a remaining bottleneck is:

- Reproducible
- Material to user-visible latency or resource usage
- Supported by evidence
- Addressable without disproportionate correctness risk

It is valid to conclude that no Phase 6 is currently justified.

## Scope Boundary

Phase 5 is a **post-optimization audit and diagnosis phase**, not another performance implementation phase.

Do not:

- Redesign the Phase 4 cache
- Change cache expiration or invalidation behavior
- Change the threading model
- Change truck-switch orchestration
- Remove or alter polling
- Add database indexes
- Implement quick wins
- Refactor production code for performance
- Re-run the entire Phase 4 implementation under a new name
- Begin Phase 6 work

Any production change discovered during profiling must be documented as a Phase 6 candidate rather than implemented here.

## Validation

After any diagnostics-only instrumentation change:

- Run the relevant Phase 4 performance and correctness tests
- Run the relevant full test suite
- Confirm normal application behavior is unchanged
- Confirm instrumentation is disabled or low-overhead by default
- Confirm stale-result and cache-invalidation behavior still passes
- Confirm no production optimization slipped into the Phase 5 diff

Automated tests must use mocks or fakes and must not require real Inventor, RADAN, COM, production databases, or mapped drives.

Real-environment operator measurements may be reported separately, but they must include environment details and must not be mixed with mocked benchmark numbers.

## Required Final Report

Report:

- Exact commit profiled
- Phase 4 artifacts and instrumentation reused
- Any Phase 4 measurement gaps discovered
- Scenario definitions and run counts
- Measured residual hot paths, ranked by impact
- Query, filesystem-check, cache, stale-result, and timing data
- UI-thread versus worker findings
- Facts separated from hypotheses
- Diagnostics-only files changed, if any
- Relevant test results
- Ranked Phase 6 candidate table
- A final recommendation: proceed with Phase 6, defer it, or stop

## Completion Gate

Phase 5 is complete only when:

- Phase 4 overlap is explicitly accounted for
- Existing Phase 4 instrumentation was reused wherever possible
- No duplicate profiling framework was introduced
- Residual hot paths are ranked using reproducible measurements
- Measurement conditions and run counts are recorded
- Facts are separated from hypotheses
- No production optimization was implemented
- Relevant tests still pass
- The Phase 6 decision is evidence-based

Do not begin Phase 6 during this phase.

## Update This File

After running Phase 5, append an `## Execution Results` section containing the measured results and Phase 6 decision. Preserve the instructions above so the audit remains reproducible.

## Execution Results

Completed on 2026-07-10.

### Exact Commit Profiled

`605a0ae32edebfc538f3b50833f77bad76b58f91` ("Add CLAUDE.md project guidance"), branch `main`, working tree clean.

Environment: Python 3.12.10, PySide6 6.10.2, Windows, `C:\Tools\.venv`. Filesystem for all measurements below was the local NTFS temp volume (`%TEMP%`), not a real mapped network drive — see the Slow/Missing Mapped Drive scenarios for how network latency was simulated.

### Phase 4 Prerequisite Check

- Phases 1-4 completion gates were satisfied: each phase file has a `## Phase N Results` section, and the full suite passes (138 passed, 28 subtests, at this commit; was 110 passed at Phase 4's own completion — additional coverage has been added since, not lost).
- Phase 4 instrumentation located and reused directly, unmodified: `performance_metrics.py` (`GLOBAL_METRICS`, `PerformanceMetrics`, `BoundedTTLCache`, `reset_performance_metrics`, `performance_snapshot`), the `FILE_METADATA_CACHE` / `KIT_STATUS_CACHE` singletons in `services.py`, and the `TruckSwitchRunContext` run-id staleness guard in `main_window.py`.
- Phase 4's baseline/final measurement report (`docs/refactor/phase_4_performance_caching.md`, "Phase 4 Results" section) was read and used as the comparison point.
- **Gap found and accounted for before profiling:** 7 commits landed between Phase 4's completion (`b622a3d`) and this commit, including [`076c649` "Improve truck explorer responsiveness and packet guards"](076c649), which extended the Phase 4 `BoundedTTLCache`/`FILE_METADATA_CACHE` pattern into a new area: print/assembly/cut-list packet-PDF detection, now wired into the truck-switch render path (`_kit_table_signature` and `_populate_status_row` in `main_window.py`). This is new code Phase 4's own measurements never covered. No other prerequisite gaps were found — no second profiling framework exists, cache TTLs are unchanged (5s/2s filesystem, 30s kit-status), and the 120ms/1500ms/30000ms/5000ms timer intervals documented by Phase 4 are all still present verbatim in `main_window.py`.
- All measurements below reuse the existing instrumentation with zero source changes. No diagnostics-only gap required a code change — every scenario was answerable with the existing counters (`filesystem_checks`, `database_queries`, `cache_hits`/`cache_misses` by cache name, `stale_results_ignored`).

### Measurement Method

A standalone script (not committed to the repository) drove the real `collect_kit_statuses()` service function and a real, offscreen `MainWindow` (`QT_QPA_PLATFORM=offscreen`) against temp-directory fixtures — no real Inventor, RADAN, COM, production database, or mapped drive was used, consistent with the Phase 4/5 mocking requirement. Flow-dashboard probing was mocked at `main_window.load_cached_flow_truck_insight` (the same seam Phase 4's own tests mock), since flow-probe subprocess cost is a separate, already-categorized external dependency, not a filesystem/cache hot path. Each scenario was repeated 10-15 times; results below report median/min/max in milliseconds rather than a single run. The fixture was one truck with 4 kits (matching the kit-name set used throughout the existing test suite: PAINT PACK, PUMP HOUSE, PUMP MOUNTS, STEP PACK), three of which had print/assembly/cut-list packet PDFs present.

### Scenario Results

| # | Scenario | Runs | Median | Min-Max | Key counters |
|---|---|---|---|---|---|
| 1 | Cold-cache truck switch (`collect_kit_statuses`, empty caches) | 15 | 5.33 ms | 5.02-6.48 ms | 48 filesystem checks (12/kit); 44 `filesystem_metadata` misses, 1 `kit_status` miss |
| 2 | Warm-cache truck switch (same truck, cache primed) | 15 | 0.026 ms | 0.026-0.052 ms | 0 filesystem checks added; 15 `kit_status` hits |
| 3 | Repeated switch to the same (already-displayed) truck | 15 | 0.539 ms | 0.533-0.851 ms | in-memory signature recompute only, no cache access |
| 4 | Rapid A -> B -> C switching | 1 (3 dispatches) | 3.06 ms total dispatch | - | 4 stale results ignored; final displayed truck (F33333) statuses all matched F33333 - no stale bleed-through |
| 5 | Manual status refresh (`use_cache=False`) | 10 | 12.97 ms | 12.02-14.55 ms | routed through `_status_executor` + polling loop, not a direct call - see analysis below |
| 6 | Folder-watcher-triggered invalidation | - | - | - | Not applicable - Phase 4's baseline explicitly recorded "no production folder watcher exists," and that remains true at this commit. Nothing to re-run. |
| 7 | Missing mapped drive (`release_root`/`fabrication_root` point at a nonexistent path) | 10 | 1.64 ms | 1.62-5.25 ms | Still returns all 4 configured kits (from `kit_templates`), each marked not-present; no exceptions, no retries |
| 8 | Slow mapped drive (50 ms injected latency per raw `os.scandir`/`Path.exists` call) | 1 | - | - | Synchronous `_on_truck_changed()` dispatch: 1.43 ms (stayed under 20 ms). Total time to visible status: 2409.75 ms |
| 9 (new) | Packet-PDF render cost (post-Phase-4 addition) | 1 | - | - | 0 incremental filesystem checks, 0 incremental DB queries, 0.036 ms for 4 kits |

### Residual Hot Path Audit (ranked by measured impact)

**1. 120ms poll-timer floor dominates cold-switch perceived latency on fast/local storage.**
- Measured fact: actual cold-switch work (scenario 1) completes in ~5.3 ms on local disk. Cold switches are dispatched to `_status_executor` and their completion is only detected by the `_status_watch_timer` (120 ms interval, unchanged since Phase 4).
- Hypothesis (not directly measured here, follows from the unchanged 120ms interval documented in Phase 4): on fast/local storage, a user can wait up to ~120 ms (average ~60 ms) for a switch to visibly complete even though the underlying work took ~5 ms - the timer granularity, not the filesystem, sets the perceived latency floor.
- This is the same "Remaining Risk" Phase 4 already flagged ("Future completion still uses Qt timer polling rather than Qt signal workers"). Phase 5 adds a concrete number to that qualitative risk: roughly a **24x** gap between real work and worst-case detection latency on local storage.
- Category: worker lifecycle / Qt event loop. Confidence: high (interval value read directly from source; work-time measured directly).

**2. Warm-cache and repeated-same-truck paths are already effectively free.**
- Measured fact: warm switch costs 0.026 ms with 0 added filesystem checks (scenario 2); reselecting the already-displayed truck costs 0.539 ms of pure in-memory signature recomputation (scenario 3), well below any perceptible threshold.
- No action warranted.

**3. Packet-PDF detection (added after Phase 4) is not a new hot path.**
- Measured fact: rendering 4 kits' print/assembly/cut-list packet columns immediately after a `collect_kit_statuses` call added 0 filesystem checks and 0.036 ms (scenario 9).
- Root cause (verified by reading `services.py`): `_detect_named_packet_pdf` and `detect_preview_pdf` both resolve through the same `_shallow_descendant_files(search_root, max_depth=2, fs_cache=FILE_METADATA_CACHE)` cache key. Since `build_kit_status` already calls `detect_preview_pdf` for every kit during status collection, the packet detectors added by commit `076c649` ride on an already-warm cache entry by the time the table renders - they add Python-level filtering only, no additional directory scans.
- This resolves the concern raised during the Phase 5 planning pass: the post-Phase-4 packet-PDF caching addition is not a residual bottleneck. No action warranted.

**4. Cold-switch filesystem-check volume scales linearly with per-operation latency on slow storage.**
- Measured fact: with 50 ms artificially injected per raw filesystem operation, a single cold switch over 4 kits (48 raw operations) took 2409.75 ms to become visible, while the UI-thread dispatch itself stayed at 1.43 ms - confirming Phase 4's async/run-id design still keeps the UI thread unblocked even under simulated slow-network conditions.
- This reproduces, with a number attached, Phase 4's own "Remaining Risks" note that the fallback path exists because "there is no reliable production folder/data watcher yet," and confirms the 5s/2s TTL caches are the only thing standing between a slow real network drive and a multi-second cold switch.
- Category: network filesystem. Confidence: high (direct measurement), but the 50 ms/op figure is a synthetic stand-in, not a measured real mapped-drive latency - real-world severity depends on the actual W/L drive latency distribution, which was not and cannot be measured in this mocked pass.

**5. Manual-refresh executor/poll round-trip measured at ~13 ms is most likely a test-harness artifact, not a new app-level cost.**
- Measured fact: `_queue_status_refresh_for_truck` (which still benefits from a warm `FILE_METADATA_CACHE`, since it only bypasses the outer `kit_status` cache) measured 12-14 ms per run in this harness, versus 0.026 ms for an equivalent direct warm `collect_kit_statuses()` call.
- Hypothesis, not confirmed: this gap is consistent with Windows' default ~15.6 ms `time.sleep()` scheduler granularity interacting with this test harness's 1 ms polling loop, rather than a real executor dispatch cost - the production code polls via a 120 ms `QTimer` (coarser still), so this number does not reveal a new production-visible hot path. Flagged as a fact/hypothesis distinction per the Phase 5 reporting requirement, not as an actionable finding.

### Facts vs. Hypotheses (explicit separation)

**Facts (directly measured):** cold/warm/repeated-switch timings and fs-check counts; packet-PDF render adds 0 fs checks; missing-drive path returns quickly with no exceptions; slow-drive dispatch stays sub-2ms while completion scales with injected per-op latency; rapid A->B->C switching produces correct final state with stale futures counted and ignored; current timer intervals (120/1500/30000/5000 ms) and cache TTLs (5s/2s/30s) match Phase 4's documented values exactly.

**Hypotheses (plausible, not directly measured):** that the 120ms poll floor is the dominant perceived-latency contributor for real users on fast storage (follows from the measured work time being far below the fixed interval, but real user perception was not measured); that the 50ms/op synthetic slow-drive figure represents real W/L drive latency (it does not - no real mapped-drive measurement was taken, per the Phase 5 mocking requirement); that the ~13ms manual-refresh figure is scheduler-granularity noise rather than real overhead (plausible given the numbers, not isolated and confirmed).

### Diagnostics-Only Files Changed

None. All measurements were produced by a standalone script run outside the repository; zero source files were modified (`git status` clean at completion). No temporary instrumentation was added or needed to be removed.

### Test Results

- `python -m pytest -q` -> 138 passed, 28 subtests passed (unchanged from pre-profiling baseline at this commit).
- `git status --short` -> clean, both before and after profiling.

### Ranked Phase 6 Candidate Table

| Candidate | Supporting measurement | Expected impact | Confidence | Correctness risk | Scope | Required tests | Recommendation |
|---|---|---|---|---|---|---|---|
| Replace/augment 120ms `QTimer` completion polling with a Qt-native worker-finished signal for status/flow futures | Scenario 1 vs. documented 120ms interval: ~24x gap between real work (5.3ms) and worst-case detection latency on fast storage | Removes up to ~115ms of perceived latency on the common (fast-storage, cold-switch) path | Medium (interval measured directly; user-perceptible impact inferred, not user-tested) | Medium - touches the run-id staleness machinery Phase 4 built; must preserve stale-result guarantees | Medium - swap timer-poll for signal-based completion in `main_window.py`, keep `TruckSwitchRunContext` semantics identical | Re-run rapid A->B->C stale-result test; add a completion-latency regression test | **Pursue** - highest ratio of expected benefit to identified cost, but must be scoped narrowly to avoid re-touching Phase 4's cache/invalidation design |
| Reduce per-kit cold-switch filesystem-check count (currently 12/kit) for slow/network-drive cases | Scenario 8: 2.4s for 48 ops at 50ms/op simulated latency; scenario 1 confirms 12 checks/kit is unchanged from Phase 4 | Would reduce worst-case cold-switch time on genuinely slow drives proportionally to checks removed | Low-Medium - real W/L drive latency was not measured, so real-world benefit is unquantified | Medium-High - combining/removing existence checks risks silently misreporting kit state | Medium - would need to audit which of the 12 checks/kit are combinable (e.g. shared parent-existence checks) | Real-drive latency measurement first (out of scope for a mocked pass); new correctness tests for any combined check | **Defer** - real mapped-drive latency data is needed before this is worth the correctness risk; not currently backed by production evidence |
| Memoize per-kit `_kit_table_signature` tuple construction to avoid recomputing packet/preview match objects on every render | Scenario 3: 0.539ms for 4 kits, all in-memory | Sub-millisecond savings per render | High (directly measured) | Low | Small | Existing render/signature tests would cover it | **Reject** - cost is already far below any perceptible threshold; not material |
| Any change to packet-PDF detection caching | Scenario 9: 0 incremental filesystem checks measured | None - already free | High | N/A | N/A | N/A | **Reject** - confirmed not a bottleneck; no Phase 6 action needed |

### Final Recommendation

**Proceed with a narrowly-scoped Phase 6**, limited to the single highest-confidence candidate: replacing the 120ms completion-polling timers with Qt-native worker-completion signals for the status/flow futures, while preserving `TruckSwitchRunContext` run-id staleness semantics exactly as Phase 4 built them. This is the only candidate here with both a measured, material gap (not just a plausible one) and a bounded, well-understood implementation surface.

The per-kit filesystem-check-count reduction candidate should **not** be started as part of that Phase 6 without first obtaining a real mapped-drive latency measurement (a real-environment operator measurement, reported separately per the Phase 5 rules, not mixed with the mocked numbers above) - the correctness risk of combining/removing existence checks is not currently justified by production evidence, only by a synthetic 50ms/op simulation.

No other Phase 6 work is currently justified by the evidence gathered in this pass.