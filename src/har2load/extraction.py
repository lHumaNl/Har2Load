"""Bounded captured-response checks, not native target or runtime execution."""

import json
import re
from dataclasses import dataclass
from typing import cast

from har2load.domain import Exchange, Har2LoadError, JSONValue
from har2load.values import MAX_TEXT_BYTES, check_text, parse_json_text

MAX_SELECTOR_LENGTH = 256
MAX_PATH_TOKENS = 64
MAX_MATCHES = 1000
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\]")


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    value: JSONValue = None
    match_count: int = 0


def _require(condition: bool) -> None:
    if not condition:
        raise Har2LoadError("PARAMETER_INVALID", "Invalid parameter declaration.", 3)


def _text(value: JSONValue) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= MAX_SELECTOR_LENGTH)
    result = cast(str, value)
    try:
        result.encode("utf-8")
    except UnicodeError:
        raise Har2LoadError("PARAMETER_INVALID", "Invalid parameter declaration.", 3) from None
    return result


def _bracket(expression: str, position: int) -> tuple[str | int, int]:
    position += 1
    if expression[position:position + 1] == '"':
        try:
            value, consumed = json.JSONDecoder().raw_decode(expression[position:])
            value.encode("utf-8")
        except (ValueError, UnicodeError):
            raise Har2LoadError("PARAMETER_INVALID", "Invalid parameter declaration.", 3) from None
        end = position + consumed
        _require(expression[end:end + 1] == "]")
        return cast(str, value), end + 1
    match = _INDEX.match(expression, position)
    _require(match is not None)
    assert match is not None
    return int(match.group()[:-1]), match.end()


def _path_token(expression: str, position: int) -> tuple[str | int, int]:
    if expression[position] == "[":
        return _bracket(expression, position)
    _require(expression[position] == ".")
    match = _FIELD.match(expression, position + 1)
    _require(match is not None)
    assert match is not None
    return match.group(), match.end()


def parse_json_path(expression: str) -> tuple[str | int, ...]:
    """Parse only root, ASCII dot names, canonical indices and JSON-quoted keys."""
    _text(expression)
    _require(expression.startswith("$"))
    tokens: list[str | int] = []
    position = 1
    while position < len(expression):
        token, position = _path_token(expression, position)
        tokens.append(token)
        _require(len(tokens) <= MAX_PATH_TOKENS)
    return tuple(tokens)


def validate_selector(area: JSONValue, selector: JSONValue) -> None:
    _require(isinstance(selector, dict))
    definition = cast(dict[str, JSONValue], selector)
    dialect = definition.get("dialect")
    if area in ("response_headers", "response_cookies"):
        _require(set(definition) == {"dialect", "name"} and dialect == "name_v1")
        _text(definition["name"])
    elif area == "response_body" and dialect == "json_path_subset_v1":
        _require(set(definition) == {"dialect", "expression"})
        parse_json_path(_text(definition["expression"]))
    elif area == "response_body" and dialect == "boundary_v1":
        _require(set(definition) == {"dialect", "left", "right"})
        _text(definition["left"])
        _text(definition["right"])
    else:
        _require(False)


def validate_cardinality(extractor: dict[str, JSONValue]) -> None:
    cardinality = extractor.get("cardinality")
    _require(cardinality in ("exactly_one", "occurrence"))
    _require(type(extractor.get("allow_empty")) is bool)
    _require(extractor.get("on_failure") == "mark_error_and_continue")
    if cardinality == "occurrence":
        occurrence = extractor.get("occurrence")
        _require(type(occurrence) is int and occurrence > 0)
    else:
        _require("occurrence" not in extractor)


def _json_matches(text: str, selector: dict[str, JSONValue]) -> list[JSONValue]:
    value = parse_json_text(text)
    for token in parse_json_path(cast(str, selector["expression"])):
        if isinstance(token, str) and isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(token, int) and isinstance(value, list) and token < len(value):
            value = value[token]
        else:
            return []
    return [value]


def _boundary_matches(text: str, selector: dict[str, JSONValue]) -> list[JSONValue]:
    left, right = cast(str, selector["left"]), cast(str, selector["right"])
    results: list[JSONValue] = []
    position = 0
    while len(results) <= MAX_MATCHES:
        start = text.find(left, position)
        if start < 0:
            break
        start += len(left)
        end = text.find(right, start)
        if end < 0:
            break
        results.append(text[start:end])
        position = end + len(right)
    return results


