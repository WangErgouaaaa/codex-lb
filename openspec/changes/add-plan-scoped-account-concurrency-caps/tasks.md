## 1. Shared plan-cap helpers

- [ ] 1.1 Add `app/core/plan_caps.py` with a lenient `parse_plan_concurrency_caps(raw) -> dict[str, tuple[int, int]]` (invalid input yields `{}`; keys normalized via `normalize_account_plan_type`), a strict `serialize_plan_concurrency_caps(mapping) -> str` (raises `ValueError` on unknown plan keys or negative caps), and `plan_cap_violates_reserve(caps, reserve)` used by settings validation.
- [ ] 1.2 Both `app/modules/settings/service.py` and the proxy runtime import these helpers; no second parser copy.

## 2. Storage and settings surface

- [x] 2.1 Add `dashboard_settings.proxy_account_plan_concurrency_caps_json` (Text, NOT NULL, default `"{}"` server default `'{}'`) in `app/db/models.py`; add alembic migration chained after `20261001_020000_add_credit_attribution_tables`.
- [x] 2.2 `DashboardSettingsResponse` gains `proxyAccountPlanConcurrencyCaps: dict[str, PlanCapPair]` (default `{}`); `DashboardSettingsUpdateRequest` gains the optional field; update-request validation rejects unknown plan keys and negative values.
- [x] 2.3 `SettingsService`/`SettingsRepository` parse/serialize the column through the shared helpers; `get_or_create` seeds `"{}"`.
- [x] 2.4 `PUT /api/settings` merges the field with `model_fields_set` semantics, adds it to the audit `changed_fields` list, and extends the `stream_recovery_reserve <= stream_limit` validation to fire when only the plan-caps JSON changes and to check every configured plan's stream cap.

## 3. Plan-aware runtime enforcement

- [x] 3.1 `AccountConcurrencyCaps` gains `plan_overrides: Mapping[str, tuple[int, int]] | None = None` (None when no overrides configured, preserving existing field-equality assertions) and `caps_for_plan(plan_type) -> tuple[int, int]` that normalizes the plan key and falls back to the global caps.
- [x] 3.2 `effective_account_concurrency_caps()` parses the JSON column from dashboard settings and attaches overrides only in the single-replica/replica-scope branch; the partitioned branch logs once and ignores overrides. `dashboard_settings=None` keeps meaning "startup defaults, no overrides".
- [x] 3.3 `_filter_states_for_account_caps` resolves caps per `state.plan_type` (covers both call sites: main filter and response-create pre-filter).
- [x] 3.4 The sticky final admission gate (hard-sticky's only cap checkpoint) resolves caps from the selected account's plan.
- [x] 3.5 `_acquire_account_response_create_lease_or_overload`, its websocket wrapper, and `acquire_account_lease` accept `plan_type`; all existing call sites pass the account's plan.
- [x] 3.6 Unbound selection filter and opportunistic admission filter resolve caps per state's plan.
- [x] 3.7 Cap-exceeded error messages report the rejected account's plan-effective numbers.
- [x] 3.8 The stream recovery reserve deducts from each plan's own stream limit; reattach keeps bypassing the reserve.

## 4. Tests

- [ ] 4.1 Unit: helper parse/serialize round-trip and lenient/strict edges; `caps_for_plan` resolution (exact normalized key, raw-cased account plan, unconfigured plan falls to global); candidate filter separates plans (plus saturates at its stream cap while pro still admits); hard-sticky final gate rejects beyond plan cap; lease acquisition honors plan caps; existing caps equality assertions stay green with `plan_overrides=None`.
- [ ] 4.2 Integration: settings GET default `{}`, PUT write/read-back, reserve-above-plan-stream 400 including a plan-JSON-only change, unrelated update preserves NULL/global cap semantics.
- [ ] 4.3 Focused suites pass in chunks on Windows; `ruff check`, `ty check`, and `scripts/check_proxy_architecture.py` pass.

## 5. Rollout

- [ ] 5.1 Restart production via the canonical script; verify the standard health checklist.
- [ ] 5.2 Configure `{"plus":{"responseCreate":4,"stream":6},"prolite":{"responseCreate":6,"stream":8},"pro":{"responseCreate":8,"stream":12}}` via `PUT /api/settings`; global caps stay 8/16; GET read-back confirms.
