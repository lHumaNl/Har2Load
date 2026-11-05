# Explicit target code: bounded authoring contract

Prefer ordinary scalar sources and bindings when they express the confirmed need.
Use authored code for an intentional transformation, not to conceal a missing
producer, unsupported project packaging or an unexplained validation failure.
Never execute source while preparing or reviewing it. Contracts and native behavior
are separate: code can be structurally accepted and still be wrong or unsafe.

## Declare the behavior before its body

For scenario 1.4+, a block upsert is `{"op":"upsert_code_block","block":...}`.
The block requires all of: `id`, `behavior_id`, `target`, `language`, `source`,
`placement`, `order`, `depends_on`, `inputs`, `outputs`, `helper_calls`,
`dependencies`, `side_effects`, `failure_policy`, `failed_input_policy`,
`reset_outputs`, `purpose`. Opt in explicitly with `runtime_abi: scalar_body_v1`.
Do not supply derived `change_id` or `source_digest` inside the block.

- Target/language pairs: `jmeter`/`groovy`, `loadrunner`/`c`.
- Placement: `before_request` or `after_response` plus `exchange_id`.
- Inputs/outputs: variable IDs, disjoint. Outputs must be variables with source
  `{"kind":"agent_code","behavior_id":"BEHAVIOR_ID"}`, initialized at that exact
  placement. Use a valid scope/reset pair as for other variables.
- Use integer order 0–10000, unique at a declared target/event position. Dependencies
  must be earlier same-target blocks; no cycles/use-before-initialization.
- Required policies: `failure_policy: mark_error_and_continue`,
  `failed_input_policy: skip_and_mark_outputs`, `reset_outputs: true`.
- Declare effects honestly (`network`, `filesystem`, `process`, `environment`,
  `shared_state`, `unknown` as applicable); declarations are not a sandbox.
- Counterparts use the same behavior ID but independently authored code/IDs. Missing
  counterparts are reported for the affected target, even for output-free behavior;
  do not claim cross-target parity or silently fake an implementation.
- Any declared external dependency currently blocks wrapper generation. Do not
  fetch, execute or silently drop it. Request a supported solution or report the gap.

## Function-body ABI (not a whole class/function/file)

Groovy block bodies receive read-only `inputs` by variable ID, `emit`, declared
`helpers`, and `context` (`vars`, `ctx`, `prev`, `sampler`, `log`). Example for an
already declared string input `raw` and string output `normalized`:

```groovy
emit('normalized', inputs.raw.trim())
```

Only explicit emits publish outputs; the block's return value is ignored. A string
must be a String, not an implicit GString; numeric output must use supported exact
types, not Float/Double or numeric strings. Do not execute captured `${...}` data.

C bodies receive `h2l_frame *frame`. Return 1 for success or 0 for reported failure.
For already declared string input `raw` and string output `normalized`, a copy body
(not equivalent to the trim example) is:

```c
const h2l_value *value = h2l_input(frame, "raw");
if (!value || !value->data) return 0;
return h2l_emit_text(frame, "normalized", value->data);
```

Inputs are borrowed/read-only. Text emit copies a string; `h2l_emit_json` parses one
complete scalar against its declared type. Helpers require explicit declarations
and calls, zero-initialized owned results, and `h2l_release` on all paths. Before
writing a complex helper, consult the matching installed distribution's complete
ABI/schema documentation; if unavailable, report that gap rather than guessing
helper fields, ownership, native SDK calls or missing capabilities.

All outputs reset before invocation. Independently valid emits survive other
reported failures; missing/invalid outputs stay markers. Do not call status-changing
native APIs or log secrets to implement correlation error handling. The wrapper is
not crash/loop/side-effect containment and does not make arbitrary authored code safe.
