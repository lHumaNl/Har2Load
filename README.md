# Har2Load

Offline HAR inspection and revisioned scenario preparation. **Early alpha,
not a complete load-test generator.**

Har2Load's intended workflow is one HAR → reviewable scenario model → externally
assisted parameterization/correlation → JMeter and LoadRunner artifacts. The
application does not contain an LLM client or contact captured endpoints.

## Implemented

- Byte-for-byte archival of one UTF-8 HAR (optional BOM), SHA-256 integrity checks,
  stable exchange IDs, and retention of unknown fields and malformed entries.
- Separate raw and interpreted body views; declared base64 handling, explicit
  missing/empty/invalid states, separate decoded-view status, and URL-encoded
  params-only forms.
- Diagnostics for invalid fields and conflicting URL/query, form/text, and MIME
  representations. Diagnostics never silently repair captured data.
- Initial grouping by consecutive `entry.comment` annotations, otherwise a
  configurable idle gap (default **5 seconds**, inclusive). Overlap and missing
  timing are diagnosed; no think-time timers are inferred.
- Private workspace, immutable revisions, paginated metadata, and bounded selective inspection.
- Typed changesets for group display names and explicit representation selections;
  read-only validation, atomic application, audit history, stale-revision protection,
  and idempotent retries.
- Scalar variables, local extractors, precise request bindings and lifecycle checks.
  `validate` checks model contracts and bounded recorded-response evidence; it does
  not run a target system or produce runtime values.

**Not implemented yet:** explicit boundary overrides, filters, correlation
candidates, datasets, expressions, agent-code sources, collection variables, regex/native
selectors, mandatory variable reports, agent extensions/helpers, both generators,
export/handoff/redaction,
author/critic skills, native validation, and replay. Both JMeter and LoadRunner
remain required for the first complete release. Alpha workspaces are not a stable
interchange format. Version `0.3.0a1` writes workspace schema `2.0` with scenario
schema `1.2`. Existing `0.2.0a1` workspaces/scenario `1.1` remain readable and support
their previous edits; parameters require an explicit `upgrade_parameters` change.
The previous `0.1.0a1`/workspace `1.0` format is **read-only**. Unknown versions are
rejected; no workspace is silently migrated.

## Install

Requires **Python 3.12+**. The application has **no runtime dependencies**, no
tokenizer downloads, and no JMeter/Java/VuGen prerequisite for preparation.

### Linux / macOS

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install setuptools==80.9.0
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/har2load --help
```

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install setuptools==80.9.0
.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.venv\Scripts\har2load.exe --help
```

`python -m har2load` is equivalent to the installed `har2load` entry point. These
are portable installation instructions, not a claim of tested Windows/Linux
compatibility. No `uv` installation is required.

## Usage

Examples below assume the virtual environment is activated, or that `har2load`
is replaced by its full path above.

```sh
mkdir workspaces
har2load import capture.har --workspace workspaces/demo --step-gap-seconds 5
har2load summary --workspace workspaces/demo --offset 0 --limit 20
har2load inspect --workspace workspaces/demo --section groups
har2load inspect --workspace workspaces/demo --section diagnostics
```

Copy an exchange `id` from summary to inspect it:

```sh
har2load inspect --workspace workspaces/demo --exchange EXCHANGE_ID --section request
har2load inspect --workspace workspaces/demo --exchange EXCHANGE_ID --pointer /request/queryString
har2load inspect --workspace workspaces/demo --exchange EXCHANGE_ID --section response-body --offset 0 --limit 4096
```

- The destination must **not exist**; its parent must exist. Existing directories,
  even empty ones, are never overwritten. Captures are limited to 64 MiB.
- Summary uses scenario order and deliberately omits URLs, headers, comments and
  body text. Its default page is 20 entries, maximum 100.
- Inspect returns an envelope containing a fragment in `data.content`. For
  `format=json-text`, concatenate fragments and then parse the resulting JSON;
  a fragment is not necessarily a complete JSON document. `format=text` is an
  interpreted body view, not a claim about original wire bytes.
