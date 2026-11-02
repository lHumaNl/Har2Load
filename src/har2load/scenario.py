"""Pure bounded edits, with no filesystem, network, code execution or HTTP reconstruction.

ChangeSet.parse accepts exactly schema_version/change_id/base_revision/author/
rationale/evidence/operations (schema 1.0). Operations are rename_group,
reset_group_name, resolve_representation and clear_resolution, never arbitrary patches. Evidence
uses exchange-relative RFC 6901 pointers; existence is not truth certification.

ScenarioEditor.initial emits schema 1.1: the original scaffold plus an ordered
representation_selections array. Only display names, selections and revision can
change. Selections contain exchange_id/location/representation/change_id, plus
header_index only for a header choice. Keys are exchange_id/location; ordering is
source index then location. Schema 1.0 is accepted only as the exact legacy draft.

apply is copy-on-success, increments revision once and requires a current base.
Frozen ChangeSet records defensively copy input; nested documents remain mutable,
so apply revalidates them. Durable audit, duplicate change IDs and transactions
belong to storage. conflicts retains every original conflict and reports selected
intent, never semantic equivalence, native readiness or executable capability.
"""

import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn, Self, cast
from urllib.parse import urlsplit

from har2load.domain import Capture, Exchange, Grouping, Har2LoadError, JSONValue
from har2load.serialization import canonical_bytes

_MAX_AUTHOR = 128
_MAX_RATIONALE = 4096
_MAX_NAME = 256
_MAX_POINTER = 256
_MAX_EVIDENCE = 32
_MAX_OPERATIONS = 100
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_CONFLICT = "HAR_REPRESENTATION_CONFLICT"
_QUERY = "/request/queryString"
_PARAMS = "/request/postData/params"
_CHOICES = {
    _QUERY: ("url", "queryString"),
    _PARAMS: ("text", "params"),
    "/request/postData/mimeType": ("mimeType", "header"),
    "/response/content/mimeType": ("mimeType", "header"),
}
_CHANGE_KEYS = {"schema_version", "change_id", "base_revision", "author", "rationale",
                "evidence", "operations"}
_REFERENCE_KEYS = {"exchange_id", "location"}


def _invalid() -> NoReturn:
    raise Har2LoadError("SCENARIO_INVALID", "Invalid scenario or unsupported change.", 3) from None


def _require(condition: bool) -> None:
    if not condition:
        _invalid()


def _object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        _invalid()
    return value


def _array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list):
        _invalid()
    return value


def _text(value: JSONValue, maximum: int | None = None, *, blank: bool = False) -> str:
    if not isinstance(value, str):
        _invalid()
    _require((blank or bool(value.strip())) and (maximum is None or len(value) <= maximum))
    return value


def _identifier(value: JSONValue) -> str:
    result = _text(value)
    _require(_IDENTIFIER.fullmatch(result) is not None)
    return result


