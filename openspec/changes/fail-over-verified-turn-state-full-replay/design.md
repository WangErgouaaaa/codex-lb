# Design: Verified Turn-State Full-Replay Failover

## Existing behavior

The HTTP responses bridge records the completed input count and prefix fingerprint for a logical Codex session. On a later request, it can prove that the client resent the stored prefix, retained the prior assistant/tool output, and appended fresh user input. It may then inject `previous_response_id`, trim the stored prefix for the first upstream attempt, and retain the original unanchored request body as a retry-safe full replay.

The pre-created retry path currently derives `hard_owner_bound` from the bridge key and always reconnects that key on the same account. That final rule ignores the stronger replay proof and causes a pre-visible quota failure to surface immediately as `limit hit`.

## Eligibility proof

A hard Codex session may become movable for the current request only when all of the following are true:

1. No downstream-visible output or upstream model output has been observed, and the existing one-replay limit has not been consumed.
2. The request carries a proxy-retained unanchored body that was produced from an exact local prefix match against the session's completed input fingerprint.
3. The full body is account-neutral: it has no nonblank `conversation`, explicit `previous_response_id`, file IDs, unknown account-scoped controls, unresolved tool outputs, or other stored upstream state.
4. The suffix after the stored prefix contains the prior completed assistant/tool output and a fresh follow-up input, so replay preserves the conversation rather than silently dropping it.
5. The failure is already classified by the existing pre-visible retry path as account-local quota, rate limit, model rejection, or another eligible retry condition.

Failure of any proof keeps the existing same-owner/fail-closed behavior.

## Cutover sequence

For an eligible request, the retry path:

1. prepares the retained unanchored request body;
2. clears the request's preferred owner and excludes the failed account;
3. clears the bridge's upstream/downstream turn-state and removes `x-codex-turn-state` from reconnect headers;
4. releases the failed request's response-create/stream leases through the existing replay cleanup;
5. selects and connects another eligible account using the existing deadline and attempt budget;
6. sends the original full-history request body and continues the same downstream response.

The plain streaming path uses the same account-neutral, prefix-match, prior-output, and fresh-follow-up proof before it removes hard owner affinity. Its replacement upstream call also receives headers without `x-codex-turn-state`.

## Safety boundaries

- A user-provided `previous_response_id` remains owner-bound. Only a proxy-injected anchor backed by the retained full body can be removed.
- File-backed and nonblank-conversation requests never qualify.
- The failed account stays excluded for the remainder of the request.
- Existing lease release, usage settlement, deadline, and maximum-attempt behavior remains authoritative.
- No event already made visible to the downstream client is replayed.

## Verification

- Unit tests prove hard turn-state quota failover, header clearing, owner exclusion, and lease release.
- Negative tests prove partial history and account-scoped inputs remain owner-bound.
- The related HTTP bridge and plain stream suites, lint, type checking, and OpenSpec validation pass.
- An isolated instance on port 2456 reproduces the hard-owner failure and proves transparent same-request failover before production port 2455 is restarted.
