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
- Bounded, deterministic scalar correlation candidates, preserving alternative
  producers and temporal uncertainty; never automatic model changes.
- Revision-pinned JSON/Markdown variable reports with model provenance, lifecycle,
  uses, recorded checks, candidate evidence, revision diffs and ungenerated source maps.
- Opaque Groovy/C code-block and helper declarations, scalar `agent_code` outputs,
  dependency/order contracts, source digests and per-target missing-implementation checks.
- First static/literal generation profile: selected request materialization, structural
  JMX and intermediate LoadRunner C source, per-target capability errors and digest-linked reports.
- Scalar runtime artifact profile: literal/extracted variables, body JSON/boundary extraction,
  lifecycle actions and exact request templates, with explicit failure markers and source maps.
- Explicit `scalar_body_v1` Groovy/C wrappers, JMeter setup helper registry, typed helper
  calls and per-output publication; correlation markers never override native result status.
- Native initial strings in JMeter Test Plan UDV and bounded VuGen `.prm/.dat`
  integration assets; five lifecycle-driven scalar generators, without a JSON input loader.
- Native cookie sessions: empty initial jars by default, with explicit selected HAR
  cookie seeds through `generate --initial-cookie`; no fixed captured Cookie headers.

**Not implemented yet:** explicit boundary overrides, filters, expressions,
collection variables, regex/native selectors, native component properties/allowlists,
response-cookie runtime
extraction, complete verified VuGen projects,
runnable bundle export/handoff/redaction,
author/critic skills, per-workspace native validation, and replay. Both JMeter and LoadRunner
remain required for the first complete release. Alpha workspaces are not a stable
interchange format. Version `0.11.0a1` writes workspace schema `2.0` with scenario
schema `1.5`. Existing `0.2.0a1` workspaces/scenario `1.1` remain readable and support
their previous edits; parameters require an explicit `upgrade_parameters` change.
The previous `0.1.0a1`/workspace `1.0` format is **read-only**. Unknown versions are
rejected; no workspace is silently migrated.
Existing `0.3.0a1`/`0.4.0a1` scenarios 1.2 remain readable/editable with their previous
operations. Target extensions and `agent_code` require an explicit `upgrade_extensions`
change to scenario 1.3. Explicit wrapper ABI opt-in additionally requires
`upgrade_extension_runtime` to scenario 1.4. Reads never perform these upgrades;
upgrading never opts existing opaque source into wrappers automatically.
Existing `0.5.0a1`/`0.6.0a1`/`0.7.0a1` workspaces remain readable/editable for their
previous operations without an upgrade; wrapper opt-in requires the explicit 1.4 transition.
Existing `0.8.0a1` workspaces remain readable/editable. Adding `source.initial_value`
requires audited `upgrade_native_inputs` from scenario 1.4 to 1.5. The upgrade
preserves existing declarations and never invents initial values; historical revisions
retain their original schema.
Existing `0.9.0a1` and `0.10.0a1` workspaces remain readable. Cookie generation options require no
schema upgrade and never mutate the model; the generated private report records them.

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
| `upgrade_extensions` | none | Explicitly upgrade scenario 1.2 to 1.3 within a revision |
| `upgrade_extension_runtime` | none | Explicitly upgrade scenario 1.3 to 1.4 without opting existing source into wrappers |
| `upgrade_native_inputs` | none | Explicitly upgrade scenario 1.4 to 1.5 without inventing initial strings |
| `upsert_variable` / `upsert_extractor` / `upsert_binding` | `variable` / `extractor` / `binding` | Add or replace a complete typed declaration |
| `remove_variable` / `remove_extractor` / `remove_binding` | `id` | Remove a declaration without cascading deletion |
| `upsert_code_block` / `upsert_helper` | `block` / `helper` | Store a complete opaque target declaration and derive its source digest |
| `remove_code_block` / `remove_helper` | `id` | Remove without cascading; reject dangling contracts |

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

## First generation profile: static requests and literal bindings

```sh
har2load generate --workspace workspaces/demo --check
har2load generate --workspace workspaces/demo --target jmeter --check
har2load inspect --workspace workspaces/demo --section materialized-requests
har2load generate --workspace workspaces/demo --output workspaces/generated-r3 --revision 3
har2load inspect --workspace workspaces/demo --section generation-diagnostics
```

`generate` requires either `--check` (render and check in memory, no writes) or
`--output NEW_DIRECTORY`. `--target` is `both` by default, or `jmeter`/`loadrunner`.
Both modes are offline and **never execute, compile or natively load artifacts**.
For literal-only models the profile is `static_literals_v1`, not the complete product
support matrix. Models with extractors/nonliteral declarations select the runtime
profile described below (and may be blocked by its capability checks).

Supported inputs are HTTP/HTTPS GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS, ASCII URLs,
ASCII headers, and bounded UTF-8 JSON/text/URL-encoded request bodies. Selected
URL/queryString, text/params and Content-Type/mimeType representations are respected;
unresolved request conflicts block generation. Response-only conflicts remain in
the evidence report but do not block this request-only profile.

Literal variables may be bound to exact request locations. JSON bindings retain
scalar types and decimal precision; only JSON bodies with a binding are structurally
reserialized. Unbound body bytes and unaffected query/form occurrences retain their
recorded spelling. A path replacement is encoded as one segment. Selected HAR query
and form arrays preserve order, duplicate names and empty values. Text replacements
use the recorded character spans. Raw HAR bytes and scenario revisions never change.

These immutable constants are **folded at preparation time**, not emitted as runtime
variables/lifecycle processors. Reports explicitly mark folded variables/lifecycle
actions and map each binding to the generated request component. Literal values are
therefore not populated as named runtime variables in Debug Sampler. Any external,
generated, extracted or agent-code variable, and even an unreferenced extractor,
blocks this static profile rather than silently dropping its contract. Code/helper
declarations select runtime lowering even without variables or bindings. Opaque
declarations without an explicit supported ABI block only their affected target.

