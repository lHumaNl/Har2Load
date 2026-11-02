"""Synthetic structural checks only: these tests never load or execute JMeter."""

import socket
import subprocess
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import replace

import pytest

from har2load.domain import Diagnostic, JSONValue
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan, TargetBuild
from har2load.jmeter import JMeterGenerator

ARTIFACT = "jmeter/scenario.jmx"


def plan(request: NativeRequest | None = None, name: str = "Checkout") -> RequestPlan:
    request = request or NativeRequest("r1", "g1", "GET", "https://example.invalid/a%2Fb?q=+%2F")
    return RequestPlan((request,), (NativeGroup("g1", name, (request.id,)),))


def tree(build: TargetBuild) -> ET.Element:
    assert build.status == "generated", build.diagnostics
    assert set(build.files) == {ARTIFACT}
    return ET.fromstring(build.files[ARTIFACT])


def prop(element: ET.Element, name: str) -> str:
    item = element.find(f".//*[@name='{name}']")
    assert item is not None, name
    return item.text or ""


def blocked(build: TargetBuild, code: str) -> None:
    assert build.status == "blocked"
    assert build.files == {} and build.source_map == ()
    assert any(d.code == code and d.target == "jmeter" for d in build.diagnostics)


@pytest.mark.parametrize("candidate", [RequestPlan((), ()), RequestPlan(
    (), (NativeGroup("g1", "Empty", ()),)), RequestPlan(
    plan().requests, (NativeGroup("g1", "Missing", ("unknown",)),)), RequestPlan(
    plan().requests, (NativeGroup("g1", "Duplicate", ("r1", "r1")),)),
])
def test_invalid_topology_is_blocked(candidate: RequestPlan) -> None:
    blocked(JMeterGenerator().generate(candidate), "JMETER_PLAN_INVALID")


def test_common_errors_produce_no_artifact_and_are_target_scoped() -> None:
    diagnostic = Diagnostic("COMMON_BLOCK", "error", "Unsupported declaration.", "r1")
    result = JMeterGenerator().generate(replace(plan(), diagnostics=(diagnostic,)))
    blocked(result, "COMMON_BLOCK")
    assert diagnostic.target is None


def test_every_test_element_is_followed_by_its_hash_tree() -> None:
    root = tree(JMeterGenerator().generate(plan()))
    assert root.tag == "jmeterTestPlan" and root.attrib["jmeter"] == "5.5"
    for branch in root.iter("hashTree"):
        children = list(branch)
        assert len(children) % 2 == 0
        for element, subtree in zip(children[::2], children[1::2], strict=True):
            assert element.tag != "hashTree" and subtree.tag == "hashTree"
            assert element.attrib["enabled"] == "true"


def test_root_debug_profile_has_no_implicit_requests_or_disabled_nodes() -> None:
    root = tree(JMeterGenerator().generate(plan()))
    children = root.findall("./hashTree/hashTree/*")
    assert [e.tag for e in children if e.tag != "hashTree"] == [
        "CookieManager", "ThreadGroup", "TestFragmentController", "ResultCollector"]
    assert root.find("./hashTree/TestPlan").attrib["testname"] == "Har2Load"  # type: ignore[union-attr]
    assert len(root.findall(".//HTTPSamplerProxy")) == 1
    assert prop(root, "CookieManager.clearEachIteration") == "true"
    assert prop(root, "ThreadGroup.num_threads") == "1"
    assert prop(root, "ThreadGroup.ramp_time") == "0"
    assert prop(root, "LoopController.loops") == "1"
    assert prop(root, "ThreadGroup.on_sample_error") == "continue"
    assert not any("Timer" in e.tag or e.tag in {
        "CacheManager", "BackendListener", "JSR223Sampler"} for e in root.iter())


