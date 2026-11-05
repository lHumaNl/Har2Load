# Explicit synthetic runtime validation

This directory is **not** part of the installable package or the default `tests/`
suite. It never runs from `har2load import`, `inspect`, `validate` or `generate`.
It is a separately invoked developer harness for generated fixed runtime code and
synthetic fixtures only. Do not supply captured traffic, authored target code,
third-party JMX files, secrets or production endpoints.

## Safety and prerequisites

- Set `HAR2LOAD_NATIVE_VALIDATION=1` explicitly. Otherwise all native tests skip.
- Currently requires macOS `/usr/bin/sandbox-exec`. Every native process, including
  compilers, runs with `(deny network*)`; the suite first proves a loopback connect
  fails with `EPERM`. Unsupported hosts **fail closed**, never execute unsandboxed.
- Child environments contain only a minimal PATH, temporary HOME/TMPDIR, locale
  and sanitizer settings. This is network isolation, not a general malicious-code
  or filesystem sandbox. It is only for this repository's trusted synthetic tests.
- Tool acquisition is a separate explicit setup step. Tests do not download tools,
  install dependencies, change system Java selection or alter trust settings.
- C tests require Apple Clang with AddressSanitizer and UndefinedBehaviorSanitizer.
  LeakSanitizer is disabled because it is not supported on this macOS setup; do not
  describe an ASan pass as proof of leak freedom.
- JMeter tests require a **verified Apache JMeter 5.5 distribution and Java 8 JDK**.
  `HAR2LOAD_JMETER_HOME` points to its root; `HAR2LOAD_JAVA8_HOME` contains `bin/java`.
  The harness checks actual runtime versions, not just environment variable names.

Run from the checkout with the development dependencies installed:

```sh
HAR2LOAD_NATIVE_VALIDATION=1 \
  .venv/bin/python -m pytest validation/c_runtime --basetemp /PRIVATE/NEW/c-tests -q

HAR2LOAD_NATIVE_VALIDATION=1 \
HAR2LOAD_JAVA8_HOME=/PRIVATE/verified-java8-home \
HAR2LOAD_JMETER_HOME=/PRIVATE/apache-jmeter-5.5 \
  .venv/bin/python -m pytest validation/jmeter --basetemp /PRIVATE/NEW/jmeter-tests -q
```

Use a new private `--basetemp` destination: pytest may remove an existing basetemp.
Do not use a workspace, toolchain directory, manual output or unrelated directory.
Generated native files, logs and execution evidence remain private temporary files.

## What the harness can establish

JMeter uses `SaveService.loadTree` on **our newly generated synthetic JMX** and
resolves ModuleController references without starting an engine. Separate tests
prepare the actual JSR223 beans, compile and invoke fixed initializer/pre/postprocessor
code against synthetic `SampleResult` objects. They inspect the resulting sampler
fields and failure statuses. No HTTP sampler is sampled; no JMeter CLI `-n`, Thread
Group Validate, test engine or replay is invoked. Load-only cases do not execute
the generated scripts.

C tests compile emitted helpers/parser and synthetic SDK stubs. This tests C
behavior, bounds and lifecycle under the stubs. It is **not** native VuGen SDK,
ABI, threading, response-buffer, project-template or wire compatibility evidence.

Neither suite certifies arbitrary scenarios, runtime environments, HTTP wire parity,
load readiness or a complete dual-target release. Preparation output remains
`native_validation: not_run`: running these developer fixtures does not magically
validate a user's separately generated artifact set or mutate its report.

## Tool provenance for the current investigation

- JMeter 5.5: `https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.5.tgz`
  with the adjacent official `.sha512` file. The archive host timed out; acquisition
  used `https://apache.root.lu/jmeter/binaries/apache-jmeter-5.5.tgz`, verified against
  that official checksum before extraction or execution:
  `d5d1ce795e9baf18efd3a13ecda150b4da80c3173a2c7ef0da2a5546ac6862b1edd2a2f4e52d971c7da05d879362c28dca6bf218c5f7570b5cc98f7ba73c92af` (SHA-512).
- Java 8 for macOS ARM64: Azul Zulu package
  `zulu8.98.0.107-ca-jdk8.0.512-macosx_aarch64.tar.gz`; official package metadata:
  `https://api.azul.com/metadata/v1/zulu/packages/7b9c3e08-bf24-4d69-82a3-4ce55807cd34`.
  Verified SHA-256 before extraction/execution:
  `f26e2a91062542cf22ef2171b2556e1c3f42cf87cf174258b3df7840365892aa`.

## Historical JMeter verification — 2026-11-04, 0.7.0a1

Against unchanged production source from `b4e5010775d2fa32e60bf3f05d3a6dfdb3f61ef8`,
the lead executed 45 cases on JMeter 5.5 / Azul Java `1.8.0_512-b08`, ARM64:
3 load-only, 41 runtime, and 1 deliberately invalid duplicate-module negative
control. The separate swallowed-processor-error negative control also passed.
Evidence records fixture SHA-256 values, actual runtime versions and expected
negative-control rejection separately from successful loading.

