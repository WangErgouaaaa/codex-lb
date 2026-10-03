## 1. Rejection-triggered refresh module

- [x] 1.1 Add `app/modules/usage/rejection_refresh.py` with the usage-limit trigger code set (excluding `rate_limit_exceeded`), a synchronous never-raising entry point, and a 15-second per-account monotonic debounce.
- [x] 1.2 Force-refresh the benched account through `UsageUpdater.force_refresh_result` inside a background session, skipping non-`RATE_LIMITED` accounts and fetch failures.
- [x] 1.3 Collect exhausted usage windows (used percent >= 99.5, plausible future `reset_at`), take the latest deadline, and extend `accounts.reset_at` extend-only via `update_status_if_current`, invalidating the selection cache on success.
- [x] 1.4 Prune the debounce map when it exceeds 512 entries and keep fire-and-forget task exceptions logged, never propagated.

## 2. Streaming wiring

- [x] 2.1 Call `maybe_trigger_usage_limit_refresh(account.id, code)` right after `mark_rate_limit` in `_handle_stream_error`, the shared SSE/native-WS failure convergence point.

## 3. Regression coverage

- [x] 3.1 Add unit coverage for code gating, debounce suppression/expiry, deadline extension, extend-only behavior, no-exhausted-window fallback, fetch failure, non-`RATE_LIMITED` skip, missing account, and CAS miss.
- [x] 3.2 Add an integration regression proving a connect-phase 429 failover triggers the forced fetch and persists the extended `reset_at` on the rejected account.

## 4. Validation

- [x] 4.1 Run `py_compile` on the new module and the focused unit/integration suites for usage refresh, load balancer, and transient retry.
