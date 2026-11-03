"""Actual generated Action/init/end execution against deliberate, non-SDK stubs."""

from dataclasses import replace
from subprocess import CalledProcessError

import pytest

from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.loadrunner_runtime import symbol
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate
from validation.c_runtime.harness import c_literal as literal
from validation.c_runtime.harness import compile_source, isolated, lifecycle_binary
from validation.support import run_isolated


def variable(entity="token", **changes):
    return {"id": entity, "name": entity, "value_type": "string",
            "source": {"kind": "extracted", "extractor_id": "extract"},
            "initialize": {"event": "after_response", "exchange_id": "producer"},
            "scope": "iteration", "reset": "iteration_start", **changes}


def extractor(entity="extract", **changes):
    return {"id": entity, "exchange_id": "producer", "area": "response_body",
            "selector": {"dialect": "json_path_subset_v1", "expression": "$.token"},
            "cardinality": "exactly_one", "allow_empty": False,
            "on_failure": "mark_error_and_continue", **changes}


def slot(entity="token", encoding="json", binding="body"):
    return RuntimeSlot(binding, entity, encoding)


def plan(kind="string", scope="iteration", header=False, extra_variables=(), extra_extractors=()):
    native = tuple(NativeRequest(name, "group" if i < 2 else "later-group", "POST" if i else "GET",
                                 "https://synthetic.invalid/" + name)
                   for i, name in enumerate(["producer", "consumer", "later"]))
    first, consumer, later = native
    live = (
        RuntimeRequest(first.id, RuntimeTemplate((first.url,)), (), None),
        RuntimeRequest(consumer.id, RuntimeTemplate(("https://synthetic.invalid/a%20b/",
                       slot(encoding="path", binding="path"),
                       "?q=", slot(encoding="form", binding="query"))),
                       (("Content-Type", RuntimeTemplate(("application/json",))),) +
                       ((("X-Token", RuntimeTemplate((
                           slot(encoding="header", binding="header"),))),)
                        if header else ()),
                       RuntimeTemplate((slot(),))),
        RuntimeRequest(later.id, RuntimeTemplate((later.url,)), (),
                       RuntimeTemplate((slot(binding="later-body"),))),
    )
    model = RuntimePlan((variable(value_type=kind, scope=scope, reset=scope + "_start"),
                         *extra_variables),
                        (extractor(), *extra_extractors), live)
    return RequestPlan(native, (NativeGroup("group", "Synthetic", (first.id, consumer.id)),
                                NativeGroup("later-group", "Later", (later.id,))), runtime=model)


