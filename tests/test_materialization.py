import json
from copy import deepcopy
from decimal import Decimal

import pytest

from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.materialization import RequestMaterializer
from har2load.scenario import ScenarioEditor


def fixture(request, response=None):
    source = HarImporter().parse(json.dumps({"log": {"entries": [{
        "request": {"method": "GET", "url": "https://example.invalid/a?keep=%2f+z", **request},
        "response": response or {"status": 200,
                                 "content": {"mimeType": "text/plain", "text": "ok"}},
    }]}}).encode())
    grouping = StepGrouper().group(source)
    return source, grouping, ScenarioEditor().initial(source, grouping)


def literal(model, source, area, value, value_type="string", **location):
    model["parameters"]["variables"].append({"id": "v", "name": "value", "value_type": value_type,
        "source": {"kind": "literal", "value": value},
        "initialize": {"event": "iteration_start"}, "scope": "iteration",
        "reset": "iteration_start",
        "purpose": "Synthetic constant", "sensitive": False, "change_id": "fixture"})
    model["parameters"]["bindings"].append({"id": "b", "variable_id": "v",
        "exchange_id": source.exchanges[0].id, "location": {"area": area, **location},
        "change_id": "fixture"})


def build(values):
    return RequestMaterializer().build(*values)


def test_static_wire_values_and_raw_bytes_are_preserved():
    values = fixture({"method": "POST", "postData": {"mimeType": "text/plain",
                                                       "text": 'line\r\n雪 ${untrusted} {x} \\'},
                      "headers": [{"name": "X-Dup", "value": "one"},
                                  {"name": "X-Dup", "value": "two"}]})
    source, _, model = values
    before = deepcopy(model)
    plan = build(values)
    assert not any(d.severity == "error" for d in plan.diagnostics)
    request = plan.requests[0]
    assert request.url.endswith("/a?keep=%2f+z")
    assert request.body == 'line\r\n雪 ${untrusted} {x} \\'.encode()
    assert request.headers[:2] == (("X-Dup", "one"), ("X-Dup", "two"))
    assert ("Content-Type", "text/plain") in request.headers
    assert source.raw_bytes and model == before


def test_query_binding_changes_only_selected_occurrence_not_other_encodings():
    values = fixture({"url": "https://example.invalid/a?x=%2f&x=old&&flag&tail=a+b"})
    literal(values[2], values[0], "request_query", "雪/+", index=1)
    request = build(values).requests[0]
    assert request.url == "https://example.invalid/a?x=%2f&x=%E9%9B%AA%2F%2B&&flag&tail=a+b"


def test_path_literal_is_one_encoded_segment():
    values = fixture({"url": "https://example.invalid/a//c%2f/?q=+"})
    literal(values[2], values[0], "request_path", "x/y?z", index=1)
    assert build(values).requests[0].url == "https://example.invalid/a/x%2Fy%3Fz/c%2f/?q=+"


def test_selected_query_and_params_forms_remain_separate():
    values = fixture({"method": "POST", "queryString": [{"name": "q", "value": "chosen"}],
                      "postData": {"mimeType": "application/x-www-form-urlencoded",
                                   "text": "wrong=raw", "params": [
                                       {"name": "p", "value": ""}, {"name": "p", "value": "a+b"}]}})
    source, _, model = values
    for location, representation in (("/request/queryString", "queryString"),
                                     ("/request/postData/params", "params")):
        model["representation_selections"].append({"exchange_id": source.exchanges[0].id,
            "location": location, "representation": representation, "change_id": "fixture"})
    request = build(values).requests[0]
    assert request.url.endswith("?q=chosen")
    assert request.body == b"p=&p=a%2Bb"


def test_json_literal_binding_remains_numeric_and_does_not_replace_equal_strings():
    values = fixture({"method": "POST", "postData": {
        "mimeType": "application/json", "text": '{"a/b":7,"other":7,"string":"7"}'}})
    literal(values[2], values[0], "request_json", Decimal("1000000000000000000.5"),
            "number", pointer="/a~1b")
    result = json.loads(build(values).requests[0].body, parse_float=Decimal)
    assert result == {"a/b": Decimal("1000000000000000000.5"), "other": 7, "string": "7"}


def test_managed_headers_are_omitted_with_explicit_decisions():
    values = fixture({"headers": [{"name": "Host", "value": "captured.invalid"},
                                   {"name": "Connection", "value": "X-Remove"},
                                   {"name": "X-Remove", "value": "managed"},
                                   {"name": "X-Keep", "value": "yes"}]})
    plan = build(values)
    assert plan.requests[0].headers == (("X-Keep", "yes"),)
    assert len([d for d in plan.decisions if d["kind"] == "managed_header_omitted"]) == 3
    literal(values[2], values[0], "request_header", "different", index=0)
    assert any(d.code == "GENERATION_MANAGED_HEADER_BINDING" for d in build(values).diagnostics)