Independent read-only review accepted the JMeter harness after closing four
oracle/coverage findings: processor ERROR detection, exact ordered module/group
correspondence, independent failures/scope resets, and native sampler-state checks.
This is fixture-level execution evidence only, subject to the limits above; it is
not approval of the C harness or native wire/engine compatibility.

## Historical integrated verification — 2026-11-04, 0.7.0a1

After separate independent **APPROVE / PASS** reviews for both harnesses, the final
evidence is **50 passing pytest tests** in the lead's fresh `integrated-run-2`
(59.02s): 49 C tests plus the JMeter batch described above. The C suite includes
1,120 seeded differential/fuzz inputs, stateful capture-contract controls, both
extractor orders, early-consumer lifecycle checks, and sanitizer/reset-mutation
negative controls. See [C verification details](c_runtime/README.md).

The integrated run preceded the final C review verdict; that reviewer inspected
the remediation without executing tests. The lead separately checked JMX digests
and retained sanitizer/mutation diagnostics. Default regression: **1101 passed**;
without opt-in: **50 skipped**; Ruff and strict mypy: passed. No production defect
was reproduced, and package source/version/dependencies remain unchanged.

Evidence is retained in a private temporary directory named
`har2load-native-validation-1bbqf285/integrated-run-2`, outside the checkout; earlier
runs remain separate. Tests and documentation are uncommitted at this checkpoint.
None of these results changes per-workspace `not_run` statuses or the outstanding
VuGen/full-engine/wire-compatibility limits.

## Current policy under test

Version 0.8 adopts the owner's revised `markers_only_native_status_v1`: generated
correlation logic must neither fail successful native results nor turn failed native
results green. JMeter checks preserve success, response codes and assertions;
LoadRunner checks require `LR_AUTO` and `lr_output_message`, not the potentially
status-changing `lr_error_message`. Mutation controls deliberately introduce both
directions of JMeter status override and C transaction/error-logging regressions.
Historical passes above do not certify changed source; each new run needs fresh
evidence and a new private basetemp.

## Authored-wrapper verification — 2026-11-04, 0.8.0a1

The lead's final fresh `wrappers-integrated-final` run passed **105 pytest tests**
in 187.43s: **103 C tests** plus two JMeter batches (**55 authored cases**, each
with two independent workers, and **53 baseline/status-control cases**).
All 108 JMX fixture digests were independently rechecked against the saved reports.
The runtime versions were JMeter 5.5 / Azul Java `1.8.0_512-b08`; C used the existing
Apple Clang 21 ASan/UBSan harness. Compiler/processor/reset/status negative controls
remain part of the evidence. A focused allocator failure confirms that output A
survives while output B becomes its marker, without changing native status.

Common/schema and JMeter reviews approved their scopes; C review passed with notes
about caller-owned C memory and the synthetic-only boundary. C argument-array extent
requirements are documented, and the suggested partial-output OOM check was added.
Default suite: **1176 passed** with both pytest entry points; native disabled:
**105 skipped**. Ruff and strict mypy (33 package modules) passed.

Installed-wheel smoke used a real 0.7-created workspace, verified read-only access,
explicit 1.4 upgrade/ABI changes, identical retry, historical revision preservation,
both target outputs and manifest digests. It reproduced and fixed a producer-version
allowlist regression. All 33 installed wheel modules matched the final source bytes.
Final wheel SHA-256:
`3b983ac6b75419863f0d0a65563897b36d68471fab469da81f6b09025e0fa026`.

The private execution record is `wrappers-integrated-final/lead-verification.json`
under the investigation root, with source/evidence digests. Package installation
evidence is in the separate private `har2load-wrappers-install-vap_4wje` directory.
No captured HTTP, engine execution, actual VuGen/SDK verification or arbitrary
customer-code execution was performed. Per-workspace readiness remains `not_run`.

## Native inputs and generated sources — 2026-11-04, 0.9.0a1

`jmeter/test_sources.py` and `jmeter/SourceHarness.groovy` exercise native Test Plan
UDV precompilation and literal decoding. An executable-function negative control
proves the encoded fixture does not merely bypass the parser. Tests cover missing,
empty, typed and marker-shaped inputs; immutable snapshots; source/output-name
collisions; signed-64 limits, dates, UUIDs, offsets and string generation; repeat and
unrelated-request lifecycle events; PRNG draws and wrapper/binding order.

The source batch has **101 native cases**, each with two workers, plus **6 Python
capability-gate checks** (not six more native cases). Actual controller initialization
and advancement reproduce the `__jm__` namespace collision without sampling HTTP;
the native variable API checks `__jmv_` semantics. This is not a complete
`JMeterThread.initRun()` or engine-scheduling test. Generation rejects both reserved
prefixes for external keys and exported runtime names.

