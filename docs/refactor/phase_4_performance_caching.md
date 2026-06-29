# Phase 4: Measured Performance, Caching, and Asynchronous Truck Switching

Complete Phases 1-3 and their tests before starting this phase.

## Recommended Codex Workflow

Phase 4 is not a single implementation task.

Use this sequence:

1. **Plan and measure**
2. Review the proposed design and measurements
3. Implement **Phase 4A**
4. Test and review Phase 4A
5. Implement **Phase 4B**
6. Run final correctness and performance validation

Do not implement during the initial planning pass.

## Goal
Improve truck-switch responsiveness and reduce unnecessary database and network-drive work without weakening correctness, filesystem safety, invalidation behavior, or Qt thread-affinity guarantees.

Performance improvements must be supported by measurements. Do not use subjective statements such as "feels faster" as the only validation.

## Important Safety and Architecture Rules
- Do not cache mutable results without an explicit invalidation strategy.
- Do not update Qt widgets from worker threads.
- Do not allow an older truck-switch result to overwrite a newer selection.
- Do not reintroduce the removed Inventor output-watcher workflow.
- Do not cache successful file existence indefinitely.
- Do not cache missing files indefinitely.
- Do not delete project or generated data as part of cache invalidation.
- Cache invalidation must be safe to perform more than once.
- The application must remain correct when mapped drives are slow, disconnected, or become available again.
- Tests must use fakes or mocks and must not require real mapped drives, Inventor, RADAN, COM, or production databases.

## Planning and Baseline Pass

Inspect the post-Phase-3 code and document the current behavior before proposing changes.

Record:

1. Every recurring SQLite polling site and its frequency.
2. Every database query triggered by:
   - Initial load
   - Manual refresh
   - Truck selection
   - Truck switching
   - Status updates
3. Every mapped-drive or network filesystem check triggered during truck switching.
4. Synchronous work currently performed on the Qt UI thread.
5. Existing service signals or write paths that can drive invalidation.
6. Existing folder watchers and what they monitor.
7. Current truck-switch latency for representative mocked scenarios:
   - Warm local data
   - Cold cache
   - Slow mapped drive
   - Missing mapped drive
   - Rapid A -> B -> C switching
8. Current query and filesystem-check counts for those scenarios.

The planning response must propose:
- Cache owners
- Cache key structure
- Cached value types
- Positive-result expiration
- Negative-result expiration
- Maximum cache size or boundedness strategy
- Invalidation events
- Thread-safety rules
- Qt signal boundaries
- Stale-result protection
- Instrumentation and test strategy
- Exact files to change

Do not modify files during this planning pass.

## Phase 4A: Instrumentation, Cache Layer, and Invalidation

### 1. Add Lightweight Instrumentation

Add testable instrumentation for:
- Database query counts
- Filesystem metadata/existence-check counts
- Truck-switch start and completion
- Cache hits, misses, and invalidations
- Stale asynchronous results ignored

Instrumentation must be optional or low-overhead in normal use.

### 2. Introduce a Focused In-Memory Cache

Create a small cache module or focused service-owned caches.

Cache only values demonstrated by the baseline to be worth caching, such as:
- Kit status and current-truck data
- Frequently repeated read-only database query results
- File existence and metadata for mapped-drive paths
- Packet and preview metadata

Define explicit typed cache keys where practical.

Each cache must define:
- Owner
- Key
- Value
- Expiration policy
- Negative-result expiration
- Invalidation events
- Maximum size or cleanup behavior
- Thread-affinity or synchronization rules

Avoid a single unstructured global dictionary.

### 3. Add Explicit Invalidation

Invalidate relevant entries after successful writes.

Prefer service-level signals or explicit invalidation calls at committed write boundaries.

Cover:
- Database updates
- File creation
- File replacement
- File deletion through explicit operator actions
- Truck data refresh
- Folder-watcher notifications

Invalidation must occur only after the underlying operation succeeds.

### 4. Phase 4A Tests

