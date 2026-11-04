"""Synthetic structural checks only; no Groovy/Java compilation or native loading."""

import base64
import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan, TargetBuild
from har2load.jmeter import JMeterGenerator
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate

ARTIFACT = "jmeter/scenario.jmx"


def variable(source: dict[str, JSONValue] | None = None) -> dict[str, JSONValue]:
    return {"id": "v1", "name": "token", "value_type": "string", "scope": "iteration",
            "reset": "iteration_start", "initialize": {"event": "iteration_start"},
            "source": source or {"kind": "literal", "value": "safe"},
            "purpose": "Synthetic test", "sensitive": False}


def extractor(dialect: str = "boundary_v1") -> dict[str, JSONValue]:
    selector: dict[str, JSONValue] = {"dialect": dialect, "left": "[", "right": "]"}
    if dialect == "json_path_subset_v1":
        selector = {"dialect": dialect, "expression": '$["a.b"][0]'}
    return {"id": "e1", "exchange_id": "r1", "area": "response_body", "selector": selector,
            "cardinality": "exactly_one", "allow_empty": False,
            "on_failure": "mark_error_and_continue"}


def plan(value: dict[str, JSONValue] | None = None) -> RequestPlan:
    request = NativeRequest("r1", "g1", "POST", "https://example.invalid/a?q=x",
                            (("Content-Type", "application/json"),), b'{"x":"old"}', "json")
    runtime = RuntimeRequest("r1", RuntimeTemplate((request.url,)),
                             (("Content-Type", RuntimeTemplate(("application/json",))),),
                             RuntimeTemplate(('{"x":', RuntimeSlot("b1", "v1", "json"), '}')))
    return RequestPlan((request,), (NativeGroup("g1", "Safe", ("r1",)),),
                       runtime=RuntimePlan((value or variable(),), (), (runtime,)))


def root(candidate: RequestPlan) -> ET.Element:
    result = JMeterGenerator().generate(candidate)
    assert result.status == "generated", result.diagnostics
    return ET.fromstring(result.files[ARTIFACT])


def prop(node: ET.Element, name: str) -> str:
    child = node.find(f"stringProp[@name='{name}']")
    assert child is not None
    return child.text or ""


def config(node: ET.Element) -> dict:
    return json.loads(base64.b64decode(prop(node, "parameters")))


def test_runtime_initializer_precedes_modules_and_processors_are_request_local() -> None:
    tree = root(plan())
    debug = tree.find(".//JSR223Sampler/..")
    assert debug is not None
    assert [e.tag for e in debug if e.tag != "hashTree"] == [
        "JSR223Sampler", "ModuleController", "DebugSampler"]
    assert len(tree.findall(".//JSR223PreProcessor")) == 1
    assert len(tree.findall(".//JSR223PostProcessor")) == 1
    for node in tree.iter():
        if node.tag.startswith("JSR223"):
            assert prop(node, "scriptLanguage") == "groovy"
            assert prop(node, "filename") == ""
            assert prop(node, "cacheKey")
            assert "${" not in prop(node, "script")


@pytest.mark.parametrize("value", ['${__groovy(throw new Error())}', '"\\\r\né<&', "\x00"])
def test_captured_payload_is_base64_data_not_executable_properties(value: str) -> None:
    tree = root(plan(variable({"kind": "literal", "value": value})))
    node = tree.find(".//JSR223Sampler")
    assert node is not None
    assert config(node)["variables"][0]["source"]["value"] == value
    assert value not in prop(node, "script")
    assert "Base64.getDecoder().decode(Parameters)" in prop(node, "script")


def test_metadata_declares_runtime_and_no_native_verification() -> None:
    metadata = JMeterGenerator().generate(plan()).metadata
    assert metadata["profile"] == "scalar_runtime_v1"
    assert metadata["compatibility"] == "unverified"
    assert metadata["native_validation"] == metadata["execution"] == "not_run"