def test_debug_variables_only_and_results_tree_enabled() -> None:
    root = tree(JMeterGenerator().generate(plan()))
    assert prop(root, "displayJMeterVariables") == "true"
    assert prop(root, "displayJMeterProperties") == "false"
    assert prop(root, "displaySystemProperties") == "false"
    collector = root.find(".//ResultCollector")
    assert collector is not None
    assert collector.attrib["guiclass"] == "ViewResultsFullVisualizer"
    assert collector.attrib["enabled"] == "true"


@pytest.mark.parametrize("label", ['Quotes " & <tag> \'', "renamed", "x" * 300])
def test_module_paths_use_final_names_and_maps_resolve(label: str) -> None:
    result = JMeterGenerator().generate(plan(name=label))
    root = tree(result)
    fragment = root.find(".//TestFragmentController")
    assert fragment is not None
    path = root.findall(".//collectionProp[@name='ModuleController.node_path']/stringProp")
    assert [e.text for e in path] == ["Test Plan", "Har2Load", fragment.attrib["testname"]]
    assert len(fragment.attrib["testname"]) < 160
    assert {row["kind"] for row in result.source_map} >= {"group", "transaction", "request"}
    for row in result.source_map:
        resolve_mapping(root, row)


def resolve_mapping(root: ET.Element, row: dict[str, JSONValue]) -> None:
    assert row["artifact"] == ARTIFACT
    locator = row["locator"]
    assert isinstance(locator, dict) and isinstance(locator["component_path"], list)
    branch = root.find("hashTree")
    for name in locator["component_path"]:
        assert branch is not None
        pairs = list(branch)
        matches = [i for i in range(0, len(pairs), 2) if pairs[i].get("testname") == name]
        assert len(matches) == 1
        element, branch = pairs[matches[0]:matches[0] + 2]
    assert prop(element, "TestPlan.comments") == row["entity_id"]


def test_unicode_labels_and_markup_are_data_not_xml() -> None:
    root = tree(JMeterGenerator().generate(plan(name='<tag> & "café"')))
    fragment = root.find(".//TestFragmentController")
    assert fragment is not None and '<tag> & "café"' in fragment.attrib["testname"]
    assert root.find(".//tag") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("body", ['{"x":"café & %2F +"}\r\n', "a=%2F&b=+&a=", 'é\r\n\r\n\n"&<>'])
def test_body_is_one_raw_unnamed_argument_with_exact_utf8(method: str, body: str) -> None:
    request = replace(plan().requests[0], method=method, body=body.encode(), body_kind="text",
                      headers=(("Content-Type", "text/plain"),))
    root = tree(JMeterGenerator().generate(plan(request)))
    arguments = root.findall(".//elementProp[@elementType='HTTPArgument']")
    assert len(arguments) == 1
    assert prop(arguments[0], "Argument.name") == ""
    assert prop(arguments[0], "Argument.value").encode() == request.body
    assert prop(arguments[0], "HTTPArgument.always_encode") == "false"
    assert prop(root, "HTTPSampler.postBodyRaw") == "true"
    assert prop(root, "HTTPSampler.contentEncoding") == "UTF-8"


def test_url_headers_and_sampler_policy_are_explicit() -> None:
    request = replace(plan().requests[0], url="https://example.invalid:8443/a%2Fb?q=+%2F",
                      headers=(("X-Repeat", "one"), ("x-repeat", "two")))
    root = tree(JMeterGenerator().generate(plan(request)))
    assert prop(root, "HTTPSampler.path") == "/a%2Fb?q=+%2F"
    assert prop(root, "HTTPSampler.domain") == "example.invalid"
    assert prop(root, "HTTPSampler.port") == "8443"
    assert prop(root, "HTTPSampler.protocol") == "https"
    assert prop(root, "HTTPSampler.method") == "GET"
    assert prop(root, "HTTPSampler.implementation") == "HttpClient4"
    assert [(prop(h, "Header.name"), prop(h, "Header.value")) for h in
            root.findall(".//elementProp[@elementType='Header']")] == list(request.headers)
    for name in ("HTTPSampler.follow_redirects", "HTTPSampler.auto_redirects",
                 "HTTPSampler.image_parser"):
        assert prop(root, name) == "false"


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "TRACE"])
def test_unverified_method_body_semantics_block_entire_target(method: str) -> None:
    request = replace(plan().requests[0], method=method, body=b"body", body_kind="text")
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_BODY_METHOD_UNSUPPORTED")


