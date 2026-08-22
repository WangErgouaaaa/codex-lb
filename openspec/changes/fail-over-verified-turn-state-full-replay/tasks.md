## 1. Contract

- [x] 1.1 Record the locally verified full-history replay proof and fail-closed boundaries.
- [x] 1.2 Record turn-state clearing, failed-account exclusion, lease release, and bounded retry requirements.

## 2. Regression Coverage

- [x] 2.1 Add a failing HTTP bridge test for a hard turn-state owner returning pre-visible quota while a verified full replay is retained.
- [x] 2.2 Assert the replacement account receives no old `x-codex-turn-state`, and the failed account is excluded with leases released.
- [x] 2.3 Add or extend negative coverage for partial history, file/conversation ownership, and downstream-visible output.
- [x] 2.4 Add plain streaming coverage for the same verified turn-state replay rule.

## 3. Implementation

- [x] 3.1 Add a narrow account-neutral full-replay eligibility helper using existing replay-safety primitives.
- [x] 3.2 Allow eligible hard bridge requests to reconnect on another account and clear retired turn-state.
- [x] 3.3 Extend plain stream verified replay to hard turn-state requests without explicit `previous_response_id`.

## 4. Verification And Deployment

- [x] 4.1 Run focused and related unit tests, ruff, type checks, architecture checks, and OpenSpec validation.
- [x] 4.2 Start an isolated instance on port 2456 using a non-production database and verify same-request hard-owner quota failover plus safety controls.
- [x] 4.3 After 2456 passes, restart production port 2455 through the canonical launcher and verify listeners, health, accounts, command line, and logs.
