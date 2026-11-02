"""Offline runtime scaffold contracts; no C compilation or native execution."""

from dataclasses import replace
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from har2load.loadrunner_runtime import fallback, symbol
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate

ACTION = "loadrunner/Action.c"


def variable(**changes: JSONValue) -> dict[str, JSONValue]:
    return {"id": "token", "name": "token", "value_type": "string",
            "source": {"kind": "extracted", "extractor_id": "extract"},
            "initialize": {"event": "after_response", "exchange_id": "producer"},
            "scope": "iteration", "reset": "iteration_start", **changes}


def extractor(**changes: JSONValue) -> dict[str, JSONValue]:
    return {"id": "extract", "exchange_id": "producer", "area": "response_body",
            "selector": {"dialect": "boundary_v1", "left": "<id>", "right": "</id>"},
            "cardinality": "exactly_one", "allow_empty": False,
            "on_failure": "mark_error_and_continue", **changes}


def plan(var: dict[str, JSONValue] | None = None,
         ext: dict[str, JSONValue] | None = None, encoding: str = "json") -> RequestPlan:
    first = NativeRequest("producer", "group", "GET", "https://example.invalid/start")
    second = NativeRequest("consumer", "group", "POST", "https://example.invalid/RECORDED",
                           (("Content-Type", "application/json"),), b'"RECORDED"', "json")
    runtime = RuntimePlan((var or variable(),), (ext or extractor(),), (
        RuntimeRequest(first.id, RuntimeTemplate((first.url,)), (), None),
        RuntimeRequest(second.id, RuntimeTemplate(("https://example.invalid/?id=",
                       RuntimeSlot("query", "token", "form"))),
                       (("Content-Type", RuntimeTemplate(("application/json",))),),
                       RuntimeTemplate((RuntimeSlot("body", "token", encoding),))),
    ))
    return RequestPlan((first, second), (NativeGroup("group", "Synthetic", (first.id, second.id)),),
                       runtime=runtime)


def test_live_correlation_precedes_binding_without_recorded_fallback() -> None:
    build = LoadRunnerGenerator().generate(plan())
    assert build.status == "generated"
    source = build.files[ACTION].decode()
    assert build.metadata["profile"] == "scalar_runtime_v1"
    assert "RECORDED" not in source
    assert source.index("web_reg_save_param_ex(") < source.index('"REQ_0001"')
    assert source.index('"REQ_0001"') < source.index('"<id>"') < source.index('"REQ_0002"')
    assert "RAW_BODY_START" in source and "h2l_slot(" in source
    assert "ERROR_token" in source and "lr_continue_on_error(LR_ON_ERROR_CONTINUE)" in source


@pytest.mark.parametrize("kind", ["external", "generated", "agent_code"])
def test_unsupported_sources_block(kind: str) -> None:
    build = LoadRunnerGenerator().generate(plan(variable(source={"kind": kind})))
    assert build.status == "blocked" and not build.files
    assert any(d.code.startswith("LOADRUNNER_RUNTIME_") for d in build.diagnostics)


@pytest.mark.parametrize("kind", ["string", "integer", "number", "boolean", "null"])
def test_json_extraction_generates_strict_scalar_type_check(kind: str) -> None:
    ext = extractor(selector={"dialect": "json_path_subset_v1", "expression": '$["a.b"][0]'})
    build = LoadRunnerGenerator().generate(plan(variable(value_type=kind), ext))
    assert build.status == "generated"
    source = build.files[ACTION].decode()
    assert '"a.b"' in source and "h2l_json_select(" in source
    assert "h2l_accept(" in source and "h2l_json_parse(" in source
    assert "web_reg_save_param_json" not in source


@pytest.mark.parametrize("kind,value", [("integer", Decimal("12345678901234567890")),
                                      ("number", Decimal("0.00000000000000000123")),
                                      ("boolean", True), ("null", None)])
def test_literal_scalar_types_are_not_stringified(kind: str, value: JSONValue) -> None:
    var = variable(value_type=kind, source={"kind": "literal", "value": value},
                   initialize={"event": "iteration_start"})
    build = LoadRunnerGenerator().generate(plan(var))
    assert build.status == "generated"
    assert "h2l_accept(" in build.files[ACTION].decode()


def test_unsupported_unreferenced_extractor_does_not_disappear() -> None:
    value = plan()
    assert value.runtime is not None
    extra = extractor(id="unused", area="response_cookies",
                      selector={"dialect": "name_v1", "name": "cookie"})
    runtime = replace(value.runtime, extractors=(*value.runtime.extractors, extra))
    assert LoadRunnerGenerator().generate(replace(value, runtime=runtime)).status == "blocked"