Managed-header policy `managed_headers_v1` omits Host, Content-Length, Connection,
Keep-Alive, Transfer-Encoding, TE, Trailer, Upgrade, Proxy-Connection and fields named
by Connection. Every omission is listed in materialization decisions; binding to
an omitted field is rejected. Native clients manage transport framing. Selected
Content-Type is retained or supplied from the chosen body metadata; no response
Content-Type is substituted for it. URL fragments are explicitly omitted as
non-HTTP data. Captured Authorization/Proxy-Authorization headers remain rejected.
Recorded Cookie headers and request cookie arrays are omitted under
`native_cookie_jar_v1`: normal scenario authentication and the native jar manage
the session. Omissions are recorded, and `GENERATION_COOKIE_AUTH_UNVERIFIED` warns
that session availability is not proven. Malformed cookie-array shape and bindings
to a managed Cookie header remain errors.

Missing bodies are not invented. POST/PUT/PATCH without postData need explicit
`bodySize: 0`; a positive known body size without data is blocked. Multipart/file
uploads, compressed request bodies, non-UTF-8 body encodings, invalid header data,
and unknown methods are unsupported. This deliberately conservative alpha may
reject real captures that later profiles will support.

### Native cookie sessions and optional initial cookies

By default, JMeter has an enabled **empty HTTP Cookie Manager** and LoadRunner uses
its native cookie jar. The actual login flow should obtain fresh server cookies.
Recorded request cookies are neither copied into initial configuration nor replayed
as fixed headers. Har2Load does not invent a missing login flow or infer that a
recorded session is still valid. Authorization header handling remains separate.

To deliberately bootstrap a jar, select individual entries from the immutable HAR:

```sh
har2load inspect --workspace workspaces/demo --exchange EXCHANGE_ID \
  --section entry --pointer /response/cookies

har2load generate --workspace workspaces/demo --revision 2 --check \
  --initial-cookie 'EXCHANGE_ID:/response/cookies/0'

har2load generate --workspace workspaces/demo --revision 2 \
  --output workspaces/cookie-seeded-r2 \
  --initial-cookie 'EXCHANGE_ID:/request/cookies/0' \
  --initial-cookie 'OTHER_EXCHANGE_ID:/response/cookies/1'
```

Replace the placeholder IDs with exact IDs from `summary`; indices are zero-based.
`--initial-cookie` is repeatable, at most 256 selections. It accepts only explicit
`/request/cookies/N` or `/response/cookies/N` array entries, not a Cookie/Set-Cookie
header, name-only matching, or an implicit "all cookies" import. Duplicate selections
and duplicate `(domain, name, path)` identities fail rather than choosing a winner.
An invalid selection fails before publication. No secret value is passed on the CLI.

Selection is a **generation option**, not a model edit. Repeat the same flags and
revision to reproduce it. The generated private report retains values, source
exchange/area/index, attributes and inference flags; ordinary output exposes only
policy/counts. Raw HAR and historical model revisions remain unchanged. Redaction
is off: seeded JMX/C and private reports contain the selected values.

The bounded selection profile preserves name/value, effective domain/path, Secure,
expiry and HttpOnly/SameSite metadata. Missing domain means host-only for the source
request host; an explicit domain must equal that host (one leading dot and case are
normalized). Parent-domain, unrelated-domain and non-ASCII-domain selections are
not supported. Missing path uses the source URL's RFC default directory; no URL
decoding occurs. Missing Secure conservatively means true for HTTPS, false for HTTP.
Explicit expiry must be a positive whole-second timezone-qualified ISO timestamp;
preparation never consults the live clock, extends expiry or converts it to a session
cookie. These rules are disclosed inference, not proof of the browser's original scope.

The current debug profile retains **reset each main iteration**: JMeter restores
its initial Cookie Manager collection; LoadRunner clears then applies selected
`web_add_cookie` seeds at Action start. Server cookies update the active jar between
requests; seeds are not reapplied per request. All users start with the same selected
seed values, so an opt-in seed is not a unique-user credential mechanism. A future
configurable session-persistence policy is not implemented.

Target limits are explicit, never silent scope/value changes:

- JMeter 5.5 XML can retain host-only flags, but its GUI cookie-table rebuild loses
  them; do not assume scope-preserving GUI edits. Persistent/expiring seeds are
  blocked because native manual-cookie matching does not reliably enforce expiry.
- LoadRunner's documented `web_add_cookie` requires Domain; host-only seeds are
  blocked rather than broadened. Its cookie scope/expiry and native API behavior
  remain SDK-unverified.
- Both targets restrict unsafe literal/metasyntax values and reject explicit
  SameSite seeds. HttpOnly metadata is retained, but native browser/JavaScript
  enforcement is not claimed. Per-target diagnostics describe capability refusal;
  another supported target may still be published.
- IPv6 cookie seeds are unsupported: the pinned JMeter API does not match the
  normalized seed domain to its bracketed URL host; the LoadRunner adapter also
  rejects that representation. This restriction does not reject IPv6 requests
  without initial IPv6 cookies. Native seed values use a bounded unquoted ASCII
  cookie-value subset; rejected values are not silently decoded or rewritten.

No custom JSON loader or runtime cookie extractor is added. `inspect` materialized
requests/generation diagnostics uses **default generation options**, not a prior
flagged run. Review the flagged run's `report.json`; for a truncated flagged `--check`,
repeat with the same options and `--output NEW_DIRECTORY` to obtain all diagnostics.

### JMeter artifact

`jmeter/scenario.jmx` is serialized structurally with an intended **JMeter 5.5 /
Java 8** baseline, checked against pinned `rel/v5.5` source contracts. Generation
does not perform native loading; separate synthetic-fixture evidence is described
below. It contains DebugTG (one thread/iteration, continue on
error), Module Controllers referencing final root Test Fragment names, Transaction
Controllers, HTTP samplers, a native Cookie Manager, Debug Sampler and View Results
Tree. There are no Backend Listeners, Cache Manager, timers, disabled samplers,
automatic redirects or embedded-resource downloads. Debug output can expose secrets.

Raw bodies are supported for POST/PUT/PATCH/DELETE. GET/HEAD/OPTIONS bodies are
rejected rather than moved into query parameters. Ordered duplicate headers are
retained. XML-invalid text and any literal `${` in native properties/display labels
are rejected to prevent recorded data becoming JMeter functions. CR/LF text is
preserved using XML character references. XML parsing alone is not native or wire
validation; `native_validation: not_run`, `compatibility: unverified` remain explicit.
POST/PUT/PATCH/DELETE also require an explicit materialized Content-Type, even for
absent or empty bodies: JMeter 5.5 would otherwise introduce a form Content-Type.
Rather than silently accept that known transformation, this target is blocked with
`JMETER_CONTENT_TYPE_REQUIRED`; LoadRunner may still produce its scaffold.

