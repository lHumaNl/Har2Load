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

## JMeter verification — 2026-11-04

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

## Integrated verification — 2026-11-04

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
