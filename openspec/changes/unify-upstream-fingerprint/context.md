# Context: unify upstream fingerprint (fork patch)

## Purpose and scope

All outbound traffic to the ChatGPT upstream must present one Codex CLI
persona: one `codex_cli_rs` User-Agent (fixed OS/arch/terminal, one live
version) and no SDK fingerprint headers. This removes the "many devices, many
builds on one account" risk signal that plagues a shared account pool, and it
keeps every client eligible for models that gate on `minimal_client_version`.

## Fork-patch constraint

This change is a deliberate fork behavior change against upstream
`Soju06/codex-lb`, which passes native Codex clients through unchanged.
**When syncing from upstream, keep this change as an owned fork patch**:
re-apply the unconditional normalization and the lowercase account header over
upstream's native-passthrough tests, and keep upstream's
`_is_native_codex_request()` intact because it still governs transport
decisions (websocket vs HTTP bridge, heartbeat classification in
`app/modules/proxy/api.py`).

## Decisions

- Normalization runs unconditionally in the three upstream builders; the
  `native` local variable disappeared from them. Transport-only users of
  `_is_native_codex_request()` are untouched.
- Account id is emitted lowercase (`chatgpt-account-id`) everywhere: the
  split casing existed only to mirror the (now removed) native exemption, and
  the upstream log helper already reads it case-insensitively.
- The secondary egresses (`/transcribe`, `/files`) get the same User-Agent
  persona but NOT the full header set: they stay minimal (no `originator`,
  `version`, `Accept`) because bulk headers trigger upstream WAF rejection on
  those endpoints. "One persona" means same UA + no SDK fingerprint headers,
  not a byte-identical header set.
- `x-stainless-*` never reaches these two egresses (their forwarding rule is
  `x-openai-`/`x-codex-` prefixes only), so no stainless handling there.
- The version cache warm lives in `ModelRefreshScheduler._run_loop()` (one
  awaited call at the top of each cycle): the loop is not leader-gated, so
  followers and account-less leaders keep the cache fresh. No new task, no
  new setting, no shared DB storage.
- `cached_version_or_default()` still does not check TTL by design (avoids
  mid-flight version flips on the hot path); only the refresh cadence changed.

## Failure modes and examples

- Warm fetch failure: `get_version()` falls back (stale cache, then the
  static default `0.155.1`) without writing the cache; the next 300 s cycle
  retries. The fallback warning in `codex_version.py` covers the cold case.
- Extreme case: a model whose `minimal_client_version` exceeds the latest
  upstream release would be rejected for everyone; that is upstream gating,
  fixed only by tracking the next release.
- Example: OpenCode (`opencode/0.0.0-dev`) and codex-tui 0.144.1 now both exit
  as `codex_cli_rs/0.155.1 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10` with
  `originator: codex_cli_rs`, `version: 0.155.1`, and
  `chatgpt-account-id` lowercase.
