# Explicit synthetic system-C runtime validation

This suite executes the **actual emitted** LoadRunner C helpers/parser and generated
`Action.c`, `vuser_init.c`, and `vuser_end.c`. It is a separate, explicitly authorized
validation phase, not ordinary preparation. It uses synthetic plans/fixtures only;
it never reads HARs, authored blocks, captured endpoints, real SDKs, or VuGen projects.

**Passing is not VuGen compatibility, SDK ABI, per-Vuser isolation, or wire-parity
evidence.** `lrun.h`, `web_api.h`, and `stubs.h` define a deliberate test contract,
not replicas of an SDK. No production metadata or readiness claims are changed.

## Run

From the repository root, choose a new artifact directory below the approved root:

```sh
ROOT=/PRIVATE/NEW/har2load-c-runtime
mkdir -p "$ROOT"
HAR2LOAD_NATIVE_VALIDATION=1 .venv/bin/python -m pytest validation/c_runtime \
  --basetemp "$ROOT/pytest-new-run" -q
```

Use a **new** basetemp name to preserve previous evidence; pytest clears a reused
basetemp. The suite skips without opt-in and is outside ordinary `tests/` discovery.
Shared `validation.support.run_isolated` handles every compiler/binary process;
the shared autouse fixture first requires loopback connection denial with `EPERM`.
macOS `/usr/bin/sandbox-exec` denies all networking. There is no unsandboxed fallback.

Compiler flags: `-std=c99 -g -O1 -fsanitize=address,undefined
-fno-omit-frame-pointer -fno-sanitize-recover=all`. Sanitizers halt on errors.
Two intentional negative controls prove that heap-buffer overflow and signed
integer overflow actually fail under these settings. Their expected error logs
are retained separately. LeakSanitizer is disabled on macOS; emitted allocations
are counted via transparent malloc/calloc/free wrappers, and every tested cleanup
must balance. This is not a general leak or resource-bound proof.

## Coverage and rationale

| Risk | Executed evidence |
| --- | --- |
| Incorrect scalar interpretation | Root strings/numbers/booleans/null, strict type acceptance, lossless large numeric lexemes, integrality, negative zero, missing versus null |
| Accepting malformed/unselected JSON | Syntax, decoded duplicate keys including unselected subtrees, trailing data, invalid escapes/surrogates/UTF-8, decoded NUL rejection |
| Resource exhaustion/off-by-one | Exact depth 64, nodes 4096, keys 1024, exponent ±10000, value 65536 bytes, field 1048576 bytes, and over-limit rejection |
| Wrong selectors | Escaped/dotted/Unicode keys, arrays/root, empty strings, absent/type-mismatched paths, huge indices, emitted selector integration |
| Boundary mistakes | First left/nearest right, nested lefts, exact cardinality, occurrence, empty policy, match/value overflow, generated boundary extraction |
| Wire-value construction | Path/form/text/JSON/header encoding, literal URL segments, exact typed successes, quoted failure markers, present/non-NULL empty raw body |
| Silent failures/stale state | Repeated Action calls, early consumer before producer distinguishing retained session from expired iteration state, producer invalidation, request expiration, session restart, after-response literals despite HTTP failure; negative control removes only the Action-start iteration reset |
| Incorrect failure metrics | Binding-local header rejection without shared-value corruption; explicit LR_FAIL independent of logging; unbound extractor tested both bad-first and good-first, fresh good extraction still available, later clean group remains LR_AUTO |
| Capture ownership/errors | Scheduled HTTP data separate from evaluated parameter storage; registration-at-HTTP and one-shot publication controls; effective save/clear; non-terminated native buffers, explicit lengths, NUL/UTF-8/size rejection, release on failure, failed registration/max length/save/eval/HTTP, post-capture reset failure |
| OOM/cleanup faults | Fail each emitted allocation in the successful lifecycle, parser allocation-failure sweep, no stale values, request continuation, transaction failure, balanced cleanup |
| Parser surprises | 160 seeded structured cases plus truncated counterparts; 800 seeded ASCII mutation cases against independent Python JSON/Decimal, duplicate-key, Unicode/NUL oracles |

