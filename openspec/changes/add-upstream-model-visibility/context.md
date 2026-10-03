# Context: 2026-09-21

## Motivation evidence
- September 2026 community reports: Pro users selecting GPT-6 Astra saw Usage
  records billed to GPT-5.5 with no UI notice; Codex server code confirms
  post-safety-check rerouting is possible. sub2api deployments caught both
  downgrades (requested astra, returned luna) and silent upgrades by comparing
  the requested model against the upstream response `model` field.
- Design decision: this is string comparison on the upstream-reported field,
  not behavioral fingerprinting (ModelTrace-style probes burn tokens and
  pollute conversations; they remain a client-side tool). A lying field is out
  of scope; same-model effort downgrades remain visible via the existing
  `reasoning_effort` column.

## Precedents reused
- `actual_service_tier` (20260320 migration) established the requested-vs-actual
  pattern, extraction sites on all three stream paths and the persistence
  funnel; `reasoning_effort` (20260912 backport) established log-column +
  dashboard-column delivery.
- Verified wiring inventory: `streaming/mixin.py` (first payload + subsequent
  events), `websocket/mixin.py` (two extraction sites, three persistence
  sites), `http_bridge/upstream_events.py` via the `service_stubs.py` static
  stub, `compact.py` response-object path, `request_log.py` two funnels,
  `request_logs/repository.py` `add_log` + the only `RequestLog` construction.
- Known non-wiring points (stay NULL): bridge prewarm state copy
  (`skip_request_log=True`), proxy and limit warmups (write their own logs,
  already omit tier), automations, transcribe, files.

## Comparison-rule location
The mismatch verdict is computed in the frontend from raw stored strings:
`normalizeModelSlug` strips case, `-YYYYMMDD` / `-YYYY-MM-DD` date snapshots
and the backend alias token set (`request_policy._MODEL_ALIAS_TOKENS` plus
ultra/max wire aliases). Stored values are never rewritten, so recalibrating
the rule after observing real slug shapes never touches data or backend.
