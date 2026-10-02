## MODIFIED Requirements

### Requirement: Account-local Responses work is capped before upstream creation

For `/v1/responses`, `/backend-api/codex/responses`, and compact Responses traffic, the proxy MUST enforce account-local response-create and streaming concurrency limits in addition to process-wide admission limits, and the configured limits MUST be cluster-wide per-account targets enforced across all replicas rather than per-replica allowances. Because per-account caps are partitioned per replica via the bridge ring and cannot be safely partitioned across intra-pod worker processes, each instance MUST run a single worker process; horizontal scaling is achieved by adding replicas. The default account response-create cap MUST be 4 and the default account stream cap MUST be 8 unless operators configure a different value.

Operators MAY additionally configure per-plan overrides in the dashboard setting `proxy_account_plan_concurrency_caps_json`, a JSON object mapping normalized plan types to `{responseCreate, stream}` pairs. For each account, the effective caps MUST resolve by exact match on the normalized account plan type, falling back to the globally configured caps, then to the startup defaults; plan equivalence classes MUST NOT participate in the lookup. Unknown or malformed JSON content MUST degrade to the global caps rather than fail the request path. The stream recovery reserve MUST deduct from the stream cap in effect for that account's plan, and reattach-stage stream leases MUST keep bypassing the reserve.

Per-plan overrides MUST apply only when account caps are not partitioned across replicas (single-replica rings or replica-scope caps). Under partitioned multi-replica scope the system MUST keep enforcing the partitioned global caps and MUST NOT apply per-plan overrides.

When an account is at either effective cap, new soft-affinity work MUST prefer another eligible account before returning local overload. A bare process-session mapping MAY supply soft locality only while the request is self-contained, pre-visible, and has no required owner. Account-cap spillover MUST be decided during account selection and MUST NOT switch an account after a request enters shared transport, replay, or durable bridge ownership. Hard-continuity work MUST remain on its required owner and MAY fail closed when that owner is saturated; the final admission gate for hard-continuity work MUST enforce the owner account's plan-effective caps. Hard Codex ownership rows MUST bypass soft sticky fallback/reallocation so pressure cannot delete or rewrite them. Cap-exceeded failure messages MUST report the cap numbers in effect for the rejected account's plan when plan overrides are configured.

#### Scenario: Soft work avoids saturated account

- **GIVEN** account A is at its account response-create cap
- **AND** account B is eligible and below cap
- **WHEN** a self-contained `/v1/responses` request has only bare process-session affinity to account A
- **THEN** the proxy selects account B instead of queueing on account A

#### Scenario: Hard continuity owner saturation fails closed

- **GIVEN** a follow-up request requires a specific previous-response owner account
- **AND** that account is at its account stream or response-create cap
- **WHEN** no safe continuity-preserving alternative exists
- **THEN** the proxy returns a bounded local overload/continuity failure
- **AND** the failure reason is stable and low-cardinality

#### Scenario: Late WebSocket cap race does not retire shared work

- **GIVEN** a request has entered an upstream WebSocket shared with another in-flight response
- **WHEN** a later account response-create lease acquisition loses a capacity race
- **THEN** the proxy rejects only the newly unadmitted request with the existing local-cap failure
- **AND** it does not retire or switch the shared upstream WebSocket to spill that request

#### Scenario: Existing bridge ownership is not replaced by cap spillover

- **GIVEN** a session header resolves to a live or durable HTTP bridge owner
- **WHEN** that owner's account or response-create gate is saturated
- **THEN** the request follows the existing hard bridge-capacity behavior
- **AND** account-cap spillover does not publish a replacement bridge under the same canonical identity

#### Scenario: Per-plan cap separates tiers

- **GIVEN** plan overrides configure `plus` to responseCreate 4 / stream 6 while the global caps are 8/16
- **AND** a Plus account holds 5 in-flight stream leases
- **WHEN** another stream lease is requested for that account
- **THEN** the request is refused under the Plus cap (or spilled to another eligible account) instead of being admitted under the global stream cap
- **AND** a Pro account with no leases at 5 in-flight streams continues to be admitted under its own effective caps

#### Scenario: Unconfigured plan keeps global caps

- **GIVEN** plan overrides configure only `plus` and `pro`
- **WHEN** a team-plan account requests leases
- **THEN** the globally configured caps apply to that account
