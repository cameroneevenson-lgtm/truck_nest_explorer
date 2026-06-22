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