@pytest.mark.parametrize("raw_request,code", [
    ({"method": "CONNECT"}, "GENERATION_METHOD_UNSUPPORTED"),
    ({"method": "POST"}, "GENERATION_BODY_UNAVAILABLE"),
    ({"cookies": [{"name": "s", "value": "secret"}]}, "GENERATION_SESSION_UNSUPPORTED"),
    ({"headers": [{"name": "Authorization", "value": "secret"}]}, "GENERATION_SESSION_UNSUPPORTED"),
    ({"headers": [{"name": "X", "value": "line\r\ninjected"}]}, "GENERATION_HEADER_INVALID"),
    ({"url": "https://user:secret@example.invalid/"}, "GENERATION_URL_UNSUPPORTED"),
    ({"headers": [{"name": "Content-Encoding", "value": "gzip"}]}, "GENERATION_BODY_UNSUPPORTED"),
    ({"method": "POST", "postData": {"mimeType": "multipart/form-data", "text": "data"}},
     "GENERATION_BODY_UNSUPPORTED"),
])
def test_invalid_or_unsupported_request_is_not_silently_repaired(raw_request, code):
    plan = build(fixture(raw_request))
    assert any(d.code == code and d.severity == "error" for d in plan.diagnostics)
    assert all("secret" not in d.message and "injected" not in d.message for d in plan.diagnostics)


def test_explicit_zero_size_post_is_empty_not_unknown():
    request = build(fixture({"method": "POST", "bodySize": 0})).requests[0]
    assert request.body is None


def test_runtime_variables_and_unreferenced_extractors_are_blocked_not_ignored():
    values = fixture({})
    literal(values[2], values[0], "request_query", "x", index=0)
    values[2]["parameters"]["variables"][0]["source"] = {"kind": "generated", "generator": "uuid"}
    assert any(d.code == "GENERATION_RUNTIME_UNSUPPORTED" for d in build(values).diagnostics)
    values = fixture({})
    values[2]["parameters"]["extractors"] = [{"id": "unused"}]
    assert any(d.code == "GENERATION_RUNTIME_UNSUPPORTED" for d in build(values).diagnostics)


def test_response_conflict_does_not_block_request_only_generation():
    values = fixture({}, {"status": 200,
                          "headers": [{"name": "Content-Type", "value": "image/png"}],
                          "content": {"mimeType": "text/plain", "text": "body"}})
    assert not any(d.severity == "error" for d in build(values).diagnostics)


def test_request_mime_choice_controls_body_and_final_content_type():
    values = fixture({"method": "POST", "headers": [{"name": "Content-Type", "value": "image/png"}],
                      "postData": {"mimeType": "application/json", "text": '{"x":1}'}})
    assert any(d.severity == "error" for d in build(values).diagnostics)
    values[2]["representation_selections"] = [{"exchange_id": values[0].exchanges[0].id,
        "location": "/request/postData/mimeType", "representation": "mimeType", "change_id": "f"}]
    request = build(values).requests[0]
    assert request.headers == (("Content-Type", "application/json"),)
    assert request.body == b'{"x":1}'


def test_selected_query_does_not_validate_discarded_query_encoding():
    values = fixture({"url": "https://example.invalid/a?bad=%GG",
                      "queryString": [{"name": "q", "value": "chosen"}]})
    values[2]["representation_selections"] = [{"exchange_id": values[0].exchanges[0].id,
        "location": "/request/queryString", "representation": "queryString", "change_id": "f"}]
    plan = build(values)
    assert not any(d.severity == "error" for d in plan.diagnostics)
    assert plan.requests[0].url == "https://example.invalid/a?q=chosen"


def test_repeated_literal_headers_respect_materialized_header_budget(monkeypatch):
    import har2load.materialization as materialization

    values = fixture({"headers": [{"name": f"X-{i}", "value": "old"} for i in range(6)]})
    literal(values[2], values[0], "request_header", "x" * 200, index=0)
    template = values[2]["parameters"]["bindings"][0]
    for index in range(1, 6):
        values[2]["parameters"]["bindings"].append({
            **template, "id": f"b{index}", "location": {"area": "request_header", "index": index}})
    monkeypatch.setattr(materialization, "MAX_TEXT_BYTES", 1000)
    assert any(d.code == "GENERATION_LIMIT" for d in build(values).diagnostics)


