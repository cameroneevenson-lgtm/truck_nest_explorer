# Phase 5: Profiling Hot Paths

Complete Phases 1–4 and their tests before starting this phase.

## Goal
Identify the actual performance bottlenecks in the current implementation (post-Phase 4) using measurements, not assumptions.

## Recommended Approach

1. **Instrument the hot paths**
   - Add lightweight timing around:
     - Truck selection / switching
     - Database queries during refresh and truck change
     - Network file existence/metadata checks
     - Cache lookup vs miss paths
     - Worker start/completion for async operations
   - Use `time.perf_counter()` or Qt’s `QElapsedTimer` for measurement.

2. **Run controlled scenarios**
   - Cold cache truck switch
   - Warm cache truck switch
   - Rapid A → B → C switching
   - Status refresh with and without cache
   - Folder watcher triggered refresh

3. **Capture data**
   - Query counts
   - Filesystem check counts
   - Wall-clock time for key operations
   - Cache hit/miss ratio
   - Time spent on UI thread vs worker

4. **Analyze**
   - Which operations dominate the time?
   - Is the bottleneck in the database, network filesystem, Python code, or Qt event loop?
   - Are there obvious low-hanging fruit (repeated queries, missing indexes, unnecessary work on UI thread, etc.)?

## Scope Boundary
Do not begin optimization work in this phase. This phase is for **measurement and diagnosis only**.

Do not:
- Start implementing fixes
- Change caching strategy
- Modify threading model
- Begin any refactoring

## Required Final Report
Report:
- Measured hot paths with timing data
- Query and filesystem check counts
- Cache effectiveness
- Whether the main bottleneck is DB, network, Python, or Qt
- Any obvious quick wins identified
- Recommendations for Phase 6 (optimization)

## Completion Gate
Phase 5 is complete when you have clear, measured data showing where time is actually being spent.

## Update this file
After running Phase 5, update this Markdown file with the profiling results and identified hot paths.