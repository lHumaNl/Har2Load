"""Opt-in ASan/UBSan checks of emitted C with independent synthetic header capture."""

from dataclasses import replace
from pathlib import Path

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import RequestPlan
from har2load.loadrunner_runtime import helpers, symbol
from har2load.runtime_types import RuntimeSlot, RuntimeTemplate
from tests.test_loadrunner_extensions import block
from tests.test_loadrunner_extensions import variable as authored_variable
from validation.c_runtime.harness import (
    POSTLUDE,
    PRELUDE,
    c_literal,
    compile_source,
    isolated,
)
from validation.c_runtime.test_lifecycle import (
    early_consumer_checks,
    early_consumer_plan,
    execute,
    extractor,
    plan,
    response_setup,
    variable,
)
from validation.header_cases import header_cases

_DRIVER = r'''
static char *read_fixture(const char *path, size_t *n) {
    FILE *file = fopen(path, "rb"); char *data = malloc(H2L_FIELD_LIMIT + 2);
    assert(file && data); *n = fread(data, 1, H2L_FIELD_LIMIT + 1, file);
    assert(!ferror(file)); data[*n] = 0; fclose(file); return data;
}
int main(int argc, char **argv) {
    h2l_headers headers; h2l_value value = {NULL, "ERROR_token", 0, 0};
    char *data, *expected, *captured; size_t n, expected_size; int ok;
    assert(argc == 8); stub_reset(); data = read_fixture(argv[1], &n);
    expected = read_fixture(argv[2], &expected_size); ok = atoi(argv[3]);
    header_response = (const unsigned char *)data; header_response_size = n;
    captured = h2l_headers_capture(1); h2l_headers_parse(&headers, captured);
    h2l_set(&value, "STALE", 5, 1, 0);
    h2l_header_select(&value, &headers, argv[4], strtoul(argv[5], NULL, 10),
                      atoi(argv[6]), atoi(argv[7]));
    assert((value.data != NULL) == ok);
    if (ok) assert(value.type == 1 && !value.integral && strlen(value.data) == expected_size &&
                   !memcmp(value.data, expected, expected_size));
    assert(eval_calls == 1 && headers_eval_calls == 1 && eval_frees == 1 && !error_calls);
    h2l_reset(&value); test_free(captured); assert(!live_allocations);
    free(data); free(expected); puts("HEADERS_OK"); return 0;
}
'''


@pytest.fixture(scope="module")
def header_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = PRELUDE + "\n".join(helpers()) + POSTLUDE + _DRIVER
    return compile_source(tmp_path_factory.mktemp("header-helpers"), source)