- Offsets/limits count **characters**, not UTF-8 bytes. The maximum fragment is
  4096 characters, and the CLI enforces a 64 KiB response cap. Follow
  `continuation` until it is null, including its `revision` as `--revision` on
  subsequent reads. This pins the view even if another writer publishes changes.
  All fragments include the capture digest.
- `--pointer` is an RFC 6901 JSON pointer relative to the selected JSON section;
  `~1` addresses `/`, and `~0` addresses `~`. Body views do not accept pointers.
  Pointers are limited to 256 characters so metadata cannot exhaust the response
  budget. For longer property names, read a shorter parent or the whole entry in
  chunks instead; reducing the fragment size does not shorten a pointer.
- A body without a decoded text view returns `content: null` with availability
  metadata and a separate `view_status`, not an invented empty body. Recorded
  binary/unsupported text can be `present` without a usable decoded view. Plain
  or valid base64 empty bodies are `empty` regardless of MIME. An unknown encoding
  with an empty recorded string is not assumed to describe an empty decoded body.
  Inspect the raw entry for recorded data.
- `validate` never includes extracted values in its default output. To inspect
  private recorded values explicitly, use `inspect --section extraction-checks`.
  If validation diagnostics are truncated, follow `diagnostics_continuation`
  (section `validation-diagnostics`) with its pinned revision.
- Import/summary include at most ten diagnostics. `diagnostic_count` and
  `diagnostics_continuation` identify remaining diagnostics; inspect that section
  to page through all of them. Top-level `truncated` also reflects this omission.
- Logs are off by default; `har2load --verbose import ...` enables value-free
  progress messages on stderr. Successful operations and expected errors produce
  JSON on stdout; help and version are plain text.

Exit codes: `0` operation completed (possibly with source diagnostics), `2` bad
input/arguments/limits, `3` invalid or unsupported workspace/changeset,
`4` existing destination, busy workspace, stale revision or reused change ID,
`5` I/O failure, `1` unexpected internal failure. Import succeeds for
diagnosable incomplete entries; this does **not** certify an executable scenario.

## Revisioned changes

First inspect group IDs, source locations and the original conflicts:

```sh
har2load inspect --workspace workspaces/demo --section groups
har2load inspect --workspace workspaces/demo --section conflicts
har2load inspect --workspace workspaces/demo --section scenario
```

Create a private `edit.json` changeset. Replace the illustrative IDs below with
actual IDs from your workspace, and use its current `scenario_revision` as
`base_revision`. Evidence locations are RFC 6901 pointers relative to an exchange.

```json
{
  "schema_version": "1.0",
  "change_id": "name-checkout-step",
  "base_revision": 1,
  "author": "local-user",
  "rationale": "Use a reviewed display name for this recorded step.",
  "evidence": [{"exchange_id": "EXCHANGE_ID", "location": "/request"}],
  "operations": [
    {"op": "rename_group", "group_id": "GROUP_ID", "name": "Checkout"}
  ]
}
```

```sh
har2load changes validate edit.json --workspace workspaces/demo
har2load changes apply edit.json --workspace workspaces/demo
har2load changes history --workspace workspaces/demo --offset 0 --limit 20
har2load inspect --workspace workspaces/demo --section history
har2load inspect --workspace workspaces/demo --section groups --revision 1
```

`validate` writes nothing. It checks the changeset, evidence existence and the
supported model contracts, **not** HTTP correctness or target readiness. `apply`
rechecks the current revision under an exclusive writer lock and publishes all
operations as one new revision, or none. Validation is a preview, not a reservation;
another writer or storage limits can still prevent a later apply.

Supported operations (unknown keys/operations are rejected):

| Operation | Required fields beyond `op` | Effect |
|---|---|---|
| `rename_group` | `group_id`, `name` | Change display name, not identity/order/boundaries |
| `reset_group_name` | `group_id` | Restore the original captured/heuristic name, even if long |
| `resolve_representation` | `exchange_id`, `location`, `representation` | Record an explicit source choice |
| `clear_resolution` | `exchange_id`, `location` | Remove a choice as a new, audited change |
| `upgrade_parameters` | none | Explicitly upgrade scenario 1.1 to 1.2 within a revision |
| `upsert_variable` / `upsert_extractor` / `upsert_binding` | `variable` / `extractor` / `binding` | Add or replace a complete typed declaration |
| `remove_variable` / `remove_extractor` / `remove_binding` | `id` | Remove a declaration without cascading deletion |