@pytest.mark.parametrize("field", ["url", "body", "header_name", "header_value", "group"])
def test_jmeter_metasyntax_is_rejected_without_echoing_values(field: str) -> None:
    expression = "${__groovy(private_marker)}"
    request = plan().requests[0]
    overrides = {"url": {"url": "https://example.invalid/" + expression},
                 "body": {"method": "POST", "body": expression.encode()},
                 "header_name": {"headers": ((expression, "v"),)},
                 "header_value": {"headers": (("X-Data", expression),)}}
    request = replace(request, **overrides.get(field, {}))
    result = JMeterGenerator().generate(plan(request, expression if field == "group" else "Safe"))
    blocked(result, "JMETER_METASYNTAX_UNSUPPORTED")
    assert "private_marker" not in repr(result.diagnostics)


@pytest.mark.parametrize("body", [b"bad\x00data", b"bad\x0bdata", b"\xef\xbf\xbe", b"\xff"])
def test_invalid_xml_or_utf8_body_is_not_silently_replaced(body: bytes) -> None:
    request = replace(plan().requests[0], method="POST", body=body)
    result = JMeterGenerator().generate(plan(request))
    assert result.status == "blocked" and result.files == {}


def test_order_unique_names_and_immutable_determinism() -> None:
    first = plan().requests[0]
    second = replace(first, id="r2", group_id="g2")
    candidate = RequestPlan((second, first), (
        NativeGroup("g1", "same", ("r1",)), NativeGroup("g2", "same", ("r2",))))
    original = deepcopy(candidate)
    result = JMeterGenerator().generate(candidate)
    assert result == JMeterGenerator().generate(candidate) and candidate == original
    root = tree(result)
    assert [prop(e, "TestPlan.comments") for e in root.iter("HTTPSamplerProxy")] == ["r1", "r2"]
    names = [e.attrib["testname"] for e in root.iter("TestFragmentController")]
    assert len(set(names)) == 2


def test_output_budget_preflight_and_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", 128)
    request = replace(plan().requests[0], method="POST", body=b"x" * 129)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_OUTPUT_LIMIT")
    blocked(JMeterGenerator().generate(plan()), "JMETER_OUTPUT_LIMIT")


def test_no_network_or_process_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Generator must not perform external execution or networking")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert JMeterGenerator().generate(plan()).status == "generated"


def test_metadata_does_not_claim_native_validation_or_readiness() -> None:
    metadata = JMeterGenerator().generate(plan()).metadata
    assert metadata["profile"] == "static_literals_v1"
    assert metadata["artifact_kind"] == "jmx"
    assert metadata["compatibility"] == "unverified"
    assert metadata["native_validation"] == metadata["execution"] == "not_run"
    assert metadata["intended_baseline"] == {"jmeter": "5.5", "java": "8"}
    assert metadata["limitations"]


@pytest.mark.parametrize("url", ["file:///private/file", "https://user@example.invalid/",
                                "https://example.invalid/#fragment", "https://[broken/",
                                "https://example.invalid:99999/", "https://example.invalid/\n"])
def test_defensive_url_checks(url: str) -> None:
    request = replace(plan().requests[0], url=url)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_URL_UNSUPPORTED")


@pytest.mark.parametrize("headers", [(("Host", "example.invalid"),), (("Cookie", "a=b"),),
                                    (("X-Test", "bad\r\n"),), (("bad name", "value"),),
                                    (("Content-Type", "text/plain"),
                                     ("Content-Type", "text/plain"))])
