"""Strict, bounded JSON and scalar types; no runtime value production."""

import json
import math
from decimal import Decimal, InvalidOperation
from typing import cast

from har2load.domain import Har2LoadError, JSONValue

SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
MAX_TEXT_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64


def check_text(text: str, maximum: int = MAX_TEXT_BYTES) -> int:
    """Return UTF-8 byte length, rejecting ill-formed strings and oversized input."""
    if len(text) > maximum:
        raise Har2LoadError("JSON_LIMIT_EXCEEDED", "JSON resource limit exceeded.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise Har2LoadError("JSON_INVALID", "Invalid JSON value.") from None
    if size > maximum:
        raise Har2LoadError("JSON_LIMIT_EXCEEDED", "JSON resource limit exceeded.")
    return size


def _number_matches(value: JSONValue, type_name: str) -> bool:
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        # Do not expand large positive exponents into a Python integer.
        return value.is_finite() and (type_name == "number" or value == value.to_integral_value())
    if isinstance(value, float):
        return math.isfinite(value) and (type_name == "number" or value.is_integer())
    return True


def matches_type(value: JSONValue, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        try:
            return isinstance(value, str) and value.encode("utf-8") is not None
        except UnicodeError:
            return False
    return type_name in ("number", "integer") and _number_matches(value, type_name)


def _depth_check(text: str) -> None:
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise Har2LoadError("JSON_LIMIT_EXCEEDED", "JSON resource limit exceeded.")
        elif character in "]}":
            depth -= 1


def _object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        key.encode("utf-8")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError


def _validate_tree(value: JSONValue) -> None:
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, Decimal) and not value.is_finite():
        raise ValueError
    elif isinstance(value, list):
        for child in value:
            _validate_tree(child)
    elif isinstance(value, dict):
        for child in value.values():
            _validate_tree(child)


def parse_json_text(text: str) -> JSONValue:
    """Parse any JSON root, preserving decimal precision with at most 64 containers."""
    check_text(text)
    _depth_check(text)
    try:
        value = cast(JSONValue, json.loads(
            text, object_pairs_hook=_object, parse_int=Decimal, parse_float=Decimal,
            parse_constant=_reject_constant,
        ))
        _validate_tree(value)
        return value
    except (ValueError, UnicodeError, InvalidOperation, RecursionError):
        raise Har2LoadError("JSON_INVALID", "Invalid JSON value.") from None