def test_source_maps_are_real_unique_and_deterministic() -> None:
    build = LoadRunnerGenerator().generate(plan())
    assert build == LoadRunnerGenerator().generate(plan())
    kinds = {item["kind"] for item in build.source_map}
    assert {"variable", "extractor", "initialize", "reset", "binding", "request", "group"} <= kinds
    assert all(item["entity_id"] == "token" for item in build.source_map
               if item["kind"] in ("variable", "initialize", "reset"))
    for item in build.source_map:
        artifact = cast(str, item["artifact"])
        assert build.files[artifact].count(cast(str, item["marker"]).encode()) == 1


def test_budget_blocks_without_partial_native_files() -> None:
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", 100):
        build = LoadRunnerGenerator().generate(plan())
    assert build.status == "blocked" and not build.files and not build.source_map


def action(value: RequestPlan) -> str:
    source = LoadRunnerGenerator().generate(value).files[ACTION].decode()
    return source.split("int Action(void) {")[1]


def with_header(value: RequestPlan, template: RuntimeTemplate) -> RequestPlan:
    assert value.runtime is not None
    first, second = value.runtime.requests
    second = replace(second, headers=(*second.headers, ("X-Token", template)))
    return replace(value, runtime=replace(value.runtime, requests=(first, second)))


def test_failed_typed_json_is_quoted_without_treating_marker_prefix_as_failure() -> None:
    ext = extractor(selector={"dialect": "json_path_subset_v1", "expression": "$.id"})
    build = LoadRunnerGenerator().generate(plan(variable(value_type="number"), ext))
    source = build.files[ACTION].decode()
    assert "quoted = mode == 4 && (!v->data || v->type == 1)" in source
    assert "expected == 7 && source->type == 2 && source->integral" in source
    assert "strncmp" not in source and 'strcmp(source->data, "ERROR_' not in source
    assert '"\\\"ERROR_token\\\""' in source
    assert build.metadata["json_failure_wire_policy"] == "quoted_error_marker"


def test_capture_reset_failure_checks_ownership_and_terminator() -> None:
    source = LoadRunnerGenerator().generate(plan()).files[ACTION].decode()
    body = action(plan())
    assert body.index('lr_save_string("", "H2L_RESPONSE")') < body.index("web_reg_save_param_ex")
    assert '"LB=", "RB="' in body and '"Ordinal=' not in body and '"Scope=Body"' in body
    assert "h2l_capture(h2l_ok && h2l_rc == LR_PASS)" in body
    assert "copy[n] = 0" in source and "n <= H2L_FIELD_LIMIT" in source
    assert "if (native) { owned = native; lr_eval_string_ext_free(&owned); }" in source
    assert "free(h2l_response)" in body
    assert "lr_eval_string_ext(data" not in source


def test_boundary_uses_first_left_and_counts_all_matches_before_acceptance() -> None:
    ext = extractor(selector={"dialect": "boundary_v1", "left": "{", "right": "}"})
    source = LoadRunnerGenerator().generate(plan(ext=ext)).files[ACTION].decode()
    assert 'h2l_boundary(&result, h2l_response, "{", "}", 1, 1, 0)' in source
    assert "start = strstr(body, left)" in source and "end = strstr(start, right)" in source
    assert "body = end + strlen(right)" in source
    assert "count > H2L_MATCH_LIMIT" in source and "exact && count != 1" in source
    recorded = "{a{b{c}"
    assert recorded[recorded.find("{") + 1:recorded.find("}")] == "a{b{c"


@pytest.mark.parametrize("occurrence,empty", [(1, False), (2, True), (1001, False)])
def test_occurrence_and_empty_policy_are_emitted(occurrence: int, empty: bool) -> None:
    source = action(plan(ext=extractor(cardinality="occurrence", occurrence=occurrence,
                                     allow_empty=empty)))
    assert f'"</id>", {occurrence}, 0, {int(empty)})' in source


def test_iteration_and_producer_resets_precede_saved_value_consumption() -> None:
    source = action(plan())
    reset = symbol("RESET_FN", "token") + "();"
    init = symbol("INIT_FN", "token") + "(&result);"
    assert source.count(reset) == 2
    assert source.index(reset) < source.index("lr_start_transaction")
    assert source.rindex(reset) < source.index("web_reg_save_param_ex")
    assert source.index('"REQ_0001"') < source.index(init) < source.index('"REQ_0002"')