Add deterministic tests proving:
- Repeated reads use the cache
- Expired entries are refreshed
- Negative results expire
- Successful writes invalidate relevant entries
- Failed writes do not invalidate valid entries unnecessarily
- Invalidation is idempotent
- Unrelated writes do not flush unrelated cache entries
- Cache size remains bounded
- Mapped-drive recovery is detected after negative-cache expiration
- Instrumentation reports expected hit, miss, query, and filesystem counts

Do not begin Phase 4B until Phase 4A tests pass.

## Phase 4B: Asynchronous Truck Switching and Polling Reduction

### 1. Add a Truck-Switch Run Context

Use a monotonically increasing request or run identifier.

The asynchronous workflow must track at least:
- Requested truck identifier
- Run identifier
- Loading state
- Worker reference
- Completion state
- Error state

Only the latest active request may update the UI.

Results from older requests must be ignored safely.

### 2. Move Heavy Truck-Switch Work Off the UI Thread

The UI should immediately:
- Record the new selection
- Enter a visible loading state
- Disable only controls that cannot safely operate during the transition

A background worker may perform:
- Database reads
- Network file checks
- Cache population
- Packet or preview metadata loading

The UI thread must perform:
- Widget updates
- Dialog creation
- Model replacement
- Final state transition

### 3. Handle Rapid Switching

Add deterministic behavior for rapid A -> B -> C switching.

Required behavior:
- C becomes the visible final state
- Results from A and B cannot overwrite C
- Stale errors do not show dialogs for inactive selections
- Cleanup is safe if stale workers finish after the current worker
- The UI does not remain permanently disabled

### 4. Reduce Constant Polling

Replace frequent full SQLite polling with event-driven refreshes where writes are controlled by the application.

For external changes:
- Use focused folder or data-change watching where reliable
- Debounce notifications
- Invalidate affected cache entries
- Use a slow fallback refresh only when necessary

Document any polling that remains and justify its interval.

### 5. Optimize Network Checks

- Reuse cached metadata when valid
- Debounce repeated checks for the same truck
- Avoid duplicate checks within one switch operation
- Revalidate after watcher events
- Use finite negative-cache durations
- Handle unavailable mapped drives without blocking the UI thread
- Avoid scanning unrelated folders

**One-way release signals (W: drive and similar L: drive cases)**: Jobs/releases marked released on W: are never unreleased. This is a natural final/irreversible state that allows aggressive or final caching of related data (long or permanent positive TTL after release detection) with far less repeated W: probing. The same principle often applies to L: drive checks. This is a direct, high-value application of the existing explicit expiration + invalidation rules; treat release markers as strong promotion-to-final or invalidation events. Only add dedicated logic if measurements show clear extra benefit beyond the general cache design.

### 6. Phase 4B Tests

Add tests proving:
- Truck switching does not perform heavy work on the UI thread
- Loading state appears before background completion
- Only the newest request updates the UI
- Stale success and stale failure results are ignored
- Controls restore after success, failure, and cancellation
- Rapid A -> B -> C switching ends on C
- Cached switching reduces query and filesystem-check counts
- Relevant watcher events invalidate cached data
- Remaining polling is debounced or uses the documented slow interval
- Missing or slow mapped drives do not freeze the UI

## Final Measurement

Repeat the baseline scenarios and report:
- Before and after truck-switch latency
- Before and after database query counts
- Before and after network filesystem-check counts
- Cache hit and miss counts
- Stale-result count during rapid switching
- UI-thread blocking observations
- Cold-cache and warm-cache results

Do not claim a performance improvement unless measurements support it.

## Required Final Report

Report:
- Baseline measurements
- Phase 4A measurements and test results
- Phase 4B measurements and test results
- Files added, changed, and removed
- Cache owners, keys, expiration, and invalidation rules
- Remaining polling and why it remains
- Stale-result protection design
- Full test-suite results
- Remaining performance or correctness risks

## Completion Gate

Phase 4 is complete only when:
- Phase 4A and Phase 4B tests pass
- Relevant full test suites pass
- Rapid switching cannot display stale results
- Cache invalidation tests pass
- Missing mapped drives do not block the UI thread
- Before-and-after measurements are recorded
- No removed Inventor watcher workflow has been reintroduced
- Update this Markdown file with the results above after running the phase.

Last updated: 2026-06-29