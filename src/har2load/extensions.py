"""Bounded opaque declarations: author contracts, not execution or native validation.

Operations permit temporary dangling references; validate the complete batch. Planning
describes declared scheduling only, and never inspects source semantics or resources.
ParameterModel owns scalar types, initialization shapes and scope/reset validation;
callers must validate parameters before checking these extension cross-references.
"""

import re
from copy import deepcopy
from hashlib import sha256
from typing import cast
from unicodedata import category

from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.values import SCALAR_TYPES, matches_type

MAX_BLOCKS, MAX_HELPERS = 128, 64
MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES = 65536, 2 * 1024 * 1024
MAX_ORDER, MAX_VARIABLE_REFS, MAX_HELPER_PARAMETERS = 10000, 256, 16
MAX_DEPENDENCIES, MAX_SIDE_EFFECTS = 32, 8
MAX_PATH_LENGTH, MAX_PURPOSE_LENGTH = 256, 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CHANGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TARGETS = {"jmeter": "groovy", "loadrunner": "c"}
_EFFECTS = ("network", "filesystem", "process", "environment", "shared_state", "unknown")
_COMMON = {"id", "target", "language", "source", "depends_on", "dependencies",
           "side_effects", "purpose"}
_FIELDS = {
    "block": _COMMON | {"behavior_id", "placement", "order", "inputs", "outputs", "helper_calls",
                        "failure_policy", "failed_input_policy", "reset_outputs"},
    "helper": _COMMON | {"namespace", "symbol", "parameters", "return_type", "stateless",
                         "thread_safe"},
}
_OPERATIONS = {"upsert_code_block": ("upsert", "block"), "remove_code_block": ("remove", "block"),
               "upsert_helper": ("upsert", "helper"), "remove_helper": ("remove", "helper")}
_LIMITS = {"blocks": MAX_BLOCKS, "helpers": MAX_HELPERS}
type Record = dict[str, JSONValue]
type Index = dict[str, Record]


def _require(condition: bool) -> None:
    if not condition:
        raise Har2LoadError("EXTENSION_INVALID", "Invalid target extension declaration.", 3)


def _object(value: JSONValue) -> Record:
    _require(isinstance(value, dict))
    return cast(Record, value)


def _shape(value: JSONValue, keys: set[str]) -> Record:
    record = _object(value)
    _require(set(record) == keys)
    return record


def _array(value: JSONValue, maximum: int) -> list[JSONValue]:
    _require(isinstance(value, list) and len(value) <= maximum)
    return cast(list[JSONValue], value)


def _identifier(value: JSONValue, *, provenance: bool = False) -> str:
    pattern = _CHANGE_ID if provenance else _IDENTIFIER
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None)
    return cast(str, value)


def _text(value: JSONValue, maximum: int) -> str:
    _require(isinstance(value, str) and len(value) <= maximum and matches_type(value, "string"))
    result = cast(str, value)
    _require(bool(result.strip()))
    return result


def _unique[T](values: list[T]) -> None:
    _require(len(set(values)) == len(values))


def _ids(value: JSONValue, maximum: int) -> list[str]:
    identifiers = [_identifier(item) for item in _array(value, maximum)]
    _unique(identifiers)
    return identifiers


def _source(value: JSONValue) -> bytes:
    text = _text(value, MAX_SOURCE_BYTES)
    _require("\0" not in text)
    data = text.encode("utf-8")
    _require(len(data) <= MAX_SOURCE_BYTES)
    return data


def _dependencies(record: Record) -> None:
    for item in _array(record["dependencies"], MAX_DEPENDENCIES):
        dependency = _shape(item, {"kind", "path"})
        _require(dependency["kind"] in ("jar", "csv", "file"))
        _require(dependency["kind"] != "jar" or record["target"] == "jmeter")
        path = _text(dependency["path"], MAX_PATH_LENGTH)
        _require(not any(character in path for character in ("\\", ":")))
        _require(all(category(character) != "Cc" for character in path))
        _require(all(segment not in ("", ".", "..") for segment in path.split("/")))


