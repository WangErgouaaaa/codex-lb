## Backend
- [x] Add nullable `request_logs.actual_model` column and guarded idempotent
      migration hooked to the current single alembic head.
- [x] Add `_upstream_model_from_event_payload` / `_upstream_model_from_response`
      helpers next to their service-tier counterparts.
- [x] Wire extraction on HTTP SSE, native websocket and websocket-bridge
      paths (including the `service_stubs` static stub and import registration).
- [x] Wire the compact response path via the facade shim.
- [x] Thread `actual_model` through both request-log funnels,
      `repository.add_log` and the single `RequestLog` construction.
- [x] Include `actual_model` in the failure dump metadata.
- [x] Expose `actual_model` on the request-logs API schema and mapper.

## Frontend
- [x] Add `actualModel` to the request-log zod schema.
- [x] Add `normalizeModelSlug` / `isModelMismatch` pure functions with
      dual-form date-suffix and alias-token suffix stripping.
- [x] Render the `Upstream:` second line and the mismatch badge in the model
      cell and the details drawer; add the three locale keys.
- [x] Update request-log test literals and add the three display states plus
      formatter unit cases; `bun run test` and `bun run typecheck` green.

## Verification
- [x] Unit tests: helper four-state coverage, per-path persistence assertions,
      API exposure, migration column existence after head upgrade.
- [x] Full unit gate against a same-day baseline with no new failures.
- [x] Canonical restart with the five acceptance checks and startup migration
      validation (`migrate check` clean, column present, alembic head match).
- [x] Confirm first real traffic populates `actual_model` (filter
      `request_kind='normal'`), record the observed upstream slug shape and
      recalibrate the normalization rule if needed.
