"""Synthetic, offline evidence for the bounded extraction subset."""

from decimal import Decimal

import pytest

from har2load.domain import Body, Exchange, Har2LoadError, JSONValue
from har2load.extraction import ExtractorChecker, parse_json_path, validate_selector
from har2load.values import matches_type, parse_json_text


def body(text: str | None, status: str = "available") -> Exchange:
    return Exchange("ex_a", 0, {}, response_body=Body(
        "present", text=text, view_status=status,
    ))


def extractor(area: str = "response_body", **updates: JSONValue) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "id": "ext", "exchange_id": "ex_a", "area": area,
        "selector": {"dialect": "json_path_subset_v1", "expression": "$.token"},
        "cardinality": "exactly_one", "allow_empty": False,
        "on_failure": "mark_error_and_continue",
    }
    result.update(updates)
    return result


@pytest.mark.parametrize(("value", "kind", "expected"), [
    (True, "integer", False), (False, "number", False), (1, "integer", True),
    (Decimal("1.0"), "integer", True), (Decimal("1.1"), "integer", False),
    (Decimal("NaN"), "number", False), (float("inf"), "number", False),
    (None, "null", True), ("", "string", True), ("\ud800", "string", False),
    ([], "string", False), (1, "unknown", False), (False, "boolean", True),
])
def test_scalar_types(value: JSONValue, kind: str, expected: bool) -> None:
    assert matches_type(value, kind) is expected


def test_json_precision_null_and_escaped_paths() -> None:
    text = '{"a.b": [{"x\\\"y": 0.12345678901234567890123456789}], "token": null}'
    selector = {"dialect": "json_path_subset_v1", "expression": '$["a.b"][0]["x\\\"y"]'}
    result = ExtractorChecker().check(extractor(selector=selector), body(text))
    assert result.status == "matched"
    assert result.value == Decimal("0.12345678901234567890123456789")
    assert ExtractorChecker().check(extractor(), body(text)).status == "matched"
    assert ExtractorChecker().check(extractor(), body("{}")).status == "missing"


@pytest.mark.parametrize("text", [
    '{"token":1,"token":2}', '{"nested":{"a":0,"a":1}}', "NaN", "Infinity",
    '"\\ud800"', '{"\\ud800":1}', "{", "[1,]", "1 2",
])
def test_strict_json_rejects_invalid_without_leaks(text: str) -> None:
    with pytest.raises(Har2LoadError) as error:
        parse_json_text(text)
    assert str(error.value) == "Invalid JSON value."
    assert error.value.__cause__ is None
    assert ExtractorChecker().check(extractor(), body(text)).status == "invalid"


@pytest.mark.parametrize("text", ["null", "true", '"hello"', "123", "[]", "{}"])
def test_json_accepts_any_root(text: str) -> None:
    result = ExtractorChecker().check(extractor(selector={
        "dialect": "json_path_subset_v1", "expression": "$",
    }), body(text))
    assert result.status == "matched"
    assert result.value == parse_json_text(text)


@pytest.mark.parametrize("expression", [
    "", "token", "$..token", "$.*", "$[01]", "$[-1]", "$[1.0]", "$['x']",
    "$[?(@.x)]", "$[0:1]", "$[ 0]", '$["\\ud800"]', "$.x()", "$.",
    "$" + ".x" * 65, "$" + "a" * 256,
])
def test_unsupported_path_grammar(expression: str) -> None:
    with pytest.raises(Har2LoadError):
        parse_json_path(expression)


def test_selector_is_structural_and_exact() -> None:
    validate_selector("response_body", {"dialect": "boundary_v1", "left": "<", "right": ">"})
    for selector in [
        {"dialect": "regex_subset_v1", "expression": "(.*)"},
        {"dialect": "boundary_v1", "left": "", "right": ">"},
        {"dialect": "name_v1", "name": "x"},
        {"dialect": "json_path_subset_v1", "expression": "$", "extra": True},
    ]:
        with pytest.raises(Har2LoadError):
            validate_selector("response_body", selector)


@pytest.mark.parametrize(("text", "options", "status", "value", "count"), [
    ("<a>", {}, "matched", "a", 1), ("<>", {}, "empty", None, 1),
    ("<>", {"allow_empty": True}, "matched", "", 1),
    ("<a><b>", {}, "multiple", None, 2),
    ("<a><b>", {"cardinality": "occurrence", "occurrence": 2}, "matched", "b", 2),
    ("<a>", {"cardinality": "occurrence", "occurrence": 2}, "missing", None, 1),
    ("none", {}, "missing", None, 0), ("<a<b>", {}, "matched", "a<b", 1),
    ("<&amp;>", {}, "matched", "&amp;", 1),
])
def test_boundaries(text: str, options: dict[str, JSONValue], status: str,
                    value: JSONValue, count: int) -> None:
    definition = extractor(selector={"dialect": "boundary_v1", "left": "<", "right": ">"})
    definition.update(options)
    result = ExtractorChecker().check(definition, body(text))
    assert (result.status, result.value, result.match_count) == (status, value, count)