@pytest.mark.parametrize("source", [{"kind": "agent_code", "behavior_id": "opaque"}])
def test_unsupported_sources_block_even_unbound_variables(source: dict[str, JSONValue]) -> None:
    candidate = plan()
    assert candidate.runtime is not None
    unsupported = {**variable(source), "id": "unused", "name": "unbound"}
    runtime = replace(candidate.runtime, variables=(*candidate.runtime.variables, unsupported))
    result = JMeterGenerator().generate(replace(candidate, runtime=runtime))
    assert result.status == "blocked" and not result.files and not result.source_map
    expected = ("GENERATION_EXTENSION_PRODUCER_UNSUPPORTED" if source["kind"] == "agent_code"
                else "JMETER_RUNTIME_SOURCE_UNSUPPORTED")
    assert any(d.code == expected for d in result.diagnostics)


def test_runtime_generation_is_deterministic_and_never_reads_environment_or_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("External I/O during preparation")
    candidate = plan()
    original = deepcopy(candidate)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    result = JMeterGenerator().generate(candidate)
    assert result.status == "generated"
    assert result == JMeterGenerator().generate(candidate) and original == candidate


@pytest.mark.parametrize("value,type_name", [
    (Decimal("12345678901234567890.123"), "number"), (10**60, "integer"),
    (Decimal("1E-10000"), "number"), (Decimal("-0.00"), "number"),
    (False, "boolean"), (True, "boolean"), (None, "null"), ("ERROR_token", "string"),
])
def test_all_scalar_configuration_types_are_preserved(value: JSONValue, type_name: str) -> None:
    declaration = {**variable({"kind": "literal", "value": value}), "value_type": type_name}
    node = root(plan(declaration)).find(".//JSR223Sampler")
    assert node is not None
    record = config(node)["variables"][0]
    assert record["value_type"] == type_name
    expected = str(value) if type_name in {"number", "integer"} else value
    assert record["source"]["value"] == expected
    assert bool(record["source"].get("numeric")) == (type_name in {"number", "integer"})


def test_failed_typed_json_is_a_string_only_when_status_is_failed() -> None:
    node = root(plan()).find(".//JSR223PreProcessor")
    assert node is not None
    script = prop(node, "script")
    assert "entry.ok ? entry.value : 'ERROR_' + declaration.name" in script
    assert "JsonOutput.toJson((Object) value)" in script
    assert "value instanceof String" in script
    assert "state.values[v.id] = [ok: false, value: marker]" in script


def test_failed_assembly_cannot_fall_back_to_captured_values_or_localhost() -> None:
    tree = root(plan())
    sampler = tree.find(".//HTTPSamplerProxy")
    pre = tree.find(".//JSR223PreProcessor")
    assert sampler is not None and pre is not None
    assert prop(sampler, "HTTPSampler.path") == "https://["
    assert "old" not in ET.tostring(sampler, encoding="unicode")
    assert "sampler.setPath(cfg.failed_url)" in prop(pre, "script")
    assert "sampler.setPath('')" not in prop(pre, "script")


def correlation(dialect: str = "json_path_subset_v1", scope: str = "iteration") -> RequestPlan:
    declaration = variable({"kind": "extracted", "extractor_id": "e1"})
    declaration.update(initialize={"event": "after_response", "exchange_id": "r1"},
                       scope=scope, reset=scope + "_start")
    candidate = plan(declaration)
    assert candidate.runtime is not None
    producer = replace(candidate.runtime.requests[0], body=RuntimeTemplate(('"producer"',)))
    consumer = replace(candidate.runtime.requests[0], id="r2")
    requests = (*candidate.requests, replace(candidate.requests[0], id="r2"))
    return replace(candidate, requests=requests,
                   groups=(NativeGroup("g1", "Safe", ("r1", "r2")),),
                   runtime=replace(candidate.runtime, extractors=(extractor(dialect),),
                                   requests=(producer, consumer)))


def resolve_path(tree: ET.Element, path: list[str]) -> ET.Element:
    branch = tree.find("hashTree")
    element = tree
    for name in path:
        assert branch is not None
        children = list(branch)
        matches = [i for i in range(0, len(children), 2) if children[i].get("testname") == name]
        assert len(matches) == 1
        element, branch = children[matches[0]:matches[0] + 2]
    return element


