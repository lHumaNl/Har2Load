"""Scalar declarations, final-batch validation and recorded evidence only.

Initialization repeats at each declared event. Scope determines reset/visibility;
it never implies a once-only guard. Preparation neither reads nor produces inputs.
"""

import re
from copy import deepcopy
from datetime import date
from typing import cast

from har2load.domain import Capture, Grouping, Har2LoadError, JSONValue
from har2load.extraction import (
    ExtractionResult,
    ExtractorChecker,
    validate_cardinality,
    validate_selector,
)
from har2load.values import SCALAR_TYPES, matches_type

MAX_VARIABLES = 256
MAX_EXTRACTORS = 256
MAX_BINDINGS = 1024
MAX_PURPOSE_LENGTH = 1024
MAX_LITERAL_LENGTH = 65536
MAX_GENERATED_LENGTH = 4096
MAX_ALPHABET_LENGTH = 95
MAX_OFFSET_SECONDS = 315360000
MAX_LOCATION_LENGTH = 256
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CHANGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_RESET = {"session": "session_start", "iteration": "iteration_start", "request": "before_request"}
_FIELDS = {
    "variable": {"id", "name", "value_type", "source", "initialize", "scope", "reset", "purpose",
                 "sensitive"},
    "extractor": {"id", "exchange_id", "area", "selector", "cardinality", "allow_empty",
                  "on_failure"},
    "binding": {"id", "variable_id", "exchange_id", "location"},
}
_LIMITS = {"variables": MAX_VARIABLES, "extractors": MAX_EXTRACTORS, "bindings": MAX_BINDINGS}
_OPERATIONS = tuple(f"{action}_{kind}" for action in ("upsert", "remove") for kind in _FIELDS)


def _require(condition: bool) -> None:
    if not condition:
        raise Har2LoadError("PARAMETER_INVALID", "Invalid parameter declaration.", 3)


def _object(value: JSONValue) -> dict[str, JSONValue]:
    _require(isinstance(value, dict))
    return cast(dict[str, JSONValue], value)


def _shape(value: JSONValue, keys: set[str]) -> dict[str, JSONValue]:
    record = _object(value)
    _require(set(record) == keys)
    return record


def _identifier(value: JSONValue, provenance: bool = False) -> str:
    pattern = _CHANGE_ID if provenance else _IDENTIFIER
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None)
    return cast(str, value)


def _string(value: JSONValue, maximum: int, *, nonblank: bool = False) -> str:
    _require(isinstance(value, str) and len(value) <= maximum and matches_type(value, "string"))
    result = cast(str, value)
    _require(not nonblank or bool(result.strip()))
    return result


def _integer(value: JSONValue, minimum: int | None = None, maximum: int | None = None) -> int:
    _require(type(value) is int)
    number = cast(int, value)
    _require((minimum is None or number >= minimum) and (maximum is None or number <= maximum))
    return number


def _initializer(variable: dict[str, JSONValue]) -> None:
    initializer = _object(variable["initialize"])
    event = initializer.get("event")
    _require(event in ("session_start", "iteration_start", "before_request", "after_response"))
    if event in ("session_start", "iteration_start"):
        _shape(initializer, {"event"})
    else:
        _shape(initializer, {"event", "exchange_id"})
        _string(initializer["exchange_id"], MAX_LOCATION_LENGTH, nonblank=True)
    scope = variable["scope"]
    _require(isinstance(scope, str) and scope in _RESET)
    _require(variable["reset"] == _RESET[cast(str, scope)])
    _require(scope != "iteration" or event != "session_start")
    _require(scope != "request" or event == "before_request")


def _date(value: JSONValue) -> date:
    _require(isinstance(value, str) and _DATE.fullmatch(value) is not None)
    try:
        return date.fromisoformat(cast(str, value))
    except ValueError:
        raise Har2LoadError("PARAMETER_INVALID", "Invalid parameter declaration.", 3) from None


def _generated_string(source: dict[str, JSONValue]) -> None:
    _shape(source, {"kind", "generator", "length", "alphabet"})
    _integer(source["length"], 1, MAX_GENERATED_LENGTH)
    alphabet = _string(source["alphabet"], MAX_ALPHABET_LENGTH)
    _require(bool(alphabet) and len(set(alphabet)) == len(alphabet))
    _require(all(" " <= character <= "~" for character in alphabet))


def _generated_date(source: dict[str, JSONValue]) -> None:
    if source["generator"] == "utc_datetime":
        _shape(source, {"kind", "generator", "offset_seconds", "format"})
        _integer(source["offset_seconds"], -MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS)
    else:
        _shape(source, {"kind", "generator", "minimum", "maximum", "format"})
        _require(_date(source["minimum"]) <= _date(source["maximum"]))
    _require(source["format"] == "iso8601")


def _generated(source: dict[str, JSONValue], value_type: str) -> None:
    generator = source.get("generator")
    _require(generator in ("integer", "string", "uuid", "utc_datetime", "utc_date_range"))
    _require(value_type == ("integer" if generator == "integer" else "string"))
    if generator == "integer":
        _shape(source, {"kind", "generator", "minimum", "maximum"})
        _require(_integer(source["minimum"]) <= _integer(source["maximum"]))
    elif generator == "string":
        _generated_string(source)
    elif generator == "uuid":
        _shape(source, {"kind", "generator"})
    else:
        _generated_date(source)