Representation choices must refer to an actual original conflict:

| Conflict `location` | Allowed `representation` |
|---|---|
| `/request/queryString` | `url` or `queryString` |
| `/request/postData/params` | `text` or non-file `params` (multipart reconstruction unsupported) |
| `/request/postData/mimeType` | `mimeType` or `header` |
| `/response/content/mimeType` | `mimeType` or `header` |

A `header` choice also requires `header_index`, the **absolute zero-based index**
in that side's headers array; the selected header must be Content-Type. That field
is forbidden for other choices. Other conflict locations remain visible but do
not have a supported selection operation yet.

Selections do not alter `capture.har`, rewrite requests, or certify equivalence.
Raw inspection and original diagnostics remain unchanged. The scenario and
`inspect --section conflicts` distinguish `unresolved` from `selected`; summary
counts both. Future serializers must honor these choices and separately check
capabilities. `author`, rationale and evidence are declarations, not authenticated
identity or proof that the decision is correct.

Rules and limits:

- `change_id`: 1–64 ASCII letters/digits/`_`/`.`/`-`, starting with a letter/digit.
  Repeating **the same document and ID** returns its original `applied_revision`
  without creating another revision, even after later edits. The response's
  `scenario_revision` describes the current snapshot. Reusing an ID with different
  content is a conflict; a new change against a stale base is also a conflict.
  Stale/reused-ID errors include the workspace ID and revision observed at rejection,
  including the under-lock recheck. That revision is not a promise that no later
  writer has committed; inspect it before preparing a new changeset.
- `author`: nonblank, up to 128 characters; `rationale`: nonblank, up to 4096.
  Require 1–32 existing evidence references and 1–100 operations. Pointers are
  limited to 256 characters; new display names to 256. Changeset input: 1 MiB.
- Undo is another changeset with a **new ID and current base revision**; no
  history rewriting or generic rollback command exists. Restore an earlier name
  with `rename_group`, its captured default with `reset_group_name`, or clear/replace
  a representation selection.
- History metadata is paginated; full author/rationale/evidence/operations are
  available through bounded `inspect --section history`. They are private data.
- Group membership/order, source IDs, timing settings and execution/target states
  remain fixed in this slice. Arbitrary JSON patches or code execution are not accepted.

## Variables, extractors and bindings

New scenarios contain `parameters: {variables: [], extractors: [], bindings: []}`.
All three lists use globally unique IDs; variable names are unique too. IDs/names
use ASCII `[A-Za-z_][A-Za-z0-9_]{0,63}`. Upserts replace the full declaration and store
the changeset's `change_id` as provenance. Final-batch validation permits adding
bindings before their referenced variable/extractor in the same batch. Removing
referenced objects without repairing/removing their consumers rejects the whole batch.

For an existing scenario 1.1, put `{"op":"upgrade_parameters"}` before parameter
operations in a normal changeset. This creates an audited new revision, preserving
existing group names, selections, all old revisions and the original capture.
It is not an in-place rewrite of old snapshots. An older CLI may reject the newer
producer marker after any write; use this version for historical reads.

Example `operations` array: replace `PRODUCER_ID` and `CONSUMER_ID` with recorded
exchange IDs and include the usual changeset author/rationale/evidence fields.
The consumer must contain a numeric `orderId` in its JSON request body.

```json
[
  {
    "op": "upsert_extractor",
    "extractor": {
      "id": "ext_order", "exchange_id": "PRODUCER_ID", "area": "response_body",
      "selector": {"dialect": "json_path_subset_v1", "expression": "$.id"},
      "cardinality": "exactly_one", "allow_empty": false,
      "on_failure": "mark_error_and_continue"
    }
  },
  {
    "op": "upsert_variable",
    "variable": {
      "id": "var_order", "name": "order_id", "value_type": "integer",
      "source": {"kind": "extracted", "extractor_id": "ext_order"},
      "initialize": {"event": "after_response", "exchange_id": "PRODUCER_ID"},
      "scope": "iteration", "reset": "iteration_start",
      "purpose": "Order identifier returned by the creation request", "sensitive": false
    }
  },
  {
    "op": "upsert_binding",
    "binding": {
      "id": "bind_order", "variable_id": "var_order", "exchange_id": "CONSUMER_ID",
      "location": {"area": "request_json", "pointer": "/orderId"}
    }
  }
]
```

