## Why

Production logs show 2211 `usage_limit_reached` rejections and not one of them carried upstream `resets_at` metadata. Without metadata, the rate-limit handler persists only the 30-second backoff floor, so an exhausted account leaves the cooldown, re-enters the rotation, hits the same rejection again, and the loop repeats until the real window ends upstream. The true reset deadline is available from the usage endpoint, which the periodic refresh eventually fetches, but nothing ties it back to the just-rejected account quickly.

## What Changes

- After a usage-limit style 429 marks an account `RATE_LIMITED`, fire-and-forget a single debounced forced usage fetch for that account (per-account 15-second debounce on a monotonic clock).
- Read the fetched usage snapshot's exhausted windows (used percent at or above 99.5 with a valid reset deadline) and extend `accounts.reset_at` to the latest one, extend-only, via the existing compare-and-set status write.
- Invalidate the account selection cache after a successful extension so selection cools down to the real window end immediately.
- Leave the 30-second fallback, `rate_limit_exceeded` burst handling, and every other rate-limit path untouched.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `usage-refresh-policy`: usage-limit rejections now trigger a merged, debounced forced usage refresh whose exhausted-window deadlines extend the persisted rate-limit cooldown.

## Impact

- Affected code: one new module (`app/modules/usage/rejection_refresh.py`) and a single wiring line in the streaming failure handler.
- Affected data: `accounts.reset_at` rows may be extended (never shortened) after a usage-limit rejection; no schema migration.
- APIs and configuration: no API, environment variable, or dashboard changes.