def execute(tmp_path, model, checks):
    binary = lifecycle_binary(tmp_path, model, checks)
    result = isolated([binary], binary.parent)
    (tmp_path / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "LIFECYCLE_OK"
    assert not result.stderr


def response_setup(body):
    return (f"scheduled_response = (const unsigned char *){literal(body)}; "
            "scheduled_response_size = strlen((const char *)scheduled_response);")


@pytest.mark.parametrize("kind,body,expected", [
    ("number", '{"token":123456789012345678901.00001}', "123456789012345678901.00001"),
    ("integer", '{"token":-0.00e-999}', "-0.00e-999"),
    ("boolean", '{"token":false}', "false"), ("null", '{"token":null}', "null"),
    ("string", '{"token":"ERROR_token"}', '"ERROR_token"'),
])
def test_typed_success_then_failed_capture_is_quoted_never_stale(tmp_path, kind, body, expected):
    execute(tmp_path, plan(kind), f"""
int main(void) {{
    stub_reset(); assert(vuser_init() == 0); assert(option_calls == 2);
    {response_setup(body)}
    assert(Action() == 0); assert(request_count == 3 && transaction_count == 2);
    assert(!strcmp(requests[1].body, {literal(expected)}));
    assert(!strcmp(requests[2].body, {literal(expected)}));
    assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);
    assert(eval_calls == 1 && eval_frees == 1 && save_calls == 2);
    assert(!saved_parameter[0]); assert(requests[1].body_present);
    assert(!strcmp(requests[1].method, "POST"));
    assert(!strcmp(requests[1].mime, "application/json"));
    /* Retain scheduled HTTP data, but failed registration cannot publish it. */
    register_rc = LR_FAIL; assert(Action() == 0);
    assert(request_count == 6 && transaction_count == 4);
    assert(!strcmp(requests[4].body, "\\\"ERROR_token\\\""));
    assert(!strcmp(requests[5].body, "\\\"ERROR_token\\\""));
    assert(transactions[2] == LR_FAIL && transactions[3] == LR_FAIL);
    assert(eval_calls == 1 && eval_frees == 1);
    assert(cookie_cleanups == 2 && cache_cleanups == 2);
    assert(vuser_end() == 0); assert(live_allocations == 0);
    puts("LIFECYCLE_OK"); return 0;
}}
""")


@pytest.mark.parametrize("failure", ["register_rc = LR_FAIL", "max_length_rc = LR_FAIL",
                                    "save_fail_at = save_calls", "eval_rc = LR_FAIL",
                                    "fail_http_at = request_count"])
def test_session_producer_failure_invalidates_prior_iteration_token(tmp_path, failure):
    execute(tmp_path, plan(scope="session"), f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"FRESH"}')}
    Action(); assert(!strcmp(requests[1].body, "\\\"FRESH\\\""));
    {failure}; Action();
    assert(request_count == 6);
    assert(!strcmp(requests[4].body, "\\\"ERROR_token\\\""));
    assert(!strcmp(requests[5].body, "\\\"ERROR_token\\\""));
    assert(transactions[2] == LR_FAIL && transactions[3] == LR_FAIL);
    assert(eval_calls == eval_frees); vuser_end(); assert(live_allocations == 0);
    puts("LIFECYCLE_OK"); return 0;
}}
""")


def test_header_rejection_is_local_and_later_group_remains_successful(tmp_path):
    value = 'a b/é%\\\r\n"'
    import json
    execute(tmp_path, plan(header=True), f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup(json.dumps({'token': value}))}
    Action(); assert(request_count == 3);
    assert(!strcmp(requests[1].header, "ERROR_token"));
    assert(!requests[2].header[0]);
    assert(!strcmp(requests[1].url, "https://synthetic.invalid/a%20b/a%20b%2F%C3%A9%25%5C%0D%0A%22?q=a+b%2F%C3%A9%25%5C%0D%0A%22"));
    assert(!strcmp(requests[1].body, requests[2].body));
    assert(strstr(requests[2].body, "ERROR_token") == NULL);
    assert(transactions[0] == LR_FAIL && transactions[1] == LR_AUTO);
    assert(!strcmp({symbol('VALUE', 'token')}()->data, {literal(value)}));
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


@pytest.mark.parametrize("failure", ["header_rc = LR_FAIL", "save_fail_at = 1"])
def test_service_failures_explicitly_fail_only_affected_transaction(tmp_path, failure):
    execute(tmp_path, plan(header=True), f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"fresh"}')}
    {failure}; Action();
    assert(request_count == 3 && error_calls > 0);
    assert(!strcmp(requests[1].body, "\\\"fresh\\\""));
    assert(!strcmp(requests[2].body, "\\\"fresh\\\""));
    assert(transactions[0] == LR_FAIL && transactions[1] == LR_AUTO);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


@pytest.mark.parametrize("order", ["bad-first", "good-first"])
def test_independent_unbound_failure(tmp_path, order):
    unbound = extractor("unbound", selector={"dialect": "json_path_subset_v1",
                                            "expression": "$.missing"})
    model = plan(extra_extractors=(unbound,))
    if order == "bad-first":
        model = replace(model, runtime=replace(model.runtime,
                        extractors=tuple(reversed(model.runtime.extractors))))
    execute(tmp_path, model, f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"fresh"}')}
    Action(); assert(request_count == 3);
    assert(!strcmp(requests[1].body, "\\\"fresh\\\""));
    assert(!strcmp(requests[2].body, "\\\"fresh\\\""));
    assert(error_calls > 0);
    assert(transactions[0] == LR_FAIL && transactions[1] == LR_AUTO);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


def early_consumer_plan():
    variables = (variable("session", scope="session", reset="session_start"),
                 variable("iteration"))
    native = tuple(NativeRequest(name, "early" if name == "early" else "fresh", "POST",
                                 "https://synthetic.invalid/" + name)
                   for name in ("early", "producer", "after"))
    live = tuple(RuntimeRequest(request.id, RuntimeTemplate((request.url,)), (),
                                None if request.id == "producer" else RuntimeTemplate((
                                    "[", slot("session", binding=request.id + "-session"), ",",
                                    slot("iteration", binding=request.id + "-iteration"), "]")))
                 for request in native)
    return RequestPlan(native, (NativeGroup("early", "Early", ("early",)),
                                NativeGroup("fresh", "Fresh", ("producer", "after"))),
                       runtime=RuntimePlan(variables, (extractor(),), live))


def early_consumer_checks():
    return f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"first"}')}
    Action(); assert(request_count == 3 && transaction_count == 2);
    assert(!strcmp(requests[0].body, "[\\\"ERROR_session\\\",\\\"ERROR_iteration\\\"]"));
    assert(!strcmp(requests[2].body, "[\\\"first\\\",\\\"first\\\"]"));
    assert(transactions[0] == LR_FAIL && transactions[1] == LR_AUTO);
    {response_setup('{"token":"second"}')}
    Action(); assert(request_count == 6 && transaction_count == 4);
    if (strcmp(requests[3].body, "[\\\"first\\\",\\\"ERROR_iteration\\\"]")) {{
        fputs("EARLY_ITERATION_RESET: session persists, iteration expires\\n", stderr);
        return 3;
    }}
    assert(!strcmp(requests[5].body, "[\\\"second\\\",\\\"second\\\"]"));
    assert(transactions[2] == LR_FAIL && transactions[3] == LR_AUTO);
    assert(eval_calls == 2 && eval_frees == 2);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
"""


