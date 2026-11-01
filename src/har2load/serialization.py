"""Strict JSON serialization for generated, versioned workspace metadata."""

import json
from decimal import Decimal, InvalidOperation
from typing import cast

from har2load.domain import Har2LoadError, JSONValue


def canonical_bytes(value: JSONValue) -> bytes:
    return _encode(value).encode("utf-8")


def _encode(value: JSONValue) -> str:
    # stdlib json.dumps cannot emit Decimal as a JSON number without converting
    # it to float. Keep this small encoder explicit; strings always use json's
    # escaping, and unknown objects cannot inject raw JSON through __str__.
    if isinstance(value, (Decimal, float)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Non-finite JSON number")
        # Use the same spelling before and after metadata float -> Decimal load.
        # Decimal(str(float)) preserves the float's round-trip decimal value;
        # raw HAR decimals never take a float conversion in either direction.
        return str(number)
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=True) + ":" + _encode(value[key]) for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def metadata_object(raw: bytes) -> dict[str, JSONValue]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_constant, parse_float=Decimal,
        )
        # Also rejects numbers such as 1e999 that json.loads converts to infinity.
        canonical_bytes(value)
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return cast(dict[str, JSONValue], value)
    except (UnicodeError, ValueError, RecursionError, InvalidOperation) as error:
        raise Har2LoadError("WORKSPACE_INVALID", "Invalid workspace metadata.", 3) from error
