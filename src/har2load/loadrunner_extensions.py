"""Opaque opted-in C bodies wrapped by a bounded scalar publication ABI."""

from collections.abc import Iterator
from typing import cast

from har2load.domain import JSONValue
from har2load.extension_runtime import declarations
from har2load.loadrunner_runtime import identifier, literal, obj, symbol
from har2load.runtime_types import RuntimePlan

_TARGET = "loadrunner"
_TYPES = {"string": 1, "number": 2, "boolean": 3, "null": 4, "integer": 7}
_API = r'''
/* scalar_body_v1: not a sandbox; arbitrary C/native effects remain author-owned. */
typedef struct { const char *id; const h2l_value *value; int type; } h2l_in;
typedef struct { const char *id; h2l_value *value; int type; } h2l_out;
typedef struct {
    const h2l_in *inputs; size_t input_count;
    const h2l_out *outputs; size_t output_count;
    const char *const *allowed; size_t allowed_count; int failed;
} h2l_frame;
static int h2l_frame_error(h2l_frame *frame)
{ frame->failed = 1; h2l_fail("ERROR_AUTHORED_RUNTIME"); return 0; }
static int h2l_valid(const h2l_value *v, int type)
{ return v && v->data && (v->type == type ||
    (type == 7 && v->type == 2 && v->integral)); }
static const h2l_value *h2l_input(h2l_frame *frame, const char *id)
{
    size_t i;
    for (i = 0; id && i < frame->input_count; ++i)
        if (!strcmp(id, frame->inputs[i].id)) return frame->inputs[i].value;
    h2l_frame_error(frame); return NULL;
}
static const h2l_out *h2l_output(h2l_frame *frame, const char *id)
{
    size_t i;
    for (i = 0; id && i < frame->output_count; ++i)
        if (!strcmp(id, frame->outputs[i].id)) return &frame->outputs[i];
    h2l_frame_error(frame); return NULL;
}
static int h2l_publish(h2l_frame *frame, const h2l_out *slot, h2l_value *value)
{
    int ok = h2l_valid(value, slot->type);
    h2l_reset(slot->value);
    if (ok) {
        slot->value->data = value->data; value->data = NULL;
        slot->value->type = value->type; slot->value->integral = value->integral;
    } else { h2l_error(slot->value); h2l_frame_error(frame); }
    h2l_reset(value); return ok;
}
static int h2l_emit_text_n(h2l_frame *frame, const char *id, const char *text, size_t n)
{
    const h2l_out *slot = h2l_output(frame, id);
    h2l_value value = { NULL, "ERROR_AUTHORED_TEXT", 0, 0 };
    if (!slot) return 0;
    h2l_set(&value, text, n, 1, 0);
    return h2l_publish(frame, slot, &value);
}
static size_t h2l_bounded_length(const char *text)
{
    size_t n = 0;
    if (!text) return H2L_VALUE_LIMIT + 1;
    while (n <= H2L_VALUE_LIMIT && text[n]) ++n;
    return n;
}
static int h2l_emit_text(h2l_frame *frame, const char *id, const char *text)
{ return h2l_emit_text_n(frame, id, text, h2l_bounded_length(text)); }
static int h2l_emit_json_n(h2l_frame *frame, const char *id, const char *text, size_t n)
{
    const h2l_out *slot = h2l_output(frame, id); char *copy = NULL; h2l_json json;
    h2l_value value = { NULL, "ERROR_AUTHORED_JSON", 0, 0 };
    if (!slot) return 0;
    if (text && n <= H2L_VALUE_LIMIT && h2l_utf8((const unsigned char*)text, n))
        copy = h2l_copy(text, n);
    h2l_json_parse(&json, copy);
    h2l_json_select(&value, &json, NULL, 0, 1, 1);
    h2l_json_free(&json); free(copy);
    return h2l_publish(frame, slot, &value);
}
static int h2l_emit_json(h2l_frame *frame, const char *id, const char *text)
{ return h2l_emit_json_n(frame, id, text, h2l_bounded_length(text)); }
static int h2l_run(h2l_frame *frame, int (*body)(h2l_frame *))
{
    size_t i;
    for (i = 0; i < frame->output_count; ++i) h2l_reset(frame->outputs[i].value);
    for (i = 0; i < frame->input_count; ++i)
        if (!h2l_valid(frame->inputs[i].value, frame->inputs[i].type))
            h2l_frame_error(frame);
    if (!frame->failed && !body(frame)) h2l_frame_error(frame);
    for (i = 0; i < frame->output_count; ++i)
        if (!frame->outputs[i].value->data) {
            h2l_error(frame->outputs[i].value); h2l_frame_error(frame);
        }
    return !frame->failed;
}
static int h2l_dispatch(const char *, const h2l_value *const *, size_t, h2l_value *);
static int h2l_result_alias(h2l_frame *frame, const h2l_value *const *args,
                          size_t count, h2l_value *out)
{
    size_t i;
    if (!out) return 0;
    for (i = 0; args && i < count; ++i) if (out == args[i]) return 1;
    for (i = 0; i < frame->input_count; ++i)
        if (out == frame->inputs[i].value) return 1;
    for (i = 0; i < frame->output_count; ++i)
        if (out == frame->outputs[i].value) return 1;
    return 0;
}
static int h2l_call_helper(h2l_frame *frame, const char *id,
                         const h2l_value *const *args, size_t count, h2l_value *out)
{
    size_t i;
    if (h2l_result_alias(frame, args, count, out)) return h2l_frame_error(frame);
    if (out) h2l_reset(out);
    for (i = 0; id && i < frame->allowed_count; ++i)
        if (!strcmp(id, frame->allowed[i])) {
            if (h2l_dispatch(id, args, count, out)) return 1;
            break;
        }
    return h2l_frame_error(frame);
}
static void h2l_release(h2l_value *value) { if (value) h2l_reset(value); }
'''