def test_session_later_initializer_persists_until_producer_but_never_reuses_failed_value() -> None:
    value = plan(variable(scope="session", reset="session_start"))
    source = action(value)
    assert source.count(symbol("RESET_FN", "token") + "();") == 1
    assert source.index(symbol("RESET_FN", "token")) < source.index("web_reg_save_param_ex")
    files = LoadRunnerGenerator().generate(value).files
    assert b"h2l_session_start();" in files["loadrunner/vuser_init.c"]
    assert b"h2l_session_end();" in files["loadrunner/vuser_end.c"]
    assert b"static h2l_value" not in files["loadrunner/globals.h"]


def test_request_scope_initializes_only_at_selected_request_then_expires() -> None:
    value = plan(variable(scope="request", reset="before_request",
                          initialize={"event": "before_request", "exchange_id": "consumer"},
                          source={"kind": "literal", "value": "fresh"}))
    source = action(value)
    init = symbol("INIT_FN", "token") + "(NULL);"
    reset = symbol("RESET_FN", "token") + "();"
    assert source.count(init) == 1
    assert source.index('"REQ_0001"') < source.index(init) < source.index('"REQ_0002"')
    assert source.rindex(reset) > source.index('"REQ_0002"')


def test_header_rejection_is_binding_local_and_preserves_later_json_consumers() -> None:
    value = with_header(plan(), RuntimeTemplate((RuntimeSlot("header", "token", "header"),)))
    first, second = value.runtime.requests
    later = replace(second, id="later", headers=first.headers, url=first.url,
                    body=RuntimeTemplate((RuntimeSlot("later_body", "token", "json"),)))
    value = replace(value, requests=(*value.requests, replace(value.requests[1], id="later")),
                    groups=(NativeGroup("group", "Synthetic", ("producer", "consumer", "later")),),
                    runtime=replace(value.runtime, requests=(first, second, later)))
    source = action(value)
    assert "h2l_header_check(" not in source
    assert "web_add_header(\"X-Token\", header_1)" in source
    complete = LoadRunnerGenerator().generate(value).files[ACTION].decode()
    assert 'strchr("{}\\\\%", *s)' in complete
    assert "*s < 32 || *s > 126" in complete
    slot = complete.split("static void h2l_slot(")[1].split("static char *h2l_capture")[0]
    assert "mode == 1 && !h2l_header_safe(s)" in slot
    assert "h2l_reset" not in slot and "h2l_error(v)" not in slot
    assert source.count(f"h2l_slot(&buffer_body, {symbol('VALUE', 'token')}(), 4)") == 2
    assert source.count(symbol("RESET_FN", "token") + "();") == 2


@pytest.mark.parametrize("encoding,mode", [("text", 0), ("form", 3), ("json", 4)])
def test_body_encodings_are_explicit_and_only_slots_are_encoded(encoding: str, mode: int) -> None:
    source = action(plan(encoding=encoding))
    assert f"h2l_slot(&buffer_body, {symbol('VALUE', 'token')}(), {mode});" in source
    assert 'h2l_append(&buffer_url, "https://example.invalid/\\077id=",' in source
    assert "RAW_BODY_START, body, (int)strlen(body), RAW_BODY_END" in source


def test_path_encoding_and_fallback_never_reencode_literal_parts() -> None:
    value = plan()
    assert value.runtime is not None
    url = RuntimeTemplate(("https://example.invalid/a%20b/", RuntimeSlot("path", "token", "path")))
    first, second = value.runtime.requests
    runtime = replace(value.runtime, requests=(first, replace(second, url=url)))
    source = action(replace(value, runtime=runtime))
    assert "a%20b/ERROR_token" in source and "a%2520b" not in source
    assert f"h2l_slot(&buffer_url, {symbol('VALUE', 'token')}(), 2);" in source
    assert fallback(url, runtime) == "https://example.invalid/a%20b/ERROR_token"


def test_empty_body_uses_allocated_nonnull_buffer_and_request_scoped_release() -> None:
    value = plan()
    assert value.runtime is not None
    first, second = value.runtime.requests
    runtime = replace(value.runtime, requests=(first, replace(second, body=RuntimeTemplate(("",)))))
    source = action(replace(value, runtime=runtime))
    assert "h2l_open(&buffer_body, 0);" in source
    assert "RAW_BODY_START, body, (int)strlen(body), RAW_BODY_END" in source
    assert "free(buffer_body.data)" in source


@pytest.mark.parametrize("text", ['{native}', 'back\\slash', '\r\ninjected', '\t', '\u00e9'])
def test_unsafe_literal_header_is_blocked_not_transformed(text: str) -> None:
    build = LoadRunnerGenerator().generate(with_header(plan(), RuntimeTemplate((text,))))
    assert build.status == "blocked" and not build.files