def assert_runtime_maps(build: TargetBuild) -> None:
    tree = ET.fromstring(build.files[ARTIFACT])
    expected = {("variable", "v1"), ("initialize", "v1"), ("reset", "v1"),
                ("extractor", "e1"), ("binding", "b1")}
    rows = [row for row in build.source_map if (row["kind"], row["entity_id"]) in expected]
    assert len(rows) == len(expected)
    for row in rows:
        locator = row["locator"]
        assert isinstance(locator, dict)
        node = resolve_path(tree, locator["component_path"])
        assert node.tag.startswith("JSR223")
        assert locator["element_type"] == node.tag
        assert row["artifact"] == ARTIFACT and row["target"] == "jmeter"


@pytest.mark.parametrize("scope", ["session", "iteration"])
@pytest.mark.parametrize("dialect", ["json_path_subset_v1", "boundary_v1"])
def test_extraction_and_lifecycle_maps_reference_real_components(scope: str, dialect: str) -> None:
    candidate = correlation(dialect, scope)
    result = JMeterGenerator().generate(candidate)
    assert result.status == "generated", result.diagnostics
    assert_runtime_maps(result)
    rows = {(row["kind"], row["entity_id"]): row for row in result.source_map}
    assert rows["initialize", "v1"]["locator"]["event"] == "after_response"
    reset = rows["reset", "v1"]["locator"]
    assert reset["event"] == scope + "_start"
    assert reset["additional_component_paths"][0][-1] == "Runtime before_request"


def test_declared_unbound_extractors_and_variables_are_not_dropped() -> None:
    candidate = correlation()
    assert candidate.runtime is not None
    extra = {**variable(), "id": "unbound", "name": "unused"}
    runtime = replace(candidate.runtime, variables=(*candidate.runtime.variables, extra),
                      extractors=(*candidate.runtime.extractors, {**extractor(), "id": "e_unused"}))
    result = JMeterGenerator().generate(replace(candidate, runtime=runtime))
    rows = {(row["kind"], row["entity_id"]) for row in result.source_map}
    assert {("variable", "unbound"), ("initialize", "unbound"), ("reset", "unbound"),
            ("extractor", "e_unused")} <= rows
    node = ET.fromstring(result.files[ARTIFACT]).find(".//JSR223PostProcessor")
    assert node is not None
    assert len(config(node)["extractors"]) == 2
    assert "log.warn('ERROR_EXTRACTOR_' + e.id)" in prop(node, "script")


@pytest.mark.parametrize("area", ["response_headers", "response_cookies"])
def test_unsupported_response_item_semantics_block_atomically(area: str) -> None:
    candidate = correlation()
    assert candidate.runtime is not None
    definition = {**extractor(), "area": area, "selector": {"dialect": "name_v1", "name": "x"}}
    candidate = replace(candidate, runtime=replace(candidate.runtime, extractors=(definition,)))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and result.files == {} and result.source_map == ()
    assert any(d.code == "JMETER_RUNTIME_RESPONSE_ITEMS_UNSUPPORTED" for d in result.diagnostics)


@pytest.mark.parametrize("encoding", ["text", "header", "path", "form", "json"])
def test_slot_encodings_are_typed_data_not_placeholder_substitution(encoding: str) -> None:
    candidate = plan()
    assert candidate.runtime is not None
    request = replace(candidate.runtime.requests[0], body=RuntimeTemplate((
        "prefix_${captured}", RuntimeSlot("b1", "v1", encoding), "suffix\\\r\n")))
    tree = root(replace(candidate, runtime=replace(candidate.runtime, requests=(request,))))
    pre = tree.find(".//JSR223PreProcessor")
    assert pre is not None
    parts = config(pre)["request"]["body"]["parts"]
    assert parts == ["prefix_${captured}", {"binding_id": "b1", "variable_id": "v1",
                                          "encoding": encoding}, "suffix\\\r\n"]