@pytest.mark.parametrize("case", header_cases(), ids=lambda case: case["name"])
def test_shared_header_vectors(
    tmp_path: Path, header_binary: Path, case: dict[str, JSONValue],
) -> None:
    text, expected = tmp_path / "capture.bin", tmp_path / "expected.bin"
    text.write_bytes(str(case["text"]).encode("utf-8"))
    expected.write_bytes(str(case["value"] or "").encode("utf-8"))
    result = isolated([header_binary, text, expected, int(bool(case["ok"])), case["selector"],
                       case["occurrence"], int(bool(case["exact"])),
                       int(bool(case["allow_empty"]))], tmp_path)
    (tmp_path / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "HEADERS_OK" and not result.stderr


def header_extractor(**changes: JSONValue) -> dict[str, JSONValue]:
    return extractor(area="response_headers", selector={"dialect": "name_v1", "name": "X-Token"},
                     **changes)


def header_plan(*, both: bool = False, kind: str = "string") -> RequestPlan:
    value = plan(kind, scope="session", header=True)
    assert value.runtime is not None
    variables = value.runtime.variables
    extractors = (header_extractor(),)
    if both:
        variables += (variable("body", source={"kind": "extracted", "extractor_id": "body"}),)
        extractors += (extractor("body"),)
    return replace(value, runtime=replace(value.runtime, variables=variables,
                                         extractors=extractors))


def schedule_headers(text: str) -> str:
    return (f"scheduled_headers = (const unsigned char *){c_literal(text)}; "
            "scheduled_headers_size = strlen((const char *)scheduled_headers);")


def expect_value(name: str, value: str | None) -> str:
    accessor = symbol("VALUE", name) + "()->data"
    return (f"assert(!strcmp({accessor}, {c_literal(value)}));" if value is not None else
            f"assert({accessor} == NULL);")


def main(checks: str) -> str:
    return "int main(void) { stub_reset(); vuser_init();\n" + checks + "\n" + r'''
    assert(eval_calls == eval_frees && error_calls == 0);
    for (int i = 0; i < transaction_count; ++i) assert(transactions[i] == LR_AUTO);
    vuser_end(); assert(live_allocations == 0); puts("LIFECYCLE_OK"); return 0;
}
'''


@pytest.mark.parametrize("fault,bad", [
    ("headers_register_rc = LR_FAIL;", "token"),
    ("register_rc = LR_FAIL;", "body"),
    ("headers_save_fail_at = headers_save_calls;", "token"),
    ("save_fail_at = save_calls;", "body"),
    ("headers_eval_rc = LR_FAIL;", "token"),
    ("eval_rc = LR_FAIL;", "body"),
    ("headers_eval_null = 1;", "token"),
    ("eval_null_output = 1;", "body"),
    (schedule_headers("X-Token: good\n folded\n"), "token"),
    (response_setup("invalid JSON"), "body"),
    ("max_length_rc = LR_FAIL;", "both"),
    ("fail_http_at = request_count;", "both"),
])
def test_channel_failure_independence_and_recovery(tmp_path: Path, fault: str, bad: str) -> None:
    setup = schedule_headers("X-Token: header\n") + response_setup('{"token":"body"}')
    checks = setup + "Action();" + expect_value("token", "header") + expect_value("body", "body")
    checks += fault + "Action();" + expect_value("token", None if bad != "body" else "header")
    checks += expect_value("body", None if bad != "token" else "body")
    checks += ("assert(request_count == 6 && output_calls > 0);"
               "vuser_end(); stub_reset(); vuser_init();")
    checks += setup + "Action();" + expect_value("token", "header") + expect_value("body", "body")
    execute(tmp_path, header_plan(both=True), main(checks))


@pytest.mark.parametrize("null,rc", [(False, "LR_FAIL"), (True, "LR_FAIL"), (True, "LR_PASS")])
def test_header_eval_cleanup_even_null_error(tmp_path: Path, null: bool, rc: str) -> None:
    checks = schedule_headers("X-Token: fresh\n") + "Action();"
    checks += f"headers_eval_null = {int(null)}; headers_eval_rc = {rc}; Action();"
    checks += expect_value("token", None)
    checks += f"assert(eval_calls == 2 && eval_frees == 2 && eval_null_frees == {int(null)});"
    checks += "headers_eval_null = 0; headers_eval_rc = LR_PASS; Action();"
    execute(tmp_path, header_plan(), main(checks + expect_value("token", "fresh")))


@pytest.mark.parametrize("fault", ["headers_register_rc = LR_FAIL;", "http_rc = LR_FAIL;",
                                  "headers_save_fail_at = 0;", "max_length_rc = LR_FAIL;"])
def test_no_header_evaluation_means_no_cleanup(tmp_path: Path, fault: str) -> None:
    checks = schedule_headers("X-Token: fresh\n") + fault + "Action();"
    checks += "assert(eval_calls == 0 && eval_frees == 0);" + expect_value("token", None)
    execute(tmp_path, header_plan(), main(checks))


@pytest.mark.parametrize("text", [b"X-Token: x\0hidden\n", b"X-Token: \xff\n",
                                  b"X-Other: \xc0\x80\nX-Token: x\n"])
def test_invalid_bytes_do_not_poison_body(tmp_path: Path, text: bytes) -> None:
    literal = '"' + "".join(f"\\{byte:03o}" for byte in text) + '"'
    checks = (f"scheduled_headers = (const unsigned char *){literal};"
              f"scheduled_headers_size={len(text)};")
    checks += response_setup('{"token":"body"}') + "Action();"
    checks += expect_value("token", None) + expect_value("body", "body")
    execute(tmp_path, header_plan(both=True), main(checks))


@pytest.mark.parametrize("kind", ["number", "integer", "boolean", "null"])
def test_headers_are_always_strings_not_scalar_json(tmp_path: Path, kind: str) -> None:
    checks = schedule_headers("X-Token: 123\n") + "Action();" + expect_value("token", None)
    checks += 'assert(!strcmp(requests[1].body, "\\\"ERROR_token\\\""));'
    execute(tmp_path, header_plan(kind=kind), main(checks))


@pytest.mark.parametrize("text", ["{H2L_RESPONSE_HEADERS}", "back\\slash", "${untrusted}"])
def test_native_parameter_syntax_is_data_but_header_binding_is_marker(
    tmp_path: Path, text: str,
) -> None:
    checks = schedule_headers("X-Token: " + text + "\n") + "Action();"
    checks += expect_value("token", text) + 'assert(!strcmp(requests[1].header, "ERROR_token"));'
    checks += 'assert(!strstr(requests[1].body, "ERROR_token"));'
    checks += 'assert(!strcmp(requests[1].body, requests[2].body));'
    execute(tmp_path, header_plan(), main(checks))


@pytest.mark.parametrize("bad_value", ["missing", "unicode", "oversize"])
def test_bad_selection_does_not_clear_independent_header(tmp_path: Path, bad_value: str) -> None:
    value = header_plan()
    assert value.runtime is not None
    bad = extractor("bad", area="response_headers", selector={"dialect": "name_v1", "name": "Bad"})
    variables = (*value.runtime.variables,
                 variable("bad", source={"kind": "extracted", "extractor_id": "bad"}))
    value = replace(value, runtime=replace(value.runtime, variables=variables,
                    extractors=(bad, *value.runtime.extractors)))
    text = "X-Token: good\n" + {"missing": "", "unicode": "Bad: é\n",
                                "oversize": "Bad: " + "x" * 65537 + "\n"}[bad_value]
    checks = schedule_headers(text) + "Action();" + expect_value("bad", None)
    execute(tmp_path, value, main(checks + expect_value("token", "good")))


def test_session_retention_and_iteration_reset(tmp_path: Path) -> None:
    value = early_consumer_plan()
    value = replace(value, runtime=replace(value.runtime, extractors=(header_extractor(),)))
    checks = early_consumer_checks()
    for text in ("first", "second"):
        checks = checks.replace(response_setup('{"token":"' + text + '"}'),
                                schedule_headers("X-Token: " + text + "\n"))
    execute(tmp_path, value, checks)


def test_unrelated_response_between_producer_and_consumer_keeps_value(tmp_path: Path) -> None:
    value = header_plan()
    assert value.runtime is not None
    producer, consumer, later = value.runtime.requests
    unrelated = replace(later, body=None)
    native = (value.requests[0], replace(value.requests[2], group_id="group"), value.requests[1])
    groups = (replace(value.groups[0], exchange_ids=("producer", "later", "consumer")),)
    value = replace(value, requests=native, groups=groups, runtime=replace(
        value.runtime, requests=(producer, unrelated, consumer)))
    checks = schedule_headers("X-Token: fresh\n") + "Action();"
    checks += 'assert(!strcmp(requests[2].body, "\\\"fresh\\\""));'
    checks += "assert(headers_registrations == 1 && headers_eval_calls == 1 && save_calls == 0);"
    checks += "assert(!saved_headers[0]);"
    execute(tmp_path, value, main(checks))


def helper_checks(tmp_path: Path, checks: str) -> None:
    source = PRELUDE + "\n".join(helpers()) + POSTLUDE
    source += "int main(void) { stub_reset();" + checks + r'''
    assert(eval_calls == eval_frees && !error_calls && !live_allocations);
    puts("HEADERS_OK"); return 0;
}
'''
    binary = compile_source(tmp_path, source)
    result = isolated([binary], tmp_path)
    (tmp_path / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "HEADERS_OK" and not result.stderr


def test_synthetic_header_publication_requires_matching_registration_and_http(
    tmp_path: Path,
) -> None:
    helper_checks(tmp_path, r'''
    const char *owned; char *out; unsigned long n;
    scheduled_headers = (const unsigned char *)"X-Token: fresh\n"; scheduled_headers_size = 15;
    lr_save_string("X-Token: stale\n", "H2L_RESPONSE_HEADERS");
    web_reg_save_param_ex("ParamName=H2L_RESPONSE", "LB=", "RB=", "NotFound=warning",
                         SEARCH_FILTERS, "Scope=Body", LAST);
    web_custom_request("test", LAST);
    assert(!strcmp((const char *)header_response, "X-Token: stale\n"));
    for (int status = LR_FAIL; status >= LR_PASS; --status) {
        web_reg_save_param_ex("ParamName=H2L_RESPONSE_HEADERS", "LB=", "RB=", "NotFound=warning",
                             SEARCH_FILTERS, "Scope=Headers", "IgnoreRedirections=No", LAST);
        http_rc = status; web_custom_request("test", LAST); assert(!headers_registered);
        assert(!strcmp((const char *)header_response,
                       status ? "X-Token: stale\n" : "X-Token: fresh\n"));
    }
    scheduled_headers = (const unsigned char *)"X-Token: later\n";
    web_custom_request("test", LAST); /* Registration is one-shot, not sticky. */
    assert(!strcmp((const char *)header_response, "X-Token: fresh\n"));
    headers_save_fail_at = headers_save_calls;
    assert(lr_save_string("", "H2L_RESPONSE_HEADERS") == LR_FAIL);
    lr_eval_string_ext("{H2L_RESPONSE_HEADERS}", 22, &out, &n, 0, 0, -1);
    assert(n == 15 && !memcmp(out, "X-Token: fresh\n", n));
    owned = out; lr_eval_string_ext_free(&owned);
    lr_save_string("", "H2L_RESPONSE_HEADERS"); assert(!header_response_size);
    ''')


@pytest.mark.parametrize("channel", ["headers", "body"])
def test_capture_allocation_failure_is_channel_local(tmp_path: Path, channel: str) -> None:
    failed = "h2l_headers_capture" if channel == "headers" else "h2l_capture"
    good = "h2l_capture" if channel == "headers" else "h2l_headers_capture"
    helper_checks(tmp_path, f'''
    char *copy;
    header_response = (const unsigned char *)"X: good"; header_response_size = 7;
    response = (const unsigned char *)"body"; response_size = 4;
    alloc_fail_at = alloc_attempts; assert({failed}(1) == NULL);
    alloc_fail_at = -1; copy = {good}(1); assert(copy); test_free(copy);
    copy = {failed}(1); assert(copy); test_free(copy);
    assert(eval_calls == 3 && eval_frees == 3);
    ''')


@pytest.mark.parametrize("channel", ["headers", "body"])
def test_capture_byte_limit_failure_is_independent(tmp_path: Path, channel: str) -> None:
    checks = schedule_headers("X-Token: header\n") + response_setup('{"token":"body"}')
    target = "scheduled_headers" if channel == "headers" else "scheduled_response"
    checks += "unsigned char *large = malloc(H2L_FIELD_LIMIT + 1); assert(large);"
    checks += "memset(large, 'x', H2L_FIELD_LIMIT + 1);"
    checks += f"{target} = large; {target}_size = H2L_FIELD_LIMIT + 1; Action();"
    checks += expect_value("token", None if channel == "headers" else "header")
    checks += expect_value("body", None if channel == "body" else "body") + "free(large);"
    execute(tmp_path, header_plan(both=True), main(checks))


def test_header_capture_exact_budget_allows_large_unselected_value(tmp_path: Path) -> None:
    helper_checks(tmp_path, r'''
    char *input = malloc(H2L_FIELD_LIMIT), *copy; h2l_headers headers;
    h2l_value value = {NULL, "ERROR_token", 0, 0}; assert(input);
    const char *prefix = "X-Token: good\nX-Other: ";
    memset(input, 'x', H2L_FIELD_LIMIT); memcpy(input, prefix, strlen(prefix));
    header_response = (const unsigned char *)input; header_response_size = H2L_FIELD_LIMIT;
    copy = h2l_headers_capture(1); assert(copy); h2l_headers_parse(&headers, copy);
    h2l_header_select(&value, &headers, "X-Token", 1, 1, 0);
    assert(value.data && !strcmp(value.data, "good")); h2l_reset(&value);
    test_free(copy); free(input);
    ''')


def test_every_header_allocation_fault_recovers_without_native_status_override(
    tmp_path: Path,
) -> None:
    setup = schedule_headers("X-Token: fresh\n")
    checks = setup + "Action(); int allocations = alloc_attempts; vuser_end();"
    checks += "for (int fault = 0; fault < allocations; ++fault) {"
    checks += "stub_reset(); alloc_fail_at = fault; vuser_init();" + setup + "Action();"
    checks += "assert(request_count == 3 && !error_calls && output_calls > 0);"
    checks += "assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);"
    checks += "assert(eval_calls == eval_frees); vuser_end(); assert(!live_allocations); }"
    checks += "stub_reset(); vuser_init();" + setup + "Action();" + expect_value("token", "fresh")
    execute(tmp_path, header_plan(), main(checks))


def test_wrapper_consumes_header_after_initialization_with_independent_output(
    tmp_path: Path,
) -> None:
    value = header_plan()
    assert value.runtime is not None
    output = authored_variable(initialize={"event": "after_response", "exchange_id": "producer"})
    source = ('const h2l_value *input = h2l_input(frame, "token");'
              'if (input && input->data) h2l_emit_text(frame, "value", input->data);')
    wrapper = block(source, inputs=["token"], outputs=["value"],
                    placement={"event": "after_response", "exchange_id": "producer"})
    live = list(value.runtime.requests)
    live[2] = replace(live[2], body=RuntimeTemplate((RuntimeSlot("wrapped", "value", "json"),)))
    value = replace(value, runtime=replace(value.runtime,
                    variables=(*value.runtime.variables, output), blocks=(wrapper,),
                    requests=tuple(live)))
    checks = schedule_headers("X-Token: fresh\n") + "Action();" + expect_value("value", "fresh")
    checks += 'assert(!strcmp(requests[2].body, "\\\"fresh\\\""));'
    execute(tmp_path, value, main(checks))