def _block(record: Record) -> None:
    _identifier(record["behavior_id"])
    placement = _shape(record["placement"], {"event", "exchange_id"})
    _require(placement["event"] in ("before_request", "after_response"))
    _text(placement["exchange_id"], MAX_PATH_LENGTH)
    _require(type(record["order"]) is int and 0 <= record["order"] <= MAX_ORDER)
    inputs, outputs = (_ids(record[key], MAX_VARIABLE_REFS) for key in ("inputs", "outputs"))
    _require(not set(inputs).intersection(outputs))
    _ids(record["helper_calls"], MAX_HELPERS)
    _require(record["failure_policy"] == "mark_error_and_continue")
    _require(record["failed_input_policy"] == "skip_and_mark_outputs")
    _require(record["reset_outputs"] is True)


def _helper(record: Record) -> None:
    _identifier(record["namespace"])
    _identifier(record["symbol"])
    names: list[str] = []
    for value in _array(record["parameters"], MAX_HELPER_PARAMETERS):
        parameter = _shape(value, {"name", "value_type"})
        names.append(_identifier(parameter["name"]))
        _require(isinstance(parameter["value_type"], str)
                 and parameter["value_type"] in SCALAR_TYPES)
    _unique(names)
    _require(isinstance(record["return_type"], str)
             and record["return_type"] in SCALAR_TYPES | {"void"})
    _require(record["stateless"] is True and record["thread_safe"] is True)
    _require("shared_state" not in cast(list[str], record["side_effects"]))


def _record(kind: str, value: JSONValue, *, stored: bool = False) -> Record:
    record = _shape(value, _FIELDS[kind] | ({"source_digest", "change_id"} if stored else set()))
    _identifier(record["id"])
    _require(isinstance(record["target"], str) and record["target"] in _TARGETS)
    _require(record["language"] == _TARGETS[cast(str, record["target"])])
    data = _source(record["source"])
    _text(record["purpose"], MAX_PURPOSE_LENGTH)
    _ids(record["depends_on"], MAX_BLOCKS if kind == "block" else MAX_HELPERS)
    effects = _array(record["side_effects"], MAX_SIDE_EFFECTS)
    _require(all(isinstance(effect, str) and effect in _EFFECTS for effect in effects))
    _unique(cast(list[str], effects))
    _dependencies(record)
    (_block if kind == "block" else _helper)(record)
    if stored:
        _identifier(record["change_id"], provenance=True)
        _require(record["source_digest"] == sha256(data).hexdigest())
    return record


def _records(extensions: Record, key: str) -> list[Record]:
    return [_record(key[:-1], value, stored=True)
            for value in _array(extensions[key], _LIMITS[key])]


def _indexed(records: list[Record]) -> Index:
    return {_identifier(record["id"]): record for record in records}


def _parameter_records(parameters: Record, key: str) -> list[Record]:
    # ParameterModel owns scalar shape and reset validation before this cross-check.
    values = parameters.get(key)
    _require(isinstance(values, list))
    return [_object(value) for value in cast(list[JSONValue], values)]


def _position(initializer: Record, order: dict[str, int]) -> int:
    event = initializer.get("event")
    if event in ("session_start", "iteration_start"):
        return -2 if event == "session_start" else -1
    _require(event in ("before_request", "after_response"))
    exchange = _text(initializer.get("exchange_id"), MAX_PATH_LENGTH)
    _require(exchange in order)
    return 2 * order[exchange] + (1 if event == "after_response" else 0)


def _block_position(block: Record, order: dict[str, int]) -> tuple[int, int]:
    return _position(_object(block["placement"]), order), cast(int, block["order"])


def _references(record: Record, field: str, index: Index) -> list[Record]:
    result: list[Record] = []
    for identifier in cast(list[str], record[field]):
        _require(identifier in index)
        referenced = index[identifier]
        _require(referenced["target"] == record["target"])
        result.append(referenced)
    return result


def _topological(helpers: Index) -> list[Record]:
    remaining, completed = dict(helpers), set[str]()
    result: list[Record] = []
    while remaining:
        ready = sorted(identifier for identifier, helper in remaining.items()
                       if set(cast(list[str], helper["depends_on"])) <= completed)
        _require(bool(ready))
        identifier = ready[0]
        result.append(remaining.pop(identifier))
        completed.add(identifier)
    return result


