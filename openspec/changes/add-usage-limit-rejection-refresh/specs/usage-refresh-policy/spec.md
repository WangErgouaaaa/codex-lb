## ADDED Requirements

### Requirement: Usage-limit rejections trigger a merged usage refresh and extend the cooldown to the real window deadline

When an account is marked `RATE_LIMITED` after a rejection whose error code is
`usage_limit_reached`, `insufficient_quota`, `usage_not_included`, or
`quota_exceeded`, the system MUST schedule a forced usage refresh for that
account as a fire-and-forget task that never propagates a failure to the
request path. Rejections for the same account MUST be merged: at most one
refresh task per account per 15-second monotonic debounce window, and the
debounce marker MUST be recorded before the task is created. The code
`rate_limit_exceeded` MUST NOT trigger the refresh.

The scheduled refresh MUST skip accounts that are not persisted as
`RATE_LIMITED`, and MUST skip extension when the forced fetch does not
succeed. When the fetch succeeds, the system MUST read the account's latest
usage windows, keep windows whose used percent is at least `99.5` and whose
`reset_at` passes the shared rate-limit plausibility bounds, and take the
latest such deadline. If no exhausted window qualifies, the existing fallback
cooldown MUST be preserved. If the deadline does not exceed the currently
persisted `reset_at`, the system MUST NOT write.

When it does exceed it, the system MUST extend `accounts.reset_at` to that
deadline with a compare-and-set write conditioned on status `RATE_LIMITED` and
the observed `reset_at`, and MUST invalidate the account selection cache only
when the compare-and-set succeeds. The persisted deadline MUST never be
shortened by this path.

#### Scenario: Metadata-free usage-limit rejection extends the cooldown to the real window

- **WHEN** an account is rejected with HTTP 429 `usage_limit_reached` and no `resets_at` metadata
- **AND** the rejection marks the account `RATE_LIMITED` with the 30-second fallback deadline
- **AND** the forced usage refresh reports a window at 100% used with `reset_at` about five hours out
- **THEN** the persisted `accounts.reset_at` is extended to the reported window deadline
- **AND** the account selection cache is invalidated

#### Scenario: Repeated rejections within the debounce window fetch once

- **WHEN** the same account receives two usage-limit rejections less than 15 seconds apart
- **THEN** only one forced usage refresh is scheduled for that account

#### Scenario: Burst rate-limit rejections do not refresh

- **WHEN** an account is rejected with `rate_limit_exceeded`
- **THEN** no forced usage refresh is scheduled

#### Scenario: No exhausted window preserves the fallback cooldown

- **WHEN** the forced refresh succeeds but every reported window is below 99.5% used
- **THEN** the persisted `reset_at` is left unchanged
- **AND** the existing 30-second fallback cooldown keeps governing selection

#### Scenario: Fetch failure leaves state untouched

- **WHEN** the forced usage fetch fails
- **THEN** no persisted account state changes
- **AND** the next usage-limit rejection may schedule a new refresh

#### Scenario: Extension never shortens a longer deadline

- **WHEN** the persisted `reset_at` is already later than the latest exhausted-window deadline
- **THEN** no compare-and-set write is issued
