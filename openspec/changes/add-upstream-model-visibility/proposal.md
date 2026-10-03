# Upstream model visibility in request logs

## Why
The Codex backend can silently route a request to a different model than the
one the client selected (community-verified service-side rerouting; peer
gateways such as sub2api surface this as a "model mismatch"). Today the proxy
records only the requested model, so a substitution is invisible in the
dashboard and in the stored evidence.

## What Changes
- Record the model reported by the upstream response (`response.model` on
  stream events; `model_extra["model"]` on the compact response object) into
  a new nullable `request_logs.actual_model` column, named after the existing
  `actual_service_tier` precedent.
- Wire the extraction through all evidence-bearing paths: HTTP SSE, the two
  websocket paths (including the bridge's static stub indirection) and the
  compact response path, down to the single `RequestLog` construction site.
- Expose `actual_model` on the request-logs API and render it in the dashboard
  model cell as a second `Upstream:` line, with a mismatch badge when the
  normalized requested and actual slugs differ. Normalization lives in one
  frontend pure function so the comparison rule can be recalibrated without
  touching stored data or the backend.
- Warmup, limit-warmup, transcribe, files and automation writers stay NULL by
  design: they do not produce request-model-vs-upstream-model evidence.

## Impact
Additive schema change (guarded idempotent migration, no server default, no
index), one extra `dict.get` per parsed upstream event, and dashboard-only
display changes. No routing, transport or tier logic changes; no new
configuration. Raw upstream strings are stored unmodified for evidence value,
so later comparison-rule changes never require a data migration.