### LoadRunner intermediate source

`loadrunner/Action.c`, `vuser_init.c`, `vuser_end.c`, `globals.h` and `README.txt`
form a **C source scaffold, not a complete VuGen project**. No fabricated `.usr`,
run-logic or `default.cfg` is supplied. `project_status: incomplete` and
`template_status: pending` remain even when these files are successfully generated.
Public API documentation 25.1–25.3 supplies construction facts only; it is not an
agreed target version. A real Web HTTP/HTML template and native environment are
still required. Do not start load or claim compilation from this scaffold.

Requests use `web_custom_request`. Body bytes use `RAW_BODY_START` byte buffers,
which the documented API does not parameterize; braces, backslashes and UTF-8 data
remain body data. EncType reflects request Content-Type and does not encode bytes.
Other parameterizable URL/header attributes containing braces are blocked, as are
duplicate header names whose native behavior cannot be preserved by this profile.
Transaction/request names are generated safe IDs; original labels remain in the
model/report rather than becoming C source. Runtime cookie/cache cleanup and
continue-on-error are requested. Redirect/resource options are emitted, but native
acceptance and their actual effects are unverified. With selected cookie seeds,
cookie cleanup/addition return values are checked and failures logged without a
generated status override; native API failures may still have native status effects.
Cache cleanup and seedless cookie cleanup returns remain unchecked. System C
compilation is not a native VuGen compatibility test.

### Publication, partial results and provenance

A blocked target produces no partial native files. Another supported target can
still be generated; the command returns exit 3 if any requested target is blocked.
With `--output`, JSON/Markdown reports and a manifest are published even if all
targets are blocked. **A nonzero result may therefore leave useful partial output.**
`generated` means artifacts were rendered, not native-ready or replay-tested. For
LoadRunner it means intermediate C source only. An explicitly requested target
never disappears from the result.

Output publication reserves a new private directory and writes `manifest.json`
last, with hashes of all native/report files. It never overwrites manual outputs.
Failure may leave incomplete output: preserve it, verify any manifest and digests,
and choose a new destination. It is not a hostile-filesystem or universal power-loss
guarantee. Default stdout is value-free metadata; explicit materialization inspection,
native files and reports are **private/unredacted**.

The generated report (schema 1.3) links the pinned model revision and capture hash
to actual file digests and stable JMX component paths/C markers. It records constant
folding, header policy, per-target limitations and generation diagnostics. No source
map claims a runtime variable that was optimized away. Ordinary `report` remains
model-only and does not discover external output directories automatically; use
the report accompanying the generated artifact set to review that set.

Budgets: 1–1000 requests, 1000 headers/request and 10,000 total headers, 10,000
structured fields per selected query/form view, 20,000 materialization decisions,
1 MiB per textual/header view/body, 4 MiB
total body bytes and 8 MiB accumulated materialized request data. Each target has
a 32 MiB output budget (combined C files for LoadRunner); report format budgets
still apply. Limits produce explicit blocked/limit diagnostics, not hidden truncation.
For long diagnostic lists follow revision-pinned `diagnostics_continuation`.

## Scalar runtime correlation artifacts

The same `generate --check` / `--output NEW_DIRECTORY` commands select
`scalar_runtime_v1` when a model declares an extractor or a nonliteral variable.
Literal-only models keep `static_literals_v1` and its preparation-time folding.
In a runtime plan, **all** variables—including literals—follow their declared
initialization/reset events. No generated script is executed during preparation.

```sh
har2load inspect --workspace workspaces/demo --section runtime-plan
har2load generate --workspace workspaces/demo --check
har2load generate --workspace workspaces/demo --output workspaces/runtime-r4 --revision 4
```

`runtime-plan` is private/unredacted, paginated and revision-pinned. It contains
typed literal/variable segments, not magic placeholders to search for in recorded
strings. Every binding keeps its model ID and exact location. Bound recorded values
are not fallback live values. Unbound fields remain capture-derived data.

### Supported declarations and explicit limits

- Sources: `literal`, `extracted`, native `external` inputs and the five `generated`
  scalar kinds; scalar string, integer, number, boolean and null.
- Extractors: `response_body` with `json_path_subset_v1` or `boundary_v1`, including
  explicit cardinality/occurrence and `allow_empty`. Unreferenced extractors are
  still emitted and checked rather than silently discarded.
- `response_headers` with `name_v1` uses those same variables, bindings and lifecycle;
  header names have no special semantic rules. See the bounded native profile below.
- JSON extraction distinguishes missing from null and strings from numeric tokens.
  Duplicate keys, invalid Unicode, non-scalar results and resource-limit violations
  fail extraction; no binary-float conversion is used for decimal values.
- Boundary extraction follows Har2Load's first-left/nearest-right, non-overlapping
  algorithm. It is **not** a direct mapping to LoadRunner's last-left native boundary
  matching. LoadRunner captures the response and uses a controlled scanner.
- Destinations: exact path/query/form/header/text/JSON locations. JSON success retains
  scalar type; path/query/form encode only the substituted value. Unchanged encoded
  occurrences keep their spelling. JSON bodies with bindings are structurally
  serialized; text spans refer to original character offsets.
- Runtime binding to Content-Type, Content-Encoding, authentication, cookies or
  managed/Connection-nominated headers is blocked. Header names remain static.
  Unsafe live header values become binding error markers, not injected header lines.
- Agent-code producers require an explicitly opted-in supported target wrapper.
  No environment, external file, random generator or authored code is
  evaluated during preparation; declared dependencies are not read or downloaded.
- Response-cookie runtime extraction remains blocked; a persistent cookie jar is
  not treated as the current HAR response cookie array.

Existing static request prerequisites still apply: unsupported uploads, compression,
recorded Authorization headers and unresolved request representations are
not made safe merely by adding runtime variables. Target-specific errors can still
leave another target's files and diagnostic reports; exit status remains 3 if any
requested target is blocked.

