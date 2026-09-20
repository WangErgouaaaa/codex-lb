## Why

Outbound requests to the ChatGPT upstream currently carry mixed client
fingerprints. The three main upstream egress builders exempt native Codex
clients from fingerprint normalization, so real Codex traffic (codex-tui,
codex_exec, Codex Desktop) reaches upstream with its original per-device
User-Agent, and the account id is emitted under two different header casings
depending on client nativeness. Two secondary egresses (`/transcribe` and
`/files`) never normalize at all: they forward the inbound User-Agent verbatim
and leak the `x-openai-client-*` SDK fingerprint family through their
`x-openai-` prefix-forwarding rule. Multiple rotating fingerprints on one
account pool is a shared-account risk signal upstream, and it defeats the
existing normalization for exactly the traffic that dominates production.

Separately, the Codex version cache can idle forever: `get_version()` is only
called from the leader-gated model-registry refresh, so a follower replica (or
a leader with no ACTIVE accounts) never warms it, and every normalized request
pins the stale static fallback version. `gpt-6-astra` upstream rejects
requests reporting `0.144.0` with "requires a newer version of Codex", so the
fallback default is also bumped to the current upstream release.

## Fork-patch note

This is a behavior change made in the fork (`WangErgouaaaa/codex-lb`) relative
to upstream `Soju06/codex-lb`. Upstream intentionally passes native Codex
client traffic through unchanged (`test_native_codex_http_request_is_left_unchanged`
family). This fork deliberately diverges: all clients present one shared
codex_cli_rs persona. When syncing from upstream, keep this change as an
owned fork patch and re-apply it over upstream's native-passthrough tests.

## What Changes

- All three upstream header builders (`_build_upstream_headers` HTTP,
  `_build_upstream_websocket_headers` in proxy.py and proxy_websocket.py)
  normalize the outbound fingerprint unconditionally; the native exemption is
  removed from the builders. `_is_native_codex_request()` itself is unchanged
  and still owns transport decisions (websocket vs HTTP, heartbeat shape).
- The upstream account header is emitted lowercase (`chatgpt-account-id`) for
  every request; the PascalCase `_CHATGPT_ACCOUNT_ID_HEADER` constant is
  removed as dead code.
- `_build_upstream_transcribe_headers` and the `/files` header builder adopt
  the same shared codex_cli_rs User-Agent persona (version from the Codex
  version cache) and strip the `_SDK_FINGERPRINT_HEADER_KEYS` family plus
  `x-openai-internal-codex-responses-lite` and `x-codex-version`. They keep
  their minimal header set (no added `originator`/`version`/`Accept`) to
  avoid upstream WAF rejection.
- `model_registry_client_version` fallback bumps from `0.144.0` to `0.155.1`.
- `ModelRefreshScheduler._run_loop()` warms the Codex version cache each
  cycle regardless of leader election, covering followers and account-less
  leaders.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `deployment-installation`: the degraded-startup catalog floor default
  (client version fallback) moves from the GPT-5.6-era `0.144.0` to the
  current upstream release `0.155.1`.

## Impact

- `app/core/clients/proxy.py`, `app/core/clients/proxy_websocket.py`,
  `app/core/clients/files.py` (outbound header construction)
- `app/core/config/settings.py` (fallback version default) and the generated
  `docs/reference/settings.md`
- `app/core/openai/model_refresh_scheduler.py` (periodic version warm)
- 18 locked unit tests across fingerprint/websocket/files/compact coverage
