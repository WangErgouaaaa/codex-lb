## 1. Version fallback robustness

- [x] 1.1 Bump `model_registry_client_version` default to `0.155.1` and update the settings comment (keep the GPT-5.6 `0.144.0` bootstrap-catalog floor fact).
- [x] 1.2 Regenerate `docs/reference/settings.md` so the rendered default matches the new value.
- [x] 1.3 Update the deployment-installation context narrative to the new fallback 口径 (fingerprint OS/arch/terminal unchanged).
- [x] 1.4 Convert the seven hardcoded `0.144.0` assertions in `tests/unit/test_codex_version.py` to dynamic reads of `get_settings().model_registry_client_version`.

## 2. Version cache periodic warm

- [x] 2.1 Await `get_codex_version_cache().get_version()` at the top of `ModelRefreshScheduler._run_loop()` (no new task, no new setting).
- [x] 2.2 Add a scheduler unit test proving `get_version()` is awaited when leader election returns `None` (follower).

## 3. Unconditional fingerprint normalization

- [x] 3.1 Remove the native exemption from `_build_upstream_headers` (proxy.py) and emit the account header lowercase.
- [x] 3.2 Remove the native exemption from `_build_upstream_websocket_headers` (proxy.py) and emit the account header lowercase.
- [x] 3.3 Remove the native exemption from `_build_upstream_websocket_headers` (proxy_websocket.py) and emit the account header lowercase.
- [x] 3.4 Delete the dead `_CHATGPT_ACCOUNT_ID_HEADER` constant and its imports.

## 4. Secondary egress persona

- [x] 4.1 Rewrite `_build_upstream_transcribe_headers` to the shared codex_cli_rs User-Agent and strip `_SDK_FINGERPRINT_HEADER_KEYS` plus the responses-lite marker and `x-codex-version`.
- [x] 4.2 Apply the same persona and stripping to `_build_files_headers` in `app/core/clients/files.py`, reusing the proxy.py constants.

## 5. Test sync

- [x] 5.1 Update the 18 locked test cases (fingerprint, proxy_utils, websocket client, upstream paths, files client) to assert the unified persona and lowercase account header; rename the passthrough-named cases.
- [x] 5.2 Run the targeted affected test files, sweep the repo for stale indirect assertions, then run the full pytest suite.