```sh
har2load changes validate parameters.json --workspace workspaces/demo
har2load changes apply parameters.json --workspace workspaces/demo
har2load validate --workspace workspaces/demo
har2load inspect --workspace workspaces/demo --section parameters
har2load inspect --workspace workspaces/demo --section extraction-checks
```

### Sources and lifecycle

Scalar types: `string`, `integer`, `number`, `boolean`, `null`. Booleans are not
numbers; integral JSON numbers can satisfy `integer`. No implicit string-to-number
conversion, HTML entity decoding or collection transformation is performed.

Supported source objects:

- `{"kind":"literal","value":7}` — must match the declared scalar type.
- `{"kind":"external","key":"API_TOKEN","allow_empty":false}` — declares a
  runtime prerequisite; preparation never reads the environment or a secrets file.
- `{"kind":"extracted","extractor_id":"ext_order"}` — a recorded producer.
- `{"kind":"generated","generator":"integer","minimum":1,"maximum":100}` —
  inclusive bounds; declaration only.
- `{"kind":"generated","generator":"string","length":12,"alphabet":"abc123"}` —
  distinct printable ASCII alphabet, length 1–4096.
- `{"kind":"generated","generator":"uuid"}`.
- `{"kind":"generated","generator":"utc_datetime","offset_seconds":0,"format":"iso8601"}`.
- `{"kind":"generated","generator":"utc_date_range","minimum":"2026-01-01","maximum":"2026-12-31","format":"iso8601"}`.

Generated sources are **not evaluated during preparation**. Their future native
implementations, parity and runtime formatting remain unverified. `purpose` is a
required nonblank explanation (up to 1024 characters), and `sensitive` is a required
boolean annotation—not automatic redaction.

Initialization events are `session_start`, `iteration_start`, `before_request`
and `after_response`; the latter two also require `exchange_id`. The initializer
repeats at **each occurrence** of that event, not at every substitution. Scope
controls visibility/reset and does not imply a hidden once-only initialization:

| `scope` | Required `reset` | Constraints |
|---|---|---|
| `session` | `session_start` | Values visible through the session; initializer still explicit |
| `iteration` | `iteration_start` | Cannot initialize only at session start |
| `request` | `before_request` | Initialize before the same request as every consumer |

Extracted variables initialize `after_response` at their extractor's producer,
with session/iteration scope. Every binding consumes a saved value before its
request. Unknown references and use-before-initialization reject the batch; no
previous-iteration fallback is assumed. Recorded timing is checked separately:
overlap or unknown completion generates `CAPTURE_DEPENDENCY_UNPROVEN`, not a claim
that the capture proves causality. The scenario remains sequential.

### Extraction profile and checks

- `response_body` + `json_path_subset_v1`: `$`, dot field names, canonical array
  indices and JSON double-quoted bracket keys (for example `$["a.b"][0]`). No
  wildcard, filter, script or fallback regex engine.
- `response_body` + `boundary_v1`: nonempty literal `left`/`right` delimiters,
  nearest pairs left-to-right, case-sensitive and non-overlapping.
- `response_headers` or `response_cookies` + `name_v1`: selector `name`; headers
  are case-insensitive, cookies case-sensitive; duplicate order is preserved.

Every extractor declares `cardinality: "exactly_one"`, or `"occurrence"` with an
explicit **1-based** positive `occurrence`. `allow_empty` governs empty strings;
JSON null remains distinct from a missing value. Only
`on_failure: "mark_error_and_continue"` is currently accepted.

Checks distinguish `matched`, `missing`, `empty`, `multiple`, `invalid`,
`type_mismatch`, `unavailable` and `limit_exceeded`. Failures do not reuse an old
value; output value is null and `failure_marker` records the planned `ERROR_<name>`
policy. These are stateless captured-fixture checks, **not a runtime reset/continue
implementation**. Native generators must still implement and test those policies.

