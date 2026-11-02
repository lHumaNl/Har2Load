import json
from copy import deepcopy

import pytest

from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.runtime_planning import GenerationPlanner
from har2load.runtime_types import RuntimeSlot
from har2load.scenario import ScenarioEditor


def fixture(raw_request=None):
    capture = HarImporter().parse(json.dumps({"log": {"entries": [
        {"request": {"method": "GET", "url": "https://example.invalid/source"},
         "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "[recorded]"}}},
        {"request": {"method": "GET", "url": "https://example.invalid/a?x=old&x=%2f+z&&flag",
                     **(raw_request or {})}, "response": {"status": 200}},
    ]}}).encode())
    grouping = StepGrouper().group(capture)
    model = ScenarioEditor().initial(capture, grouping)
    producer, consumer = (e.id for e in capture.exchanges)
    model["parameters"] = {"variables": [{"id": "v", "name": "token", "value_type": "string",
        "source": {"kind": "extracted", "extractor_id": "e"}, "scope": "iteration",
        "reset": "iteration_start", "initialize": {"event": "after_response",
        "exchange_id": producer}, "purpose": "Synthetic correlation", "sensitive": True,
        "change_id": "f"}], "extractors": [{"id": "e", "exchange_id": producer,
        "area": "response_body", "selector": {"dialect": "boundary_v1", "left": "[", "right": "]"},
        "cardinality": "exactly_one", "allow_empty": False,
        "on_failure": "mark_error_and_continue", "change_id": "f"}],
        "bindings": [{"id": "b", "variable_id": "v", "exchange_id": consumer,
                      "location": {"area": "request_query", "index": 0}, "change_id": "f"}]}
    return capture, grouping, model


def test_runtime_slots_are_typed_and_do_not_substitute_recorded_values():
    values = fixture()
    before = deepcopy(values[2])
    plan = GenerationPlanner().build(*values)
    assert not plan.diagnostics
    assert plan.runtime.requests[1].url.parts == (
        "https://example.invalid/a?x=", RuntimeSlot("b", "v", "form"), "&x=%2f+z&&flag")
    assert all(d["kind"] != "literal_variable_folded" for d in plan.decisions)
    assert values[2] == before


def test_literal_only_preserves_previous_profile():
    values = fixture()
    values[2]["parameters"]["extractors"] = []
    values[2]["parameters"]["variables"][0]["source"] = {"kind": "literal", "value": "constant"}
    plan = GenerationPlanner().build(*values)
    assert plan.runtime is None
    assert plan.requests[1].url.endswith("?x=constant&x=%2f+z&&flag")


def test_json_templates_preserve_type_and_unbound_decimal_precision():
    values = fixture({"method": "POST", "postData": {"mimeType": "application/json",
        "text": '{"x":7,"same":7,"precise":9007199254740993.5}'}})
    p = values[2]["parameters"]
    p["variables"][0]["value_type"] = "integer"
    p["bindings"][0]["location"] = {"area": "request_json", "pointer": "/x"}
    plan = GenerationPlanner().build(*values)
    assert not plan.diagnostics
    assert plan.runtime.requests[1].body.parts == (
        '{"precise":9007199254740993.5,"same":7,"x":', RuntimeSlot("b", "v", "json"), "}")


def test_text_literal_and_dynamic_spans_never_shift_offsets():
    values = fixture({"method": "POST", "postData": {"mimeType": "text/plain", "text": "雪abc"}})
    p = values[2]["parameters"]
    p["bindings"][0]["location"] = {"area": "request_text", "start": 0, "end": 1}
    p["variables"].append({**deepcopy(p["variables"][0]), "id": "literal", "name": "constant",
                           "source": {"kind": "literal", "value": "${DATA}"}})
    p["bindings"].append({**deepcopy(p["bindings"][0]), "id": "b2", "variable_id": "literal",
                          "location": {"area": "request_text", "start": 2, "end": 3}})
    plan = GenerationPlanner().build(*values)
    assert plan.runtime.requests[1].body.parts == (
        RuntimeSlot("b", "v", "text"), "a", RuntimeSlot("b2", "literal", "text"), "c")


@pytest.mark.parametrize("header", ["Content-Type", "Content-Encoding", "Host", "Connection"])
def test_dynamic_control_headers_block_explicitly(header):
    values = fixture({"headers": [{"name": header, "value": "identity"}]})
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_header", "index": 0}
    plan = GenerationPlanner().build(*values)
    assert any(d.code == "GENERATION_RUNTIME_HEADER_UNSUPPORTED" for d in plan.diagnostics)


def test_original_header_occurrence_maps_after_managed_omission():
    values = fixture({"headers": [{"name": "Host", "value": "example.invalid"},
        {"name": "X-Dup", "value": "one"}, {"name": "X-Dup", "value": "two"}]})
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_header", "index": 2}
    plan = GenerationPlanner().build(*values)
    assert plan.runtime.requests[1].headers[0][1].parts == ("one",)
    assert plan.runtime.requests[1].headers[1][1].parts == (RuntimeSlot("b", "v", "header"),)


def test_selected_query_form_and_path_remain_independent():
    values = fixture({"method": "POST", "queryString": [{"name": "q", "value": "selected"}],
        "postData": {"mimeType": "application/x-www-form-urlencoded", "text": "wrong=raw",
                     "params": [{"name": "q", "value": "selected-form"}]}})
    model = values[2]
    entity = values[0].exchanges[1].id
    model["representation_selections"] = [{"exchange_id": entity, "change_id": "f",
        "location": location, "representation": representation} for location, representation in (
            ("/request/queryString", "queryString"), ("/request/postData/params", "params"))]
    p = model["parameters"]
    p["bindings"].extend({**p["bindings"][0], "id": name,
                          "location": {"area": area, "index": 0}}
                         for name, area in (("path", "request_path"), ("form", "request_form")))
    plan = GenerationPlanner().build(*values)
    assert not plan.diagnostics
    assert plan.runtime.requests[1].url.parts == (
        "https://example.invalid/", RuntimeSlot("path", "v", "path"), "?q=",
        RuntimeSlot("b", "v", "form"))
    assert plan.runtime.requests[1].body.parts == ("q=", RuntimeSlot("form", "v", "form"))


def test_unbound_extractors_retained_and_model_declarations_detached():
    values = fixture()
    plan = GenerationPlanner().build(*values)
    plan.runtime.variables[0]["source"]["kind"] = "mutated"
    assert values[2]["parameters"]["variables"][0]["source"]["kind"] == "extracted"
    values[2]["parameters"]["variables"] = []
    values[2]["parameters"]["bindings"] = []
    assert len(GenerationPlanner().build(*values).runtime.extractors) == 1


def test_json_pointer_escaping():
    values = fixture({"method": "POST", "postData": {"mimeType": "application/json",
                                                      "text": '{"a/b~c":"old"}'}})
    values[2]["parameters"]["bindings"][0]["location"] = {
        "area": "request_json", "pointer": "/a~1b~0c"}
    assert GenerationPlanner().build(*values).runtime.requests[1].body.parts == (
        '{"a/b~c":', RuntimeSlot("b", "v", "json"), "}")


@pytest.mark.parametrize("value,value_type", [("root", "string"), (7, "integer"),
                                             (True, "boolean"), (None, "null")])
def test_root_json_scalar_destination(value, value_type):
    values = fixture({"method": "POST", "postData": {"mimeType": "application/json",
                                                      "text": json.dumps(value)}})
    values[2]["parameters"]["variables"][0]["value_type"] = value_type
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_json", "pointer": ""}
    plan = GenerationPlanner().build(*values)
    assert not plan.diagnostics
    assert plan.runtime.requests[1].body.parts == (RuntimeSlot("b", "v", "json"),)


def test_runtime_plan_limits_expansion_before_full_json_template(monkeypatch):
    import har2load.runtime_planning as module

    values = fixture({"method": "POST", "postData": {"mimeType": "application/json",
        "text": json.dumps({"a": "雪" * 20, "b": "old"}, ensure_ascii=False)}})
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_json", "pointer": "/b"}
    monkeypatch.setattr(module, "MAX_RUNTIME_FIELD_BYTES", 100)
    plan = GenerationPlanner().build(*values)
    assert any(d.code == "GENERATION_RUNTIME_LIMIT" for d in plan.diagnostics)


def test_explicit_selected_empty_query_survives_runtime_lowering():
    values = fixture({"url": "https://example.invalid/a?",
                      "queryString": [{"name": "discard", "value": "1"}]})
    values[2]["parameters"]["bindings"] = []
    values[2]["representation_selections"] = [{"exchange_id": values[0].exchanges[1].id,
        "location": "/request/queryString", "representation": "url", "change_id": "f"}]
    plan = GenerationPlanner().build(*values)
    assert plan.runtime.requests[1].url.parts == ("https://example.invalid/a?",)


def test_runtime_form_binding_keeps_bare_flag_and_duplicate_field_spelling():
    values = fixture({"method": "POST", "postData": {
        "mimeType": "application/x-www-form-urlencoded", "text": "x=%2f+z&&flag&x=old"}})
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_form", "index": 2}
    assert GenerationPlanner().build(*values).runtime.requests[1].body.parts == (
        "x=%2f+z&&flag&x=", RuntimeSlot("b", "v", "form"))


def test_bound_connection_nominated_header_cannot_disappear():
    values = fixture({"headers": [{"name": "Connection", "value": "X-Secret"},
                                  {"name": "X-Secret", "value": "old"}]})
    values[2]["parameters"]["bindings"][0]["location"] = {"area": "request_header", "index": 1}
    plan = GenerationPlanner().build(*values)
    assert any(d.code == "GENERATION_RUNTIME_HEADER_UNSUPPORTED" for d in plan.diagnostics)


def test_runtime_inspection_serializes_decimal_declarations_without_float_conversion():
    from decimal import Decimal

    from har2load.runtime_types import runtime_document
    from har2load.serialization import canonical_bytes

    values = fixture()
    values[2]["parameters"]["variables"].append({
        **deepcopy(values[2]["parameters"]["variables"][0]), "id": "number", "name": "number",
        "value_type": "number", "source": {"kind": "literal",
                                          "value": Decimal("9007199254740993.5")}})
    plan = GenerationPlanner().build(*values)
    assert b"9007199254740993.5" in canonical_bytes(runtime_document(plan.runtime))
