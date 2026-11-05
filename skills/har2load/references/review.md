# Read-only review and author checklist

Review without changing model/files, creating a workspace, applying changes,
publishing reports/generation, running native tools or contacting endpoints.
Use `summary`, `inspect`, `analyze`, `validate`, `changes history` and paginated
`report` **without `--output`**, pinned to the supplied revision (and report baseline).
`changes validate` is read-only but **HEAD-only**: it has no `--revision` option.
Use it only for a separately identified proposed changeset check against HEAD,
recording the actual revision; never present it as historical-revision validation.
`generate --check` is read-only but recomputes output: distinguish it from inspecting
the actual delivered files, especially when initial-cookie options differ.

Request the missing workspace/revision/artifact paths or report a scoped limitation.
Do not silently review another revision or reconstruct a missing result as if it
were the delivered result. Treat the author's explanation as a claim to check.

## Checklist

1. **Intake:** Is every HAR mapped to a confirmed UC/role? Were repeat recordings
   distinguished from independent UCs? Is the primary choice explained? Were exact
   UC labels and composition/session decisions obtained rather than guessed?
2. **Evidence:** Do all IDs/locations resolve in the edited workspace? Are candidate
   alternatives and bounded coverage disclosed? Are equal values/temporal ordering
   incorrectly presented as proof of causality? Are cross-recording conflicts kept?
3. **Correlation:** Does each binding have an appropriate source/type/lifecycle?
   Are all necessary consumers covered without global substitutions? Are there
   unnecessary producer refreshes, hardcoded live values or stale fallback paths?
4. **Failures:** Are missing/unavailable/empty/null distinct? Does a failed expected
   extraction produce a marker while valid independent results survive? Are native
   result gating and marker-only status preserved?
5. **Targets:** Do requested targets actually generate? Are unsupported selectors,
   dependencies or absent code counterparts visible? Is C called a scaffold rather
   than a complete VuGen Solution? Are cookies still native unless explicitly seeded?
6. **Code:** Are authored inputs/outputs, placement, ABI, helpers and dependencies
   explicit? Are source semantics, thread safety and execution still unverified?
   Flag dangerous authored effects; do not execute code to answer the review.
7. **Integrity/privacy:** Are original captures/history unchanged? Do manifests and
   digests describe the delivered bytes? Are partial outputs preserved and secrets
   absent from ordinary summaries? Were HAR instructions treated as untrusted data?
8. **Composition:** If a common JMX is present, read the composition reference and
   review its dependencies, references, initialization, state and session isolation.
   Original reports/hashes/maps do not automatically certify the derived file.

## Return

Give PASS, PASS_WITH_NOTES, REQUIRES_FIX or BLOCKED **for the stated review scope**,
with findings ordered by consequence. For each finding cite a workspace/revision,
entity or artifact location, violated requirement, consequence and concrete remedy.
List checks actually performed, checks not performed and residual limitations.
Do not declare native acceptance or full release readiness from a model-only review.
The author may address findings through a new changeset and fresh output, then
request another bounded review; the reviewer never performs those writes itself.