def _source(source: dict[str, JSONValue], value_type: str) -> None:
    kind = source.get("kind")
    if kind == "literal":
        _shape(source, {"kind", "value"})
        _require(matches_type(source["value"], value_type))
        if isinstance(source["value"], str):
            _string(source["value"], MAX_LITERAL_LENGTH)
    elif kind == "external":
        _shape(source, {"kind", "key", "allow_empty"})
        _identifier(source["key"])
        _require(type(source["allow_empty"]) is bool)
    elif kind == "extracted":
        _shape(source, {"kind", "extractor_id"})
        _identifier(source["extractor_id"])
    elif kind == "generated":
        _generated(source, value_type)
    elif kind == "agent_code":
        _shape(source, {"kind", "behavior_id"})
        _identifier(source["behavior_id"])
    else:
        _require(False)


def _variable(record: dict[str, JSONValue]) -> None:
    _identifier(record["name"])
    value_type = record["value_type"]
    _require(isinstance(value_type, str) and value_type in SCALAR_TYPES)
    _string(record["purpose"], MAX_PURPOSE_LENGTH, nonblank=True)
    _require(type(record["sensitive"]) is bool)
    _initializer(record)
    _source(_object(record["source"]), cast(str, value_type))


def _extractor(record: dict[str, JSONValue]) -> None:
    _string(record["exchange_id"], MAX_LOCATION_LENGTH, nonblank=True)
    validate_cardinality(record)
    validate_selector(record["area"], record["selector"])


def _pointer(value: JSONValue) -> None:
    pointer = _string(value, MAX_LOCATION_LENGTH)
    _require(not pointer or pointer.startswith("/"))
    _require(re.search(r"~(?![01])", pointer) is None)


def _location(value: JSONValue) -> None:
    location = _object(value)
    area = location.get("area")
    if area == "request_json":
        _shape(location, {"area", "pointer"})
        _pointer(location["pointer"])
    elif area in ("request_query", "request_header", "request_form", "request_path"):
        _shape(location, {"area", "index"})
        _integer(location["index"], 0)
    elif area == "request_text":
        _shape(location, {"area", "start", "end"})
        _integer(location["end"], _integer(location["start"], 0))
    else:
        _require(False)


def _binding(record: dict[str, JSONValue]) -> None:
    _identifier(record["variable_id"])
    _string(record["exchange_id"], MAX_LOCATION_LENGTH, nonblank=True)
    _location(record["location"])


def _record(kind: str, value: JSONValue, *, stored: bool = False) -> dict[str, JSONValue]:
    record = _object(value)
    keys = _FIELDS[kind].copy()
    if kind == "extractor" and record.get("cardinality") == "occurrence":
        keys.add("occurrence")
    if stored:
        keys.add("change_id")
    _shape(record, keys)
    _identifier(record["id"])
    if stored:
        _identifier(record["change_id"], provenance=True)
    {"variable": _variable, "extractor": _extractor, "binding": _binding}[kind](record)
    return record


def _records(parameters: dict[str, JSONValue], key: str) -> list[dict[str, JSONValue]]:
    values = parameters[key]
    _require(isinstance(values, list) and len(values) <= _LIMITS[key])
    return [_record(key[:-1], value, stored=True) for value in cast(list[JSONValue], values)]


def _position(initializer: dict[str, JSONValue], order: dict[str, int]) -> int:
    event = initializer["event"]
    if event == "session_start":
        return -2
    if event == "iteration_start":
        return -1
    exchange = cast(str, initializer["exchange_id"])
    _require(exchange in order)
    return 2 * order[exchange] + (1 if event == "after_response" else 0)


def _variable_references(
    variable: dict[str, JSONValue], extractors: dict[str, dict[str, JSONValue]],
    order: dict[str, int],
) -> None:
    initializer = _object(variable["initialize"])
    _position(initializer, order)
    source = _object(variable["source"])
    if source["kind"] != "extracted":
        return
    extractor_id = cast(str, source["extractor_id"])
    _require(extractor_id in extractors and variable["scope"] != "request")
    producer = extractors[extractor_id]["exchange_id"]
    _require(initializer["event"] == "after_response")
    _require(initializer.get("exchange_id") == producer)


def _binding_references(binding: dict[str, JSONValue], variables: dict[str, dict[str, JSONValue]],
                        order: dict[str, int]) -> None:
    variable_id, exchange_id = cast(str, binding["variable_id"]), cast(str, binding["exchange_id"])
    _require(variable_id in variables and exchange_id in order)
    variable = variables[variable_id]
    initializer = _object(variable["initialize"])
    _require(_position(initializer, order) <= 2 * order[exchange_id])
    if variable["scope"] == "request":
        _require(initializer["exchange_id"] == exchange_id)
    if _object(binding["location"])["area"] != "request_json":
        _require(variable["value_type"] != "null")