### Native response-header extraction

`native_response_headers_v1` adds an ordinary source to existing correlation.
Declare `area: "response_headers"` and `selector: {"dialect": "name_v1", "name":
"X-Token"}` on the existing extractor. Any supported header name works; there are
no CSRF-specific rules, hardcoded recorded values or inferred refresh events.
The selector name must be an ASCII HTTP token of 1–256 characters. Matching is
case-insensitive, with existing exactly-one or explicit one-based occurrence
semantics. Duplicate fields retain their native-exposed order; commas are not split.

Values are **strings**, including `001`, `false` and `null`; incompatible declared
variable types fail rather than coercing text. Only edge SP/HTAB is trimmed. Internal
whitespace, colons and quotes remain data. Selecting `Set-Cookie` returns the entire
field as an ordinary string, not a cookie value; no cookie parser or jar lookup is added.

Only a declared producer attempt resets and refreshes its outputs. Unrelated responses
preserve saved values; declared lifecycle boundaries still reset them. Successful-native-
result gating is unchanged (`prev.isSuccessful()` / `h2l_rc == LR_PASS`). Missing,
ambiguous or invalid extraction leaves `ERROR_<name>` and value-free diagnostics,
continues, and does not generate a native-status override. Independent valid header
or body outputs survive another extraction/channel failure. A binding-local unsafe
header value does not invalidate the shared scalar or its valid JSON use.

The parser accepts one native-exposed header block, LF or CRLF, with an optional first
final-response status line and no required final blank separator. Visible folding,
bare CR, controls, leading blank framing, multiple status/response blocks and fields
after a blank terminator fail closed. Limits: 1 MiB capture, 1000 fields and 64 KiB
selected value. Selected values use bounded ASCII; valid UTF-8 non-ASCII unselected
values may remain. These are output/parser limits, not process-memory caps.

JMeter reads `SampleResult.getResponseHeaders()` independently of response-body
decoding. Its HTTP stack may already unfold or normalize headers. LoadRunner registers
a separate `Scope=Headers` capture before the producer and keeps body/header buffers,
registration results and cleanup independent. `H2L_RESPONSE_HEADERS` is reserved.
Exact SDK serialization, capture-overflow/status effects and HTTP wire parity remain
unverified; `NotFound=warning` does not establish native failure containment.
This profile is **not byte-identical HAR/browser/wire parity**. Preparation never
executes targets, and generated reports continue to say native validation `not_run`.

Datasets/CSV loading, row cursors and EOF policy are **outside the deterministic
core**, not a required next feature. If needed, an external agent handles that
integration. Existing native scalar initial inputs are separate and remain supported.

### Native initial inputs and generated sources

An external source optionally carries `initial_value`, a **string**, even for a
numeric/boolean/null variable. It is native configuration text parsed against the
declared scalar type at runtime, not an expression or a captured-value fallback.
Model strings must be valid UTF-8, NUL-free and at most 65,536 bytes. Absence differs
from an explicit empty string; `allow_empty` permits empty string values, not invalid
numeric input. Shared keys deduplicate equal initials; conflicting explicit strings
reject the final changeset. An absent declaration can share another variable's initial.
Redaction is off: explicit secrets are preserved in private native artifacts.

**JMeter:** initials live in the native Test Plan User Defined Variables panel, not
a custom runtime file. Missing keys are omitted; add a UDV entry to supply one.
Generated and manually edited values must follow `jmeter_udv_literal_v1`: double
every backslash, then prefix every `${` opener with a backslash. The runtime reverses
this encoding once as text. Do not enter raw JMeter functions. XML 1.0 unsupported
characters block generation rather than silently changing data. Native inputs are
snapshotted once per thread **before** runtime variable writes, preserving key/name
collisions and legitimate `ERROR_*` strings.
JMeter reserves variable names/input keys beginning `__jm__` or `__jmv_`; generation
rejects them instead of silently consuming or overwriting native controller state.

**LoadRunner:** `native_inputs.prm` and `data/input_<sha256>.dat` are native parameter
integration assets, **not a complete VuGen project**. Integrate them with a real
project's parameter file and retain relative data paths; no `.usr` is fabricated.
Only explicitly configured keys are read and snapshotted once per Vuser. Missing
initials produce markers without probing unknown SDK parameters. To add a previously
undefined key, provide `source.initial_value` and regenerate. Native project loading,
missing/corrupt files, encoding and parameter-existence behavior are not certified by
synthetic C stubs. Unsupported table representations block that target, not the model
or an otherwise supported JMeter output. Consult generated `loadrunner/README.txt`.
The current table subset is nonempty printable ASCII, without quotes, commas or
leading/trailing spaces; empty, Unicode and multiline initials are blocked. The
native capture keys `H2L_RESPONSE` and `H2L_RESPONSE_HEADERS` are reserved. Runtime edits of configured parameters
still undergo strict UTF-8/type/size checks. This is not proof of arbitrary native
table encoding or quiet missing-parameter detection.

`native_initial_snapshot_v1` reads those snapshots at declared initialization events.
Missing/invalid values emit `ERROR_<name>` and value-free diagnostics, continue, and
never override native status; there is no external-input preflight stop. Native loader
failures are not claimed to be contained. Dataset preflight/EOF is outside this core.

`per_user_generated_scalars_v1` uses per-user state and computes only at declared
lifecycle events, never per binding: inclusive signed-64 integers (including the full
range), printable-ASCII strings, lowercase UUIDv4, UTC second-precision timestamps
with the declared offset, and inclusive ISO date ranges in years 0001–9999.
Bindings reuse saved values. There is no cryptographic, global-uniqueness or
identical-cross-target-stream guarantee. Preparation computes no live values.

### Lifecycle and approved failure-wire behavior

Session initialization is per user/thread; iteration initialization repeats per
iteration. `before_request` / `after_response` initializers remain attached to their
declared request. Scope reset does not turn initialization into an implicit once-only
operation. Producer outputs are cleared before another extraction attempt, including
transport failure paths; there is no previous-iteration or captured-value fallback.

