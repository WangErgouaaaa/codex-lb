# sticky-session-operations Delta

## ADDED Requirements

### Requirement: Verified full-history requests may shed failed turn-state ownership

A request whose client-supplied hard turn-state resolves to an account MUST remain owner-bound unless codex-lb locally proves that the request contains a self-contained, account-neutral full replay of the logical conversation. The proof MUST include an exact match of the locally stored completed input prefix, retained prior assistant or tool output after that prefix, and a fresh follow-up input. Once proven, the request no longer depends on the failed account's turn-state only after codex-lb removes that token and all other account-owned continuity headers from the replacement attempt.

#### Scenario: Verified full replay sheds a quota-exhausted turn-state owner

- **GIVEN** a hard turn-state maps a request to account A
- **AND** local continuity data proves the request contains the completed input prefix, retained prior output, and fresh follow-up input
- **AND** the full request is account-neutral
- **WHEN** account A returns a retryable quota or rate-limit failure before downstream-visible output
- **THEN** the proxy excludes account A for the current request
- **AND** clears account A's turn-state before selecting account B
- **AND** retries the same request within the existing deadline and attempt budget

#### Scenario: Partial history remains owner-bound

- **GIVEN** a hard turn-state maps a request to account A
- **AND** the request does not exactly match the stored completed input prefix or does not retain prior output
- **WHEN** account A is quota-exhausted or unavailable
- **THEN** the proxy fails closed instead of sending the partial request to another account

#### Scenario: Account-scoped input remains owner-bound

- **GIVEN** a hard turn-state request contains a nonblank `conversation`, explicit client `previous_response_id`, `input_file.file_id`, file-backed image, or another account-scoped field
- **WHEN** the owner account is quota-exhausted or unavailable
- **THEN** the proxy does not clear ownership or replay the request on another account
