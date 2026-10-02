# Design: plan-scoped account concurrency caps

## Context

The deployment pools 5 Plus, 2 Pro, and 1 ProLite ChatGPT accounts behind one codex-lb instance (single bridge-ring replica). Community-measured human parallel-session ranges (Plus ~2-3, ProLite ~4-8, Pro ~8-25) make a single 8/16 global cap the wrong shape: it over-exposes the Plus tier while leaving headroom unused elsewhere. Ban risk tracks usage-pattern realism more than raw limits, so per-plan caps are a safety control, not a throughput control.

## Goals / Non-Goals

Goals: per-plan caps configurable at runtime via the dashboard settings API; identical behavior to today until configured; single shared parser; minimal upstream-merge surface (one column, one JSON blob, no new env knob, no frontend change).

Non-goals: per-account overrides; per-plan partitioning across multi-replica rings (future change if this deployment ever scales out); frontend UI (single operator, one-time `PUT /api/settings`); plan equivalence-class fallback.

## Decisions

### Single JSON column over six integer columns

`proxy_account_plan_concurrency_caps_json` follows the `additional_quota_routing_policies_json` precedent (parse at read, validate on write, lenient on corrupt input). One column, one repository kwarg, one schema field — versus six columns each touching every settings-layer site. Avoids the Settings-env ratchet (`MAX_SETTINGS_FIELDS`), settings-reference regeneration, and `.env.example` drift tests entirely.

### No equivalence-class fallback

`ACCOUNT_PLAN_EQUIVALENTS` maps prolite→pro for quota semantics. Reusing it for caps would make "delete the prolite key" mean "inherit pro's caps" instead of the predictable "fall back to global caps". Operators configure the plans they pool; anything else keeps today's global behavior.

### Overrides only attach to non-partitioned caps

`partition_cap` is identity for a single replica and `observe_members` early-exits when membership is unchanged, so per-plan partitioning logic is unreachable code on this deployment. Attaching overrides only in the single-replica/replica-scope branch keeps the partitioned path exactly as it is today (strictly conservative versus per-plan values, which may be higher); a one-time log line documents the skip if a ring ever grows.

### `plan_overrides=None` when unconfigured

`AccountConcurrencyCaps` is compared field-by-field in existing tests (`test_load_balancer_concurrency.py` equality assertions). An empty-dict default would break them and invite truthiness bugs; `None` means "no overrides" and `{}` never appears.

### Shared parser, one copy

The repo already carries a duplicated additional-quota parser (settings service vs `load_balancer.py`). The new helpers live in `app/core/plan_caps.py` and both layers import them; the lenient parse and strict serialize semantics mirror the existing JSON-column pattern.

## Risks / Trade-offs

- Raw plan strings from `Account.plan_type` are not normalized; every lookup normalizes first, so stray casings degrade to global caps (today's behavior) instead of KeyError.
- The hard-sticky path bypasses candidate filtering by design (ownership constraint); its final admission gate is the only enforcement point there and must stay plan-aware or Plus accounts over-admit silently between the plan cap and the global cap.
- Reserve validation must fire on plan-JSON-only changes; the existing trigger keys off the two global cap fields' `model_fields_set`.

## Migration Notes

Alembic adds one NOT NULL TEXT column with server default `'{}'` on `dashboard_settings` (batch_alter_table, idempotent column-exists guard per house pattern). Rollback: clear the JSON to `{}` (behavior-identical) or downgrade the column. No production data is rewritten.
