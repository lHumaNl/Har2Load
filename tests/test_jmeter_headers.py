"""Offline header lowering checks; real native execution is opt-in under validation/."""

import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import replace

import pytest

from har2load.generation_types import RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.jmeter_response import RUNTIME_SCRIPT
from tests.test_jmeter_extensions import authored
from tests.test_jmeter_runtime import (
    ARTIFACT,
    assert_runtime_maps,
    config,
    correlation,
    extractor,
    prop,
    resolve_path,
    root,
)


def headers(name: str = "X-Token", scope: str = "iteration") -> RequestPlan:
    candidate = correlation(scope=scope)
    assert candidate.runtime is not None
    definition = {**extractor(), "area": "response_headers",
                  "selector": {"dialect": "name_v1", "name": name}}
    return replace(candidate, runtime=replace(candidate.runtime, extractors=(definition,)))


def test_header_edge_whitespace_uses_linear_scans_not_backtracking_regex():
    reader = RUNTIME_SCRIPT.split("def headerField =", 1)[1].split("def parseHeaders =", 1)[0]
    assert "replaceAll" not in reader
    assert "while (start < end" in reader
    assert "while (end > start" in reader
    assert "line.substring(start, end)" in reader


@pytest.mark.parametrize("name", ["X-Token", "Set-Cookie", "a" * 256, "!#$%&'*+.^_`|~-"])
def test_header_declarations_render_without_reading_native_data(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Preparation attempted external I/O")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    candidate = headers(name)
    original = deepcopy(candidate)
    build = JMeterGenerator().generate(candidate)
    assert build.status == "generated", build.diagnostics
    assert build == JMeterGenerator().generate(candidate) and candidate == original
    assert_runtime_maps(build)


@pytest.mark.parametrize("name", ["", "a" * 257, "X Token", "X:", "雪", "X\n", " X"])
def test_header_selector_native_caps_block_atomically(name: str) -> None:
    result = JMeterGenerator().generate(headers(name))
    assert result.status == "blocked" and not result.files and not result.source_map
    assert {d.code for d in result.diagnostics} == {"RUNTIME_RESPONSE_HEADER_SELECTOR_UNSUPPORTED"}


def test_wrong_header_dialect_and_cookies_stay_unsupported() -> None:
    candidate = headers()
    assert candidate.runtime is not None
    for definition in ({**extractor(), "area": "response_headers"},
                       {**candidate.runtime.extractors[0], "area": "response_cookies"}):
        result = JMeterGenerator().generate(replace(candidate, runtime=replace(
            candidate.runtime, extractors=(definition,))))
        assert result.status == "blocked" and not result.files and not result.source_map


@pytest.mark.parametrize("type_name", ["string", "integer", "number", "boolean", "null"])
def test_header_types_remain_declarations_not_coercion(type_name: str) -> None:
    candidate = headers()
    assert candidate.runtime is not None
    declaration = {**candidate.runtime.variables[0], "value_type": type_name}
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(declaration,)))
    node = root(candidate).find(".//JSR223PostProcessor")
    assert node is not None
    assert config(node)["variables"][0]["value_type"] == type_name
    assert "def valid = checked(value, v.value_type)" in prop(node, "script")


def test_header_config_retains_occurrence_and_shared_limits() -> None:
    candidate = headers()
    assert candidate.runtime is not None
    definition = {**candidate.runtime.extractors[0], "occurrence": 10**30,
                  "cardinality": "occurrence", "allow_empty": True}
    node = root(replace(candidate, runtime=replace(
        candidate.runtime, extractors=(definition,)))).find(".//JSR223PostProcessor")
    assert node is not None
    cfg = config(node)
    assert cfg["extractors"][0] == {**definition, "occurrence": str(10**30)}
    assert cfg["header_field_limit"] == cfg["match_limit"] == 1000
    assert cfg["field_limit"] == 1024 * 1024 and cfg["value_limit"] == 65536


def test_header_metadata_is_fixed_value_free_and_profiles_advance() -> None:
    metadata = JMeterGenerator().generate(headers("PRIVATE_HEADER_NAME")).metadata
    assert metadata["response_header_policy"] == "native_response_headers_v1"
    assert metadata["profile_version"] == "5"
    assert metadata["execution"] == metadata["native_validation"] == "not_run"
    assert "PRIVATE_HEADER_NAME" not in json.dumps(metadata)
    assert JMeterGenerator().generate(authored()).metadata["profile_version"] == "4"
    static = JMeterGenerator().generate(replace(headers(), runtime=None))
    assert static.metadata["profile_version"] == "1"


def test_unreferenced_header_and_body_maps_point_to_actual_producer() -> None:
    candidate = headers()
    assert candidate.runtime is not None
    extra = ({**candidate.runtime.extractors[0], "id": "unused_header", "exchange_id": "r2"},
             {**extractor(), "id": "unused_body"})
    build = JMeterGenerator().generate(replace(candidate, runtime=replace(
        candidate.runtime, extractors=(*candidate.runtime.extractors, *extra))))
    tree = ET.fromstring(build.files[ARTIFACT])
    rows = [row for row in build.source_map if row["kind"] == "extractor"]
    assert {row["entity_id"] for row in rows} == {"e1", "unused_header", "unused_body"}
    for row in rows:
        node = resolve_path(tree, row["locator"]["component_path"])
        assert node.tag == "JSR223PostProcessor"
        expected = "r2" if row["entity_id"] == "unused_header" else "r1"
        assert config(node)["request"]["id"] == expected


def test_headers_do_not_change_cookie_configuration_or_native_status() -> None:
    tree = root(headers())
    baseline = root(correlation())
    manager = ET.tostring(tree.find(".//CookieManager"))
    assert manager == ET.tostring(baseline.find(".//CookieManager"))
    script = prop(tree.find(".//JSR223PostProcessor"), "script")
    assert "prev.getResponseHeaders()" in script
    assert "setSuccessful(" not in script and "AssertionResult" not in script
    assert "getCookieManager" not in script and "getRequestHeaders" not in script
    assert "prev != null && prev.isSuccessful()" in script
