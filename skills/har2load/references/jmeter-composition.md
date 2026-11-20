# JMeter UC transfer and common-plan assembly

Composition is a **post-generation** activity, not HAR merging. Keep each generated
output directory, report and manifest intact. If a common plan is outside the CLI
profile, deliver the separate UCs and explain what further work would be needed.
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

## Compose with the 0.12.0a1 CLI

Use `compose jmeter` when the user wants a callable named Test Fragment and a
separate single-user debug Thread Group for each UC. Cookies and initial variable
storage are separate for each UC. The plan retains the supplied UC order; neither
that order nor a dotted label specifies a runtime schedule, nesting or session sharing.

Pass **2–16 ordered** `--uc LABEL GEN_DIR` pairs. A label must match
`TF_[A-Za-z0-9][A-Za-z0-9_.-]{0,59}` in full; reject duplicates ignoring case.
`GEN_DIR` is a complete, trusted local `generate --output` directory with its
generation manifest, report and generated JMX. Accept only producer `0.11.0a1`
or `0.12.0a1` and profile `static_literals_v1` version 1 or `scalar_runtime_v1`
version 5 without extensions. The 0.11.0a1 CLI can produce eligible sources but
cannot compose them. Never pass arbitrary, modified, standalone, authored/extension
or previously composed plans. Verification of manifests and digests checks integrity
relative to the manifest, not who created it: a replacement manifest can match
replacement files. Do not use composition as a safety check for untrusted bundles.

For example, with a resolved CLI executable `H2L`, two complete generated outputs
and the user-confirmed labels below, first check, then publish only if requested:

```sh
"$H2L" compose jmeter --uc TF_UC1 "generated/catalog" --uc TF_UC1.1 "generated/search" --check
"$H2L" compose jmeter --uc TF_UC1 "generated/catalog" --uc TF_UC1.1 "generated/search" --output "generated/common-debug"
```

Exactly one of `--check` (read-only) and `--output NEW_DIRECTORY` is required.
Repeat the same labels, directories and order for publication. Limits are 32 MiB
aggregate JMX and 128 MiB total source files including verified report bytes.
In the **new private directory**, check `manifest.json` (written last) and its
digests for `jmeter/scenario.jmx` and `composition.json`. Review the latter for
source identities, revisions, digests and statuses; it is not a source map for
the new tree. Preserve failed output and retry at a fresh path. Do not repair
manifests or remove extensions to force eligibility. Neither mode runs JMeter
or sends HTTP. Native execution and wire behavior remain unverified.

## External or manual transfer: inspect dependencies first

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
