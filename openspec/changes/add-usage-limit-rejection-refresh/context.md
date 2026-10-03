# Usage-Limit Rejection Refresh

## Purpose and scope

This change closes the gap between a metadata-free usage-limit 429 and the real exhausted-window reset deadline that the usage endpoint already knows. It covers only the window-exhaustion rejection codes; burst-style throttles keep the existing short fallback.

## Incident shape

Production recorded 2211 `usage_limit_reached` rejections, none carrying `resets_at`. Every such rejection persisted the 30-second backoff floor (`RATE_LIMITED_MIN_COOLDOWN_SECONDS`) as the account cooldown. The account then re-entered selection, hit the identical rejection, and re-marked — a return-to-rotation loop that lasted until the upstream window ended on its own. Meanwhile the background usage refresh (60-second cadence) fetched the true `reset_at` into `usage_history`, but nothing propagated it back into the routing cooldown.

## Decision rationale

- The forced refresh reuses the auto-redeem precedent (`_refresh_usage_after_auto_redeem`): background session, three repositories, `UsageUpdater.force_refresh_result(..., ignore_refresh_disabled=True)`. It inherits the in-process singleflight, so overlapping triggers and the periodic refresh merge into one upstream fetch.
- The refresh is wired at `_handle_stream_error`, the single convergence point for SSE and native WebSocket rate-limit penalties, so every path that benches an account with a usage-limit 429 benefits.
- Only extend, never shorten: a deadline a peer replica already relies on must not be pulled back by a stale or raced snapshot. The CAS (`update_status_if_current` with `expected_status=RATE_LIMITED`, `expected_reset_at=<observed>`) makes the extension atomic against concurrent markings.
- A 15-second per-account monotonic debounce turns rejection storms into at most one fetch per window per account per 15 seconds while the "record not-before before creating the task" ordering closes the task-startup race.
- Window-exhaustion evidence is bounded: used percent >= 99.5 and `plausible_rate_limit_reset_at` (future, within the 366-day horizon). No exhausted window means the 429 may have been a burst or a race, and the 30-second fallback stays.
- `rate_limit_exceeded` is deliberately not in the trigger set: it signals "too fast", not "window empty", for which the short cooldown is already correct.

## Constraints and failure modes

- The entry point is synchronous and never raises; the fire-and-forget task logs warnings and absorbs every failure so the request path is unaffected.
- Non-`RATE_LIMITED` accounts are skipped: the refresh exists to serve a bench that `mark_rate_limit` just applied.
- A lost CAS race logs debug and waits for the next rejection; the selection cache is invalidated only when the extension actually landed.
- The debounce map is pruned once it exceeds 512 entries so unbounded account churn cannot leak memory.
- The 30-second initial marking behavior (`handle_rate_limit`) is intentionally unchanged: this feature is a post-hoc extension, not a replacement.

## Operational notes

No migration or configuration is required. After deployment, the first usage-limit rejection per account triggers one extra usage fetch within the request's process; the persisted cooldown then matches the real window end, and selection self-heals at that deadline through the existing `accounts.reset_at` consumption path.