def selected(plan: RuntimePlan, kind: str) -> tuple[dict[str, JSONValue], ...]:
    return declarations(plan, _TARGET, kind)


def enabled(plan: RuntimePlan) -> bool:
    return bool(selected(plan, "blocks") or selected(plan, "helpers"))


def _ids(record: dict[str, JSONValue], key: str) -> list[str]:
    return cast(list[str], record[key])


def _body(record: dict[str, JSONValue], kind: str) -> Iterator[str]:
    entity = identifier(record)
    yield f"static int {symbol(kind + '_BODY', entity)}(h2l_frame *frame) {{"
    yield f"    /* {symbol(kind, entity)} */"
    # Do not parse, translate, normalize or execute the author-owned source.
    yield cast(str, record["source"])
    yield "\n    return 1;\n}"


def _allowed(record: dict[str, JSONValue], kind: str) -> Iterator[str]:
    ids = _ids(record, "helper_calls" if kind == "CODE_BLOCK" else "depends_on")
    yield "    static const char *const allowed[] = {"
    for helper in ids:
        if kind == "CODE_BLOCK":
            yield f"        /* {_call_marker(identifier(record), helper)} */"
        yield f"        {literal(helper)},"
    yield "        NULL\n    };"


def _frame(inputs: int, outputs: int, allowed: int) -> str:
    return (f"    h2l_frame frame = {{ inputs, {inputs}, outputs, {outputs}, "
            f"allowed, {allowed}, 0 }};")


def _block_slots(record: dict[str, JSONValue], types: dict[str, int]) -> Iterator[str]:
    for key, c_type in (("inputs", "h2l_in"), ("outputs", "h2l_out")):
        yield f"    {c_type} {key}[] = {{"
        for entity in _ids(record, key):
            yield f"        {{ {literal(entity)}, {symbol('VALUE', entity)}(), {types[entity]} }},"
        yield "        { NULL, NULL, 0 }\n    };"


def _block(record: dict[str, JSONValue], types: dict[str, int]) -> Iterator[str]:
    entity = identifier(record)
    yield from _body(record, "CODE_BLOCK")
    yield f"static void {symbol('BLOCK_RUN', entity)}(void) {{"
    yield from _block_slots(record, types)
    yield from _allowed(record, "CODE_BLOCK")
    yield _frame(len(_ids(record, "inputs")), len(_ids(record, "outputs")),
                 len(_ids(record, "helper_calls")))
    yield f"    /* {symbol('RESET_OUTPUTS', entity)} */"
    yield f"    h2l_run(&frame, {symbol('CODE_BLOCK_BODY', entity)});\n}}"