def _helper_references(helpers: Index) -> None:
    names: list[tuple[str, ...]] = []
    c_symbols: list[str] = []
    for helper in helpers.values():
        names.append(tuple(cast(str, helper[key]) for key in ("target", "namespace", "symbol")))
        if helper["target"] == "loadrunner":
            c_symbols.append(cast(str, helper["symbol"]))
        _references(helper, "depends_on", helpers)
    _unique(names)
    _unique(c_symbols)
    _topological(helpers)


def _behavior(variable: Record) -> str | None:
    source = _object(variable["source"])
    if source.get("kind") != "agent_code":
        return None
    _shape(source, {"kind", "behavior_id"})
    return _identifier(source["behavior_id"])


def _outputs(block: Record, variables: Index) -> None:
    for identifier in cast(list[str], block["outputs"]):
        _require(identifier in variables)
        variable = variables[identifier]
        _require(_behavior(variable) == block["behavior_id"])
        _require(variable["initialize"] == block["placement"])


def _counterpart_outputs(blocks: Index, variables: Index) -> None:
    for identifier, variable in variables.items():
        behavior = _behavior(variable)
        if behavior is None:
            continue
        producers = [block for block in blocks.values() if block["behavior_id"] == behavior]
        _require(bool(producers))
        _require(all(identifier in cast(list[str], block["outputs"]) for block in producers))


def _input(block: Record, variable: Record, blocks: Index, order: dict[str, int]) -> None:
    initializer, placement = _object(variable["initialize"]), _object(block["placement"])
    initialized, consumed = _position(initializer, order), _position(placement, order)
    _require(initialized <= consumed)
    if variable["scope"] == "request":
        _require(initializer.get("exchange_id") == placement["exchange_id"])
    behavior = _behavior(variable)
    if behavior is not None and initialized == consumed:
        producers = [item for item in blocks.values()
                     if item["behavior_id"] == behavior and item["target"] == block["target"]]
        # Missing native counterparts are target diagnostics, not model corruption.
        _require(all(cast(int, producer["order"]) < cast(int, block["order"])
                     for producer in producers))


def _block_references(blocks: Index, helpers: Index, variables: Index,
                      order: dict[str, int]) -> None:
    behaviors: list[tuple[str, str]] = []
    slots: list[tuple[str, tuple[int, int]]] = []
    for block in blocks.values():
        target = cast(str, block["target"])
        behaviors.append((cast(str, block["behavior_id"]), target))
        position = _block_position(block, order)
        slots.append((target, position))
        for dependency in _references(block, "depends_on", blocks):
            _require(_block_position(dependency, order) < position)
        _references(block, "helper_calls", helpers)
        _outputs(block, variables)
        for identifier in cast(list[str], block["inputs"]):
            _require(identifier in variables)
            _input(block, variables[identifier], blocks, order)
    _unique(behaviors)
    _unique(slots)
    _counterpart_outputs(blocks, variables)


def _validate_records(blocks: list[Record], helpers: list[Record], parameters: Record,
                      order: dict[str, int]) -> None:
    parameter_records = {key: _parameter_records(parameters, key)
                         for key in ("variables", "extractors", "bindings")}
    all_records = blocks + helpers + [item for values in parameter_records.values()
                                     for item in values]
    _unique([_identifier(record["id"]) for record in all_records])
    _require(sum(len(cast(str, item["source"]).encode("utf-8")) for item in blocks + helpers)
             <= MAX_TOTAL_SOURCE_BYTES)
    block_index, helper_index = _indexed(blocks), _indexed(helpers)
    _helper_references(helper_index)
    _block_references(block_index, helper_index, _indexed(parameter_records["variables"]), order)


def _apply(extensions: Record, op: Record, change_id: str) -> None:
    action, kind = _OPERATIONS[cast(str, op["op"])]
    values = extensions[kind + "s"]
    _require(isinstance(values, list))
    authored = _object(op[kind]) if action == "upsert" else None
    identifier = authored["id"] if authored is not None else op["id"]
    remaining = [item for item in cast(list[JSONValue], values)
                 if _object(item)["id"] != identifier]
    if authored is None:
        _require(len(remaining) < len(cast(list[JSONValue], values)))
    else:
        remaining.append({**deepcopy(authored), "change_id": change_id,
                          "source_digest": sha256(_source(authored["source"])).hexdigest()})
    extensions[kind + "s"] = sorted(remaining, key=lambda item: cast(str, _object(item)["id"]))