def _body_matches(exchange: Exchange, selector: dict[str, JSONValue]) -> list[JSONValue]:
    text = exchange.response_body.text
    assert text is not None
    check_text(text)
    if selector["dialect"] == "json_path_subset_v1":
        return _json_matches(text, selector)
    return _boundary_matches(text, selector)


def _response_items(exchange: Exchange, area: str) -> JSONValue:
    if not isinstance(exchange.raw, dict):
        raise Har2LoadError("EXTRACTION_INVALID", "Invalid recorded response.")
    if "response" not in exchange.raw:
        raise Har2LoadError("EXTRACTION_UNAVAILABLE", "Recorded response unavailable.")
    response = exchange.raw["response"]
    if not isinstance(response, dict):
        raise Har2LoadError("EXTRACTION_INVALID", "Invalid recorded response.")
    key = area.removeprefix("response_")
    if key not in response:
        raise Har2LoadError("EXTRACTION_UNAVAILABLE", "Recorded response unavailable.")
    return response[key]


def _item(item: JSONValue) -> tuple[str, str]:
    if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str)):
        raise Har2LoadError("EXTRACTION_INVALID", "Invalid recorded response.")
    return cast(str, item["name"]), cast(str, item["value"])


def _named_matches(exchange: Exchange, area: str, name: str) -> list[JSONValue]:
    items = _response_items(exchange, area)
    if not isinstance(items, list):
        raise Har2LoadError("EXTRACTION_INVALID", "Invalid recorded response.")
    results: list[JSONValue] = []
    size = 0
    for item in items:
        key, value = _item(item)
        # Empty records must consume budget too, bounding scans of malformed HAR arrays.
        size += max(1, check_text(key) + check_text(value))
        if size > MAX_TEXT_BYTES:
            raise Har2LoadError("JSON_LIMIT_EXCEEDED", "JSON resource limit exceeded.")
        equal = key.lower() == name.lower() if area == "response_headers" else key == name
        if equal:
            results.append(value)
            if len(results) > MAX_MATCHES:
                break
    return results


def _choose(extractor: dict[str, JSONValue], matches: list[JSONValue]) -> ExtractionResult:
    count = len(matches)
    if count > MAX_MATCHES:
        return ExtractionResult("limit_exceeded", match_count=count)
    occurrence = cast(int, extractor.get("occurrence", 1))
    if count < occurrence:
        return ExtractionResult("missing", match_count=count)
    if extractor["cardinality"] == "exactly_one" and count != 1:
        return ExtractionResult("multiple", match_count=count)
    value = matches[occurrence - 1]
    if value == "" and not extractor["allow_empty"]:
        return ExtractionResult("empty", match_count=count)
    return ExtractionResult("matched", value, count)


def _check(extractor: dict[str, JSONValue], exchange: Exchange) -> ExtractionResult:
    validate_selector(extractor.get("area"), extractor.get("selector"))
    validate_cardinality(extractor)
    selector = cast(dict[str, JSONValue], extractor["selector"])
    area = cast(str, extractor["area"])
    if area != "response_body":
        return _choose(extractor, _named_matches(exchange, area, cast(str, selector["name"])))
    if exchange.response_body.view_status == "limit_exceeded":
        return ExtractionResult("limit_exceeded")
    if exchange.response_body.view_status == "invalid":
        return ExtractionResult("invalid")
    if exchange.response_body.view_status != "available" or exchange.response_body.text is None:
        return ExtractionResult("unavailable")
    return _choose(extractor, _body_matches(exchange, selector))


class ExtractorChecker:
    """Stateless checks of recorded evidence, without stale-value fallback."""

    @staticmethod
    def check(extractor: dict[str, JSONValue], exchange: Exchange) -> ExtractionResult:
        try:
            return _check(extractor, exchange)
        except Har2LoadError as error:
            status = {"JSON_LIMIT_EXCEEDED": "limit_exceeded",
                      "EXTRACTION_UNAVAILABLE": "unavailable"}.get(error.code, "invalid")
            return ExtractionResult(status)