`validate` reports model-contract validity separately from captured evidence.
Missing selector matches and other observed check failures return exit 3.
Unavailable recorded sources and resource-limit results are explicitly unverified
warnings; external inputs remain required at
runtime, not fabricated values. Matching a recorded value does not certify a
future response, business success, target compatibility or a safe load test.
Actual values are omitted from default validation output, even when `sensitive`
is false. Explicit extraction inspection is private/unredacted and identifies
whether the body view uses a scenario-selected representation.

Every declared extractor is checked, including extractors not yet referenced by
a variable. Such rows have `check_kind: "extractor"`, `variable_id: null` and no
variable-specific failure marker. Referenced extractors are evaluated once, then
receive per-variable type checks (`check_kind: "variable"`). `check_count` counts
these rows; `checked_extractor_count` counts distinct declarations. Only declared
body producers receive body projections; headers/cookies use their recorded arrays.

### Request destinations and limits

Binding `location` is one of:

- `request_json` with RFC 6901 `pointer`: existing scalar value, compatible type.
- `request_query`, `request_header`, `request_form` with **zero-based absolute**
  `index` in the selected ordered representation, not an occurrence by name.
- `request_path` with zero-based `index`: split the URL path on `/`, omit only
  the initial slash separator, retain internal/trailing empty segments.
- `request_text` with `start`/`end`: half-open character offsets in UTF-8 text
  bodies (`text/*`), including explicit empty-span insertion.

Duplicate/overlapping destinations are rejected. JSON bindings cannot silently
turn recorded numbers into quoted strings; null is accepted only for JSON
destinations. Query and form fields are separate. Unresolved representation
conflicts affecting a destination block its binding; selections determine which
URL/form/body/MIME view is addressed. A binding to a Content-Type header discarded
by a MIME selection is rejected. Source views are derived in memory without
changing raw bytes. **Bindings are not yet applied to emitted HTTP/native scripts**;
escaping, substitution and target capability parity remain generator work.

Limits: 256 variables, 256 extractors, 1024 bindings; 1 MiB UTF-8 per local body/JSON
check and active textual binding view; 64 JSON containers/path tokens; 256-character
selectors/delimiters; 1000 matches. Overflow is unverified, not a truncated success.
Literal strings are limited to 65536 characters. Unknown source/selector dialects
are rejected, not silently approximated. Dataset/expression/agent-code/collection
support and the bounded regex profile remain future work in the full specification.

## Preservation and limitations

The workspace contains `capture.har` (original bytes), `scenario.json` (immutable
revision 1), `revision-000002-<sha256>.json` and subsequent immutable revision
objects, and `manifest.json` (version, head pointer, checksums and privacy state).
Each object contains a scenario snapshot, changeset, timestamp and parent digest.
The manifest is published last using atomic replacement. Reads verify the chain
and replay supported model operations to check that the audit matches the model;
this does not execute captured or agent-authored code.

Failed initial creation can leave an incomplete directory; inspect it manually
and choose another destination. Failed updates leave the old head readable or
may have already committed if the error happened after publication. In that case,
inspect history and **retry the identical changeset/ID**, not a newly named copy.
Unreferenced revision objects may remain after interrupted writes; reads ignore
them. No automatic cleanup deletes these files.

Writers coordinate through `.changes.lock`; readers do not acquire it. A crashed
writer can leave a stale lock. It is **never automatically stolen or deleted**:
before manually removing it, ensure every writer using that workspace has stopped.
Use a trusted local filesystem; network-filesystem locking and universal
power-loss durability are not claimed. Limits: 1000 revisions including the
initial one, 16 MiB per revision object, 64 MiB total committed revision objects.
Capture parsing and history verification/replay happen on each read; this is not
a streaming or large-history performance claim.

Manual edits to persisted files are not imported; no overwrite or automatic
migration exists. An old schema-1.0 workspace remains readable with this version,
but cannot be edited. To start revisioned work, explicitly import its archived
`capture.har` into a **new** workspace. Copying a complete current workspace does
not depend on its original absolute path.