@pytest.mark.parametrize(("area", "count"), [("response_headers", 2), ("response_cookies", 1)])
def test_name_matching_and_order(area: str, count: int) -> None:
    exchange = Exchange("ex_a", 0, {"response": {area.removeprefix("response_"): [
        {"name": "Token", "value": "first"}, {"name": "token", "value": "second"},
    ]}})
    definition = extractor(area, selector={"dialect": "name_v1", "name": "token"},
                           cardinality="occurrence", occurrence=count)
    result = ExtractorChecker().check(definition, exchange)
    assert (result.status, result.value, result.match_count) == ("matched", "second", count)


@pytest.mark.parametrize(("raw", "status"), [
    ({}, "unavailable"), ({"response": {}}, "unavailable"),
    ({"response": {"headers": None}}, "invalid"),
    ({"response": {"headers": {}}}, "invalid"),
    ({"response": {"headers": [{"name": "x", "value": 1}]}}, "invalid"),
    ({"response": {"headers": []}}, "missing"),
])
def test_header_shape(raw: JSONValue, status: str) -> None:
    result = ExtractorChecker().check(extractor("response_headers", selector={
        "dialect": "name_v1", "name": "x",
    }), Exchange("ex_a", 0, raw))
    assert result.status == status


@pytest.mark.parametrize("status", ["unavailable", "unsupported", "nontext"])
def test_unavailable_body(status: str) -> None:
    result = ExtractorChecker().check(extractor(), body('{"token":1}', status))
    assert result.status == "unavailable"


def test_limits_and_failure_statelessness() -> None:
    checker = ExtractorChecker()
    assert checker.check(extractor(), body('{"token":"old"}')).value == "old"
    assert checker.check(extractor(), body("{}")).value is None
    for text in ["x" * (1024 * 1024 + 1), "[" * 65 + "0" + "]" * 65]:
        assert checker.check(extractor(), body(text)).status == "limit_exceeded"
    definition = extractor(selector={"dialect": "boundary_v1", "left": "<", "right": ">"})
    assert checker.check(definition, body("<a>" * 1000)).status == "multiple"
    result = checker.check(definition, body("<a>" * 1001))
    assert result.status == "limit_exceeded"
    assert result.value is None


def test_multibyte_limit_and_depth_boundary() -> None:
    assert parse_json_text("[" * 64 + "0" + "]" * 64) is not None
    with pytest.raises(Har2LoadError) as error:
        parse_json_text('"' + "é" * (1024 * 512) + '"')
    assert error.value.code == "JSON_LIMIT_EXCEEDED"


def test_decimal_integrality_does_not_expand_exponents() -> None:
    class NoIntegerExpansion(Decimal):
        def __floor__(self) -> int:
            pytest.fail("Integral Decimal validation must not materialize a giant integer")
    assert matches_type(NoIntegerExpansion("1e20"), "integer")


@pytest.mark.parametrize("updates", [
    {"cardinality": "all"}, {"cardinality": "occurrence"},
    {"cardinality": "occurrence", "occurrence": True},
    {"cardinality": "occurrence", "occurrence": 0}, {"occurrence": 1},
    {"allow_empty": 1}, {"on_failure": "stop"}, {"area": []}, {"selector": None},
])
def test_invalid_check_declarations(updates: dict[str, JSONValue]) -> None:
    result = ExtractorChecker.check(extractor(**updates), body('{"token":"safe"}'))
    assert (result.status, result.value) == ("invalid", None)


@pytest.mark.parametrize("items", [
    [{"name": "x", "value": "a"}] * 1001,
    [{"name": "x", "value": "a" * (1024 * 1024)}],
    [{"name": "x", "value": "a" * (512 * 1024)}] * 2,
])
def test_named_extraction_limits(items: JSONValue) -> None:
    definition = extractor("response_headers", selector={"dialect": "name_v1", "name": "x"})
    exchange = Exchange("ex_a", 0, {"response": {"headers": items}})
    assert ExtractorChecker.check(definition, exchange).status == "limit_exceeded"


def test_json_string_state_and_scalar_mismatches() -> None:
    assert parse_json_text('"' + "[" * 100 + '"') == "[" * 100
    for text in ['{"token":[]}', '{"token":{}}', '{"token":false}']:
        result = ExtractorChecker.check(extractor(selector={
            "dialect": "json_path_subset_v1", "expression": "$.token[0]",
        }), body(text))
        assert result.status == "missing"
    assert parse_json_path('$["\\u0061"][0][""]') == ("a", 0, "")


def test_boundary_pairs_do_not_overlap() -> None:
    definition = extractor(selector={"dialect": "boundary_v1", "left": "|", "right": "|"})
    result = ExtractorChecker.check(definition, body("|a|b|c|"))
    assert (result.status, result.match_count) == ("multiple", 2)
    assert ExtractorChecker.check(extractor(), body(None)).status == "unavailable"
    assert ExtractorChecker.check(extractor(), body("{}", "invalid")).status == "invalid"


def test_empty_named_records_still_consume_scan_budget() -> None:
    definition = extractor("response_headers", selector={"dialect": "name_v1", "name": "x"})
    exchange = Exchange("ex_a", 0, {"response": {
        "headers": [{"name": "", "value": ""}] * (1024 * 1024 + 1),
    }})
    assert ExtractorChecker.check(definition, exchange).status == "limit_exceeded"