**`error_marker_json_string_v1`:** failed values produce `ERROR_<name>` and explicit
failure diagnostics while the debug scenario continues. For a failed JSON binding,
the marker is a **quoted JSON string even when the declared success type is number,
integer, boolean or null**. This deliberate, owner-approved exception applies only
to failure. It is not typed success, null/zero substitution, or permission to change
successful JSON types. A legitimate extracted string equal to `ERROR_<name>` remains
distinguishable internally from failure.

**`markers_only_native_status_v1` (owner-approved policy change in 0.8):** extraction,
initialization, binding, block and helper errors change variable state and diagnostics,
not native sample/transaction success. JMeter adds no correlation failure assertion
and never forces a result successful or unsuccessful. LoadRunner ends transactions
with `LR_AUTO` and logs generated correlation diagnostics through `lr_output_message`,
not `lr_error_message` (which can fail transactions through a runtime setting).
Native transport errors and user-authored assertions are not hidden or forced green.
This supersedes 0.7's explicit native failure marking; regenerate artifacts to adopt
the new policy. Rejection at one header binding does not erase a valid shared scalar.

### Native construction and verification boundary

JMeter uses fixed inline cached JSR223 processors and a per-thread state object;
configuration is encoded data, not interpolated Groovy source. Request fields are
installed programmatically so captured/live `${...}` is not deliberately evaluated
as JMeter functions. JSON parsing uses the Jackson Core already bundled with the
intended JMeter 5.5 distribution, not an automatically downloaded dependency.
The debug layout, enabled Debug Sampler and View Results Tree remain intact.
If a whole request field cannot be assembled, the JMeter sampler uses an invalid-URL
failed-sample sentinel rather than sending a partial/stale request or falling back
to localhost. This is an explicit safety failure, not an automatically disabled sampler.

LoadRunner emits controlled C parsing/encoding and lifecycle code alongside the
existing `web_custom_request` scaffold. JSON numeric values are retained without
`double` conversion; runtime bodies still use non-parameterized RAW_BODY buffers.
It remains an **incomplete VuGen project** with unknown target version, unverified
native state isolation and no `.usr`/run-logic template. System C compilation would
not establish VuGen compatibility.

Both engines are bounded, deliberately conservative profiles—not interchangeable
general-purpose JSON libraries. Response/body and field limits are 1 MiB, scalar
values 64 KiB, JSON nesting 64 containers, and boundary matches at most 1000.
Per-target numeric, Unicode, response decoding and allocation limits are recorded
in generated target metadata. Limits are not a total process-memory guarantee.
JMeter accepts strict UTF-8 responses (missing charset is treated as UTF-8), rejects
other charsets and extraction from unsuccessful HTTP samples, and bounds decimal
scale to ±65536. Known literal scale violations block preparation. LoadRunner rejects
embedded NUL, limits parsed response JSON to 4096 nodes and 1024 keys per object,
and limits response numeric exponents to ±10000. Its request-field preflight uses
worst-case encoded size for every slot, so a field may be blocked even when expected
live values are small—for example, three maximum-size JSON slots exceed 1 MiB.
Native representation details such as numeric spelling/negative zero may differ;
successful JSON numeric values are not intentionally converted through binary floats.

The runtime report uses schema **1.7**; static generation uses 1.3 and ordinary
model-only `report` remains 1.1. Runtime maps link each variable, initializer, reset,
extractor and binding to actual native locations and file hashes; missing, duplicate
or unexpected declaration mappings refuse publication. Removed mappings remain in
revision history. Reports disclose failure-wire policy and per-target implementation
status separately from captured extraction checks. The scalar runtime profile now
reports `profile_version: "5"` for native response headers. Authored wrappers use
`authored_scalar_runtime_v1`, version 4, with `per_output_validated_v1` publication.

**Runtime correctness/native compatibility is not certified by these artifacts.**
The default Python `tests/` suite inspects structure, data, ordering and contracts;
it does not execute Groovy/C, load JMeter, invoke VuGen or replay HTTP. `native_validation`,
`runtime_validation` and execution remain `not_run`. Live debug output may contain
secrets; redaction is still off. Native acceptance remains a separate explicit step.

### Separate synthetic execution harness

The opt-in developer suite in [`validation/`](validation/README.md) runs outside
ordinary preparation. It has exercised generated JMX loading/module resolution and
fixed JSR223 processors on **JMeter 5.5 / Azul Zulu Java 8u512**, macOS ARM64, with
synthetic response objects and an OS-enforced network prohibition. It does not
start a JMeter engine or sample HTTP requests. Successful fixtures do not certify
all scenarios, full thread-engine behavior, GUI operation or wire parity.

The same suite compiles and executes emitted C helpers/parser and lifecycle code
using **Apple Clang 21, ASan/UBSan and deliberate synthetic SDK stubs**. This is not
VuGen compilation, SDK ABI, concurrent-Vuser isolation or native transport evidence.
Sanitizer and processor-error negative controls check the test oracles themselves.

These tests require explicit `HAR2LOAD_NATIVE_VALIDATION=1` and a supported
network-denying sandbox; they skip without opt-in and refuse unsandboxed fallback.
Tool acquisition is separate, verified setup—not an implicit download in tests or
the CLI. No real HAR, captured endpoint, authored block or declared dependency is
used. Developer-fixture results do not update a workspace or turn its generated
artifact manifest's `not_run` statuses into a native acceptance claim.

## Candidates and evidence reports

```sh
har2load analyze --workspace workspaces/demo
har2load inspect --workspace workspaces/demo --section candidates
har2load inspect --workspace workspaces/demo --section analysis-diagnostics
har2load report --workspace workspaces/demo --format markdown
har2load report --workspace workspaces/demo --revision 3 --compare-revision 1
har2load report --workspace workspaces/demo --output workspaces/report-r3
```

`analyze` is read-only. Its paginated summary omits captured values and locations;
explicit `inspect --section candidates` or `analysis` exposes private evidence.
The `scalar_candidates_v1` profile finds typed scalar equality in recorded sources
and later requests. It retains alternative producers and all discovered consumers,
with per-alternative timing status. A response that finishes after a consumer
starts, or lacks timing, cannot establish temporal availability. Even available
timing is **not proof of causality**. Short/common values are low-priority evidence,
not silently accepted correlations. No variables, selectors or bindings are applied.

