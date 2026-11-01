# Har2Load

Offline HAR inspection and scenario preparation. **Early alpha: the first
working slice, not a complete load-test generator.**

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
- Private initial workspace, paginated metadata, and bounded selective inspection.

**Not implemented yet:** editable scenario revisions, explicit boundary overrides,
filters, correlation candidates, variables/bindings/datasets, mandatory variable
reports, agent extensions/helpers, both generators, export/handoff/redaction,
author/critic skills, native validation, and replay. Both JMeter and LoadRunner
remain required for the first complete release. Alpha workspaces are not a stable
interchange format; this version rejects other producer versions rather than
silently migrating them.

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
  `continuation` until it is null. All fragments include the capture digest.
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
- Import/summary include at most ten diagnostics. `diagnostic_count` and
  `diagnostics_continuation` identify remaining diagnostics; inspect that section
  to page through all of them. Top-level `truncated` also reflects this omission.
- Logs are off by default; `har2load --verbose import ...` enables value-free
  progress messages on stderr. Successful operations and expected errors produce
  JSON on stdout; help and version are plain text.

Exit codes: `0` operation completed (possibly with source diagnostics), `2` bad
input/arguments/limits, `3` invalid or unsupported workspace, `4` existing
destination, `5` I/O failure, `1` unexpected internal failure. Import succeeds for
diagnosable incomplete entries; this does **not** certify an executable scenario.

## Preservation and limitations

The workspace contains `capture.har` (original bytes), `scenario.json` (immutable
initial grouping), and `manifest.json` (version, checksums, explicit redaction-off
state and privacy warning). The manifest is published last. Failed creation can
leave an incomplete directory; inspect it
manually and choose another destination. There is no automatic deletion,
overwrite, migration, or manual-edit import. Copying the whole workspace does not
depend on its original absolute path.

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
python -m pip install --no-index --find-links wheelhouse har2load==0.1.0a1
# Optional development installation:
python -m pip install --no-index --find-links wheelhouse -r requirements-dev.txt
python -m pip install --no-index --no-build-isolation --no-deps -e .
```

Keep wheelhouses outside the repository, or ensure they are not committed. These
commands prepare/install artifacts only; they do not execute a load test.

## Legacy converter

`main.py` is unchanged and separate from the new package. It still needs
`tiktoken` and `PyYAML`, may initialize/download tokenizer data even for help, and
retains its old decoding/filtering behavior. The new package neither imports it
nor depends on its tokenizer or output format. Legacy installation, if needed:

```sh
python -m pip install tiktoken PyYAML
python main.py --input capture.har --output optimized --format json
```

The legacy `--no-cookies` option is **not sanitization**. Its stdout may include
URLs and a prompt; that is distinct from the new CLI's JSON envelope.
