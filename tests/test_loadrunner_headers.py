"""Offline response-header lowering contracts; no SDK execution."""

from dataclasses import replace

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from har2load.loadrunner_runtime import symbol
from tests.test_loadrunner_runtime import ACTION, action, extractor, plan


def header_plan(**changes: JSONValue) -> RequestPlan:
    return plan(ext=extractor(area="response_headers",
                             selector={"dialect": "name_v1", "name": "X-Token"}, **changes))


def test_header_only_registers_independent_capture_before_http() -> None:
    build = LoadRunnerGenerator().generate(header_plan())
    assert build.status == "generated"
    body = action(header_plan())
    registration = '"ParamName=H2L_RESPONSE_HEADERS", "LB=", "RB="'
    assert body.index(registration) < body.index('"REQ_0001"')
    assert '"Scope=Headers", "IgnoreRedirections=No", LAST' in body
    assert '"Scope=Body"' not in body and '"Ordinal=' not in body
    assert "HeaderNames=" not in body and "h2l_capture(h2l_ok" not in body
    assert "h2l_headers_capture(h2l_headers_ok && h2l_rc == LR_PASS)" in body
    assert build.metadata["response_header_policy"] == "native_response_headers_v1"
    assert build.metadata["profile_version"] == "5"


def test_both_channels_and_unbound_extractors_have_real_action_markers() -> None:
    value = header_plan()
    assert value.runtime is not None
    value = replace(value, runtime=replace(value.runtime, extractors=(
        *value.runtime.extractors, extractor(id="body-unused"))))
    build = LoadRunnerGenerator().generate(value)
    body = action(value)
    assert '"Scope=Body"' in body and '"Scope=Headers"' in body
    assert "h2l_capture(h2l_ok && h2l_rc == LR_PASS)" in body
    assert body.count("h2l_headers_parse(&headers,") == 1
    for entity in ("extract", "body-unused"):
        assert body.count(symbol("EXTRACTOR", entity)) == 1
        assert any(row["entity_id"] == entity for row in build.source_map)
    assert b"lr_error_message(" not in build.files[ACTION]
    assert b"lr_set_transaction_status" not in build.files[ACTION]


@pytest.mark.parametrize("selector", [
    {"dialect": "name_v1", "name": "X Token"},
    {"dialect": "name_v1", "name": "x" * 257},
    {"dialect": "regex_v1", "expression": "x"},
])
def test_unsupported_header_selector_is_blocked(selector: dict[str, JSONValue]) -> None:
    value = plan(ext=extractor(area="response_headers", selector=selector))
    build = LoadRunnerGenerator().generate(value)
    assert build.status == "blocked" and not build.files
    assert any(d.code == "RUNTIME_RESPONSE_HEADER_SELECTOR_UNSUPPORTED" for d in build.diagnostics)


def test_header_source_has_no_environment_clock_or_target_execution() -> None:
    from unittest.mock import patch

    with patch("builtins.open", side_effect=AssertionError("file read")), \
            patch("os.getenv", side_effect=AssertionError("environment read")), \
            patch("time.time", side_effect=AssertionError("live clock")):
        build = LoadRunnerGenerator().generate(header_plan())
    assert build.status == "generated"
    assert build == LoadRunnerGenerator().generate(header_plan())
    body = action(header_plan())
    assert "h2l_json_parse(" not in body
    assert '"ContentType=' not in body and '"Scope=Cookies"' not in body
    assert '"{H2L_RESPONSE_HEADERS}", 22,' in build.files[ACTION].decode()


def test_all_unbound_headers_are_emitted_even_without_variables() -> None:
    value = header_plan()
    assert value.runtime is not None
    first = value.runtime.requests[0]
    value = replace(value, requests=value.requests[:1],
                    groups=(replace(value.groups[0], exchange_ids=("producer",)),),
                    runtime=replace(value.runtime, variables=(), requests=(first,)))
    build = LoadRunnerGenerator().generate(value)
    assert build.status == "generated"
    body = action(value)
    assert "h2l_header_select(&result, &headers," in body
    assert symbol("EXTRACTOR", "extract") in body