The bounded profile covers header/cookie response values, JSON scalar leaves and
whole textual response bodies. Request locations include headers, path segments,
query values, JSON leaves, forms and whole text bodies. Query/form decoding and path
percent-decoding are contextual and labeled; numbers, strings, booleans and null
are not implicitly interchangeable. Unsupported or unresolved representations,
malformed/missing data and exhausted budgets appear in coverage/diagnostics. There
is no arbitrary token mining, regex inference, semantic causality inference or
candidate acceptance/rejection workflow yet. `complete` coverage refers only to
this bounded profile, not to all possible correlations. Consult `analysis.limits`
and `analysis.coverage`; a partial result never establishes absence of other matches.

Default budgets: 10,000 enumerated slots, 10,000 JSON nodes, 1,000 candidates,
10,000 source-consumer links, 1 MiB per text, 4 MiB cumulative scanned UTF-8 text
and 8 MiB accounted candidate payload. Accounting is deliberately conservative
(including unmatched producers and repeated view scans). Limits are configurable
through the Python `AnalysisLimits` API; the CLI uses the versioned defaults.
When a budget interrupts discovery, a consumer's alternatives are retained as a
complete set or not published; the undiscovered remainder is unknown. Candidate
IDs hash source locators, not values; IDs alone do not imply unchanged evidence
across revisions. Invalid path data does not suppress an independently selected
query representation.

`report` renders the audited model, not a separate narrative written from memory.
It includes all scalar declarations, source/generator settings, lifecycle, exact
uses, author/rationale/evidence, sensitivity annotations, recorded extraction
results, unreferenced extractors, unresolved candidates, conflicts and diagnostics.
Source-map entries remain `not_generated`; targets are `not_generated` or `blocked`
when a required counterpart is missing. No JMX/C aliases or component locators are
fabricated. Opaque code/helper declarations, source digests, declared calls and
runtime dependencies are included; their execution remains explicitly unverified.
Dataset integration is outside the deterministic core. Source annotations,
purpose text and captured values are data, never instructions to execute.

The JSON and Markdown pair describes the same pinned revision. Markdown uses
indented JSON evidence blocks to prevent captured HTML, links or fences becoming
active markup. The SHA-256 of the **exact complete `report.json` bytes** identifies
the review artifact; Markdown and the export manifest reference it. Reports do
not assert that a critic has reviewed them (`review: not_recorded`). Any change to
revision, report content, analyzer profile or tool version can change this digest.

Without `--output`, the JSON envelope contains a bounded **unredacted** character
fragment (`--format json|markdown`, `--offset`, `--limit`); follow `continuation`
unchanged to preserve the revision and comparison baseline. The default diff is
against the previous **model revision**, or an empty baseline at revision 1—not a
claim that a previous report file exists. `--compare-revision` selects an earlier
revision explicitly. Added/changed/removed variables, extractors and bindings are
reported, including before/after changed declarations and removed locations.
Report schema 1.1 also diffs code blocks/helpers, preserves removed call-site
dispositions and distinguishes declared code semantics from recorded evidence.

With `--output NEW_DIRECTORY`, Har2Load writes `report.json`, `report.md` and finally
`manifest.json` with file digests. The directory must not exist; its parent must
exist. Fragment options cannot be combined with export. The CLI returns metadata,
not private report values. Files are private (0700 directory/0600 files where the
platform supports these permissions). **Redaction is OFF**: reports may contain
credentials and are not safe to publish. Export is not a runnable handoff bundle.

Publication is manifest-last, not atomic directory replacement. A failed write may
leave an incomplete directory; do not treat directory existence as success or
retry into it. Preserve it for inspection and use a new destination. If a failure
occurs after manifest creation, verify all digests before trusting it. Existing
files/symlinks/manual outputs are never overwritten. Reports are limited to 32 MiB
per format; exceeding the limit is an explicit `REPORT_LIMIT`, not silent truncation.
Serialization/rendering stop accumulating output at this budget; it is not a
guarantee that total process memory stays under 32 MiB.
Report command success means the report was produced, **not** that its recorded
checks passed or its target scripts are ready; inspect its independent statuses.

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
  native input, initially absent. Scenario 1.5 permits optional `"initial_value":"text"`;
  preparation never reads the environment or a secrets file.
- `{"kind":"extracted","extractor_id":"ext_order"}` — a recorded producer.
- `{"kind":"agent_code","behavior_id":"make_order_id"}` — a scalar output of
  explicit target code blocks (scenario 1.3); no local evaluation or inferred value.
- `{"kind":"generated","generator":"integer","minimum":1,"maximum":100}` —
  inclusive bounds; native generation requires signed-64 endpoints.
- `{"kind":"generated","generator":"string","length":12,"alphabet":"abc123"}` —
  distinct printable ASCII alphabet, length 1–4096.
- `{"kind":"generated","generator":"uuid"}`.
- `{"kind":"generated","generator":"utc_datetime","offset_seconds":0,"format":"iso8601"}`.
- `{"kind":"generated","generator":"utc_date_range","minimum":"2026-01-01","maximum":"2026-12-31","format":"iso8601"}`.

Generated sources are **not evaluated during preparation**. Native implementations
exist in the bounded profiles; engine/wire parity remains unverified. `purpose` is a
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
policy. These are stateless captured-fixture checks, **not runtime validation**.
The scalar runtime generation profile emits separate reset/continue code, with the
explicit JSON failure-string policy described above; native behavior remains unverified.

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
changing raw bytes. Static generation folds literal bindings; scalar runtime generation
emits typed templates and native formation code. Their capability limits and unverified
native parity are separate from accepting a binding into the model.

Limits: 256 variables, 256 extractors, 1024 bindings; 1 MiB UTF-8 per local body/JSON
check and active textual binding view; 64 JSON containers/path tokens; 256-character
selectors/delimiters; 1000 matches. Overflow is unverified, not a truncated success.
Literal strings are limited to 65536 characters. Unknown source/selector dialects
are rejected, not silently approximated. Expression/collection
support and the bounded regex profile remain future work in the full specification.

## Opaque target code and helpers