def test_literal_data_escaping_preserves_trigraphs_crlf_quotes_unicode_as_data() -> None:
    value = '??/"\\%\r\n\u00e9'
    var = variable(source={"kind": "literal", "value": value},
                   initialize={"event": "iteration_start"})
    source = LoadRunnerGenerator().generate(plan(var)).files[ACTION].decode()
    assert "??/" not in source and "\\077\\077/" in source
    assert "\\015\\012\\303\\251" in source and "\\\\%" in source


def test_decoded_nul_in_json_path_is_blocked() -> None:
    ext = extractor(selector={"dialect": "json_path_subset_v1", "expression": '$["\\u0000"]'})
    build = LoadRunnerGenerator().generate(plan(ext=ext))
    assert build.status == "blocked" and not build.files


def test_global_budget_exact_boundary_and_no_native_certification() -> None:
    value = plan()
    expected = LoadRunnerGenerator().generate(value)
    total = sum(map(len, expected.files.values()))
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total):
        assert LoadRunnerGenerator().generate(value) == expected
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total - 1):
        assert LoadRunnerGenerator().generate(value).status == "blocked"
    for key, result in {"project_status": "incomplete", "template_status": "pending",
                        "native_validation": "not_run", "compatibility": "unverified",
                        "target_version": None}.items():
        assert expected.metadata[key] == result


def test_variable_locator_names_the_actual_state_accessor_not_action() -> None:
    build = LoadRunnerGenerator().generate(plan())
    row = next(item for item in build.source_map if item["kind"] == "variable")
    locator = cast(dict[str, JSONValue], row["locator"])
    assert locator["function"] == symbol("VALUE", "token")
    source = build.files[ACTION].decode()
    assert f"static h2l_value *{locator['function']}(void) {{" in source
    assert f"    /* {row['marker']} */\n    static h2l_value value" in source


def test_worst_case_field_limit_blocks_before_amplifying_c_literals() -> None:
    value = plan()
    assert value.runtime is not None
    body = RuntimeTemplate(tuple(RuntimeSlot(f"body_{i}", "token", "json") for i in range(3)))
    first, second = value.runtime.requests
    runtime = replace(value.runtime, requests=(first, replace(second, body=body)))
    with patch("har2load.loadrunner._literal", side_effect=AssertionError("Expanded C")):
        build = LoadRunnerGenerator().generate(replace(value, runtime=runtime))
    assert build.status == "blocked" and not build.files
    assert any(d.code == "LOADRUNNER_RUNTIME_FIELD_LIMIT" for d in build.diagnostics)


def test_literal_after_response_event_runs_after_response_not_at_session_start() -> None:
    var = variable(source={"kind": "literal", "value": "fresh"}, scope="session",
                   reset="session_start")
    source = action(plan(var))
    init = symbol("INIT_FN", "token") + "(NULL);"
    assert source.index('"REQ_0001"') < source.index(init) < source.index('"REQ_0002"')
    assert source.count(init) == 1


def test_unreferenced_json_extractor_is_emitted_and_whole_document_validated() -> None:
    value = plan()
    assert value.runtime is not None
    extra = extractor(id="unused", selector={"dialect": "json_path_subset_v1", "expression": "$"})
    runtime = replace(value.runtime, extractors=(*value.runtime.extractors, extra))
    build = LoadRunnerGenerator().generate(replace(value, runtime=runtime))
    assert build.status == "generated"
    source = build.files[ACTION].decode()
    assert symbol("EXTRACTOR", "unused") in source and "h2l_json_parse(&document" in source
    assert any(item["kind"] == "extractor" and item["entity_id"] == "unused"
               for item in build.source_map)


def test_runtime_failures_explicitly_fail_transactions_without_aborting_http():
    value = plan()
    source = LoadRunnerGenerator().generate(value).files[ACTION].decode()
    body = action(value)
    assert 'lr_end_transaction("TC_0001", h2l_transaction_failed ? LR_FAIL : LR_AUTO)' in body
    assert body.index("h2l_transaction_failed = 0;") > body.index("lr_end_transaction")
    assert "if (!v->data) h2l_fail(v->error)" in source
    assert "h2l_transaction_failed = 1; lr_error_message" in source
    assert "h2l_capture(h2l_ok && h2l_rc == LR_PASS)" in body
    assert "h2l_transaction_failed &&" not in body
    assert body.count("web_custom_request(") == 2


def test_session_and_iteration_initialization_errors_survive_until_first_group():
    var = variable(source={"kind": "literal", "value": "constant"},
                   initialize={"event": "iteration_start"})
    source = action(plan(var))
    assert source.index(symbol("INIT_FN", "token")) < source.index("lr_start_transaction")
    assert "h2l_transaction_failed = 0" not in source.split("lr_end_transaction", 1)[0]
