## Why

Per-account concurrency caps are a single global pair (response-create 8 / stream 16 on this deployment, startup default 4/8). Community-measured human concurrency ranges differ sharply by plan: Plus users top out around 2-3 parallel sessions, ProLite around 4-8, Pro 8-25. One global cap forces every plan to the same ceiling, so the five Plus accounts in the production pool run at a pattern well above human range for that tier — the exact "subscription as production backend" fingerprint that risk control flags. Operators need per-plan caps without recompiling or restarting into different env values.

## What Changes

- Add dashboard setting `proxy_account_plan_concurrency_caps_json` (JSON object, default `"{}"`) mapping normalized plan types to `{responseCreate, stream}` caps, applied on top of the existing global caps.
- Resolution chain per account: normalized exact plan key → global dashboard caps → startup defaults. No equivalence-class fallback (prolite never silently inherits pro's caps); an unconfigured plan keeps today's global behavior.
- Enforcement becomes plan-aware at every cap decision point: candidate filtering (`_filter_states_for_account_caps`), the hard-sticky final admission gate, response-create lease acquisition, stream lease acquisition, and opportunistic admission. The stream recovery reserve deduction applies to each plan's own stream limit.
- Plan overrides attach only when caps are not partitioned across replicas (single replica or replica scope). Under partitioned multi-replica scope the global partitioned caps continue to apply and the override is ignored with a log line; per-plan partitioning is future work.
- Cap-exceeded error messages report the numbers in effect for the rejected account's plan.
- No new environment variable (dashboard-only setting), no frontend UI change in this change (single-operator deployment configures via `PUT /api/settings`).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `proxy-admission-control`: account-local response-create/stream caps gain per-plan overrides resolved from a new dashboard JSON setting, with plan-aware enforcement, reserve deduction, and partition-scope gating.

## Impact

- Affected code: `app/core/plan_caps.py` (new shared parse/serialize helper), `app/db/models.py` + one alembic migration (new column), `app/modules/settings/` (schemas/service/repository/api), `app/modules/proxy/` (caps dataclass, effective-caps construction, filter/admission/lease call sites, error message).
- Affected data: `dashboard_settings` gains a NOT NULL TEXT column defaulting to `"{}"`; no other table changes.
- APIs and configuration: `GET/PUT /api/settings` gain `proxyAccountPlanConcurrencyCaps`; `proxyAccountStreamRecoveryReserve` validation extends to each configured plan's stream cap.
- Rollout: default `{}` is behavior-identical to today; the operator opts in per plan via the dashboard API.