def test_early_consumer_distinguishes_session_retention_from_iteration_reset(tmp_path):
    execute(tmp_path, early_consumer_plan(), early_consumer_checks())


def test_early_consumer_control_detects_removed_action_start_reset(tmp_path):
    """Only this trusted synthetic generated artifact is mutated, never production."""
    lifecycle_binary(tmp_path, early_consumer_plan(), early_consumer_checks())
    action = tmp_path / "Action.c"
    original = action.read_text()
    (tmp_path / "Action.original.c").write_text(original)
    declarations, body = original.split("int Action(void) {", 1)
    before_group, remainder = body.split("lr_start_transaction", 1)
    reset = f"    {symbol('RESET_FN', 'iteration')}();\n"
    assert before_group.count(reset) == 1
    mutated = (declarations + "int Action(void) {" + before_group.replace(reset, "", 1)
               + "lr_start_transaction" + remainder)
    assert original.count(reset) == mutated.count(reset) + 1
    action.write_text(mutated)
    (tmp_path / "mutation.txt").write_text("Removed only Action-start iteration reset:\n" + reset)
    binary = compile_source(tmp_path, (tmp_path / "driver.c").read_text())
    with pytest.raises(CalledProcessError) as failure:
        run_isolated([str(binary)], tmp_path)
    assert failure.value.returncode == 3
    assert "EARLY_ITERATION_RESET:" in failure.value.stderr
    (tmp_path / "expected-mutation-failure.txt").write_text(failure.value.stderr)


def test_scope_events_request_expiration_session_restart_and_failed_http_literal(tmp_path):
    extra = (
        variable("session", scope="session", reset="session_start",
                 source={"kind": "literal", "value": "SESSION"},
                 initialize={"event": "session_start"}),
        variable("iteration", source={"kind": "literal", "value": "ITERATION"},
                 initialize={"event": "iteration_start"}),
        variable("request", scope="request", reset="before_request",
                 source={"kind": "literal", "value": "REQUEST"},
                 initialize={"event": "before_request", "exchange_id": "consumer"}),
        variable("after", scope="session", reset="session_start",
                 source={"kind": "literal", "value": "AFTER"}),
    )
    model = plan(extra_variables=extra)
    first, consumer, later = model.runtime.requests
    fields = tuple(part for name in ("session", "iteration", "request", "after")
                   for part in ("/", slot(name, "path", name)))
    url = RuntimeTemplate(("https://synthetic.invalid", *fields))
    model = replace(model, runtime=replace(model.runtime, requests=(
        first, replace(consumer, url=url), replace(later, url=url))))
    access = {name: symbol("VALUE", name) + "()"
              for name in ("session", "iteration", "request", "after", "token")}
    execute(tmp_path, model, f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"fresh"}')}
    assert(!strcmp({access['session']}->data, "SESSION"));
    assert({access['iteration']}->data == NULL && {access['after']}->data == NULL);
    Action();
    assert(!strcmp(requests[1].url, "https://synthetic.invalid/SESSION/ITERATION/REQUEST/AFTER"));
    assert(!strcmp(requests[2].url, "https://synthetic.invalid/SESSION/ITERATION/ERROR_request/AFTER"));
    assert({access['request']}->data == NULL);
    assert(transactions[0] == LR_AUTO && transactions[1] == LR_FAIL);
    /* Iteration literal must be reinitialized; after_response literal runs despite failed HTTP. */
    h2l_set({access['iteration']}, "STALE", 5, 1, 0); h2l_reset({access['after']});
    fail_http_at = request_count; Action();
    assert(!strcmp(requests[4].url, "https://synthetic.invalid/SESSION/ITERATION/REQUEST/AFTER"));
    assert(!strcmp(requests[4].body, "\\\"ERROR_token\\\""));
    vuser_end(); assert(live_allocations == 0);
    assert({access['session']}->data == NULL && {access['iteration']}->data == NULL);
    assert({access['after']}->data == NULL);
    vuser_init(); assert(!strcmp({access['session']}->data, "SESSION"));
    assert({access['token']}->data == NULL && {access['after']}->data == NULL);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