Declarations remain **unverified authored contracts and source**. Explicit ABI opt-in
can additionally emit wrappers; opaque legacy declarations remain blocked. No
Groovy/C interpreter, compiler, native validator, plugin loader or dependency
resolver runs during import, changes, validation, inspection, analysis or report.
Source is untrusted data. Structurally valid code may still be incorrect, unsafe,
non-compiling or behaviorally different across targets. `code_semantics` remains
`unverified`; authored effects and thread-safety annotations are not a sandbox.

New scenarios include `target_extensions: {blocks: [], helpers: []}`. On scenario
1.2, prepend `{"op":"upgrade_extensions"}` to a changeset. On 1.1, prepend
`upgrade_parameters`, then `upgrade_extensions`, in that order. Existing names,
selections, parameters and historical revisions are preserved. Upserts replace
whole declarations; removal never cascades. All references are checked after the
complete batch, so definitions may appear after their consumers in that batch.
For scenario 1.3 use `{"op":"upgrade_extension_runtime"}` before adding
`"runtime_abi":"scalar_body_v1"` to a block or helper. Earlier schemas require the
preceding upgrades in order. The upgrade preserves source/digests and leaves ABI
absent on existing declarations. Opt-in is a separate upsert, not inferred from code.

### Code blocks

An opaque authored `block` has exactly these fields (example is **not a working
script**). Scenario 1.4 additionally permits the optional `runtime_abi` field:

```json
{
  "id": "make_id_jmeter", "behavior_id": "make_id",
  "target": "jmeter", "language": "groovy",
  "source": "// Replace with an explicitly authored implementation.\n",
  "placement": {"event": "before_request", "exchange_id": "EXCHANGE_ID"},
  "order": 10, "depends_on": [], "inputs": [], "outputs": ["order_id"],
  "helper_calls": [], "dependencies": [], "side_effects": ["unknown"],
  "failure_policy": "mark_error_and_continue",
  "failed_input_policy": "skip_and_mark_outputs", "reset_outputs": true,
  "purpose": "Author explanation of the output algorithm and its limitations"
}
```

Put this object in `{"op":"upsert_code_block","block":...}`. Declare `order_id`
as a scalar variable whose source is `{"kind":"agent_code","behavior_id":"make_id"}`
and whose `initialize` exactly matches the block's placement. Add a separate block
with the same `behavior_id`, `target: "loadrunner"`, `language: "c"` for the other
target. The actual source is independently authored—not translated automatically.
Each existing counterpart must declare every output variable of that behavior.
At least one producer must exist; otherwise the model has a dangling source.

Supported targets/languages are only `jmeter`/`groovy` and `loadrunner`/`c`.
Placement is currently `before_request` or `after_response` at a stable exchange
ID. Group placement is derived from that exchange. Session/init/iteration blocks,
standalone samplers, arbitrary native nodes and a registration placed before a
request with delayed output after its response are outside this initial profile.
Baseline extractors' eventual native registration remains generator work.

`inputs`/`outputs` name variable IDs; they must be disjoint. Only `agent_code`
variables of the same behavior may be outputs. The variable's type/scope/reset
remain in its declaration. Request-scoped inputs cannot cross exchanges.
`depends_on` names same-target blocks at strictly earlier declared positions;
`helper_calls` names same-target helpers. No call is inferred by scanning source.

Scheduling is a **declared generator contract**: baseline variable and
extractor actions precede code blocks at the same event; blocks run in ascending
`order`, then before-request bindings read their saved outputs. Equal-event code
inputs need an earlier same-target producer. Positions cannot share an order;
use-before-initialization, cycles and duplicate writers are rejected. Initializers
repeat each time their event occurs; no hidden once-only behavior is implied.
Explicit ABI wrappers implement this schedule; source semantics remain unverified.

Only `mark_error_and_continue`, `skip_and_mark_outputs` and `reset_outputs: true`
are accepted. Wrappers clear outputs, avoid stale values, skip a block
when a known failed input makes it unusable, mark outputs `ERROR_<variable>` and
continue debugging. `mark_error_and_continue` describes variable errors, not native
sample/transaction failure. Each validated output is published independently:
already-valid outputs survive another output failure or a later handled body error.
Missing/invalid outputs retain markers, and legitimate null differs from no output.
A marker is not typed success. Preparation does not simulate the body. Wrappers
are not fault containment for native crashes, undefined behavior or infinite loops.

One implementation may be saved while the other is still missing. Storage and
reports remain available; `validate` exits 3 and marks **only the missing target's
extension contract** blocked. Valid contracts do not establish native capability
or readiness. Even side-effect-only behaviors with no outputs need a counterpart
for both targets. Counterparts may use different inputs/helpers/source; equivalent
business behavior is not proven by matching `behavior_id`.

### Helpers and runtime dependencies

An opaque authored `helper` contains exactly the following fields; scenario 1.4
also permits optional `runtime_abi: "scalar_body_v1"`:

```json
{
  "id": "normalize_jmeter", "target": "jmeter", "language": "groovy",
  "source": "// Explicitly authored helper implementation goes here.\n",
  "namespace": "scenario_helpers", "symbol": "normalize",
  "parameters": [{"name": "value", "value_type": "string"}],
  "return_type": "string", "depends_on": [], "dependencies": [],
  "side_effects": [], "stateless": true, "thread_safe": true,
  "purpose": "Declared callable contract; behavior is not locally verified"
}
```

Use `upsert_helper` with `helper`. Helpers declare scalar parameter/return types
(`void` return is also allowed), not per-user variable references. Helper dependencies
form a same-target DAG, registered in deterministic topological order. Namespace/
symbol pairs must be unique per target; C symbols must also be unique across
LoadRunner namespaces. Identifier spelling is checked, not native compilation.
The intended placement is JMeter setup registry / LoadRunner helper definitions;
preparation emits source for registry/functions but never creates native runtime
objects. JMeter code remains inline in JMX, not external `.groovy` files.

`stateless: true` and `thread_safe: true` are required declarations, not verified
properties. Helpers cannot declare `shared_state` effects. Other allowed effect
labels are `network`, `filesystem`, `process`, `environment`, `unknown`; blocks
may also declare `shared_state`. An empty effects list is an author claim, not
proof that source has no other effects. Helper calls and typed arguments inside
opaque source are not parsed or type-checked against these declarations.