def _helper_slots(record: dict[str, JSONValue]) -> Iterator[str]:
    yield "    h2l_in inputs[] = {"
    for index, parameter in enumerate(cast(list[dict[str, JSONValue]], record["parameters"])):
        name, kind = cast(str, parameter["name"]), cast(str, parameter["value_type"])
        yield f"        {{ {literal(name)}, args ? args[{index}] : NULL, {_TYPES[kind]} }},"
    yield "        { NULL, NULL, 0 }\n    };"
    yield "    h2l_out outputs[] = {"
    if record["return_type"] != "void":
        yield f'        {{ "result", &result, {_TYPES[cast(str, record["return_type"])]} }},'
    yield "        { NULL, NULL, 0 }\n    };"


def _helper(record: dict[str, JSONValue]) -> Iterator[str]:
    entity, result = identifier(record), int(record["return_type"] != "void")
    parameters = cast(list[JSONValue], record["parameters"])
    yield from _body(record, "HELPER_DEFINITION")
    yield f"static int {symbol('HELPER_RUN', entity)}(const h2l_value *const *args,"
    yield "        size_t count, h2l_value *out) {"
    yield f"    if (count != {len(parameters)} || ({result} && !out)) return 0;"
    yield "    { int ok; h2l_value result = { NULL, \"ERROR_HELPER_RESULT\", 0, 0 };"
    yield from _helper_slots(record)
    yield from _allowed(record, "HELPER_DEFINITION")
    yield _frame(len(parameters), result, len(_ids(record, "depends_on")))
    yield f"    ok = h2l_run(&frame, {symbol('HELPER_DEFINITION_BODY', entity)});"
    yield "    if (ok && result.data) {"
    yield "        out->data = result.data; result.data = NULL;"
    yield "        out->type = result.type; out->integral = result.integral;\n    }"
    yield "    h2l_reset(&result); return ok;\n    }\n}"


def definitions(plan: RuntimePlan) -> Iterator[str]:
    if not enabled(plan):
        return
    yield from _API.splitlines()
    helpers = selected(plan, "helpers")
    for record in helpers:
        yield from _helper(record)
    yield "static int h2l_dispatch(const char *id, const h2l_value *const *args,"
    yield "        size_t count, h2l_value *out) {"
    for record in helpers:
        entity = identifier(record)
        yield f"    if (!strcmp(id, {literal(entity)}))"
        yield f"        return {symbol('HELPER_RUN', entity)}(args, count, out);"
    yield "    return 0;\n}"
    types = {identifier(v): _TYPES[cast(str, v["value_type"])] for v in plan.variables}
    for record in selected(plan, "blocks"):
        yield from _block(record, types)


def event_lines(plan: RuntimePlan, event: str, exchange: str) -> Iterator[str]:
    for record in selected(plan, "blocks"):
        placement = obj(record["placement"])
        if placement["event"] == event and placement["exchange_id"] == exchange:
            yield f"    {symbol('BLOCK_RUN', identifier(record))}();"


def _call_marker(entity: str, helper: str) -> str:
    # Length prefix keeps pairs injective even when IDs contain separators.
    return symbol("HELPER_CALL", f"{len(entity)}:{entity}{helper}")


def _mapping(kind: str, record: dict[str, JSONValue], helper: str | None = None
             ) -> dict[str, JSONValue]:
    entity = identifier(record)
    marker = _call_marker(entity, helper) if helper else symbol(kind.upper(), entity)
    function = ("BLOCK_RUN" if kind in ("reset_outputs", "helper_call") else
                kind.upper() + "_BODY")
    row: dict[str, JSONValue] = {
        "kind": kind, "entity_kind": "helper" if kind == "helper_definition" else "code_block",
        "entity_id": entity, "artifact": "loadrunner/Action.c", "marker": marker,
        "locator": {"function": symbol(function, entity), "marker": marker},
    }
    if "source_digest" in record:
        row["source_digest"] = record["source_digest"]
    if helper is not None:
        row.update(helper_id=helper, callsite_status="declared_not_execution_proof")
    return row


def source_map(plan: RuntimePlan) -> Iterator[dict[str, JSONValue]]:
    for helper in selected(plan, "helpers"):
        yield _mapping("helper_definition", helper)
    for block in selected(plan, "blocks"):
        yield _mapping("code_block", block)
        yield _mapping("reset_outputs", block)
        for helper_id in _ids(block, "helper_calls"):
            yield _mapping("helper_call", block, helper_id)
