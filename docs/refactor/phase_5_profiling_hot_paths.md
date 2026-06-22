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