Helper inputs are hex data files, never interpolated C. Lifecycle fixture strings
use an independent all-octal UTF-8 encoder. Helpers are included directly from the
production emitter; positive lifecycle tests include the emitted files without
rewriting their bodies. Allocator calls are intercepted for counting/fault injection.
One explicit negative control removes exactly one Action-start iteration reset
from a temporary synthetic generated artifact; it must fail the early-consumer
assertion with exit 3. The original artifact is retained. Production is never changed.
Stubs snapshot request fields, headers and transaction status deterministically;
logging alone cannot mark a transaction failed.

The capture contract intentionally distinguishes `scheduled_response` (HTTP fixture)
from `response`/`response_size` (evaluated parameter view). Registration does not
publish data; only the next successful HTTP while registered copies the scheduled
bytes into bounded `saved_parameter` storage. Every HTTP consumes the registration,
including a failed HTTP. Unregistered/failed HTTP preserves prior parameter storage,
so preseeded stale data and effective clearing can be tested. Successful saves update
that same evaluated storage; failed saves leave it unchanged. Evaluation allocates
a separate buffer of the explicit byte length, without a NUL terminator, even when
returning the injected failure status; callers must release it.

The existing helper-only capture driver directly seeds the evaluated parameter view
to test arbitrary native byte buffers independently of HTTP. That low-level seam is
not used to supply lifecycle responses: all lifecycle fixtures use scheduled data
and must pass through registration and HTTP publication. This small synthetic
contract deliberately does not emulate all native SDK behavior.

## Verification record (2026-11-04, Har2Load 0.7.0a1)

- Apple clang 21.0.0 (`clang-2100.1.1.101`), `arm64-apple-darwin25.6.0`.
- Original explicit suite: **35 passed** (16.20s), including independent fixture encoding.
- After three critic S3 harness findings: **49 passed** (23.82s).
- Without opt-in after remediation: **49 skipped** (0.02s), no C compilation/execution.
- Ordinary regression suite in the original validation phase: **1098 passed** (2.29s);
  not rerun during this harness-only remediation.
- `ruff check validation/c_runtime`: passed after remediation.
- No production-code defect reproduced in these runs; no source fixes requested.

Remediation evidence persists in three new directories under the approved private
artifact root (old basetemps were not reused):

- `critic-s3-red-1`: **3 failed** (1.81s), before the stub fix. Save, clear, and
  failed-save controls reproduced the evaluated-storage disconnect. Each includes
  `stub-contract-snapshot.h`, the binary, and `contract-failure.txt`.
- `critic-s3-targeted-1`: **15 passed, 22 deselected** (8.42s): eleven capture
  contract controls, both extractor orders, early-consumer positive/control tests.
- `critic-s3-full-1`: **49 passed** (23.82s), the full fresh C suite. The iteration
  mutation control exited 3 with `EARLY_ITERATION_RESET: session persists, iteration expires`;
  both existing sanitizer negative controls also passed.

Reproduction commands (with a private `ROOT`; use new basetemp names):

```sh
HAR2LOAD_NATIVE_VALIDATION=1 .venv/bin/python -m pytest validation/c_runtime -q \
  --basetemp "$ROOT/critic-s3-full-NEW"
.venv/bin/python -m ruff check validation/c_runtime
.venv/bin/python -m pytest validation/c_runtime -q
```

Independent read-only critic re-review subsequently returned **APPROVE / PASS**,
closing all three S3 findings. The lead independently reran all 49 C tests together
with the JMeter batch in a fresh `integrated-run-2`: **50 passed** (59.02s).
The final default suite passed **1101 tests**, Ruff and mypy passed, and the complete
native suite skipped **50 tests** without opt-in. Production source remains unchanged
from `b4e5010775d2fa32e60bf3f05d3a6dfdb3f61ef8`.
Findings addressed harness weaknesses; no production defect or fix is claimed.
Each build contains
`driver.c`, its binary, `compiler.txt`, and `compile.txt`; lifecycle builds also
retain emitted files and `execution.txt`. Helper builds retain `.cases` inputs and
`.out` outputs. Negative controls retain `expected-sanitizer-failure.txt` or
`Action.original.c`, `mutation.txt`, and `expected-mutation-failure.txt`.

Residual gaps: no actual SDK/VuGen, transport, native header/parameter expansion,
concurrent Vusers, native response transcoding, or target isolation validation;
no native leak detector; no exhaustive fuzzing. Stub semantics cannot establish
real SDK return-value, allocation, encoding, or request behavior. Initialization
options and cookie/cache service return values remain production-documented
unchecked behavior, not newly certified reliability guarantees.
