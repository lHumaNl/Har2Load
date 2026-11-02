import json

import pytest

from har2load.binding_locations import BindingLocations, SourceViews
from har2load.domain import Har2LoadError
from har2load.importer import HarImporter


def capture(request):
    return HarImporter().parse(json.dumps({"log": {"entries": [{
        "request": request, "response": {"status": 200, "content": {"text": "ok"}},
    }]}}).encode())


def parameters(source, area, *, value_type="string", **location):
    return {"variables": [{"id": "var", "value_type": value_type}], "bindings": [{
        "id": "binding", "variable_id": "var", "exchange_id": source.exchanges[0].id,
        "location": {"area": area, **location},
    }]}


def test_json_binding_preserves_scalar_types_and_escaped_locations():
    source = capture({"method": "POST", "url": "https://example.invalid/",
                      "postData": {"mimeType": "application/json", "text": '{"a/b":{"~id":7}}'}})
    model = parameters(source, "request_json", value_type="integer", pointer="/a~1b/~0id")
    before = source.raw_bytes
    BindingLocations().validate(model, source, [])
    model["variables"][0]["value_type"] = "string"
    with pytest.raises(Har2LoadError):
        BindingLocations().validate(model, source, [])
    assert source.raw_bytes == before


def test_query_conflict_requires_selection_and_uses_selected_occurrence():
    source = capture({"method": "GET", "url": "https://example.invalid/?x=one&x=two",
                      "queryString": [{"name": "x", "value": "different"}]})
    model = parameters(source, "request_query", index=1)
    with pytest.raises(Har2LoadError) as caught:
        BindingLocations().validate(model, source, [])
    assert caught.value.code == "BINDING_REPRESENTATION_CONFLICT"
    selection = {"exchange_id": source.exchanges[0].id, "location": "/request/queryString",
                 "representation": "url", "change_id": "choose"}
    BindingLocations().validate(model, source, [selection])
    selection["representation"] = "queryString"
    with pytest.raises(Har2LoadError):
        BindingLocations().validate(model, source, [selection])


def test_form_occurrence_is_not_query_and_honors_params_choice():
    source = capture({"method": "POST", "url": "https://example.invalid/?query=one",
                      "postData": {"mimeType": "application/x-www-form-urlencoded",
                                   "text": "x=one", "params": [
                                       {"name": "x", "value": ""}, {"name": "x", "value": "two"},
                                   ]}})
    model = parameters(source, "request_form", index=1)
    choice = {"exchange_id": source.exchanges[0].id, "location": "/request/postData/params",
              "representation": "params", "change_id": "choose"}
    BindingLocations().validate(model, source, [choice])
    with pytest.raises(Har2LoadError):
        BindingLocations().validate(model, source, [])
    choice["representation"] = "text"
    with pytest.raises(Har2LoadError):
        BindingLocations().validate(model, source, [choice])


@pytest.mark.parametrize("area,location", [
    ("request_header", {"index": 1}), ("request_path", {"index": 1}),
    ("request_text", {"start": 1, "end": 3}),
])
def test_exact_header_path_and_text_targets(area, location):
    source = capture({"method": "POST", "url": "https://example.invalid/a/b",
                      "headers": [{"name": "X-ID", "value": "first"},
                                  {"name": "X-ID", "value": "second"}],
                      "postData": {"mimeType": "text/plain", "text": "abcde"}})
    BindingLocations().validate(parameters(source, area, **location), source, [])


@pytest.mark.parametrize("left,right", [
    ({"area": "request_text", "start": 0, "end": 3},
     {"area": "request_text", "start": 2, "end": 5}),
    ({"area": "request_header", "index": 0}, {"area": "request_header", "index": 0}),
])
def test_overlapping_destinations_are_rejected(left, right):
    source = capture({"method": "POST", "url": "https://example.invalid/",
                      "headers": [{"name": "X", "value": "v"}],
                      "postData": {"mimeType": "text/plain", "text": "abcde"}})
    model = parameters(source, **left)
    model["bindings"].append({**model["bindings"][0], "id": "another", "location": right})
    with pytest.raises(Har2LoadError) as caught:
        BindingLocations().validate(model, source, [])
    assert caught.value.code == "BINDING_OVERLAP"


def test_response_mime_choice_provides_derived_view_without_rewriting_source():
    raw = {"request": {"method": "GET", "url": "https://example.invalid/"},
           "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "image/png"}],
                        "content": {"mimeType": "application/json", "text": '{"id":7}'}}}
    source = HarImporter().parse(json.dumps({"log": {"entries": [raw]}}).encode())
    assert source.exchanges[0].response_body.text is None
    views = SourceViews(source, [])
    assert views.for_extraction().exchanges[0].response_body.text is None
    choice = {"exchange_id": source.exchanges[0].id, "location": "/response/content/mimeType",
              "representation": "mimeType", "change_id": "choose"}
    derived = SourceViews(source, [choice]).for_extraction()
    assert derived.exchanges[0].response_body.text == '{"id":7}'
    assert derived.exchanges[0].id == source.exchanges[0].id
    assert derived.raw_bytes == source.raw_bytes
    assert derived.exchanges[0].raw == source.exchanges[0].raw
    assert source.exchanges[0].response_body.text is None