Dependencies are objects such as `{"kind":"jar","path":"lib/example.jar"}`;
`csv` and `file` kinds are also supported, `jar` is JMeter-only. Paths must be
relative POSIX paths without empty, `.` or `..` segments, drive colons, backslashes
or control characters. Files are never opened or downloaded to satisfy declarations.
Reports list them as `required_at_runtime` / `not_checked`, not as bundled assets.

Har2Load adds `change_id` and `source_digest` (SHA-256 of exact UTF-8 source bytes).
Do not supply either in authored records. Line endings and whitespace are preserved;
stored digest tampering is rejected. All block/helper/parameter record IDs are
globally unique. Source changes alter revision/report identity and invalidate prior
review; no approved status can be injected through an authored declaration.

Limits: 128 blocks, 64 helpers, 65,536 UTF-8 bytes per source, 2 MiB total source;
order 0–10,000; 256 inputs and outputs, 64 helper calls, 16 helper parameters and
32 dependency declarations per record. Existing changeset/history/report budgets
still apply and may be reached first. Source cannot be blank, invalid UTF-8 or
contain NUL. This is deliberately not a universal transformation DSL.

```sh
har2load changes validate extensions.json --workspace workspaces/demo
har2load changes apply extensions.json --workspace workspaces/demo
har2load validate --workspace workspaces/demo
har2load inspect --workspace workspaces/demo --section target-extensions
har2load inspect --workspace workspaces/demo --section code-blocks
har2load inspect --workspace workspaces/demo --section helpers
har2load inspect --workspace workspaces/demo --section extension-plan
```

Default validation/summary omit source text and dependency paths. Explicit inspection
and reports contain private source, effects, provenance, source digests, declared
helper call sites, scheduling, and dependencies. They are not safe public handoffs.

### `scalar_body_v1` invocation API

Opted-in `source` is a **function body**, not a whole function, class or file.
Existing source must be adapted explicitly; an ABI-only upsert does not rewrite it.
The body remains opaque during preparation—even syntactically invalid code can be
serialized. Any declared external dependency currently blocks wrapper generation;
it is not silently ignored, fetched or bundled. Full source is private report/artifact
data, never included in public generation-summary metadata.

**JMeter:** the generated `Authored setup` group compiles source from base64 JMX
configuration into isolated classes once per run and replaces a revision-specific
registry in `props`. No external `.groovy` files or per-request compilation are used.
Base64 prevents JMeter `${...}` expansion from changing source; Groovy string rules
still apply. Shared helper implementations do not inherit setup-thread bindings.

Block bodies receive `inputs` (read-only variable-ID map), `emit`, `helpers`
(declared helper-ID callables), and `context` (explicit native `vars`, `ctx`, `prev`,
`sampler`, `log`). For declared input `raw`, output `normalized`, and helper `clean`:

```groovy
emit('normalized', helpers.clean(inputs.raw))
```

The helper's arguments are positional in declared parameter order. A string helper
with parameter `text` could have this body:

```groovy
return inputs.text.trim()
```

Helpers receive only `inputs` (parameter-name map) and their declared dependency
`helpers`. Arguments and return values are validated; `void` requires null.
Block return values are ignored: only explicit `emit` publishes outputs. Strings
must be `String`, not GString; numbers accept BigDecimal, BigInteger and boxed
integral types, not Float/Double or numeric strings. Boolean/null remain distinct.
Use `.toString()` explicitly for intended GStrings. Strict UTF-8 and scalar limits
apply. A missing output differs from an explicitly emitted null.

**LoadRunner:** bodies are inserted into hashed static functions with an
`h2l_frame *frame` argument. Return 1 for success, 0 for a reported failure;
falling through returns 1. Helper declaration names never become unchecked C
function identifiers. The supported body API is:

```c
const h2l_value *h2l_input(h2l_frame *frame, const char *id);
int h2l_emit_text(h2l_frame *frame, const char *id, const char *text);
int h2l_emit_text_n(h2l_frame *frame, const char *id, const char *text, size_t n);
int h2l_emit_json(h2l_frame *frame, const char *id, const char *json);
int h2l_emit_json_n(h2l_frame *frame, const char *id, const char *json, size_t n);
int h2l_call_helper(h2l_frame *frame, const char *helper_id,
                    const h2l_value *const *arguments, size_t count, h2l_value *out);
void h2l_release(h2l_value *value);
```

Block inputs/outputs use variable IDs. Helper inputs use parameter names, and a
non-void helper publishes its one result with output ID `result`. Text emit copies
a string; JSON emit parses one complete scalar and validates its type. Numbers
retain exact JSON lexemes; numeric strings are not silently converted. Both inputs
are bounded to 65,536 bytes and reject NUL. A string-copy helper body can be:

```c
const h2l_value *value = h2l_input(frame, "text");
if (!value) return 0;
return h2l_emit_text(frame, "result", value->data);
```

Inputs are borrowed/read-only; never free or mutate them. `data == NULL` means
unavailable, not JSON null. For helper calls, pass a separate zero-initialized
`h2l_value result = {0}`, then call `h2l_release(&result)` on all paths. Non-void
calls require an output; void calls allow NULL. The argument array must contain
at least `count` readable entries, even for rejected calls; pointers/allocation
extents remain the author's responsibility. Helpers may call only declared
dependencies. Calls validate arity/types and do not return stale results on failure.

A block calling a declared string helper `clean` can use:

```c
h2l_value result = {0};
const h2l_value *args[] = {h2l_input(frame, "raw")};
int ok = h2l_call_helper(frame, "clean", args, 1, &result);
if (ok) h2l_emit_text(frame, "normalized", result.data);
h2l_release(&result);
return ok;
```

**Both targets:** before every block invocation all outputs reset. Each valid emit
commits only its own output; an invalid replacement invalidates only that slot.
Other successful outputs survive later handled errors. No correlation error changes
native sample/transaction status. Authors can still directly change native state or
perform unsafe operations: these wrappers are not a sandbox, execution approval,
fault-containment mechanism, or proof of declared thread safety. C allocation
ownership and native Vuser isolation remain separate responsibilities/limitations.

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
python -m pip install --no-index --find-links wheelhouse har2load==0.11.0a1
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
