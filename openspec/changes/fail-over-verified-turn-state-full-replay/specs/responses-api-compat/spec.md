# responses-api-compat Delta

## ADDED Requirements

### Requirement: Pre-visible verified full-replay failover continues the same response

When a `/responses` request is eligible to shed a failed hard turn-state owner, the HTTP bridge and plain streaming transports MUST continue the original downstream request on another eligible account without requiring the client to resend it. The failed account MUST remain excluded, its request leases MUST be released before replacement selection, and the replacement upstream request MUST NOT carry the failed account's `x-codex-turn-state`. The operation MUST remain bounded by the existing request deadline, retry count, and usage-settlement rules.

#### Scenario: HTTP bridge quota failure transparently selects a replacement

- **GIVEN** a hard Codex bridge request has a locally verified account-neutral full-history replay body
- **AND** the owner returns `usage_limit_reached` before visible output
- **WHEN** another account is eligible
- **THEN** the bridge releases the failed attempt's leases and reconnects on the replacement account
- **AND** sends the retained unanchored full-history body without the old turn-state
- **AND** the client receives the replacement account's completion as the result of the original request

#### Scenario: Plain stream quota failure transparently selects a replacement

- **GIVEN** the HTTP bridge is unavailable or disabled
- **AND** the plain stream path locally verifies the same account-neutral full-history proof
- **WHEN** the hard turn-state owner fails with a pre-visible quota error
- **THEN** the plain stream excludes the owner, removes `x-codex-turn-state`, and retries on another eligible account

#### Scenario: Visible output blocks replay

- **GIVEN** any part of model output has become visible to the downstream client
- **WHEN** the upstream account later fails or reaches quota
- **THEN** neither transport replays the request on another account
