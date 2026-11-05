# Authoring supported correlation

Contents: [Changeset](#complete-minimal-changeset) ·
[Selectors and bindings](#selectors-types-and-bindings) · [Lifecycle](#lifecycle-and-other-sources)

Use the current workspace's IDs and chosen representations, not copied IDs or
offsets from another HAR. Inspect the model and concrete producer/consumer locations
before editing. This reference covers the 0.11 scalar baseline; unsupported native
components/selectors must not be disguised as supported declarations.

## Complete minimal changeset

This executable-contract example assumes revision 1, a producer response header
`X-Token`, and a later JSON request with an existing string at `/token`. Replace
`PRODUCER_ID` and `CONSUMER_ID` with actual IDs. Choose a unique stable change ID,
real author, accurate rationale and relevant evidence; do not apply an example
unchanged to unrelated traffic. Names here have no special business semantics.

```json
{
  "schema_version": "1.0",
  "change_id": "correlate-token-001",
  "base_revision": 1,
  "author": "har2load-agent",
  "rationale": "Reuse the declared producer header at the confirmed consumer location",
  "evidence": [{"exchange_id": "PRODUCER_ID", "location": "/response/headers/0"}],
  "operations": [
    {"op": "upsert_extractor", "extractor": {
      "id": "ext_token", "exchange_id": "PRODUCER_ID", "area": "response_headers",
      "selector": {"dialect": "name_v1", "name": "X-Token"},
      "cardinality": "exactly_one", "allow_empty": false,
      "on_failure": "mark_error_and_continue"
    }},
    {"op": "upsert_variable", "variable": {
      "id": "var_token", "name": "token", "value_type": "string",
      "source": {"kind": "extracted", "extractor_id": "ext_token"},
      "initialize": {"event": "after_response", "exchange_id": "PRODUCER_ID"},
      "scope": "iteration", "reset": "iteration_start",
      "purpose": "Reuse the producer value within this iteration", "sensitive": true
    }},
    {"op": "upsert_binding", "binding": {
      "id": "bind_token", "variable_id": "var_token", "exchange_id": "CONSUMER_ID",
      "location": {"area": "request_json", "pointer": "/token"}
    }}
  ]
}
```

An upsert replaces the whole declaration. Keep existing fields deliberately, not
through assumed partial-update behavior. All references must be valid in the final
batch. Candidate IDs are evidence references, not an implemented candidate-approval
operation: author actual variables/extractors/bindings. Do not invent `accept_candidate`.
Group renaming uses `{"op":"rename_group","group_id":"GROUP_ID","name":"Agreed label"}`;
it does not reorder requests, create a new UC container or merge workspaces.

## Selectors, types and bindings

- Body JSON: `area: response_body`, selector
  `{"dialect":"json_path_subset_v1","expression":"$.order.id"}`. Only scalar paths,
  canonical array indices and JSON bracket keys; no wildcard/filter/full JSONPath.
- Body boundaries: selector `{"dialect":"boundary_v1","left":"<id>","right":"</id>"}`.
  Match literal nonempty boundaries, left-to-right nearest non-overlapping pairs.
- Response headers: `name_v1`, ASCII token name 1–256 characters, case-insensitive.
  Values stay strings; numeric-looking text is not coerced. Use exactly-one or
  `cardinality: occurrence` with explicit **one-based** `occurrence`. Native capture
  is bounded (1 MiB, 1000 fields, selected ASCII value 64 KiB); trim only edge SP/HTAB.
- Recorded `response_cookies/name_v1` checks exist, but native cookie extraction
  remains unsupported. Use the native jar for ordinary sessions. Selecting a whole
  `Set-Cookie` header is not extracting a cookie value.
- Types: string, integer, number, boolean, null. Distinguish missing, empty, null
  and failed values. `allow_empty` does not make missing matches valid.
- JSON consumers use an existing RFC 6901 scalar `pointer`. Query/header/form
  consumers use zero-based **absolute** array `index`, not occurrence by name.
  Path indices omit only the first slash separator; text ranges use half-open
  character `start`/`end`. Inspect the chosen view before assigning locations.
- Do not globally replace equal strings. Do not bind managed headers or Cookie,
  Authorization, Content-Type/Encoding to circumvent target restrictions.

## Lifecycle and other sources

Choose initialization separately from visibility/reset. Session scope resets at
`session_start`, iteration at `iteration_start`, request at `before_request`.
Extracted variables initialize `after_response` at their own producer and use
session/iteration scope. A producer attempt resets its outputs; unrelated responses
do not. Initializers repeat on each declared event, not each binding read.

Use literal only for an intentional constant. Native external input is
`{"kind":"external","key":"API_TOKEN","allow_empty":false}` with optional explicit
string `initial_value` in scenario 1.5. Never infer an initial from captured secrets
or read environment/secrets files during preparation. Missing values become markers
at runtime, not fabricated valid inputs or a new automatic stop policy.

Supported generated sources are integer, string, uuid, utc_datetime and
utc_date_range; consult the installed tool's matching contract for their required
bounds/formats rather than guessing fields. These are event-driven runtime sources,
not preparation-time random values or CSV datasets. Keep declared initial native
strings and generated sources distinct. JMeter UDV literals need its documented
encoding; do not paste `${...}` as an executable native function accidentally.

Keep marker-only status: `ERROR_<name>`, value-free diagnostics and continuation;
no generated correlation assertion or forced native transaction result. Good
independent outputs survive another failure. Recorded `validate` failures must be
explained/fixed, not hidden by deleting declarations or switching `allow_empty`.
Unavailable recorded evidence remains unverified, not a matched check.

Schema upgrades are explicit: 1.1→1.2 `upgrade_parameters`, 1.2→1.3
`upgrade_extensions`, 1.3→1.4 `upgrade_extension_runtime`, 1.4→1.5
`upgrade_native_inputs`. Inspect first and use only needed, approved audited
operations. Do not opt old code into wrappers or invent initial values as an upgrade.