def test_every_runtime_allocation_failure_is_bounded_clean_and_marks_failure(tmp_path):
    extra = (variable("session", scope="session", reset="session_start",
                      source={"kind": "literal", "value": "fixed"},
                      initialize={"event": "session_start"}),
             variable("iteration", source={"kind": "literal", "value": "fixed"},
                      initialize={"event": "iteration_start"}))
    execute(tmp_path, plan(extra_variables=extra), f"""
int main(void) {{
    int fault, allocations;
    stub_reset(); vuser_init(); {response_setup('{"token":"fresh"}')}
    Action(); allocations = alloc_attempts; vuser_end(); assert(live_allocations == 0);
    assert(allocations > 10 && allocations < 100);
    for (fault = 0; fault < allocations; ++fault) {{
        stub_reset(); alloc_fail_at = fault;
        vuser_init(); {response_setup('{"token":"fresh"}')}
        Action();
        assert(request_count == 3 && transaction_count == 2);
        assert(error_calls > 0);
        assert(transactions[0] == LR_FAIL || transactions[1] == LR_FAIL);
        if (fault < 2) assert(transactions[0] == LR_FAIL); /* session/iteration initialization */
        assert(strstr(requests[1].body, "STALE") == NULL);
        assert(eval_calls == eval_frees);
        vuser_end(); assert(live_allocations == 0);
    }}
    puts("LIFECYCLE_OK"); return 0;
}}
""")


@pytest.mark.parametrize("expression,body,expected", [
    ('$["a.b"][1]["a\\\"b"]', '{"a.b":[0,{"a\\\"b":42}]}', "42"),
    ('$[999999999999999999999999999999999999999999]', '[1]', '"ERROR_token"'),
    ('$', 'null', "null"),
])
def test_generated_json_selector_paths_not_only_helper_calls(tmp_path, expression, body, expected):
    model = plan("null" if body == "null" else "number")
    ext = extractor(selector={"dialect": "json_path_subset_v1", "expression": expression})
    model = replace(model, runtime=replace(model.runtime, extractors=(ext,)))
    execute(tmp_path, model, f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup(body)}
    Action(); assert(request_count == 3);
    assert(!strcmp(requests[1].body, {literal(expected)}));
    assert(!strcmp(requests[2].body, {literal(expected)}));
    assert(transactions[0] == {"LR_FAIL" if "ERROR_token" in expected else "LR_AUTO"});
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


@pytest.mark.parametrize("encoding,expected", [("text", "a b/+%"), ("form", "a+b%2F%2B%25"),
                                             ("json", '"a b/+%"')])
def test_generated_boundary_extraction_and_body_encoding(tmp_path, encoding, expected):
    model = plan()
    ext = extractor(selector={"dialect": "boundary_v1", "left": "<id>", "right": "</id>"},
                    cardinality="occurrence", occurrence=2)
    first, consumer, later = model.runtime.requests
    template = RuntimeTemplate(("prefix:", slot(encoding=encoding), ":suffix"))
    model = replace(model, runtime=replace(model.runtime, extractors=(ext,), requests=(
        first, replace(consumer, body=template), later)))
    execute(tmp_path, model, f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('<id>ignored</id><id>a b/+%</id>')}
    Action(); assert(!strcmp(requests[1].body, {literal('prefix:' + expected + ':suffix')}));
    assert(!strcmp(requests[2].body, "\\\"a b/+%\\\""));
    assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")


def test_empty_generated_raw_body_is_present_and_nonnull(tmp_path):
    model = plan()
    first, consumer, later = model.runtime.requests
    model = replace(model, runtime=replace(model.runtime, requests=(first,
                    replace(consumer, body=RuntimeTemplate(("",))), later)))
    execute(tmp_path, model, f"""
int main(void) {{
    stub_reset(); vuser_init(); {response_setup('{"token":"fresh"}')}
    Action(); assert(requests[0].body_present == 0);
    assert(requests[1].body_present == 1 && requests[1].body_size == 0);
    assert(transactions[0] == LR_AUTO);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}}
""")
