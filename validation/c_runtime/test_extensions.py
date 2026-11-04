"""Explicitly opted-in, synthetic C ABI execution; never captured/authored customer code."""

from dataclasses import replace
from pathlib import Path
from subprocess import CalledProcessError

import pytest

from har2load.generation_types import RequestPlan
from har2load.loadrunner_runtime import symbol
from har2load.runtime_types import RuntimeSlot, RuntimeTemplate
from tests.test_loadrunner_extensions import authored, block, helper, variable
from validation.c_runtime.harness import c_literal, compile_source, lifecycle_binary
from validation.support import run_isolated

NUL_JSON = r'"\u0000"'


def execute(directory: Path, plan: RequestPlan, checks: str) -> None:
    binary = lifecycle_binary(directory, plan, checks)
    result = run_isolated([str(binary)], directory)
    (directory / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "EXTENSIONS_OK" and not result.stderr


def main(checks: str, setup: str = "") -> str:
    return f"""
int main(void) {{
    stub_reset(); vuser_init(); {setup}
    Action(); assert(request_count == 2 && transaction_count == 2);
    assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);
    assert(error_calls == 0);
    {checks}
    vuser_end(); assert(live_allocations == 0);
    puts("EXTENSIONS_OK"); return 0;
}}
"""


@pytest.mark.parametrize("kind,text,wire", [
    ("string", '"ERROR_value"', '"ERROR_value"'),
    ("string", '""', '""'), ("string", '"a\\u00e9"', '"aé"'),
    ("number", "123456789012345678901.00001", "123456789012345678901.00001"),
    ("integer", "-0.00e-999", "-0.00e-999"),
    ("boolean", "false", "false"), ("null", "null", "null"),
])
def test_scalar_types_preserve_exact_json(tmp_path: Path, kind: str, text: str, wire: str) -> None:
    source = f'h2l_emit_json(frame, "value", {c_literal(text)});'
    checks = (f"assert(!strcmp(requests[0].body, {c_literal(wire)}));"
              f"assert(!strcmp(requests[1].body, {c_literal(wire)}));"
              "assert(output_calls == 0);")
    execute(tmp_path, authored(source, kind), main(checks))


@pytest.mark.parametrize("source,kind", [
    ('h2l_emit_json(frame, "value", "null");', "string"),
    ('h2l_emit_json(frame, "value", "1.1");', "integer"),
    ('h2l_emit_json(frame, "value", "[1]");', "number"),
    ('h2l_emit_json(frame, "value", "true trailing");', "boolean"),
    ('h2l_emit_json(frame, "value", "1e10001");', "number"),
    ('h2l_emit_json(frame, "value", NULL);', "null"),
    ('/* Missing is not JSON null. */', "null"),
    ('h2l_emit_text_n(frame, "value", "a\\0b", 3);', "string"),
    ('h2l_emit_text(frame, "value", "\\377");', "string"),
    (f'h2l_emit_json(frame, "value", {c_literal(NUL_JSON)});', "string"),
    ('h2l_emit_json_n(frame, "value", "1\\0", 2);', "number"),
    ('h2l_emit_text_n(frame, "value", "x", H2L_VALUE_LIMIT + 1);', "string"),
    ('h2l_emit_json_n(frame, "value", "1", H2L_VALUE_LIMIT + 1);', "number"),
    ('h2l_emit_text(frame, "undeclared", "x");', "string"),
])
def test_missing_wrong_type_invalid_and_overflow_outputs_are_markers(
    tmp_path: Path, source: str, kind: str,
) -> None:
    checks = ('assert(!strcmp(requests[0].body, "\\\"ERROR_value\\\""));'
              'assert(!strcmp(requests[1].body, "\\\"ERROR_value\\\""));'
              'assert(output_calls > 0 && error_calls == 0);')
    execute(tmp_path, authored(source, kind), main(checks))


@pytest.mark.parametrize("tail", ["return 0;", 'h2l_emit_json(frame, "other", "42");',
                                 'h2l_emit_text(frame, "undeclared", "x");'])
def test_partial_success_persists_despite_other_output_or_late_failure(
    tmp_path: Path, tail: str,
) -> None:
    source = 'h2l_emit_text(frame, "value", "fresh");' + tail
    plan = authored(variables=(variable(), variable("other")),
                    blocks=(block(source, outputs=["value", "other"]),))
    checks = (f'assert({symbol("VALUE", "other")}()->data == NULL);'
              'assert(!strcmp(requests[0].body, "\\\"fresh\\\""));'
              'assert(!strcmp(requests[1].body, "\\\"fresh\\\""));'
              'assert(output_calls > 0);')
    execute(tmp_path, plan, main(checks))


def repeat_checks() -> str:
    return main('''
    assert(!strcmp(requests[0].body, "\\\"first\\\""));
    assert(!strcmp(requests[1].body, "\\\"first\\\""));
    Action();
    if (strcmp(requests[2].body, "\\\"ERROR_value\\\"")) {
        fputs("STALE_AUTHORED_OUTPUT\\n", stderr); return 3;
    }
    assert(!strcmp(requests[3].body, "\\\"ERROR_value\\\""));
    assert(output_calls > 0 && error_calls == 0);
    assert(transactions[2] == LR_AUTO && transactions[3] == LR_AUTO);
    ''')


def repeat_plan() -> RequestPlan:
    return authored('''
    static int calls = 0;
    if (!calls++) h2l_emit_text(frame, "value", "first");
    ''')


def test_repeat_missing_emit_does_not_reuse_session_output(tmp_path: Path) -> None:
    execute(tmp_path, repeat_plan(), repeat_checks())


def test_negative_control_detects_removed_invocation_reset(tmp_path: Path) -> None:
    lifecycle_binary(tmp_path, repeat_plan(), repeat_checks())
    action = tmp_path / "Action.c"
    original = action.read_text()
    (tmp_path / "Action.original.c").write_text(original)
    reset = "for (i = 0; i < frame->output_count; ++i) h2l_reset(frame->outputs[i].value);"
    assert original.count(reset) == 1
    action.write_text(original.replace(reset, "/* Deliberate negative control. */"))
    binary = compile_source(tmp_path, (tmp_path / "driver.c").read_text())
    with pytest.raises(CalledProcessError) as failure:
        run_isolated([str(binary)], tmp_path)
    assert failure.value.returncode == 3 and "STALE_AUTHORED_OUTPUT" in failure.value.stderr
    (tmp_path / "expected-mutation-failure.txt").write_text(failure.value.stderr)


def test_invalid_reemit_clears_previous_valid_output(tmp_path: Path) -> None:
    source = ('h2l_emit_text(frame, "value", "first");'
              'h2l_emit_json(frame, "value", "42");')
    execute(tmp_path, authored(source), main(
        'assert(!strcmp(requests[0].body, "\\\"ERROR_value\\\""));'
        'assert(output_calls > 0);'))


def test_allocation_failure_in_one_output_preserves_other_wire_value(tmp_path: Path) -> None:
    source = '''
    h2l_emit_text(frame, "value", "survives");
    alloc_fail_at = alloc_attempts; /* Synthetic allocator fault, not a runtime ABI feature. */
    h2l_emit_text(frame, "other", "not-published");
    alloc_fail_at = -1;
    '''
    plan = authored(variables=(variable(), variable("other")),
                    blocks=(block(source, outputs=["value", "other"]),))
    assert plan.runtime is not None
    first, second = plan.runtime.requests
    second = replace(second, body=RuntimeTemplate((RuntimeSlot("other-body", "other", "json"),)))
    plan = replace(plan, runtime=replace(plan.runtime, requests=(first, second)))
    execute(tmp_path, plan, main(
        'assert(!strcmp(requests[0].body, "\\\"survives\\\""));'
        'assert(!strcmp(requests[1].body, "\\\"ERROR_other\\\""));'
        'assert(output_calls > 0 && error_calls == 0);'))


@pytest.mark.parametrize("valid", [True, False])
def test_input_validity_not_marker_prefix_controls_body(tmp_path: Path, valid: bool) -> None:
    literal = variable("input", source={"kind": "literal", "value": "ERROR_ordinary"},
                       initialize={"event": "session_start"})
    source = 'h2l_emit_text(frame, "value", h2l_input(frame, "input")->data);'
    plan = authored(variables=(variable(), literal),
                    blocks=(block(source, inputs=["input"], outputs=["value"]),))
    expected = '"ERROR_ordinary"' if valid else '"ERROR_value"'
    setup = "" if valid else f'h2l_reset({symbol("VALUE", "input")}());'
    execute(tmp_path, plan, main(
        f'assert(!strcmp(requests[0].body, {c_literal(expected)}));', setup))


def helper_plan(helper_source: str, *, depends: bool = True) -> RequestPlan:
    literal = variable("input", source={"kind": "literal", "value": "original"},
                       initialize={"event": "session_start"})
    source = '''
    h2l_value result = { NULL, "ERROR_LOCAL", 0, 0 };
    const h2l_value *args[] = { h2l_input(frame, "input") };
    int ok = h2l_call_helper(frame, "outer", args, 1, &result);
    if (ok) h2l_emit_text(frame, "value", result.data);
    h2l_release(&result); return ok;
    '''
    parameters = [{"name": "arg", "value_type": "string"}]
    leaf = helper('h2l_emit_text(frame, "result", h2l_input(frame, "arg")->data);',
                  id="leaf", parameters=parameters, return_type="string")
    outer = helper(helper_source, id="outer", parameters=parameters, return_type="string",
                   depends_on=["leaf"] if depends else [])
    return authored(variables=(variable(), literal), helpers=(leaf, outer),
                    blocks=(block(source, inputs=["input"], outputs=["value"],
                                  helper_calls=["outer"]),))


NESTED = '''
    h2l_value result = { NULL, "ERROR_LOCAL", 0, 0 };
    const h2l_value *args[] = { h2l_input(frame, "arg") };
    int ok = h2l_call_helper(frame, "leaf", args, 1, &result);
    if (ok) h2l_emit_text(frame, "result", result.data);
    h2l_release(&result); return ok;
'''


@pytest.mark.parametrize("depends", [True, False])
def test_nested_helper_permissions_and_owned_results(tmp_path: Path, depends: bool) -> None:
    expected = '"original"' if depends else '"ERROR_value"'
    checks = (f'assert(!strcmp(requests[0].body, {c_literal(expected)}));'
              f'assert(!strcmp({symbol("VALUE", "input")}()->data, "original"));')
    execute(tmp_path, helper_plan(NESTED, depends=depends), main(checks))


@pytest.mark.parametrize("source", [
    'h2l_emit_text(frame, "result", "temporary"); return 0;',
    'h2l_emit_json(frame, "result", "42");', '/* Missing result. */',
    '''h2l_value out = { NULL, "ERROR_LOCAL", 0, 0 };
       int ok = h2l_call_helper(frame, "leaf", NULL, 0, &out);
       h2l_release(&out); return ok;''',
    '''h2l_value number = { "1", "ERROR_LOCAL", 2, 1 }, out = { 0 };
       const h2l_value *args[] = { &number };
       int ok = h2l_call_helper(frame, "leaf", args, 1, &out);
       h2l_release(&out); return ok;''',
    '''h2l_value out = { NULL, "ERROR_LOCAL", 0, 0 };
       int ok = h2l_call_helper(frame, "leaf", NULL, 1, &out);
       h2l_release(&out); return ok;''',
])
def test_helper_failure_type_arity_and_missing_inputs(tmp_path: Path, source: str) -> None:
    execute(tmp_path, helper_plan(source), main(
        'assert(!strcmp(requests[0].body, "\\\"ERROR_value\\\""));'
        'assert(output_calls > 0);'))


def test_helper_failure_does_not_undo_prior_block_output(tmp_path: Path) -> None:
    source = '''
    h2l_value result = { NULL, "ERROR_LOCAL", 0, 0 };
    h2l_emit_text(frame, "value", "survives");
    h2l_call_helper(frame, "helper", NULL, 0, &result);
    h2l_release(&result);
    '''
    plan = authored(blocks=(block(source, outputs=["value"], helper_calls=["helper"]),),
                    helpers=(helper('h2l_emit_text(frame, "result", "lost"); return 0;',
                                    return_type="string"),))
    execute(tmp_path, plan, main('assert(!strcmp(requests[0].body, "\\\"survives\\\""));'
                                'assert(output_calls > 0);'))


def test_helper_result_cannot_alias_caller_input(tmp_path: Path) -> None:
    source = '''
    const h2l_value *input = h2l_input(frame, "arg");
    const h2l_value *args[] = { input };
    assert(!h2l_call_helper(frame, "leaf", args, 1, (h2l_value *)input));
    assert(!strcmp(input->data, "original")); return 0;
    '''
    execute(tmp_path, helper_plan(source), main(
        f'assert(!strcmp({symbol("VALUE", "input")}()->data, "original"));'
        'assert(output_calls > 0);'))


@pytest.mark.parametrize("source", [NESTED,
    'h2l_emit_json(frame, "result", "\\\"original\\\"");'])
def test_all_generated_allocations_fail_cleanly_including_helper_results(
    tmp_path: Path, source: str,
) -> None:
    checks = '''
int main(void) {
    int fault, allocations;
    stub_reset(); vuser_init(); Action(); allocations = alloc_attempts;
    vuser_end(); assert(live_allocations == 0 && allocations > 5);
    for (fault = 0; fault < allocations; ++fault) {
        stub_reset(); alloc_fail_at = fault; vuser_init(); Action();
        assert(request_count == 2 && transaction_count == 2);
        assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);
        assert(output_calls > 0 && error_calls == 0);
        vuser_end(); assert(live_allocations == 0);
    }
    puts("EXTENSIONS_OK"); return 0;
}
'''
    execute(tmp_path, helper_plan(source), checks)


def chain_plan() -> RequestPlan:
    literal = variable("base", source={"kind": "literal", "value": "fresh"},
                       initialize={"event": "after_response", "exchange_id": "producer"})
    before = block('h2l_emit_text(frame, "value", "before");', outputs=["value"])
    middle = block('h2l_emit_text(frame, "middle", h2l_input(frame, "base")->data);',
                   id="middle", behavior_id="middle", outputs=["middle"], inputs=["base"],
                   placement={"event": "after_response", "exchange_id": "producer"})
    after = block('h2l_emit_text(frame, "value", h2l_input(frame, "middle")->data);',
                  id="after", inputs=["middle"], outputs=["value"], order=1,
                  placement={"event": "after_response", "exchange_id": "producer"})
    midvar = variable("middle", source={"kind": "agent_code", "behavior_id": "middle"})
    return authored(variables=(variable(), literal, midvar), blocks=(before, middle, after))


def test_phase_chains_baseline_init_and_native_transport_failure_preserved(tmp_path: Path) -> None:
    execute(tmp_path, chain_plan(), main('''
    assert(!strcmp(requests[0].body, "\\\"before\\\""));
    assert(!strcmp(requests[1].body, "\\\"fresh\\\""));
    assert(requests[0].status == LR_FAIL && requests[1].status == LR_PASS);
    assert(output_calls > 0 && error_calls == 0);
    ''', "fail_http_at = 0;"))


def test_output_free_and_void_helper_execute_without_slots(tmp_path: Path) -> None:
    source = 'return h2l_call_helper(frame, "helper", NULL, 0, NULL);'
    plan = authored(variables=(), blocks=(block(source, helper_calls=["helper"]),),
                    helpers=(helper('lr_output_message("SYNTHETIC_VOID_HELPER");'),))
    assert plan.runtime is not None
    requests = tuple(replace(r, body=None) for r in plan.runtime.requests)
    plan = replace(plan, runtime=replace(plan.runtime, requests=requests))
    execute(tmp_path, plan, main('assert(output_calls == 1);'))


@pytest.mark.parametrize("kind,value", [("number", "12345678901234567890.0001"),
    ("integer", "1.00e0"), ("boolean", "true"), ("null", "null")])
def test_typed_helper_inputs_and_results(tmp_path: Path, kind: str, value: str) -> None:
    source = '''
    h2l_value result = { NULL, "ERROR_LOCAL", 0, 0 };
    const h2l_value *args[] = { h2l_input(frame, "input") };
    if (h2l_call_helper(frame, "helper", args, 1, &result))
        h2l_emit_json(frame, "value", result.data);
    h2l_release(&result);
    '''
    producer = block(f'h2l_emit_json(frame, "input", {c_literal(value)});',
                     id="input", behavior_id="input", outputs=["input"])
    input_var = variable("input", kind, source={"kind": "agent_code", "behavior_id": "input"})
    contract = helper('h2l_emit_json(frame, "result", h2l_input(frame, "arg")->data);',
                      parameters=[{"name": "arg", "value_type": kind}], return_type=kind)
    plan = authored(kind=kind, variables=(variable(kind=kind), input_var), helpers=(contract,),
                    blocks=(producer, block(source, outputs=["value"], inputs=["input"],
                                            helper_calls=["helper"], order=1)))
    execute(tmp_path, plan, main(f'assert(!strcmp(requests[0].body, {c_literal(value)}));'
                                'assert(output_calls == 0);'))


def test_failed_helper_retry_preserves_prior_output(tmp_path: Path) -> None:
    source = '''
    h2l_value result = { NULL, "ERROR_LOCAL", 0, 0 };
    const h2l_value *args[] = { h2l_input(frame, "input") };
    if (h2l_call_helper(frame, "outer", args, 1, &result))
        h2l_emit_text(frame, "value", result.data);
    assert(!h2l_call_helper(frame, "outer", NULL, 0, &result));
    assert(result.data == NULL); h2l_release(&result);
    '''
    plan = helper_plan(NESTED)
    assert plan.runtime is not None
    plan = replace(plan, runtime=replace(plan.runtime, blocks=(
        block(source, inputs=["input"], outputs=["value"], helper_calls=["outer"]),)))
    execute(tmp_path, plan, main('assert(!strcmp(requests[0].body, "\\\"original\\\""));'
                                'assert(output_calls > 0);'))


@pytest.mark.parametrize("capture_ok", [True, False])
def test_after_response_extraction_precedes_code_and_bad_input_skips(
    tmp_path: Path, capture_ok: bool,
) -> None:
    source = 'h2l_emit_text(frame, "value", h2l_input(frame, "input")->data);'
    placement = {"event": "after_response", "exchange_id": "producer"}
    input_var = variable("input", source={"kind": "extracted", "extractor_id": "extract"},
                         initialize=placement)
    plan = authored(variables=(variable(), input_var), blocks=(
        block(source, inputs=["input"], outputs=["value"], placement=placement),))
    extract = {"id": "extract", "exchange_id": "producer", "area": "response_body",
               "selector": {"dialect": "json_path_subset_v1", "expression": "$.input"},
               "cardinality": "exactly_one", "allow_empty": False}
    assert plan.runtime is not None
    plan = replace(plan, runtime=replace(plan.runtime, extractors=(extract,)))
    setup = ('scheduled_response = (const unsigned char *)"{\\\"input\\\":\\\"captured\\\"}";'
             'scheduled_response_size = strlen((const char *)scheduled_response);')
    setup += "" if capture_ok else "register_rc = LR_FAIL;"
    expected = '"captured"' if capture_ok else '"ERROR_value"'
    checks = f'assert(!strcmp(requests[1].body, {c_literal(expected)}));'
    execute(tmp_path, plan, main(checks, setup))
