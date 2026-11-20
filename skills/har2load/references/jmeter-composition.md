# JMeter UC transfer and common-plan assembly

Composition is a **post-generation** activity, not HAR merging. Keep each generated
output directory, report and manifest intact. If a common plan is outside the CLI
profile, deliver the separate UCs and explain what further work would be needed.
For this workflow, VuGen remains one UC per Solution; do not apply JMeter assembly
rules to C scaffolds or fabricate missing VuGen project files.

[Layout](#confirm-the-requested-layout) · [Plain composition](#compose-independent-ucs) ·
[Candidate decisions](#select-shared-steps) · [Plan and commands](#write-and-check-the-plan) ·
[Manual transfer](#external-or-manual-transfer-inspect-dependencies-first)

## Confirm the requested layout

Obtain the mapping from source outputs/groups to agreed UC labels (`TF_UC1`,
`TF_UC2`, `TF_UC1.1`, etc.). Clarify what nested numbering means. Ask whether UCs
should be callable independently or execute in a sequence, whether users/sessions
and inputs should be shared, and which debug entry points are wanted. Do not choose
concurrency, load levels, shared authentication or cookie resets from numbering.

Distinguish a complete UC from a generated step fragment: one scenario can have
multiple TF groups, Module Controller calls and lifecycle processors. Preserve
their order and required setup; do not copy just the visibly interesting samplers.

## Compose independent UCs

Use `compose jmeter` when the user wants a callable named Test Fragment and a
separate single-user debug Thread Group for each UC. Cookies and initial variable
storage are separate for each UC. The plan retains the supplied UC order; neither
that order nor a dotted label specifies a runtime schedule, nesting or session sharing.

Pass **2–16 ordered** `--uc LABEL GEN_DIR` pairs. A label must match
`TF_[A-Za-z0-9][A-Za-z0-9_.-]{0,59}` in full; reject duplicates ignoring case.
`GEN_DIR` is a complete, trusted local `generate --output` directory with its
generation manifest, report and generated JMX. With the `0.13.0a1` CLI accept only
producer `0.11.0a1`, `0.12.0a1` or `0.13.0a1` and profile `static_literals_v1`
version 1 or `scalar_runtime_v1` version 5 without extensions. The 0.11.0a1 CLI
cannot compose; 0.12.0a1 supports plain composition only. Never pass arbitrary, modified, standalone, authored/extension
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

## Select shared steps

Without an explicit yes to the [intake choice](intake.md#shared-step-choice), stop here:
no candidate search, no reuse, no plan. Plain composition does not deduplicate even
identical steps. The following is agent work, not automatic CLI discovery.

1. For each agreed UC, verify the generation manifest and its file hashes. Read
   `scenario.groups[].id` **in array order** from that bundle's `report.json`.
   Match each proposed range to the generated requests and inspect what enters and
   leaves it: request inputs, response effects, bindings, lifecycle and later consumers.
2. Show **every candidate** to the user. For each, identify the UCs and ordered
   group IDs, business action, inputs and outputs (including “none”), evidence,
   concrete differences and proposed disposition. Use names/locations, not secret
   values. An equal URL, label or response value alone does not establish equivalence.
3. Mark **HIGH** only when you can explain matching intent, operations and data
   dependencies and account for the differences. Within an opted-in task, an
   explained HIGH can enter the plan as `high_confidence` unless declined.
   Mark **UNCERTAIN** when something material is unknown. Ask one focused question;
   do not assign a percentage or treat permission to search as a business decision.
   Use `user_confirmed` only after an actual answer resolves that particular
   uncertainty and confirms reuse. Neither decision bypasses technical checks.
4. Keep rejected, unanswered and unresolved candidates separate; record the reason
   in the private decision record (in the response only for read-only review), not
   in the executable plan. Do not ask repeatedly. If none qualify, omit the option.

**Example candidate review** (synthetic IDs and business context):

| Candidate and source ranges | Inputs → outputs | Evidence, differences and decision |
|---|---|---|
| Open catalog → open subcatalog. `TF_UC1`: `group_a1`, `group_a2`; `TF_UC1.1`: `group_b1`, `group_b2`. | Same fixed catalog and subcatalog paths; no declared scalar inputs or outputs. | **HIGH**: both generated ranges contain the same two ordered GET requests, headers and empty bodies, with no extractor or binding. Both are navigation to the same subcatalog. The next groups differ: view item vs. search. Reuse only the first two groups as `TF_OpenCatalog`. |
| Select regional listing. `TF_UC1`: `group_a4`; `TF_UC1.1`: `group_b4`. | Region query input → region-filtered listing; no declared scalar output. | **UNCERTAIN**: same endpoint and label, but the region query differs. “Is region an intentional market selection that these UCs need to keep distinct?” Leave separate on no answer or confirmation that the markets differ. Do not erase the query or invent a variable mapping. |

For an uncertainty about *meaning* in an otherwise compatible range, a focused
exchange could be: “`group_a5` and `group_b5` send the same request with no scalar
inputs or outputs. Does ‘Browse’ mean the same read-only item preview as ‘Inspect’,
and should these groups be reused?” **Only if the user actually replies**, for
example, “Yes, both are the item preview; reuse those groups,” record that scope and
basis as `user_confirmed`. An example reply is not evidence for a real task.

## Write and check the plan

In prepare mode only, write a private strict JSON file (maximum **256 KiB**).
The example is a template: substitute the exact `--uc` labels and report group IDs
for your inputs, and replace each hash placeholder with **64 lowercase hex digits**
of SHA-256 over that UC's **raw generation `manifest.json` bytes**. Do not hash
reserialized JSON, the workspace manifest or `scenario.json`, or pin by revision alone.

```json
{
  "schema_version": "1.0",
  "blocks": [{
    "name": "TF_OpenCatalog",
    "decision": "high_confidence",
    "reason": "Both UCs open the same catalog and subcatalog in order before their distinct next actions; neither range produces or consumes a scalar.",
    "uses": [
      {"uc": "TF_UC1", "manifest_sha256": "CATALOG_MANIFEST_SHA256", "groups": ["group_a1", "group_a2"]},
      {"uc": "TF_UC1.1", "manifest_sha256": "SEARCH_MANIFEST_SHA256", "groups": ["group_b1", "group_b2"]}
    ]
  }]
}
```

Use 1–32 blocks. Apply the UC label syntax to each block name; all block and UC
names must be unique under ASCII case folding. Each block needs at least two
distinct UCs, one range per UC. List 1–128 **complete consecutive** group IDs
per use in source order; no group may overlap another selected range in its UC.
Use only the shown keys. `decision` is `high_confidence` or `user_confirmed`;
`reason` is a nonblank, single-line private evidence explanation (up to 1024
characters; omit control characters), not a place for literal secrets.
This plan selects composition steps only; it is not a changeset or a runtime loader.

```sh
"$H2L" compose jmeter --uc TF_UC1 "generated/catalog" --uc TF_UC1.1 "generated/search" --shared-steps "shared.json" --check
"$H2L" compose jmeter --uc TF_UC1 "generated/catalog" --uc TF_UC1.1 "generated/search" --shared-steps "shared.json" --output "generated/common-shared"
```

Check first; for authorized publication use the identical plan bytes, sources and
flags apart from the destination mode. Each accepted block becomes one root TF
called in place by the participating UCs' Module Controllers, in each caller's
own virtual-user state. Nothing is executed once on behalf of all users. Verify
the derived manifest and JMX. In the private `composition.json`, compare `shared_steps`
with the reviewed decisions and `shared_steps_sha256` with the hash of the exact plan
file bytes. Those decisions are supplied by the author, not verified business intent.
A stale pin, incompatible range
or unmapped group blocks the plan; there is no silent fallback or partial reuse.
Report the specific blocked range if known. Do not coerce requests, rewrite
variables or drop a block unnoticed. Revisit the decision before changing the plan.

**Technical limits:** A shared block uses either all static or all scalar-runtime
call sites. Compare complete ordered requests and group boundaries. For scalar
inputs/outputs, exported names, external key names, slot order/encoding, relevant
variable lifecycles, and extractor behavior must match. Caller-specific model IDs
and external initial values may differ. A required request-specific producer
outside the selected block is not mapped: leave the steps separate unless a
larger contiguous range qualifies on its own evidence. There is no general
variable-renaming or parameterization mechanism in the composer.

The first listed use supplies the shared fragment's visible transaction and
sampler labels/comments. For attribution to a different caller use the plan's
source pins and group IDs, not those representative labels. Do not edit internal
`har2load.shared.*` User Parameters. They hold original per-caller processor data;
the Debug Sampler and its results can expose these values (base64 is not redaction).

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
