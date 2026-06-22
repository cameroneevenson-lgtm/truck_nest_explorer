# Phase 4: Performance, Caching, and Truck Switching Optimization

Complete Phases 1-3 before starting this phase.

## Goal
Improve overall snappiness, especially truck switching, by introducing caching, reducing polling, and making heavy operations async.

## Tasks

### 1. Introduce Caching Layer
Create a simple in-memory caching system (e.g. `cache.py` or add to an existing service).

Focus on:
- Kit status / current truck data
- Frequently accessed DB results
- File existence / metadata for mapped network drives
- Packet and preview data

Implement cache invalidation when DB writes occur (emit signals from services after writes).

### 2. Make Truck Switching Asynchronous
Refactor truck selection / switching to:
- Immediately show loading state
- Run heavy work (DB query, network file checks, cache population) in a background worker
- Update UI only when ready

### 3. Replace Constant Polling
- Remove or greatly reduce constant SQLite polling for screen drawing.
- Use event-driven updates (signals from services after DB changes).
- For external changes, use folder watching + cache invalidation instead of full polling.

### 4. Optimize Network Drive Checks
- Cache file existence and metadata aggressively.
- Debounce checks when switching trucks or refreshing.
- Only re-verify files when watcher signals a change or on a slow background timer.

### 5. Phase 4 Validation
- Test truck switching feels fast.
- Verify caching reduces DB and network hits.
- Ensure UI remains responsive during heavy operations.
- Measure overall perceived snappiness.

After this phase, the app should feel significantly more responsive, especially when switching trucks.