def _indexed(records: list[dict[str, JSONValue]]) -> dict[str, dict[str, JSONValue]]:
    return {cast(str, record["id"]): record for record in records}


def _validate_references(records: dict[str, list[dict[str, JSONValue]]], capture: Capture,
                         grouping: Grouping) -> None:
    exchanges = {exchange.id for exchange in capture.exchanges}
    order = {identifier: index for index, identifier in enumerate(grouping.ordered_exchange_ids)}
    _require(set(order) == exchanges and len(order) == len(grouping.ordered_exchange_ids))
    extractors, variables = _indexed(records["extractors"]), _indexed(records["variables"])
    for extractor in extractors.values():
        _require(extractor["exchange_id"] in exchanges)
    for variable in variables.values():
        _variable_references(variable, extractors, order)
    for binding in records["bindings"]:
        _binding_references(binding, variables, order)


def _apply(
    parameters: dict[str, JSONValue], operation: dict[str, JSONValue], change_id: str,
) -> None:
    action, kind = cast(str, operation["op"]).split("_", 1)
    records = cast(list[JSONValue], parameters[kind + "s"])
    authored = _object(operation[kind]) if action == "upsert" else None
    identifier = authored["id"] if authored is not None else operation["id"]
    remaining = [item for item in records if _object(item)["id"] != identifier]
    if authored is None:
        _require(len(remaining) < len(records))
    else:
        remaining.append({**deepcopy(authored), "change_id": change_id})
    parameters[kind + "s"] = sorted(remaining, key=lambda item: cast(str, _object(item)["id"]))


def _check(variable: dict[str, JSONValue] | None, extractor: dict[str, JSONValue],
           result: ExtractionResult) -> JSONValue:
    status, value = result.status, result.value
    if (variable is not None and status == "matched"
            and not matches_type(value, cast(str, variable["value_type"]))):
        status, value = "type_mismatch", None
    return {
        "check_kind": "variable" if variable is not None else "extractor",
        "variable_id": variable["id"] if variable is not None else None,
        "extractor_id": extractor["id"], "status": status,
        "match_count": result.match_count, "value": value,
        "failure_marker": (
            "ERROR_" + cast(str, variable["name"])
            if variable is not None and status != "matched" else None
        ),
        "evidence": {"exchange_id": extractor["exchange_id"], "area": extractor["area"],
                     "selector": deepcopy(extractor["selector"])},
        "verification": "recorded_response_only",
    }


class ParameterModel:
    """Operations mutate only their provided clone; validate after the complete batch."""

    @staticmethod
    def empty() -> dict[str, JSONValue]:
        return {"variables": [], "extractors": [], "bindings": []}

    @staticmethod
    def validate_operation(op: dict[str, JSONValue]) -> bool:
        name = op.get("op")
        if name not in _OPERATIONS:
            return False
        action, kind = cast(str, name).split("_", 1)
        _shape(op, {"op", kind if action == "upsert" else "id"})
        if action == "upsert":
            _record(kind, op[kind])
        else:
            _identifier(op["id"])
        return True

    @staticmethod
    def apply_operation(parameters: dict[str, JSONValue], op: dict[str, JSONValue],
                        change_id: str) -> bool:
        if not ParameterModel.validate_operation(op):
            return False
        _identifier(change_id, provenance=True)
        _apply(parameters, op, change_id)
        return True

    @staticmethod
    def validate(
        parameters: JSONValue, capture: Capture, grouping: Grouping, *,
        allow_agent_code: bool = False,
    ) -> None:
        block = _shape(parameters, set(_LIMITS))
        records = {key: _records(block, key) for key in _LIMITS}
        identifiers = [record["id"] for values in records.values() for record in values]
        names = [record["name"] for record in records["variables"]]
        _require(len(set(cast(list[str], identifiers))) == len(identifiers))
        _require(len(set(cast(list[str], names))) == len(names))
        if not allow_agent_code:
            _require(all(_object(item["source"])["kind"] != "agent_code"
                         for item in records["variables"]))
        _validate_references(records, capture, grouping)

    @staticmethod
    def checks(parameters: dict[str, JSONValue], capture: Capture) -> list[JSONValue]:
        extractors = _indexed(_records(parameters, "extractors"))
        exchanges = {item.id: item for item in capture.exchanges}
        consumers: dict[str, list[dict[str, JSONValue]]] = {key: [] for key in extractors}
        checks: list[JSONValue] = []
        for variable in _records(parameters, "variables"):
            source = _object(variable["source"])
            if source["kind"] == "extracted":
                identifier = cast(str, source["extractor_id"])
                _require(identifier in extractors)
                consumers[identifier].append(variable)
        for identifier, extractor in extractors.items():
            exchange_id = cast(str, extractor["exchange_id"])
            _require(exchange_id in exchanges)
            result = ExtractorChecker.check(extractor, exchanges[exchange_id])
            if consumers[identifier]:
                checks.extend(_check(variable, extractor, result)
                              for variable in consumers[identifier])
            else:
                checks.append(_check(None, extractor, result))
        return checks
