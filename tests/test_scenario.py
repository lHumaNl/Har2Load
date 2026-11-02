"""Synthetic contracts for bounded, offline scenario edits and selection intent."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast

import pytest

from har2load.domain import Capture, Har2LoadError, JSONValue
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.scenario import ChangeSet, ScenarioEditor

QUERY = "/request/queryString"
PARAMS = "/request/postData/params"
REQUEST_MIME = "/request/postData/mimeType"
RESPONSE_MIME = "/response/content/mimeType"
EDITOR = ScenarioEditor()


def raw_entry() -> dict[str, JSONValue]:
    return {
        "comment": " Original ", "a/b~c": {"": [None, "evidence"]},
        "request": {
            "method": "POST", "url": "https://example.invalid/?a=url",
            "queryString": [{"name": "a", "value": "query"}],
            "headers": [{"name": "Other", "value": "ignored"},
                        {"name": "CONTENT-TYPE", "value": "text/plain"},
                        {"name": "Content-Type", "value": "application/json"}],
            "postData": {"mimeType": "application/x-www-form-urlencoded", "text": "a=text",
                         "params": [{"name": "a", "value": "params"}]},
        },
        "response": {"status": 200, "headers": [{"name": "content-type", "value": "text/plain"}],
                     "content": {"mimeType": "application/json", "text": "{}"}},
    }


def import_entries(*entries: JSONValue) -> Capture:
    return HarImporter().parse(json.dumps({"log": {"entries": entries}}).encode())


@pytest.fixture
def capture() -> Capture:
    return import_entries(raw_entry())


def initial(capture: Capture) -> dict[str, JSONValue]:
    return EDITOR.initial(capture, StepGrouper().group(capture))


def objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], value)


def change_document(capture: Capture, *operations: JSONValue) -> dict[str, JSONValue]:
    return {
        "schema_version": "1.0", "change_id": "change-1", "base_revision": 1,
        "author": "synthetic author", "rationale": "Explicit author intent, not equivalence.",
        "evidence": [{"exchange_id": capture.exchanges[0].id, "location": "/request"}],
        "operations": list(operations) or [rename(capture)],
    }


def rename(capture: Capture, name: str = "Renamed") -> dict[str, JSONValue]:
    return {"op": "rename_group", "group_id": StepGrouper().group(capture).groups[0].id,
            "name": name}


def resolve(capture: Capture, location: str = QUERY,
            representation: str = "url", **extra: JSONValue) -> dict[str, JSONValue]:
    return {"op": "resolve_representation", "exchange_id": capture.exchanges[0].id,
            "location": location, "representation": representation, **extra}


def apply(capture: Capture, document: JSONValue,
          scenario: dict[str, JSONValue] | None = None) -> dict[str, JSONValue]:
    return EDITOR.apply(initial(capture) if scenario is None else scenario, capture,
                        StepGrouper().group(capture), ChangeSet.parse(document))


def test_initial_contract_and_legacy_read_only(capture: Capture) -> None:
    scenario = initial(capture)
    assert scenario == {
        "schema_version": "1.3", "revision": 1, "source_capture": capture.id,
        "step_gap_seconds": 5.0, "groups": [{
            "id": StepGrouper().group(capture).groups[0].id,
            "exchange_ids": [capture.exchanges[0].id], "name": " Original ",
            "origin": "annotation", "boundary_reason": "capture_start"}],
        "ordered_exchange_ids": [capture.exchanges[0].id], "status": "draft",
        "targets": {"jmeter": "not_generated", "loadrunner": "not_generated"},
        "native_validation": "not_run", "execution": "not_run", "representation_selections": [],
        "parameters": {"variables": [], "extractors": [], "bindings": []},
        "target_extensions": {"blocks": [], "helpers": []},
    }
    legacy = {key: value for key, value in scenario.items()
              if key not in ("representation_selections", "parameters", "target_extensions")}
    legacy["schema_version"] = "1.0"
    EDITOR.validate(legacy, capture, StepGrouper().group(capture))
    with pytest.raises(Har2LoadError):
        apply(capture, change_document(capture), legacy)


def test_changeset_copies_document_and_is_frozen(capture: Capture) -> None:
    document = change_document(capture)
    change = ChangeSet.parse(document)
    assert change.change_id == "change-1" and change.base_revision == 1
    assert change.document == document and change.document is not document
    objects(document["operations"])[0]["name"] = "Changed outside"
    assert objects(change.document["operations"])[0]["name"] == "Renamed"
    with pytest.raises(FrozenInstanceError):
        change.change_id = "different"  # type: ignore[misc]


@pytest.mark.parametrize("key,value", [
    ("extra", "private-sentinel"), ("schema_version", "1.1"), ("change_id", "../private-sentinel"),
    ("change_id", "x" * 65), ("change_id", "é"), ("change_id", ""),
    ("base_revision", True), ("base_revision", False), ("base_revision", 0),
    ("base_revision", -1), ("base_revision", 1.0), ("base_revision", Decimal(1)),
    ("base_revision", float("nan")), ("base_revision", Decimal("Infinity")),
    ("author", " "), ("author", "x" * 129), ("author", None),
    ("rationale", ""), ("rationale", "x" * 4097), ("rationale", []),
    ("operations", []), ("operations", [{}] * 101), ("operations", {}),
    ("evidence", []), ("evidence", [{}] * 33), ("evidence", "private-sentinel"),
])
def test_strict_changeset_fields(capture: Capture, key: str, value: JSONValue) -> None:
    document = {**change_document(capture), key: value}
    with pytest.raises(Har2LoadError) as caught:
        ChangeSet.parse(document)
    assert caught.value.exit_code == 3 and caught.value.code.isupper()
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize("key", ["schema_version", "change_id", "base_revision", "author",
                                 "rationale", "evidence", "operations"])
def test_required_changeset_keys(capture: Capture, key: str) -> None:
    document = change_document(capture)
    del document[key]
    with pytest.raises(Har2LoadError):
        ChangeSet.parse(document)


@pytest.mark.parametrize("operation", [
    None, [], {}, {"op": "eval", "code": "private-sentinel"},
    {"op": "rename_group", "group_id": "group", "name": " "},
    {"op": "rename_group", "group_id": True, "name": "name"},
    {"op": "rename_group", "group_id": "group", "name": "x" * 257},
    {"op": "rename_group", "group_id": "group", "name": "name", "extra": None},
    {"op": "clear_resolution", "exchange_id": "ex", "location": QUERY, "representation": "url"},
])
def test_strict_operations(capture: Capture, operation: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        ChangeSet.parse(change_document(capture, operation))


@pytest.mark.parametrize("location,representation,extra", [
    (QUERY, "url", {"header_index": 0}), (QUERY, "text", {}),
    (REQUEST_MIME, "header", {}), (REQUEST_MIME, "header", {"header_index": True}),
    (REQUEST_MIME, "header", {"header_index": -1}),
    (REQUEST_MIME, "header", {"header_index": 1.0}), (PARAMS, "params", {"extra": "secret"}),
    ("/unsupported", "text", {}),
])
def test_invalid_selection_shapes(capture: Capture, location: str, representation: str,
                                  extra: dict[str, JSONValue]) -> None:
    operation = resolve(capture, location, representation, **extra)
    with pytest.raises(Har2LoadError):
        apply(capture, change_document(capture, operation))


@pytest.mark.parametrize("pointer", ["", "/", "/a~1b~0c//0", "/a~1b~0c//1"])
def test_rfc6901_evidence(capture: Capture, pointer: str) -> None:
    entry = raw_entry()
    entry[""] = "empty key"
    capture = import_entries(entry)
    document = change_document(capture)
    objects(document["evidence"])[0]["location"] = pointer
    assert apply(capture, document)["revision"] == 2


@pytest.mark.parametrize("pointer", ["missing", "/~", "/~2", "/missing", "/a~1b~0c//01",
                                    "/a~1b~0c//-", "/a~1b~0c//2", "/a~1b~0c//0/a",
                                    "/a~1b~0c//١", "/" + "x" * 256])
def test_bad_or_missing_evidence_pointer(capture: Capture, pointer: str) -> None:
    document = change_document(capture)
    objects(document["evidence"])[0]["location"] = pointer
    with pytest.raises(Har2LoadError):
        apply(capture, document)


@pytest.mark.parametrize("evidence", [
    {"exchange_id": "private-sentinel", "location": ""},
    {"exchange_id": False, "location": ""}, {"location": ""},
    {"exchange_id": "ex", "location": [], "extra": 1},
])
def test_bad_evidence_reference(capture: Capture, evidence: JSONValue) -> None:
    with pytest.raises(Har2LoadError) as caught:
        apply(capture, {**change_document(capture), "evidence": [evidence]})
    assert "private-sentinel" not in str(caught.value)


def test_rename_undo_and_revision_once(capture: Capture) -> None:
    result = apply(capture, change_document(capture, rename(capture, "First"), rename(capture)))
    assert result["revision"] == 2 and objects(result["groups"])[0]["name"] == "Renamed"
    undo = {**change_document(capture, rename(capture, " Original ")),
            "change_id": "undo", "base_revision": 2}
    result = apply(capture, undo, result)
    assert result["revision"] == 3 and objects(result["groups"])[0]["name"] == " Original "


def test_stale_revision_is_exit_four(capture: Capture) -> None:
    document = change_document(capture)
    result = apply(capture, document)
    with pytest.raises(Har2LoadError) as caught:
        apply(capture, document, result)
    assert caught.value.exit_code == 4 and caught.value.code.isupper()


def test_atomic_failure_and_source_immutability(capture: Capture) -> None:
    scenario = initial(capture)
    before = deepcopy((scenario, capture))
    document = change_document(capture, rename(capture), resolve(capture, REQUEST_MIME, "header",
                                                                header_index=99))
    document_before = deepcopy(document)
    with pytest.raises(Har2LoadError):
        apply(capture, document, scenario)
    assert (scenario, capture) == before and document == document_before
    apply(capture, change_document(capture, rename(capture), resolve(capture)), scenario)
    assert (scenario, capture) == before


@pytest.mark.parametrize("location,representation,extra", [
    (QUERY, "url", {}), (QUERY, "queryString", {}), (PARAMS, "text", {}), (PARAMS, "params", {}),
    (REQUEST_MIME, "mimeType", {}), (REQUEST_MIME, "header", {"header_index": 1}),
    (REQUEST_MIME, "header", {"header_index": 2}), (RESPONSE_MIME, "mimeType", {}),
    (RESPONSE_MIME, "header", {"header_index": 0}),
])
def test_all_representation_choices(capture: Capture, location: str, representation: str,
                                    extra: dict[str, JSONValue]) -> None:
    original = deepcopy(capture)
    result = apply(capture, change_document(capture, resolve(capture, location, representation,
                                                           **extra)))
    expected = {"exchange_id": capture.exchanges[0].id, "location": location,
                "representation": representation, "change_id": "change-1", **extra}
    assert result["representation_selections"] == [expected]
    conflicts = objects(EDITOR.conflicts(result, capture))
    assert len(conflicts) == 4 and sum(c["status"] == "selected" for c in conflicts) == 1
    assert next(c for c in conflicts if c["location"] == location)["selection"] == expected
    assert result["status"] == "draft"
    assert result["execution"] == result["native_validation"] == "not_run"
    assert capture == original


def test_selection_replace_clear_and_detached_conflict_records(capture: Capture) -> None:
    result = apply(capture, change_document(capture, resolve(capture),
                                           resolve(capture, QUERY, "queryString")))
    assert len(objects(result["representation_selections"])) == 1
    records = EDITOR.conflicts(result, capture)
    selected = next(record["selection"] for record in objects(records) if record["selection"])
    cast(dict[str, JSONValue], selected)["representation"] = "tampered"
    assert objects(result["representation_selections"])[0]["representation"] == "queryString"
    clear = {"op": "clear_resolution", "exchange_id": capture.exchanges[0].id, "location": QUERY}
    document = {**change_document(capture, clear), "base_revision": 2, "change_id": "undo"}
    cleared = apply(capture, document, result)
    assert cleared["representation_selections"] == [] and cleared["revision"] == 3
    assert all(c["status"] == "unresolved" and c["selection"] is None
               for c in objects(EDITOR.conflicts(cleared, capture)))


@pytest.mark.parametrize("pointer,value,location,representation,extra", [
    ("/request/url", "ftp://example.invalid", QUERY, "url", {}),
    ("/request/url", "https://example.invalid:bad/", QUERY, "url", {}),
    ("/request/url", " https://example.invalid/", QUERY, "url", {}),
    ("/request/queryString", [{"name": "a", "value": 1}], QUERY, "queryString", {}),
    (PARAMS, [{"name": "a", "value": "b", "fileName": "f"}], PARAMS, "params", {}),
    ("/request/postData/params", [{"name": "a"}], PARAMS, "params", {}),
    ("/request/postData/text", False, PARAMS, "text", {}),
    ("/request/postData/mimeType", " ", REQUEST_MIME, "mimeType", {}),
    ("/request/headers/1/value", "", REQUEST_MIME, "header", {"header_index": 1}),
    ("/request/headers/1/name", "Other", REQUEST_MIME, "header", {"header_index": 1}),
])
def test_invalid_selected_source_data(pointer: str, value: JSONValue, location: str,
                                      representation: str, extra: dict[str, JSONValue]) -> None:
    entry = raw_entry()
    parts = pointer.strip("/").split("/")
    parent = entry
    for part in parts[:-1]:
        parent = cast(dict[str, JSONValue], parent[int(part)] if isinstance(parent, list)
                      else parent[part])
    parent[parts[-1]] = value
    source = import_entries(entry)
    with pytest.raises(Har2LoadError):
        apply(source, change_document(source, resolve(source, location, representation, **extra)))


@pytest.mark.parametrize("field,value", [
    ("unknown", None), ("revision", True), ("revision", 1.0), ("revision", 0),
    ("source_capture", "wrong"), ("step_gap_seconds", True), ("step_gap_seconds", float("nan")),
    ("ordered_exchange_ids", []), ("groups", []), ("targets", {"jmeter": "generated"}),
    ("status", "validated"), ("native_validation", "passed"), ("execution", "passed"),
    ("representation_selections", {}), ("schema_version", "2.0"),
])
def test_immutable_scenario_fields(capture: Capture, field: str, value: JSONValue) -> None:
    scenario = {**initial(capture), field: value}
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, capture, StepGrouper().group(capture))


@pytest.mark.parametrize("field,value", [
    ("id", "wrong"), ("exchange_ids", []), ("origin", "manual"), ("boundary_reason", "custom"),
    ("name", "x" * 257), ("name", False), ("name", " "), ("extra", 1),
])
def test_strict_group_fields(capture: Capture, field: str, value: JSONValue) -> None:
    scenario = initial(capture)
    objects(scenario["groups"])[0][field] = value
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, capture, StepGrouper().group(capture))


def test_huge_raw_annotation_is_preserved_but_new_name_is_bounded() -> None:
    entry = {**raw_entry(), "comment": "x" * 100_000}
    source = import_entries(entry)
    scenario = initial(source)
    EDITOR.validate(scenario, source, StepGrouper().group(source))
    result = apply(source, change_document(source, resolve(source)))
    assert objects(result["groups"])[0]["name"] == entry["comment"]
    assert objects(apply(source, change_document(source))["groups"])[0]["name"] == "Renamed"


def test_selections_are_sorted_by_source_index_and_location() -> None:
    source = import_entries(raw_entry(), raw_entry())
    second = {**resolve(source, RESPONSE_MIME, "mimeType"), "exchange_id": source.exchanges[1].id}
    first = resolve(source, REQUEST_MIME, "mimeType")
    result = apply(source, change_document(source, second, first, resolve(source)))
    selections = objects(result["representation_selections"])
    assert [(s["exchange_id"], s["location"]) for s in selections] == [
        (source.exchanges[0].id, REQUEST_MIME), (source.exchanges[0].id, QUERY),
        (source.exchanges[1].id, RESPONSE_MIME)]


def test_no_arbitrary_conflicts_or_unknown_groups(capture: Capture) -> None:
    entry = raw_entry()
    cast(dict[str, JSONValue], entry["request"])["queryString"] = [{"name": "a", "value": "url"}]
    source = import_entries(entry)
    for operation in (resolve(source), {**rename(source), "group_id": "private-sentinel"}):
        with pytest.raises(Har2LoadError) as caught:
            apply(source, change_document(source, operation))
        assert "private-sentinel" not in str(caught.value)


def test_empty_text_and_pairs_are_valid_choices() -> None:
    entry = raw_entry()
    post = cast(dict[str, JSONValue], cast(dict[str, JSONValue], entry["request"])["postData"])
    post["text"] = ""
    source = import_entries(entry)
    assert apply(source, change_document(source, resolve(source, PARAMS, "text")))["revision"] == 2
    post["params"], post["text"] = [], "different"
    source = import_entries(entry)
    result = apply(source, change_document(source, resolve(source, PARAMS, "params")))
    assert result["revision"] == 2


@pytest.mark.parametrize("field,value", [
    ("unknown", None), ("change_id", "../secret"), ("representation", True),
    ("header_index", 0), ("exchange_id", "missing"), ("location", PARAMS),
])
def test_strict_stored_selections(capture: Capture, field: str, value: JSONValue) -> None:
    scenario = apply(capture, change_document(capture, resolve(capture)))
    objects(scenario["representation_selections"])[0][field] = value
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, capture, StepGrouper().group(capture))


@pytest.mark.parametrize("duplicate", [True, False])
def test_duplicate_or_unsorted_selections(capture: Capture, duplicate: bool) -> None:
    scenario = apply(capture, change_document(capture, resolve(capture),
                                             resolve(capture, REQUEST_MIME, "mimeType")))
    selections = objects(scenario["representation_selections"])
    if duplicate:
        selections.append(deepcopy(selections[0]))
    else:
        selections.reverse()
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, capture, StepGrouper().group(capture))


@pytest.mark.parametrize("field,value", [("revision", 2), ("representation_selections", []),
                                        ("groups", []), ("status", "validated")])
def test_legacy_must_match_exact_initial(capture: Capture, field: str, value: JSONValue) -> None:
    legacy = EDITOR.initial(capture, StepGrouper().group(capture), schema_version="1.0")
    legacy[field] = value
    with pytest.raises(Har2LoadError):
        EDITOR.validate(legacy, capture, StepGrouper().group(capture))


def test_missing_mime_can_choose_existing_header_only() -> None:
    entry = raw_entry()
    request = cast(dict[str, JSONValue], entry["request"])
    del cast(dict[str, JSONValue], request["postData"])["mimeType"]
    source = import_entries(entry)
    with pytest.raises(Har2LoadError):
        apply(source, change_document(source, resolve(source, REQUEST_MIME, "mimeType")))
    header = resolve(source, REQUEST_MIME, "header", header_index=1)
    assert apply(source, change_document(source, header))["revision"] == 2


def test_unsupported_original_conflict_is_retained() -> None:
    entry = raw_entry()
    response = cast(dict[str, JSONValue], entry["response"])
    cast(dict[str, JSONValue], response["content"])["params"] = [{"name": "x", "value": "y"}]
    source = import_entries(entry)
    records = objects(EDITOR.conflicts(initial(source), source))
    assert len(records) == 5
    location = "/response/content/params"
    unsupported = next(record for record in records if record["location"] == location)
    assert unsupported["code"] == "HAR_REPRESENTATION_CONFLICT"
    assert unsupported["selection"] is None and unsupported["status"] == "unresolved"
    with pytest.raises(Har2LoadError):
        apply(source, change_document(source, resolve(source, "/response/content/params", "text")))


def test_mutated_or_forged_changeset_is_revalidated(capture: Capture) -> None:
    change = ChangeSet.parse(change_document(capture))
    change.document["base_revision"] = True
    forged = ChangeSet(change_document(capture), "mismatch", 1)
    for invalid in (change, forged):
        with pytest.raises(Har2LoadError):
            EDITOR.apply(initial(capture), capture, StepGrouper().group(capture), invalid)


def test_non_json_nested_container_is_rejected(capture: Capture) -> None:
    scenario = initial(capture)
    scenario["ordered_exchange_ids"] = cast(JSONValue, (capture.exchanges[0].id,))
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, capture, StepGrouper().group(capture))


@pytest.mark.parametrize("value", [None, True, [], 1, float("inf"), Decimal("NaN")])
def test_root_types_are_rejected(capture: Capture, value: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        ChangeSet.parse(value)
    with pytest.raises(Har2LoadError):
        EDITOR.validate(value, capture, StepGrouper().group(capture))


def test_maximum_authored_bounds_are_accepted(capture: Capture) -> None:
    document = change_document(capture, *[rename(capture, "n" * 256)] * 100)
    document.update(change_id="A" + "x" * 63, author="a" * 128, rationale="r" * 4096)
    document["evidence"] = objects(document["evidence"]) * 32
    assert apply(capture, document)["revision"] == 2


def test_explicit_reset_restores_even_large_source_annotation():
    source = import_entries({**raw_entry(), "comment": "original" * 200})
    edited = apply(source, change_document(source))
    reset = {"op": "reset_group_name", "group_id": objects(edited["groups"])[0]["id"]}
    document = {**change_document(source, reset), "change_id": "undo-name", "base_revision": 2}
    restored = apply(source, document, edited)
    assert objects(restored["groups"])[0]["name"] == "original" * 200
    assert restored["revision"] == 3


def test_error_text_does_not_include_selected_source_values() -> None:
    entry = raw_entry()
    cast(dict[str, JSONValue], entry["request"])["url"] = "https://example.invalid:private-sentinel"
    source = import_entries(entry)
    with pytest.raises(Har2LoadError) as caught:
        apply(source, change_document(source, resolve(source)))
    assert "private-sentinel" not in str(caught.value)
    assert caught.value.__suppress_context__
