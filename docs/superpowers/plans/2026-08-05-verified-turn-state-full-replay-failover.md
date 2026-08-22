# Verified Turn-State Full-Replay Failover Implementation Plan

> **For Codex:** Execute this plan task-by-task with test-driven development. The governing behavior contract is `openspec/changes/fail-over-verified-turn-state-full-replay/`.

**Goal:** Keep one Codex request running when its hard turn-state owner reaches quota by transparently replaying a locally verified, account-neutral full history on another account.

**Architecture:** Reuse the HTTP bridge's retained unanchored full body, completed-prefix fingerprint, and replay-safety primitives. Convert hard ownership to movable routing only for the current pre-visible attempt, clear account-owned turn-state, preserve the failed-account exclusion, and let the existing reconnect/settlement machinery select and account for the replacement. Mirror the proof in the plain stream fallback.

**Tech Stack:** Python 3.13, FastAPI, asyncio/AnyIO, Pydantic, pytest, Ruff, ty, OpenSpec.

---

### Task 1: Lock the HTTP bridge regression

**Files:**
- Modify: `tests/unit/test_proxy_utils.py`

1. Add a test session with a hard Codex turn-state owner, stored completed-prefix fingerprint, retained full-history replay body, and a pre-visible retry state.
2. Make the reconnect stub expose whether the failed owner was excluded and whether the old turn-state was removed.
3. Expect another account to be selected and the full unanchored body to be sent.
4. Run only the new test and confirm it fails because `require_same_account=True` and the owner is not excluded.

### Task 2: Implement the bridge eligibility gate

**Files:**
- Modify: `app/modules/proxy/_service/http_bridge/request_submit.py`
- Test: `tests/unit/test_proxy_utils.py`

1. Parse the retained fresh request body and reject malformed or non-`response.create` payloads.
2. Require exact stored-prefix continuity, retained prior output, fresh follow-up input, and the existing account-neutral payload validator.
3. Use the gate only when the request has not produced visible/model output and the existing replay limit permits retry.
4. For an eligible hard request, clear the preferred owner, exclude the failed account, clear upstream/downstream turn-state and header, and reconnect without the same-account requirement.
5. Run the new test and the existing hard-owner/file/visible-output tests.

### Task 3: Lock and implement the plain stream fallback

**Files:**
- Modify: `tests/unit/test_proxy_utils.py`
- Modify: `app/modules/proxy/_service/streaming/retry.py`

1. Add a hard turn-state test with a local continuity prefix, retained prior output, fresh follow-up input, no explicit `previous_response_id`, and a pre-visible quota response.
2. Confirm the test fails by returning the existing owner-unavailable response.
3. Extend `_verified_cross_transport_fresh_replay` to recognize this exact proof while preserving the explicit previous-response path.
4. When the owner is moved, replace the outbound headers with a copy that omits `x-codex-turn-state`.
5. Run the new test and existing previous-response/file/visible-output coverage.

### Task 4: Negative safety coverage

**Files:**
- Modify: `tests/unit/test_proxy_utils.py`

1. Prove a prefix mismatch or missing retained prior output remains same-owner/fail-closed.
2. Prove account-neutral validation rejects nonblank `conversation` and file-backed input.
3. Prove any downstream-visible output prevents replay.

### Task 5: Static and contract verification

**Files:**
- Modify: `openspec/changes/fail-over-verified-turn-state-full-replay/tasks.md`

1. Run focused tests for all new cases.
2. Run related HTTP bridge, streaming retry, replay safety, and upstream path tests.
3. Run Ruff check/format verification, ty, and the proxy architecture check.
4. Run strict validation for `fail-over-verified-turn-state-full-replay` and repo-wide OpenSpec spec validation.
5. Mark completed OpenSpec tasks only after fresh evidence passes.

### Task 6: Isolated 2456 verification

**Files:**
- Reuse the repository's canonical/local launch scripts and temporary test state; do not modify production data.

1. Start codex-lb on `127.0.0.1:2456` with an isolated database and encryption key.
2. Seed controlled accounts/stubs or use the repository's failure-injection harness so the hard owner returns a pre-visible quota error and a replacement completes.
3. Send a two-turn Codex-shaped request whose second turn includes the registered turn-state and full retained history.
4. Verify the second request completes without client resend, logs show the failed account excluded and replacement selected, and the replacement receives no old turn-state.
5. Run negative 2456 probes for partial history and account-scoped input.

### Task 7: Production deployment after 2456 passes

**Files:**
- No production database modifications.

1. Run the canonical restart script for `repo-upstream-migration` only after Task 6 passes.
2. Verify port 2455 on `0.0.0.0`, port 15956 on `127.0.0.1`, `/health/live`, the non-empty production account list, the 2455 process command line, and newest logs.
3. Send a non-destructive production smoke request and confirm the current Codex window remains operational.
4. Report the exact verification evidence and any remaining real-quota test limitation.