Raw JSON retains order and duplicates **in arrays** (headers, cookies, query and
form parameters). Duplicate JSON object keys, non-finite numbers, malformed JSON,
or unsupported top-level structure are rejected rather than interpreted
ambiguously. This is focused validation, not a full HAR-schema validator.

Structured inspection preserves finite decimal numbers without conversion to
binary floats (including large decimals and very small exponents). It emits JSON
number tokens, not quoted replacements. Consumers that need the same precision
must use a lossless numeric parser; for example, Python's
`json.loads(fragment_text, parse_float=decimal.Decimal)`. Formatting and object-key
order in structured views are canonicalized; only `capture.har` preserves the
original bytes. Extreme numeric syntax beyond the parser's resource/representation
limits is rejected with a diagnostic rather than silently rounded.

Base64 is decoded only when declared; arbitrary bodies are not URL-unquoted.
Only provably supported form representations are reconstructed. Binary and
unsupported encoding/form data remain available in the original capture, not
silently converted into plausible text. Request/response HTTP semantics and
future target compatibility are not established by successful import.

Annotations are exact, consecutive labels: `A,A,B,A` produces three groups.
Empty annotations do not inherit a neighboring label. All timestamps must be
usable for time ordering; otherwise source order is retained. An unknown prior
interval prevents later inferred idle gaps, conservatively. No requests are
removed or disabled. No explicit user boundary override exists in this slice.

Time comparisons use decimal arithmetic, not a fuzzy epsilon. The supported
timestamp profile is a full date/time with seconds, optional fractional seconds,
and `Z` or a `±HH:MM` offset. Other timestamp forms and timing beyond the bounded
numeric budget (1000 significant/fractional digits and float-finite magnitude)
are diagnosed, not rounded into trusted boundaries. Original fields remain intact.

## Privacy and trust

**Redaction is OFF and not implemented in this alpha.** Raw captures, scenario
annotations and explicit inspection may contain credentials and session data.
Do not upload them to an untrusted agent, remote service or public repository.
`workspaces/` and `samples/` are ignored locally. POSIX workspace directories use
owner-only permissions and files use `0600`; Windows requires appropriate ACLs
on the parent directory. This is not encryption or a sandbox.

Workspace files must be regular files; direct symlinks are rejected. Checksums
detect accidental corruption, not malicious replacement of both data and manifest.
Use trusted local parent directories without hostile concurrent writers. The
64 MiB input cap bounds input size, not peak memory: this alpha parses in memory
and reparses the capture for each read. No streaming-performance claim is made.

HAR comments and bodies are data, never instructions or executable code. No
Groovy/C/native script, captured request or embedded command is executed during
import or inspection.

## Development

All fixtures are independently authored synthetic Python data. Do not add real
traffic to the test suite. `AGENTS.md`, root `docs/` and root `samples/` stay local.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy
```

On Windows replace `.venv/bin/python` with `.venv\Scripts\python.exe`. Versions
of build/test/lint/type-check tools are pinned in `requirements-dev.txt`; runtime
installation needs none of the development tools.

### Offline installation

On a connected preparation machine, after installing development requirements:

```sh
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir wheelhouse
python -m pip download --only-binary=:all: -r requirements-dev.txt --dest wheelhouse
```

Prepare development wheelhouses on the destination OS/architecture/Python version
(mypy and ruff wheels are platform-specific). Transfer the built Har2Load wheel;
include the development wheels only if needed. In the disconnected environment:

```sh
python -m venv .venv
# Activate the environment, then:
python -m pip install --no-index --find-links wheelhouse har2load==0.3.0a1
# Optional development installation:
python -m pip install --no-index --find-links wheelhouse -r requirements-dev.txt
python -m pip install --no-index --no-build-isolation --no-deps -e .
```

Keep wheelhouses outside the repository, or ensure they are not committed. These
commands prepare/install artifacts only; they do not execute a load test.

## Previous standalone converter

The legacy root `main.py` has been removed. Use `har2load` or
`python -m har2load` as described above. This alpha does not reproduce the old
JSON/YAML compaction or prompt-generation interface.