def test_strict_json_source_guards_duplicate_keys_precision_null_and_limits() -> None:
    tree = root(correlation())
    node = tree.find(".//JSR223PostProcessor")
    assert node is not None
    script = prop(node, "script")
    required = ["STRICT_DUPLICATE_DETECTION", "new BigDecimal(", "parser.nextToken() != null",
                "depth >= cfg.depth_limit", "CodingErrorAction.REPORT", "JsonToken.VALUE_NULL",
                "value.containsKey(token.key)", "Math.abs((long) number.scale())",
                "bytes.length > cfg.field_limit", "value instanceof Map || value instanceof List"]
    assert all(item in script for item in required)
    assert "getResponseDataAsString" not in script and "doubleValue" not in script
    selector = config(node)["extractors"][0]["selector"]
    assert selector["tokens"] == [{"key": "a.b"}, {"index": "0"}]


def test_runtime_order_reset_and_boundary_cardinality_contracts_are_explicit() -> None:
    node = root(correlation("boundary_v1")).find(".//JSR223PostProcessor")
    assert node is not None
    script = prop(node, "script")
    assert script.index("extract()") < script.index("event('after_response')")
    assert "if (!state.started) { event('session_start'); state.started = true }" in script
    assert "position = end + selector.right.length()" in script
    assert "values.size() > cfg.match_limit || occurrence > values.size()" in script
    assert "e.cardinality == 'exactly_one' && values.size() != 1" in script
    assert "if (value == '' && !e.allow_empty) fail()" in script
    assert "it.initialize.exchange_id == cfg.request.id }.each { mark(it) }" in script
    assert "state.values[v.id] = [ok: false, value: marker]" in script


def test_output_budget_checks_repeated_engine_before_building_tree(monkeypatch: pytest.MonkeyPatch):
    import har2load.jmeter as module

    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", 1000)
    monkeypatch.setattr(module, "_document", lambda _: pytest.fail("Unbounded XML constructed"))
    result = JMeterGenerator().generate(plan())
    assert result.status == "blocked" and not result.files
    assert any(d.code == "JMETER_OUTPUT_LIMIT" for d in result.diagnostics)


def test_runtime_base_payloads_never_become_native_properties() -> None:
    candidate = plan()
    request = replace(candidate.requests[0], body=b'${__groovy(private_marker)}\x00',
                      headers=(("Content-Type", "application/json"), ("X-Data", "${secret}")))
    tree = root(replace(candidate, requests=(request,)))
    sampler = tree.find(".//HTTPSamplerProxy")
    assert sampler is not None
    assert "private_marker" not in ET.tostring(tree, encoding="unicode")
    assert tree.find(".//elementProp[@elementType='HTTPArgument']") is None


def test_native_labels_remain_metasyntax_blocked() -> None:
    candidate = replace(plan(), groups=(NativeGroup("g1", "${bad}", ("r1",)),))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and not result.files
    assert any(d.code == "JMETER_METASYNTAX_UNSUPPORTED" for d in result.diagnostics)


def test_runtime_topology_mismatch_blocks_target() -> None:
    candidate = plan()
    assert candidate.runtime is not None
    candidate = replace(candidate, runtime=replace(candidate.runtime, requests=()))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and not result.files and not result.source_map
    assert any(d.code == "JMETER_RUNTIME_PLAN_INVALID" for d in result.diagnostics)


def test_lifecycle_does_not_read_external_inputs_or_execute_authored_source() -> None:
    node = root(plan()).find(".//JSR223Sampler")
    assert node is not None
    script = prop(node, "script")
    assert not any(value in script for value in (
        "System.getenv", "GroovyShell", ".evaluate(", "Eval.", "__groovy", "new File("))
    assert "vars.putObject(cfg.state_key, state)" in script
    assert "." in config(node)["state_key"]


@pytest.mark.parametrize("value", ["x" * 65537, "é" * 32769, "\ud800"])
def test_literal_limits_fail_atomically_without_echoing_values(value: str) -> None:
    result = JMeterGenerator().generate(plan(variable({"kind": "literal", "value": value})))
    assert result.status == "blocked" and not result.files and not result.source_map
    assert any(d.code in {"JMETER_RUNTIME_VALUE_LIMIT", "JMETER_RUNTIME_ENCODING_UNSUPPORTED"}
               for d in result.diagnostics)


