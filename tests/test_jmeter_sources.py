"""Offline native-input/generator contracts; native execution lives in validation/."""

import json
import random
from copy import deepcopy
from dataclasses import replace

import pytest

from har2load.domain import JSONValue
from har2load.jmeter import JMeterGenerator
from tests.test_jmeter_extensions import authored
from tests.test_jmeter_runtime import config, plan, prop, root, variable


def external(value: str | None = None, key: str = "token") -> dict[str, JSONValue]:
    source: dict[str, JSONValue] = {"kind": "external", "key": key, "allow_empty": False}
    if value is not None:
        source["initial_value"] = value
    return variable(source)


@pytest.mark.parametrize("value", ["visible", "", "ERROR_token", "\\path\\", "雪\t\r\n<&",
                                  '${__groovy(throw new Error())}', '\\${x}\\\\${y}'])
def test_udv_values_are_literal_encoded_only_once_and_not_in_runtime_config(value: str) -> None:
    candidate = plan(external(value))
    original = deepcopy(candidate)
    tree = root(candidate)
    arguments = tree.findall(".//elementProp[@elementType='Argument']")
    assert len(arguments) == 1
    assert prop(arguments[0], "Argument.name") == "token"
    assert prop(arguments[0], "Argument.value") == value.replace("\\", "\\\\").replace("${", "\\${")
    for node in tree.iter():
        if node.tag.startswith("JSR223"):
            assert "initial_value" not in config(node)["variables"][0]["source"]
            assert "${" not in prop(node, "script")
    assert candidate == original


def test_missing_input_has_no_active_udv_entry_but_names_are_in_private_comments() -> None:
    tree = root(plan(external(key="missing_native_key")))
    assert not tree.findall(".//elementProp[@elementType='Argument']")
    assert "missing_native_key" in prop(tree.find(".//TestPlan"), "TestPlan.comments")


def test_same_key_deduplicates_defaults_and_absent_default_does_not_conflict() -> None:
    candidate = plan(external("shared"))
    extra = [{**external(value), "id": f"v{i}", "name": f"name{i}"}
             for i, value in enumerate([None, "shared"], 2)]
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        variables=(*candidate.runtime.variables, *extra)))
    assert len(root(candidate).findall(".//elementProp[@elementType='Argument']")) == 1


@pytest.mark.parametrize("value", ["\x00", "\x01", "\ud800", "\ufffe"])
def test_xml_unsupported_input_blocks_without_replacing_or_echoing(value: str) -> None:
    result = JMeterGenerator().generate(plan(external(value)))
    assert result.status == "blocked" and not result.files
    assert "JMETER_XML_CHARACTER_UNSUPPORTED" in {d.code for d in result.diagnostics}
    assert value not in json.dumps(result.metadata)


@pytest.mark.parametrize("source,code", [
    ({"kind": "generated", "generator": "unknown"}, "RUNTIME_GENERATOR_UNSUPPORTED"),
    ({"kind": "generated", "generator": "integer", "minimum": -(2**63)-1, "maximum": 0},
     "RUNTIME_GENERATOR_RANGE_UNSUPPORTED"),
])
def test_direct_generator_checks_source_capabilities(
    source: dict[str, JSONValue], code: str,
) -> None:
    result = JMeterGenerator().generate(plan(variable(source)))
    assert result.status == "blocked" and not result.files
    assert code in {d.code for d in result.diagnostics}


def test_direct_generator_rejects_conflicting_initials() -> None:
    candidate = plan(external("one"))
    second = {**external("two"), "id": "second", "name": "second"}
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        variables=(*candidate.runtime.variables, second)))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and not result.files
    assert "RUNTIME_INPUT_INITIAL_CONFLICT" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("source", [
    {"kind": "generated", "generator": "integer", "minimum": -(2**63), "maximum": 2**63-1},
    {"kind": "generated", "generator": "string", "length": 4096, "alphabet": "a$\\{}"},
    {"kind": "generated", "generator": "uuid"},
    {"kind": "generated", "generator": "utc_datetime", "offset_seconds": 315360000,
     "format": "iso8601"},
    {"kind": "generated", "generator": "utc_date_range", "minimum": "0001-01-01",
     "maximum": "9999-12-31", "format": "iso8601"},
])
def test_generated_configuration_is_data_and_deterministic(
    source: dict[str, JSONValue], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Preparation must not generate random values")
    monkeypatch.setattr(random, "Random", forbidden)
    candidate = plan(variable(source))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "generated" and result == JMeterGenerator().generate(candidate)
    cfg = config(root(candidate).find(".//JSR223Sampler"))
    expected = dict(source)
    if source["generator"] == "integer":
        expected.update(minimum=str(source["minimum"]), maximum=str(source["maximum"]))
    assert cfg["variables"][0]["source"] == expected
    assert not root(candidate).findall(".//elementProp[@elementType='Argument']")


def test_metadata_is_value_free_and_versions_policies_are_explicit() -> None:
    candidate = plan({**external("SYNTHETIC_PRIVATE_SENTINEL", "private_key"), "sensitive": True})
    build = JMeterGenerator().generate(candidate)
    assert b"SYNTHETIC_PRIVATE_SENTINEL" in build.files["jmeter/scenario.jmx"]
    metadata = build.metadata
    assert metadata["profile_version"] == "5"
    assert metadata["input_policy"] == "native_initial_snapshot_v1"
    assert metadata["generator_policy"] == "per_user_generated_scalars_v1"
    assert metadata["udv_encoding"] == "jmeter_udv_literal_v1"
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in json.dumps(metadata)
    assert "private_key" not in json.dumps(metadata)
    assert JMeterGenerator().generate(authored()).metadata["profile_version"] == "4"


@pytest.mark.parametrize("name", ["__jm__DebugTG__idx", "__jm__future", "__jmv_SAME_USER",
                                  "__jmv_future"])
@pytest.mark.parametrize("value", [None, "SYNTHETIC_PRIVATE_INITIAL"])
def test_reserved_native_input_names_block_with_or_without_defaults(
    name: str, value: str | None,
) -> None:
    candidate = plan(external(value, name))
    original = deepcopy(candidate)
    build = JMeterGenerator().generate(candidate)
    assert build.status == "blocked" and not build.files and not build.source_map
    assert {d.code for d in build.diagnostics} == {"JMETER_NATIVE_NAME_RESERVED"}
    assert name not in str(build.diagnostics) and name not in json.dumps(build.metadata)
    assert candidate == original


@pytest.mark.parametrize("name", ["__jm__DebugTG__idx", "__jm__future", "__jmv_SAME_USER",
                                  "__jmv_future"])
@pytest.mark.parametrize("wrapper", [False, True])
def test_reserved_exported_names_block_including_authored_outputs(
    name: str, wrapper: bool,
) -> None:
    candidate = authored() if wrapper else plan()
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        variables=({**candidate.runtime.variables[0], "name": name},)))
    build = JMeterGenerator().generate(candidate)
    assert build.status == "blocked" and not build.files and not build.source_map
    assert {d.code for d in build.diagnostics} == {"JMETER_NATIVE_NAME_RESERVED"}


@pytest.mark.parametrize("name", ["__JM__DebugTG__idx", "__jm_name", "prefix__jmv_SAME_USER"])
def test_reserved_namespace_check_is_prefix_based_and_case_sensitive(name: str) -> None:
    assert JMeterGenerator().generate(plan({**external("value", name), "name": name})).status == (
        "generated")