def test_decision_budget_prevents_omission_metadata_amplification(monkeypatch):
    import har2load.materialization as materialization

    monkeypatch.setattr(materialization, "MAX_DECISIONS", 2, raising=False)
    values = fixture({"headers": [{"name": "Host", "value": "example.invalid"}] * 3})
    plan = build(values)
    assert any(d.code == "GENERATION_LIMIT" for d in plan.diagnostics)
    assert len(plan.decisions) <= 2


def test_explicit_url_selection_preserves_empty_query_delimiter():
    values = fixture({"url": "https://example.invalid/a?",
                      "queryString": [{"name": "discard", "value": "1"}]})
    values[2]["representation_selections"] = [{"exchange_id": values[0].exchanges[0].id,
        "location": "/request/queryString", "representation": "url", "change_id": "f"}]
    assert build(values).requests[0].url == "https://example.invalid/a?"


def test_connection_nomination_cannot_hide_unsupported_content_encoding():
    values = fixture({"method": "POST", "headers": [
        {"name": "Connection", "value": "Content-Encoding"},
        {"name": "Content-Encoding", "value": "gzip"}],
        "postData": {"mimeType": "text/plain", "text": "payload"}})
    assert any(d.code == "GENERATION_BODY_UNSUPPORTED" for d in build(values).diagnostics)


def test_text_budget_rejects_before_expanding_all_insertions(monkeypatch):
    import har2load.materialization as module

    values = fixture({"method": "POST", "postData": {"mimeType": "text/plain", "text": "x" * 100}})
    literal(values[2], values[0], "request_text", "雪" * 200, start=0, end=0)
    first = values[2]["parameters"]["bindings"][0]
    for index in range(1, 100):
        values[2]["parameters"]["bindings"].append({**first, "id": f"b{index}",
            "location": {"area": "request_text", "start": index, "end": index}})
    original = module._Request.value
    visited = 0

    def counted(self, binding):
        nonlocal visited
        visited += 1
        return original(self, binding)

    monkeypatch.setattr(module, "MAX_TEXT_BYTES", 1000)
    monkeypatch.setattr(module._Request, "value", counted)
    assert any(d.code == "GENERATION_LIMIT" for d in build(values).diagnostics)
    assert visited < 5


def test_json_budget_rejects_before_serializing_expanded_document(monkeypatch):
    import har2load.materialization as module

    values = fixture({"method": "POST", "postData": {"mimeType": "application/json",
        "text": json.dumps({str(i): "" for i in range(40)})}})
    literal(values[2], values[0], "request_json", "x" * 100, pointer="/0")
    first = values[2]["parameters"]["bindings"][0]
    for index in range(1, 40):
        values[2]["parameters"]["bindings"].append({**first, "id": f"b{index}",
            "location": {"area": "request_json", "pointer": f"/{index}"}})
    original = module.canonical_bytes
    whole_documents = 0

    def counted(value):
        nonlocal whole_documents
        whole_documents += isinstance(value, dict)
        return original(value)

    monkeypatch.setattr(module, "MAX_TEXT_BYTES", 1000)
    monkeypatch.setattr(module, "canonical_bytes", counted)
    assert any(d.code == "GENERATION_LIMIT" for d in build(values).diagnostics)
    assert whole_documents == 0


@pytest.mark.parametrize("area", ["request_query", "request_form", "request_path"])
def test_encoded_replacements_stop_at_budget_before_joining_expanded_values(monkeypatch, area):
    import har2load.materialization as module

    encoded = "&".join(f"x{i}=x" for i in range(20))
    raw = ({"url": "https://example.invalid/?" + encoded} if area == "request_query" else
           {"url": "https://example.invalid/" + "/".join(["x"] * 20)} if area == "request_path"
           else {"method": "POST", "postData": {
               "mimeType": "application/x-www-form-urlencoded", "text": encoded}})
    values = fixture(raw)
    literal(values[2], values[0], area, "x" * 200, index=0)
    first = values[2]["parameters"]["bindings"][0]
    for index in range(1, 20):
        values[2]["parameters"]["bindings"].append({**first, "id": f"b{index}",
            "location": {"area": area, "index": index}})
    name = "quote" if area == "request_path" else "quote_plus"
    original, calls = getattr(module, name), 0

    def counted(value, **kwargs):
        nonlocal calls
        calls += 1
        return original(value, **kwargs)

    monkeypatch.setattr(module, "MAX_TEXT_BYTES", 1000)
    monkeypatch.setattr(module, name, counted)
    assert any(d.code == "GENERATION_LIMIT" for d in build(values).diagnostics)
    assert calls < 10