def test_configuration_budget_accounts_for_json_and_base64_amplification() -> None:
    from har2load.jmeter_runtime import RuntimeRejected, configuration

    candidate = plan(variable({"kind": "literal", "value": "\x00" * 1024}))
    assert candidate.runtime is not None
    encoded = configuration(candidate.runtime, "iteration_start", budget=100000)
    assert configuration(candidate.runtime, "iteration_start", budget=len(encoded)) == encoded
    with pytest.raises(RuntimeRejected, match="JMETER_OUTPUT_LIMIT"):
        configuration(candidate.runtime, "iteration_start", budget=len(encoded) - 1)


@pytest.mark.parametrize("type_name", ["string", "integer", "number", "boolean", "null"])
def test_extracted_variables_keep_each_declared_scalar_type(type_name: str) -> None:
    candidate = correlation()
    assert candidate.runtime is not None
    declaration = {**candidate.runtime.variables[0], "value_type": type_name}
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(declaration,)))
    node = root(candidate).find(".//JSR223PostProcessor")
    assert node is not None
    assert config(node)["variables"][0]["value_type"] == type_name
    assert "save(v, result.value)" in prop(node, "script")
    assert "def valid = checked(value, v.value_type)" in prop(node, "script")


def test_request_scope_resets_map_every_preprocessor_but_initializer_is_request_local() -> None:
    candidate = correlation()
    assert candidate.runtime is not None
    declaration = {**variable(), "scope": "request", "reset": "before_request",
                   "initialize": {"event": "before_request", "exchange_id": "r2"}}
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(declaration,)))
    result = JMeterGenerator().generate(candidate)
    reset = next(row for row in result.source_map if row["kind"] == "reset")
    assert len(reset["locator"]["additional_component_paths"]) == 2
    initializer = next(row for row in result.source_map if row["kind"] == "initialize")
    path = initializer["locator"]["component_path"]
    assert path[-2] == "HTTP_2_r2" and path[-1] == "Runtime before_request"


@pytest.mark.parametrize("number,allowed", [
    ("1E+65536", True), ("1E-65536", True), ("1E+65537", False), ("1E-65537", False),
    ("1.00E+65538", True), ("1.00E-65535", False),
])
def test_known_numeric_literal_scale_is_checked_before_publication(number, allowed):
    candidate = plan()
    value = {**variable({"kind": "literal", "value": Decimal(number)}),
             "id": "unbound", "name": "unbound", "value_type": "number"}
    candidate = replace(candidate, runtime=replace(
        candidate.runtime, variables=(*candidate.runtime.variables, value)))
    result = JMeterGenerator().generate(candidate)
    assert (result.status == "generated") is allowed
    if not allowed:
        assert not result.files and not result.source_map
        assert any(d.code == "JMETER_RUNTIME_NUMERIC_SCALE_UNSUPPORTED" for d in result.diagnostics)


def test_literal_initialization_after_response_does_not_require_http_success():
    candidate = correlation()
    literal = {**variable({"kind": "literal", "value": 7}), "value_type": "integer",
               "initialize": {"event": "after_response", "exchange_id": "r1"}}
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(literal,)))
    node = root(candidate).find(".//JSR223PostProcessor")
    script = prop(node, "script")
    initialize = script.split("def initialize =", 1)[1].split("def event =", 1)[0]
    assert "responseAvailable" not in initialize
    assert "save(v, source.numeric ? decimal(source.value) : source.value)" in initialize


def test_correlation_never_overrides_native_status_or_adds_assertions() -> None:
    script = prop(root(correlation()).find(".//JSR223PostProcessor"), "script")
    assert "def transportSucceeded = prev != null && prev.isSuccessful()" in script
    assert "def responseAvailable = { transportSucceeded }" in script
    assert script.index("def transportSucceeded") < script.index("extract()")
    assert "setSuccessful(" not in script
    assert "AssertionResult" not in script
    assert "log.warn('ERROR_' + v.name)" in script
    assert "log.warn('ERROR_BINDING_' + slot.binding_id)" in script
