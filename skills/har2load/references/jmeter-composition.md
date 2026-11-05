# JMeter UC transfer and common-plan assembly

This is a separate **post-generation** activity, not HAR merging. Har2Load 0.11 has
no CLI JMX composer. Keep each generated output directory, report and manifest
intact. The absence of a composer must not prevent delivering useful separate UCs.
For this workflow, VuGen remains one UC per Solution; do not apply JMeter assembly
rules to C scaffolds or fabricate missing VuGen project files.

## Confirm the requested layout

Obtain the mapping from source outputs/groups to agreed UC labels (`TF_UC1`,
`TF_UC2`, `TF_UC1.1`, etc.). Clarify what nested numbering means. Ask whether UCs
should be callable independently or execute in a sequence, whether users/sessions
and inputs should be shared, and which debug entry points are wanted. Do not choose
concurrency, load levels, shared authentication or cookie resets from numbering.

Distinguish a complete UC from a generated step fragment: one scenario can have
multiple TF groups, Module Controller calls and lifecycle processors. Preserve
their order and required setup; do not copy just the visibly interesting samplers.

## Inspect the whole dependency set before offering a transfer

For each source JMX, inspect structural data without loading it into a native runner:

- TestPlan User Defined Variables and their literal encoding; duplicate input names
  or differing initial values must not silently overwrite each other.
- Cookie Managers, explicit seed provenance, scope and iteration reset policy;
  multiple copied managers are not a proven isolated-session design.
- Debug/main Thread Groups, Module Controllers and their full target node paths;
  moving or renaming a TF can invalidate those paths.
- Runtime initialization/reset processors outside the TF and processors inside it.
- Authored setup groups, helper registries, declared dependencies and namespace use.
- Exported variables and internal state. Current generated runtime uses the shared
  key `har2load.scalar_runtime_v1.state`; copying two scenarios into the same thread
  can collide even when visible TF names differ. Request/variable IDs and encoded
  processor configuration must also be considered.
- Shared settings/listeners and target-specific limitations; preserve intentional
  enabled states and never assume that a Test Fragment executes by itself.

Do not claim isolation merely because fragments have different names. Separate
Thread Groups alone are not a complete proof either: TestPlan inputs and `props`
registries can still be shared. Do not globally replace strings or patch base64
runtime configuration blindly to hide collisions. If required remapping/isolation
cannot be justified with the current contract, mark assembly blocked/pending and
deliver the separate artifacts plus the precise missing work.

## Agent-assisted or manual path

Only assemble a common plan when the user requested it and the dependency/conflict
analysis supports the chosen layout. Create a **new derived artifact**, never edit
the generated originals. Do not label it as an unchanged Har2Load-generated bundle.
Keep a private composition record listing source manifests/hashes/revisions, UC
mapping, copied dependencies, renames/reference fixes, isolation choices and checks.

For manual transfer, instruct the user to work on copies and open source/destination
plans separately if convenient. Give a source-specific checklist of the UC subtree
**and** required surrounding setup/configuration, then identify exact references
and conflicts requiring repair. Do not give a blanket "copy TF and it works"
instruction. Do not open JMeter or start its engine yourself as an implicit step.
Warn that editing/rebuilding JMeter's Cookie Manager GUI can lose host-only flags.

After assembly, check XML/tree structure, resolved intended references, unique
names/registries/state where required, initialization placement, inputs and cookie
scope/reset choices. Flag unresolved behavior explicitly. A generic XML parse is
not native JMeter validation; original source maps and fixture results no longer
describe the modified bytes. New checks need their own artifact identity/evidence.
Native loading, processor execution and HTTP replay each require separate explicit
authorization and an appropriate safe environment; do not start load to "verify" it.

## Future CLI boundary

A later JMeter-only composer can automate this mechanical work for supported
Har2Load-generated artifacts. It is not implemented by this skill. Do not invent
`merge`, `compose`, multi-workspace generation flags or an arbitrary-JMX compatibility
promise. Keep the UC decisions and dependency record usable for that later task.