def _integer(value: JSONValue, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        _invalid()
    return value


def _pointer(value: JSONValue) -> str:
    pointer = _text(value, _MAX_POINTER, blank=True)
    _require(not pointer or pointer.startswith("/"))
    _require(re.search(r"~(?![01])", pointer) is None)
    return pointer


def _lookup(value: JSONValue, pointer: str) -> JSONValue:
    for part in pointer.split("/")[1:] if pointer else []:
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            _require(index < len(value))
            value = value[index]
        else:
            _invalid()
    return value


def _reference(value: dict[str, JSONValue]) -> None:
    _text(value.get("exchange_id"))
    _pointer(value.get("location"))


def _choice(value: dict[str, JSONValue]) -> None:
    _reference(value)
    location = _text(value.get("location"))
    representation = _text(value.get("representation"))
    _require(location in _CHOICES and representation in _CHOICES[location])
    if representation == "header":
        _integer(value.get("header_index"), 0)
    else:
        _require("header_index" not in value)


def _operation(value: JSONValue) -> None:
    operation = _object(value)
    kind = operation.get("op")
    if kind == "rename_group":
        _require(operation.keys() == {"op", "group_id", "name"})
        _text(operation["group_id"])
        _text(operation["name"], _MAX_NAME)
    elif kind == "reset_group_name":
        _require(operation.keys() == {"op", "group_id"})
        _text(operation["group_id"])
    elif kind == "clear_resolution":
        _require(operation.keys() == {"op"} | _REFERENCE_KEYS)
        _reference(operation)
        _require(_text(operation["location"]) in _CHOICES)
    elif kind == "resolve_representation":
        keys = {"op", "representation"} | _REFERENCE_KEYS
        optional = {"header_index"} if "header_index" in operation else set()
        _require(operation.keys() == keys | optional)
        _choice(operation)
    else:
        _invalid()


def _change_document(value: JSONValue) -> dict[str, JSONValue]:
    document = _object(value)
    _require(document.keys() == _CHANGE_KEYS and document["schema_version"] == "1.0")
    _identifier(document["change_id"])
    _integer(document["base_revision"], 1)
    _text(document["author"], _MAX_AUTHOR)
    _text(document["rationale"], _MAX_RATIONALE)
    evidence, operations = _array(document["evidence"]), _array(document["operations"])
    _require(0 < len(evidence) <= _MAX_EVIDENCE and 0 < len(operations) <= _MAX_OPERATIONS)
    for item in evidence:
        reference = _object(item)
        _require(reference.keys() == _REFERENCE_KEYS)
        _reference(reference)
    for operation in operations:
        _operation(operation)
    return document


@dataclass(frozen=True)
class ChangeSet:
    """Validated bounded change document, copied independently of caller-owned input."""

    document: dict[str, JSONValue]
    change_id: str
    base_revision: int

    @classmethod
    def parse(cls, value: JSONValue) -> Self:
        try:
            document = deepcopy(_change_document(value))
        except Har2LoadError:
            raise Har2LoadError(
                "CHANGE_INVALID", "Invalid or unsupported change document.", 3,
            ) from None
        return cls(document, cast(str, document["change_id"]), cast(int, document["base_revision"]))


def _key(value: dict[str, JSONValue]) -> tuple[str, str]:
    return _text(value.get("exchange_id")), _text(value.get("location"), blank=True)


class _Source:
    def __init__(self, capture: Capture) -> None:
        self.exchanges = {exchange.id: exchange for exchange in capture.exchanges}
        self.diagnostics = tuple(item for item in capture.diagnostics if item.code == _CONFLICT)
        self.keys = {(item.entity_id, item.location) for item in self.diagnostics}

    def exchange(self, entity_id: JSONValue) -> Exchange:
        exchange = self.exchanges.get(_text(entity_id))
        if exchange is None:
            _invalid()
        return exchange

    def conflict(self, value: dict[str, JSONValue]) -> Exchange:
        _require(_key(value) in self.keys)
        return self.exchange(value.get("exchange_id"))

    def order(self, value: JSONValue) -> tuple[int, str]:
        item = _object(value)
        return self.exchange(item.get("exchange_id")).index, _text(item.get("location"))


def _url(value: JSONValue) -> None:
    text = _text(value)
    _require(not any(ord(char) <= 32 or ord(char) == 127 for char in text))
    _require("\\" not in text and re.search(r"%(?![0-9a-fA-F]{2})", text) is None)
    try:
        parsed = urlsplit(text)
        _require(parsed.scheme in {"http", "https"} and bool(parsed.hostname))
        _ = parsed.port
    except ValueError:
        _invalid()


def _pairs(value: JSONValue, *, form: bool) -> None:
    for item in _array(value):
        pair = _object(item)
        _text(pair.get("name"), blank=True)
        _text(pair.get("value"), blank=True)
        _require(not form or "fileName" not in pair)


def _header(raw: JSONValue, selection: dict[str, JSONValue]) -> None:
    side = _text(selection["location"]).split("/")[1]
    headers = _array(_lookup(raw, f"/{side}/headers"))
    index = _integer(selection.get("header_index"), 0)
    _require(index < len(headers))
    header = _object(headers[index])
    _require(_text(header.get("name")).lower() == "content-type")
    _text(header.get("value"))


def _body_choice(raw: JSONValue, location: str, representation: str) -> None:
    parent = location.rsplit("/", 1)[0]
    selected = _lookup(raw, parent + "/" + representation)
    if representation == "params":
        _pairs(selected, form=True)
        mime = _object(_lookup(raw, parent)).get("mimeType")
        _require(not isinstance(mime, str) or not mime.strip().lower().startswith("multipart/"))
    else:
        _text(selected, blank=representation == "text")


def _selected(selection: dict[str, JSONValue], source: _Source) -> None:
    _choice(selection)
    raw = source.conflict(selection).raw
    location, representation = _text(selection["location"]), _text(selection["representation"])
    if representation == "header":
        _header(raw, selection)
    elif location == _QUERY:
        value = _lookup(raw, "/request/" + representation)
        if representation == "url":
            _url(value)
        else:
            _pairs(value, form=False)
    else:
        _body_choice(raw, location, representation)


def _json_shape(value: JSONValue) -> None:
    # The JSON encoder accepts tuples, but the domain deliberately accepts only lists.
    if isinstance(value, dict):
        for key, item in value.items():
            _require(isinstance(key, str))
            _json_shape(item)
    elif isinstance(value, list):
        for item in value:
            _json_shape(item)
    else:
        _require(type(value) in (str, int, bool, float, Decimal, type(None)))


def _same(left: JSONValue, right: JSONValue) -> bool:
    try:
        _json_shape(left)
        return canonical_bytes(left) == canonical_bytes(right)
    except (ValueError, TypeError, RecursionError, OverflowError):
        _invalid()


def _groups(value: JSONValue, expected: JSONValue) -> None:
    groups, originals = _array(value), _array(expected)
    _require(len(groups) == len(originals))
    for item, original in zip(groups, originals, strict=True):
        group, baseline = _object(item), _object(original)
        if group.get("name") != baseline["name"]:
            _text(group.get("name"), _MAX_NAME)
        _require("name" in group and _same({**group, "name": baseline["name"]}, baseline))


def _selections(value: JSONValue, source: _Source) -> None:
    selections = _array(value)
    seen: set[tuple[str, str]] = set()
    for item in selections:
        selection = _object(item)
        keys = _REFERENCE_KEYS | {"representation", "change_id"}
        if "header_index" in selection:
            keys = keys | {"header_index"}
        _require(selection.keys() == keys)
        _identifier(selection["change_id"])
        _selected(selection, source)
        key = _key(selection)
        _require(key not in seen)
        seen.add(key)
    _require(selections == sorted(selections, key=source.order))


def _evidence(document: dict[str, JSONValue], source: _Source) -> None:
    for item in _array(document["evidence"]):
        reference = _object(item)
        exchange = source.exchange(reference["exchange_id"])
        _lookup(exchange.raw, _pointer(reference["location"]))


def _rename(scenario: dict[str, JSONValue], operation: dict[str, JSONValue]) -> None:
    for item in _array(scenario["groups"]):
        group = _object(item)
        if group["id"] == operation["group_id"]:
            group["name"] = operation["name"]
            return
    _invalid()


def _edit(scenario: dict[str, JSONValue], operation: dict[str, JSONValue],
          source: _Source, change_id: str, grouping: Grouping) -> None:
    if operation["op"] == "rename_group":
        _rename(scenario, operation)
        return
    if operation["op"] == "reset_group_name":
        for group in grouping.groups:
            if group.id == operation["group_id"]:
                _rename(scenario, {"group_id": group.id, "name": group.name})
                return
        _invalid()
    source.conflict(operation)
    selections = [item for item in _array(scenario["representation_selections"])
                  if _key(_object(item)) != _key(operation)]
    if operation["op"] == "resolve_representation":
        _selected(operation, source)
        selections.append({key: value for key, value in operation.items() if key != "op"})
        _object(selections[-1])["change_id"] = change_id
    scenario["representation_selections"] = sorted(selections, key=source.order)


class ScenarioEditor:
    """Copy-on-success author intent; immutable source grouping and execution states."""

    def initial(self, capture: Capture, grouping: Grouping) -> dict[str, JSONValue]:
        return {
            "schema_version": "1.1", "revision": 1, "source_capture": capture.id,
            "step_gap_seconds": grouping.step_gap_seconds,
            "groups": [{
                "id": group.id, "exchange_ids": list(group.exchange_ids), "name": group.name,
                "origin": group.origin, "boundary_reason": group.boundary_reason,
            } for group in grouping.groups],
            "ordered_exchange_ids": list(grouping.ordered_exchange_ids), "status": "draft",
            "targets": {"jmeter": "not_generated", "loadrunner": "not_generated"},
            "native_validation": "not_run", "execution": "not_run",
            "representation_selections": [],
        }

    def validate(self, scenario: JSONValue, capture: Capture, grouping: Grouping) -> None:
        value, expected = _object(scenario), self.initial(capture, grouping)
        _integer(value.get("revision"), 1)
        if value.get("schema_version") == "1.0":
            expected["schema_version"] = "1.0"
            del expected["representation_selections"]
            _require(_same(value, expected))
            return
        _require(value.keys() == expected.keys())
        _groups(value["groups"], expected["groups"])
        _selections(value["representation_selections"], _Source(capture))
        immutable = {**value, "groups": expected["groups"], "revision": 1,
                     "representation_selections": []}
        _require(_same(immutable, expected))

    def apply(self, scenario: dict[str, JSONValue], capture: Capture, grouping: Grouping,
              change: ChangeSet) -> dict[str, JSONValue]:
        self.validate(scenario, capture, grouping)
        _require(scenario["schema_version"] == "1.1")
        validated = ChangeSet.parse(change.document)
        _require(validated.change_id == change.change_id and type(change.base_revision) is int
                 and validated.base_revision == change.base_revision)
        if validated.base_revision != scenario["revision"]:
            raise Har2LoadError("CHANGE_STALE", "Change base revision is stale.", 4)
        source = _Source(capture)
        _evidence(validated.document, source)
        result = deepcopy(scenario)
        for operation in _array(validated.document["operations"]):
            _edit(result, _object(operation), source, validated.change_id, grouping)
        result["revision"] = validated.base_revision + 1
        self.validate(result, capture, grouping)
        return result

    def conflicts(self, scenario: dict[str, JSONValue], capture: Capture) -> list[JSONValue]:
        source = _Source(capture)
        values = scenario.get("representation_selections", [])
        _selections(values, source)
        selections = {_key(_object(item)): item for item in _array(values)}
        records: list[JSONValue] = []
        for diagnostic in source.diagnostics:
            selection = selections.get((_text(diagnostic.entity_id), _text(diagnostic.location)))
            records.append({"exchange_id": diagnostic.entity_id, "location": diagnostic.location,
                            "code": diagnostic.code,
                            "status": "selected" if selection is not None else "unresolved",
                            "selection": deepcopy(selection)})
        return sorted(records, key=source.order)