def _missing(blocks: list[Record]) -> list[Diagnostic]:
    implemented: dict[str, set[str]] = {}
    for block in blocks:
        targets = implemented.setdefault(cast(str, block["behavior_id"]), set())
        targets.add(cast(str, block["target"]))
    return [Diagnostic("TARGET_IMPLEMENTATION_MISSING", "error",
                       "Declared behavior has no implementation for this target.",
                       entity_id=behavior, target=target)
            for behavior in sorted(implemented) for target in _TARGETS
            if target not in implemented[behavior]]


def _helper_plan(helper: Record) -> Record:
    return {key: deepcopy(helper[key]) for key in ("id", "namespace", "symbol", "depends_on")}


def _block_plan(block: Record, grouping: Grouping) -> Record:
    result = {key: deepcopy(block[key]) for key in (
        "id", "behavior_id", "placement", "order", "depends_on", "inputs", "outputs",
        "helper_calls",
    )}
    exchange = _object(block["placement"])["exchange_id"]
    result["group_id"] = next((group.id for group in grouping.groups
                               if exchange in group.exchange_ids), None)
    return result


def _target_plan(target: str, blocks: list[Record], helpers: list[Record],
                 grouping: Grouping) -> Record:
    order = {identifier: index for index, identifier in enumerate(grouping.ordered_exchange_ids)}
    selected = sorted((block for block in blocks if block["target"] == target),
                      key=lambda block: (*_block_position(block, order), cast(str, block["id"])))
    selected_helpers = _indexed([helper for helper in helpers if helper["target"] == target])
    return {
        "helper_placement": "setup_registry" if target == "jmeter" else "helper_definitions",
        "helpers": [_helper_plan(helper) for helper in _topological(selected_helpers)],
        "blocks": [_block_plan(block, grouping) for block in selected],
    }


class TargetExtensions:
    """Strict storage API; references and aggregate budgets are final-batch checks."""

    @staticmethod
    def empty() -> Record:
        return {"blocks": [], "helpers": []}

    @staticmethod
    def validate_operation(op: Record) -> bool:
        name = op.get("op")
        if not isinstance(name, str) or name not in _OPERATIONS:
            return False
        action, kind = _OPERATIONS[name]
        _shape(op, {"op", kind if action == "upsert" else "id"})
        if action == "upsert":
            _record(kind, op[kind])
        else:
            _identifier(op["id"])
        return True

    @staticmethod
    def apply_operation(extensions: Record, op: Record, change_id: str) -> bool:
        if not TargetExtensions.validate_operation(op):
            return False
        _identifier(change_id, provenance=True)
        _shape(extensions, set(_LIMITS))
        _apply(extensions, op, change_id)
        return True

    @staticmethod
    def validate(extensions: JSONValue, parameters: Record, capture: Capture,
                 grouping: Grouping) -> None:
        container = _shape(extensions, set(_LIMITS))
        blocks, helpers = (_records(container, key) for key in _LIMITS)
        order = {identifier: index
                 for index, identifier in enumerate(grouping.ordered_exchange_ids)}
        _require(set(order) == {exchange.id for exchange in capture.exchanges}
                 and len(order) == len(grouping.ordered_exchange_ids))
        _validate_records(blocks, helpers, parameters, order)

    @staticmethod
    def diagnostics(extensions: Record, parameters: Record) -> list[Diagnostic]:
        blocks, helpers = (_records(extensions, key) for key in _LIMITS)
        return _missing(blocks) + [Diagnostic(
            "EXTENSION_CODE_UNVERIFIED", "warning",
            "Opaque source semantics, side effects and declared contracts are unverified.",
            entity_id=cast(str, record["id"]), target=cast(str, record["target"]),
        ) for record in sorted(blocks + helpers, key=lambda record: cast(str, record["id"]))]

    @staticmethod
    def plan(extensions: Record, grouping: Grouping) -> Record:
        blocks, helpers = (_records(extensions, key) for key in _LIMITS)
        targets: Record = {target: _target_plan(target, blocks, helpers, grouping)
                           for target in _TARGETS}
        return {"code_semantics": "unverified" if blocks or helpers else "not_applicable",
                "scheduling": "author_declared", "targets": targets}