def test_defensive_header_checks(headers: tuple[tuple[str, str], ...]) -> None:
    request = replace(plan().requests[0], headers=headers)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_HEADER_UNSUPPORTED")


@pytest.mark.parametrize("name", ["bad\x00", "bad\ud800", "bad\uffff"])
def test_invalid_xml_in_display_names_is_blocked(name: str) -> None:
    blocked(JMeterGenerator().generate(plan(name=name)), "JMETER_XML_CHARACTER_UNSUPPORTED")


def test_one_bad_request_blocks_all_samplers_and_mappings() -> None:
    good = plan().requests[0]
    bad = replace(good, id="r2", method="CONNECT")
    candidate = RequestPlan((good, bad), (NativeGroup("g1", "Both", ("r1", "r2")),))
    blocked(JMeterGenerator().generate(candidate), "JMETER_METHOD_UNSUPPORTED")


@pytest.mark.parametrize("body", [None, b""])
def test_absent_and_empty_body_have_distinct_argument_representation(body: bytes | None) -> None:
    request = replace(plan().requests[0], method="POST", body=body,
                      headers=(("Content-Type", "text/plain"),))
    root = tree(JMeterGenerator().generate(plan(request)))
    arguments = root.findall(".//elementProp[@elementType='HTTPArgument']")
    assert len(arguments) == (0 if body is None else 1)
    assert prop(root, "HTTPSampler.postBodyRaw") == str(body is not None).lower()


@pytest.mark.parametrize("url,path,host", [
    ("https://example.invalid", "/", "example.invalid"),
    ("https://example.invalid?", "/?", "example.invalid"),
    ("http://[2001:db8::1]:8080/%2F?a=+&b=%26", "/%2F?a=+&b=%26", "[2001:db8::1]"),
])
def test_query_separator_and_ipv6_are_not_decoded(url: str, path: str, host: str) -> None:
    root = tree(JMeterGenerator().generate(plan(replace(plan().requests[0], url=url))))
    assert prop(root, "HTTPSampler.path") == path
    assert prop(root, "HTTPSampler.domain") == host


def test_body_caps_are_checked_without_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    request = replace(plan().requests[0], method="POST", body=b"a" * 20)
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 19)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_BODY_LIMIT")
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 20)
    monkeypatch.setattr(module, "MAX_TOTAL_BODY_BYTES", 19)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_BODY_LIMIT")


def test_serialized_budget_counts_escaping_and_cr(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    request = replace(plan().requests[0], method="POST", body=b"<&\r" * 100,
                      headers=(("Content-Type", "text/plain"),))
    candidate = plan(request)
    size = len(JMeterGenerator().generate(candidate).files[ARTIFACT])
    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", size)
    assert len(JMeterGenerator().generate(candidate).files[ARTIFACT]) == size
    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", size - 1)
    blocked(JMeterGenerator().generate(candidate), "JMETER_OUTPUT_LIMIT")


def test_preflight_output_limit_does_not_construct_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    def forbidden(*args: object) -> None:
        pytest.fail("Oversized input must be rejected before tree construction")

    monkeypatch.setattr(module, "_document", forbidden)
    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", 10)
    blocked(JMeterGenerator().generate(plan()), "JMETER_OUTPUT_LIMIT")


def test_header_node_budget_precedes_tree_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    request = replace(plan().requests[0], headers=tuple((f"X-{i}", "x") for i in range(1001)))
    monkeypatch.setattr(module, "_document", lambda _: pytest.fail("Oversized header tree built"))
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_HEADER_LIMIT")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("body", [None, b""])
def test_missing_content_type_does_not_accept_known_native_default(method, body):
    request = replace(plan().requests[0], method=method, body=body)
    blocked(JMeterGenerator().generate(plan(request)), "JMETER_CONTENT_TYPE_REQUIRED")
