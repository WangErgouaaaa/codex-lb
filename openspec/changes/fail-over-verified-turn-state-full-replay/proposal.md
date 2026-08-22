# Fail Over Verified Turn-State Full Replays

## Summary

Allow one in-flight Codex request to continue on another eligible account when its hard turn-state owner reaches quota, but only when codex-lb can prove that the client supplied a self-contained, account-neutral full conversation replay.

## Why

The HTTP responses bridge already detects and trims verified full-history requests. When the selected account returns a pre-visible `usage_limit_reached` or equivalent rate-limit failure, the replay path still treats the bridge's hard session key as an unconditional same-account constraint. The client therefore receives `limit hit` and stops until the user manually resends the request, even though the same retained request can safely run on another account.

## What Changes

- Treat a locally verified, account-neutral full-history request as independent of the failed account's turn-state after a pre-visible quota or capacity failure.
- Exclude the failed account, release its local leases, clear account-owned turn-state headers, and retry the same request on another eligible account within the existing retry budget.
- Keep partial-history, explicit `previous_response_id`, nonblank `conversation`, file-backed, account-scoped, or downstream-visible requests fail-closed.
- Apply the same proof rule to the plain streaming fallback path so disabling the HTTP bridge does not restore the defect.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `sticky-session-operations`: a verified full replay no longer depends on the failed account's hard turn-state once that token is removed.
- `responses-api-compat`: pre-visible quota failover may transparently continue the same request on another account when replay safety is locally proven.

## Non-Goals

- No global downgrade of hard turn-state affinity.
- No replay after any downstream-visible model output.
- No cross-account replay of uploaded files, server-stored conversation state, or unverified/partial history.
- No retry-budget expansion and no change to account eligibility or quota classification.