`c_runtime/test_sources.py` tests emitted generators and native-input snapshots with
synthetic SDK parameter storage and controlled time/Vuser IDs. The full C suite now
contains **181 tests**, including nullable cleanup after both input and response
evaluation, allocated failure, recovery and no-evaluation/no-cleanup paths. The
stub cleanup contract follows the documented `lr_eval_string_ext_free` requirement
even for a NULL result. This does not establish that an actual SDK leak existed.
The bounded PRNG rejection-exhaustion branch is not dynamically forced in C.

Native `.prm/.dat` parsing is **not tested with VuGen**. ConfigParser/CSV inspection
only verifies structural fixture consistency; arbitrary encoding, relative-path
acceptance, missing/corrupt native files and concurrent Vuser isolation remain
unverified. No `.usr` project is fabricated, and no custom input JSON loader is used.

The five source generators run only at declared initialization events; UDV/native
parameters store initial configuration, not random streams. Source failures retain
`markers_only_native_status_v1`. No crypto, uniqueness or cross-target stream parity
is promised. These developer tests preserve all per-workspace `not_run` statuses.

Final lead run: **184 pytest tests passed in 392.13s**—181 C tests and three JMeter
batches (101 source, 55 authored, 53 baseline cases). All **209 JMX digests** matched
their retained execution reports. Without opt-in, **184 skipped**. Default tests:
**1342 passed** with both pytest entry points; Ruff and mypy (36 modules) passed.
The private record is `har2load-sources-final-AFX6I5/lead-verification.json` under
the approved temporary root; SHA-256:
`4162975382c199c2a0e4d5dabec881afb1b05d572f0d5bd1c0923934b9ef1014`.

Independent shared, JMeter and C gates approved after closing native-name collision,
negative lifecycle coverage and nullable SDK cleanup findings. Reviews were read-only;
the lead performed the final executions and byte/digest checks. A rebuilt 0.9 wheel
read/upgraded a real installed-0.8 workspace, preserved historical objects/raw HAR,
and emitted both targets with verified manifest hashes. All 36 source modules match
the wheel and installed bytes. Final wheel SHA-256:
`b977de7116d6c70a21f0becb8e6c67e082c44f40f4b5d63f5136dead7f61926c`.
Git publication is separate from these synthetic validation results.

## Native cookie sessions and opt-in seeds — 2026-11-05, 0.10.0a1

`jmeter/test_cookies.py` and `CookieHarness.groovy` load synthetic generated JMX,
run real precompilation, and exercise native CookieManager lifecycle APIs. The
cookie batch covers 20 static/runtime cases with two workers each: empty default,
explicit seeds, domain/host/path/Secure matching, response-driven replacement,
per-worker isolation and next-main-iteration seed restoration. It does not start
an HTTP engine or make requests. Four controls demonstrate why unsupported cases
are rejected: function interpretation, acceptance of expired manual seeds,
CookiePanel GUI scope/expiry loss and IPv6 native host mismatch.

`c_runtime/test_cookies.py` adds 30 tests to the prior 181. These compile actual
static/runtime output and inspect native API call order, seed arguments, expiry
formatting, repeated iterations and failure/status handling. The deliberate SDK
stubs **do not simulate a cookie jar** and do not establish domain/path/expiry,
server-cookie updates, native status side effects or concurrent Vuser parity.

Ordinary preparation remains offline and never runs this harness. The cookie
option is not a runtime extractor; default generation omits captured Cookie data
and relies on normal scenario authentication. Selection and scope inference are
recorded in private reports, not silently applied to the scenario model.

The final cookie batch additionally verifies same-name cookies at `/` and `/app`:
both survive native loading, the longer path precedes the shorter path, response
replacement affects only the matching identity, and iteration restoration/worker
isolation retain both seeds. It uses indexed collection keys rather than cookie
names as property identity.

Lead verification: **1682 default tests passed** through both pytest entry points,
Ruff and mypy (39 modules) passed; **215 native tests skip** without explicit opt-in.
The fresh full isolated run `har2load-cookie-integrated-gfAd6U` passed **215 tests
in 386.23s** (211 C + four JMeter batches), with 18 cookie cases at that point.
After the test-only multi-path enhancement, the lead reran the cookie batch in
`har2load-cookie-lead-final-VzjUVc`: **20 cases × two workers**, four controls passed.
Production code did not change. Do not describe the earlier full run as containing
the later cases. All 227 full-run JMX digests and 20 follow-up JMX digests were
checked; the latest four-batch fixture set comprises 229 JMX files.

Final evidence: `har2load-cookie-lead-final-VzjUVc/lead-verification.json` under the
approved temporary root, SHA-256
`48c3f2761d7fbdd310999b1c08830952a6216d24619c7c6d252fbbbf00dba281`.
Installed compatibility used a real 0.9 wheel rebuilt from committed `25e770c`
(old temporary artifacts had expired), then installed 0.10 and verified default
and selected-cookie generation, historical reads, immutable HAR/model objects,
native artifacts and manifest hashes. All 39 source modules match wheel/installed
bytes. Final wheel SHA-256:
`dcdbfd2ee7f7ccf60c8a363f9a1bee505bc314ad601e99f0ac1ca4765f5fcfbd`.
