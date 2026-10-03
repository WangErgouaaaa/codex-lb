## ADDED Requirements

### Requirement: Request logs record the upstream-reported model
For every request whose upstream exchange yields a response object or stream
event carrying a `response.model` string, the proxy SHALL store that raw
string in `request_logs.actual_model` alongside the requested model, and
SHALL expose it through the request-logs API unchanged. Requests that end
before any such evidence exists, and warmup, limit-warmup, transcribe, file,
image and automation writers, SHALL leave the column NULL. The stored value
SHALL NOT be normalized or rewritten after the fact.

### Requirement: Dashboard surfaces upstream model mismatches
The dashboard request table SHALL render the upstream-reported model as a
second line under the requested model whenever it is present, and SHALL mark
the row when the normalized requested and actual slugs differ. Normalization
SHALL ignore case, date-snapshot suffixes in both `-YYYYMMDD` and
`-YYYY-MM-DD` forms, and the backend model-alias token suffixes, so that
same-model snapshot or alias differences are not reported as mismatches.
The mismatch verdict SHALL be derived at display time from the stored raw
strings, not persisted.
