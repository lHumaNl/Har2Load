"""Synthetic evidence for the bounded, read-only scalar candidate profile."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from har2load.candidates import AnalysisLimits, CandidateAnalyzer
from har2load.domain import Capture, Grouping
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.serialization import canonical_bytes


def entry(response: Any = None, request: Any = None) -> dict[str, Any]:
    return {"request": {"method": "POST", "url": "https://example.invalid/",
                        **(request or {})},
            "response": {"status": 200, "content": {
                "mimeType": "application/json", "text": json.dumps(response)}}}


def capture(*entries: dict[str, Any]) -> Capture:
    return HarImporter().parse(json.dumps({"log": {"entries": entries}}).encode())


def analyze(source: Capture, choices: Any = None, *, limits: AnalysisLimits | None = None,
            grouping: Grouping | None = None) -> Any:
    return CandidateAnalyzer(limits=limits).analyze(
        source, grouping or StepGrouper().group(source),
        {"representation_selections": choices or []},
    )


def body(value: Any, mime: str = "application/json") -> dict[str, Any]:
    return {"postData": {"mimeType": mime, "text": json.dumps(value)}}


def candidate(result: Any, value: Any) -> Any:
    return next(item for item in result["candidates"]
                if item["value"] == value and type(item["value"]) is type(value))


def selection(source: Capture, index: int, location: str, representation: str) -> Any:
    return {"exchange_id": source.exchanges[index].id, "location": location,
            "representation": representation, "change_id": "synthetic-choice"}


def test_create_use_repeated_consumers_and_alternate_producers() -> None:
    source = capture(entry({"id": "token"}), entry({"other": "token"}),
                     entry(request=body(["token", "token"])))
    result = analyze(source)
    item = candidate(result, "token")
    assert item["knowledge_status"] == "heuristic_candidate"
    assert len(item["sources"]) == 2 and len(item["consumers"]) == 2
    assert item["ambiguity"] is True
    assert all(len(consumer["alternatives"]) == 2 for consumer in item["consumers"])
    assert result["profile"] == "scalar_candidates_v1"
    assert result["coverage"]["links"] == 4


def test_alternatives_are_per_consumer_and_exclude_late_sources() -> None:
    source = capture(entry("token"), entry("token", body("token")),
                     entry(request=body("token")), entry("token"))
    item = candidate(analyze(source), "token")
    assert [len(c["alternatives"]) for c in item["consumers"]] == [1, 2]
    assert len(item["sources"]) == 2
    assert source.exchanges[-1].id not in [s["exchange_id"] for s in item["sources"]]


@pytest.mark.parametrize("end,start,status", [(1, 2, "available_before_request"),
                                             (3, 2, "overlap"),
                                             (None, 2, "unknown"), (1, None, "unknown")])
def test_temporal_status_never_claims_causality(end: Any, start: Any, status: str) -> None:
    source = capture(entry("token"), entry(request=body("token")))
    source = replace(source, exchanges=(replace(source.exchanges[0], start_time=0, end_time=end),
                                       replace(source.exchanges[1], start_time=start)))
    item = candidate(analyze(source), "token")
    assert item["consumers"][0]["alternatives"][0]["temporal_status"] == status
    assert "Equality does not prove causality." in item["limitations"]


def test_grouping_order_not_raw_index_controls_eligibility() -> None:
    source = capture(entry(request=body("token")), entry("token"))
    grouping = replace(StepGrouper().group(source),
                       ordered_exchange_ids=tuple(e.id for e in reversed(source.exchanges)))
    assert candidate(analyze(source, grouping=grouping), "token")["consumers"]
    assert analyze(source)["candidates"] == []


def test_typed_values_numeric_equivalence_and_escaped_json_pointers() -> None:
    values = {"a/b": {"~id": [True, 1, None, "1", "", False, 0, "abc"]}}
    source = capture(entry(values), entry(request=body(values)))
    result = analyze(source)
    assert len(result["candidates"]) == 8
    assert all(item["priority"] == "low" for item in result["candidates"])
    item = candidate(result, Decimal(1))
    assert item["consumers"][0]["location"] == {
        "area": "request_json", "pointer": "/a~1b/~0id/1"}
    assert item["sources"][0]["location"]["decoded_body_pointer"] == "/a~1b/~0id/1"
    source = capture(entry(1), entry(request=body(1.0)))
    assert len(analyze(source)["candidates"]) == 1


def test_url_decoding_is_once_and_path_plus_is_literal() -> None:
    source = capture(entry(["/", "a b", "a+b", "%2F"]), entry(request={
        "url": "https://example.invalid/%2F/a+b/%252F?q=%2F&q=a+b&q=%252F"}))
    result = analyze(source)
    assert [c["area"] for c in candidate(result, "a+b")["consumers"]] == ["request_path"]
    assert [c["area"] for c in candidate(result, "a b")["consumers"]] == ["request_query"]
    assert len(candidate(result, "%2F")["consumers"]) == 2
    assert all(c["match_method"] == "url_decoded" for item in result["candidates"]
               for c in item["consumers"])


def test_query_conflict_is_local_and_explicit_choice_is_respected() -> None:
    source = capture(entry(["url-value", "selected-value"]), entry(request={
        "url": "https://example.invalid/url-value?q=url-value",
        "queryString": [{"name": "q", "value": "selected-value"}]}))
    result = analyze(source)
    assert result["coverage"]["conflicts"] == 1
    assert len(candidate(result, "url-value")["consumers"]) == 1
    choice = selection(source, 1, "/request/queryString", "queryString")
    selected = candidate(analyze(source, [choice]), "selected-value")
    assert selected["consumers"][0]["match_method"] == "exact"


def test_forms_are_separate_and_selected_params_preserve_duplicate_indices() -> None:
    source = capture(entry(["query", "form", "chosen"]), entry(request={
        "url": "https://example.invalid/?q=query", "postData": {
            "mimeType": "application/x-www-form-urlencoded", "text": "q=form",
            "params": [{"name": "q", "value": "chosen"}, {"name": "q", "value": "chosen"}]}}))
    assert analyze(source)["coverage"]["conflicts"] == 1
    choice = selection(source, 1, "/request/postData/params", "params")
    result = analyze(source, [choice])
    assert [c["location"]["index"] for c in candidate(result, "chosen")["consumers"]] == [0, 1]
    assert candidate(result, "query")["consumers"][0]["area"] == "request_query"


def test_response_mime_selection_uses_selected_view() -> None:
    producer = entry("token")
    producer["response"]["headers"] = [{"name": "Content-Type", "value": "image/png"}]
    source = capture(producer, entry(request=body("token")))
    assert analyze(source)["candidates"] == []
    choice = selection(source, 0, "/response/content/mimeType", "mimeType")
    item = candidate(analyze(source, [choice]), "token")
    assert item["sources"][0]["source_view"] == "scenario_selected"


def test_header_cookie_duplicates_and_whole_text_only() -> None:
    producer = entry()
    producer["response"] = {"status": 200, "headers": [{"name": "X", "value": "token"}] * 2,
                            "cookies": [{"name": "cookie", "value": "token"}],
                            "content": {"mimeType": "text/plain", "text": "token"}}
    source = capture(producer, entry(request={"headers": [{"name": "X", "value": "token"}],
                     "postData": {"mimeType": "text/plain", "text": "token"}}))
    item = candidate(analyze(source), "token")
    assert len(item["sources"]) == 4
    assert len({s["id"] for s in item["sources"]}) == 4
    assert len(item["consumers"]) == 2
    assert item["consumers"][1]["location"] == {"area": "request_text", "start": 0, "end": 5}


@pytest.mark.parametrize("url", ["https://example.invalid/?x=%GG", "https://example.invalid/%ff",
                                 "https://example.invalid:bad/", "not-a-url"])
def test_malformed_urls_are_diagnosed_without_values(url: str) -> None:
    result = analyze(capture(entry("secret"), entry(request={"url": url})))
    assert result["coverage"]["invalid"] > 0
    assert "secret" not in json.dumps(result["diagnostics"])


def test_no_mutation_and_deterministic_identifiers() -> None:
    source = capture(entry("token"), entry(request=body("token")))
    before = deepcopy(source)
    first, second = analyze(source), analyze(source)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert source == before


@pytest.mark.parametrize("kwargs", [{"max_values": 0}, {"max_links": True},
                                    {"max_candidates": -1}, {"max_text_bytes": 10**20}])
def test_limits_require_bounded_positive_integers(kwargs: Any) -> None:
    with pytest.raises(ValueError, match="limits"):
        AnalysisLimits(**kwargs)


@pytest.mark.parametrize("kwargs", [{"max_values": 4}, {"max_links": 1},
                                    {"max_candidates": 1}, {"max_body_nodes": 2},
                                    {"max_indexed_text_bytes": 8}, {"max_payload_bytes": 64}])
def test_budget_exhaustion_is_explicit_partial(kwargs: Any) -> None:
    source = capture(entry(["token", "other"]), entry(request=body(["token", "other", "token"])))
    result = analyze(source, limits=AnalysisLimits(**kwargs))
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["truncated"] > 0
    assert result["coverage"]["unknown_remainder"] is True
    assert any(d["code"] == "CANDIDATE_LIMIT" for d in result["diagnostics"])


def test_huge_decimal_is_exact_without_decimal_context_rounding() -> None:
    producer = entry()
    producer["response"]["content"]["text"] = "1e999"
    consumer = entry(request={"postData": {"mimeType": "application/json", "text": "1e999"}})
    result = analyze(capture(producer, consumer))
    assert candidate(result, Decimal("1e999"))["value_type"] == "integer"


def test_limits_do_not_publish_empty_candidates_or_incomplete_alternatives() -> None:
    source = capture(entry("token"), entry("token"), entry(request=body("token")))
    result = analyze(source, limits=AnalysisLimits(max_links=1))
    assert result["coverage"]["status"] == "partial"
    assert result["candidates"] == []


def test_selected_mime_does_not_match_shadowed_content_type_header() -> None:
    source = capture(entry("text/plain"), entry(request={
        "headers": [{"name": "Content-Type", "value": "text/plain"}],
        "postData": {"mimeType": "application/json", "text": '"other"'}}))
    choice = selection(source, 1, "/request/postData/mimeType", "mimeType")
    assert analyze(source, [choice])["candidates"] == []


@pytest.mark.parametrize("content,category", [({}, "unavailable"),
    ({"text": "{bad", "mimeType": "application/json"}, "invalid"),
    ({"text": "secret", "mimeType": "image/png"}, "unsupported"),
    ({"text": "secret", "mimeType": "text/plain", "encoding": "alien"}, "unsupported")])
def test_unavailable_invalid_and_nontext_bodies_are_counted(content: Any, category: str) -> None:
    producer = entry()
    producer["response"]["content"] = content
    result = analyze(capture(producer))
    assert result["coverage"][category] > 0
    assert result["coverage"]["status"] == "partial"
    assert "secret" not in json.dumps(result["diagnostics"])


def test_vendor_extensions_are_not_traversed_and_json_depth_is_bounded() -> None:
    source = capture(entry("token"), entry(request=body("token")))
    raw = source.exchanges[0].raw
    assert isinstance(raw, dict)
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    raw["vendor"] = cyclic
    raw["response"]["content"]["vendor"] = cyclic  # type: ignore[index]
    assert candidate(analyze(source), "token")["consumers"]
    deep = entry()
    deep["response"]["content"]["text"] = "[" * 65 + "0" + "]" * 65
    result = analyze(capture(deep))
    assert result["coverage"]["truncated"] > 0


def test_unknown_pair_shapes_and_duplicate_budgets_are_not_silent() -> None:
    producer = entry()
    producer["response"]["headers"] = [None, {"name": "X"}, {"name": "X", "value": "token"}]
    producer["response"]["cookies"] = {"unexpected": "secret"}
    result = analyze(capture(producer, entry(request=body("token"))))
    assert result["coverage"]["invalid"] == 3
    assert candidate(result, "token")["sources"][0]["location"]["pointer"].endswith("/2/value")
    producer["response"]["headers"] = [None] * 1000
    bounded = analyze(capture(producer), limits=AnalysisLimits(max_values=100))
    assert bounded["coverage"]["values"] <= 100
    assert bounded["coverage"]["truncated"] > 0


def test_escaped_pointer_payload_budget_prevents_output_multiplication() -> None:
    value = {"x" * 100: ["token"] * 30}
    source = capture(entry(value), entry(request=body(["token"] * 30)))
    result = analyze(source, limits=AnalysisLimits(max_payload_bytes=5000))
    assert result["coverage"]["truncated"] > 0
    assert len(canonical_bytes(result["candidates"])) <= 5000


def test_form_decoding_and_empty_text_keep_values_without_substring_mining() -> None:
    source = capture(entry(["a b", "%2F", "", "token"]), entry(request={
        "postData": {"mimeType": "application/x-www-form-urlencoded", "text": "x=a+b&x=%252F&x="}}),
        entry(request={"postData": {"mimeType": "text/plain", "text": "prefix-token"}}))
    result = analyze(source)
    assert candidate(result, "a b")["consumers"][0]["match_method"] == "url_decoded"
    assert candidate(result, "%2F")["consumers"][0]["location"]["index"] == 1
    assert candidate(result, "")["priority"] == "low"
    assert not any(item["value"] == "token" for item in result["candidates"])


def test_analysis_has_no_filesystem_environment_network_or_runtime_access(monkeypatch: Any) -> None:
    import builtins
    import os
    import socket
    import subprocess

    source = capture(entry("__import__('os').system('false')"),
                     entry(request=body("__import__('os').system('false')")))
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Forbidden side effect")
    with monkeypatch.context() as patch:
        for target, name in [(builtins, "open"), (builtins, "eval"), (builtins, "exec"),
                             (os, "getenv"), (socket, "socket"), (subprocess, "Popen")]:
            patch.setattr(target, name, forbidden)
        assert analyze(source)["candidates"]


def test_common_long_values_are_retained_with_low_priority() -> None:
    source = capture(entry("common-value"), entry(request=body(["common-value"] * 10)))
    item = candidate(analyze(source), "common-value")
    assert item["priority"] == "low"
    assert len(item["consumers"]) == 10


def test_complete_means_profile_coverage_not_causal_certainty() -> None:
    source = capture(entry("token", body({})), entry({}, body("token")))
    result = analyze(source)
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["limitations"]
    assert candidate(result, "token")["consumers"][0]["alternatives"][0][
        "temporal_status"] == "unknown"


def test_identifier_depends_on_locator_not_scalar_value_or_decimal_context() -> None:
    source = capture(entry("token"), entry(request=body("token")))
    first = candidate(analyze(source), "token")
    producer, consumer = source.exchanges
    changed = replace(source, exchanges=(
        replace(producer, response_body=replace(producer.response_body, text='"other"')),
        replace(consumer, request_body=replace(consumer.request_body, text='"other"'))))
    second = candidate(analyze(changed), "other")
    assert first["id"] == second["id"]
    assert first["sources"][0]["id"] == second["sources"][0]["id"]


def test_selected_views_ignore_cyclic_vendor_extensions_without_mutation() -> None:
    source = capture(entry("token"), entry(request=body("token")))
    raw: Any = source.exchanges[0].raw
    raw["response"]["content"]["vendor"] = raw
    choices = [selection(source, 0, "/response/content/mimeType", "mimeType")]
    before = deepcopy(choices)
    assert candidate(analyze(source, choices), "token")["consumers"]
    assert choices == before
    assert raw["response"]["content"]["vendor"] is raw


def test_per_text_limit_is_reported_without_leaking_oversized_value() -> None:
    source = capture(entry("private-value" * 100))
    result = analyze(source, limits=AnalysisLimits(max_text_bytes=100))
    assert result["coverage"]["truncated"] == 1
    assert "private-value" not in json.dumps(result["diagnostics"])


@pytest.mark.parametrize("maximum", [100, 500, 1000, 2000, 4000, 8000])
def test_payload_limits_preserve_consistent_published_counts(maximum: int) -> None:
    source = capture(entry(["token", "other"]), entry(request=body(["token", "other"] * 3)))
    result = analyze(source, limits=AnalysisLimits(max_payload_bytes=maximum))
    assert result["coverage"]["candidates"] == len(result["candidates"])
    assert result["coverage"]["links"] == sum(
        len(consumer["alternatives"]) for item in result["candidates"]
        for consumer in item["consumers"])
    assert len(canonical_bytes(result["candidates"])) <= maximum


@pytest.mark.parametrize("url", ["https://example.invalid/%FF?q=other", "not-a-url"])
def test_selected_query_is_independent_of_invalid_discarded_url_or_path(url: str) -> None:
    source = capture(entry("token"), entry(request={
        "url": url, "queryString": [{"name": "q", "value": "token"}],
    }))
    choice = selection(source, 1, "/request/queryString", "queryString")
    result = analyze(source, [choice])
    assert result["coverage"]["invalid"] > 0
    item = candidate(result, "token")
    assert item["consumers"][0]["location"] == {"area": "request_query", "index": 0}
    assert item["consumers"][0]["match_method"] == "exact"


def test_url_query_is_independent_of_invalid_path_encoding() -> None:
    source = capture(entry("token"), entry(request={"url": "https://example.invalid/%FF?q=token"}))
    result = analyze(source)
    assert result["coverage"]["invalid"] > 0
    assert candidate(result, "token")["consumers"][0]["match_method"] == "url_decoded"
