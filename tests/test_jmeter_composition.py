"""Offline contract tests using synthetic, generator-produced 0.11 plans."""

import base64
import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from decimal import Decimal

import pytest

from har2load import jmeter, jmeter_composition
from har2load.domain import Har2LoadError
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.jmeter import ARTIFACT, JMeterGenerator
from har2load.jmeter_composition import compose_jmx
from har2load.jmeter_sources import literal_input
from tests.test_jmeter import plan as static
from tests.test_jmeter_cookies import same_name_seeds
from tests.test_jmeter_extensions import authored
from tests.test_jmeter_headers import headers
from tests.test_jmeter_runtime import config, correlation, plan, prop, variable
from tests.test_jmeter_sources import external

FIRST, SECOND = "TF_UC1.1", "TF_UC2"


def generated(candidate: RequestPlan) -> bytes:
    build = JMeterGenerator().generate(candidate)
    assert build.status == "generated", build.diagnostics
    return build.files[ARTIFACT]


def composed(one: RequestPlan, two: RequestPlan) -> ET.Element:
    return ET.fromstring(compose_jmx([(FIRST, generated(one)), (SECOND, generated(two))]))


def tags(tree: ET.Element) -> list[str]:
    return [node.tag for node in tree[::2]]


def test_explicit_paths_and_separate_debug_groups_preserve_step_order() -> None:
    one = static(name="A & B")
    second = replace(one.requests[0], id="r2", group_id="g2")
    third = replace(second, id="r3")
    one = replace(one, requests=(*one.requests, second, third),
                  groups=(*one.groups, NativeGroup("g2", "A & B", ("r3", "r2"))))
    branch = composed(one, static())[0][1]
    assert tags(branch) == ["ThreadGroup", "TestFragmentController"] * 2 + ["ResultCollector"]
    for offset, label in [(0, FIRST), (4, SECOND)]:
        group, children, wrapper, steps = branch[offset:offset + 4]
        assert group.get("testname") == "DebugTG_" + label and wrapper.get("testname") == label
        assert tags(children) == ["CookieManager", "ModuleController", "DebugSampler"]
        assert [p.text for p in children[2][0]] == ["Test Plan", "Har2Load", label]
        assert all(node.tag == "GenericController" for node in steps[::2])
    assert [s.get("testname") for s in branch[3][::2]] == ["TF_1_A & B", "TF_2_A & B"]
    assert [prop(s, "TestPlan.comments") for s in branch[3].iter("HTTPSamplerProxy")] == [
        "r1", "r3", "r2"]


def input_plan(value: str | None, spare: str | None) -> RequestPlan:
    candidate = plan(external(value))
    assert candidate.runtime is not None
    extra = {**external(spare, key="spare"), "name": "unbound", "id": "v2"}
    alias = {**external(None), "name": "alias", "id": "v3"}
    runtime = replace(candidate.runtime, variables=(*candidate.runtime.variables, extra, alias))
    return replace(candidate, runtime=runtime)


def assert_declarations(nodes: list[ET.Element], label: str) -> None:
    for node in nodes:
        declarations = config(node)["variables"]
        assert [d["source"]["key"] for d in declarations] == [
            f"har2load.input.{label}.{k}" for k in ("token", "spare", "token")]
        assert [d["name"] for d in declarations] == ["token", "unbound", "alias"]


def test_runtime_keys_scope_all_inputs_including_missing_and_unbound() -> None:
    first_value = '\\dir\r\n${x}\\\\end'
    first, second = input_plan(first_value, None), input_plan(None, "SYNTHETIC_PRIVATE")
    document = composed(first, second)
    assert not document.findall(".//elementProp[@elementType='Argument']")
    branch = document[0][1]
    for offset, label, key, value in [(0, FIRST, "token", first_value),
                                       (4, SECOND, "spare", "SYNTHETIC_PRIVATE")]:
        children, steps = branch[offset + 1], branch[offset + 3]
        assert tags(children) == ["CookieManager", "JSR223Sampler", "ModuleController",
                                  "DebugSampler"]
        parameters, subtree = children[3]
        assert parameters.tag == "UserParameters" and len(subtree) == 0
        assert [p.text for p in parameters.find("*[@name='UserParameters.names']")] == [
            f"har2load.input.{label}.{key}"]
        assert [p.text for p in parameters.findall("*[@name='UserParameters.thread_values']/*/*")
                ] == [literal_input(value)]
        assert_declarations([children[2], *steps.iter("JSR223PreProcessor"),
                             *steps.iter("JSR223PostProcessor")], label)
    assert b"SYNTHETIC_PRIVATE" not in ET.tostring(branch[1])
    assert FIRST.encode() not in ET.tostring(branch[5])


def test_static_body_and_names_round_trip_with_carriage_returns() -> None:
    body = b"x\r\ny\rz"
    request = NativeRequest("r", "g1", "POST", "https://example.invalid/",
                            (("Content-Type", "text/plain"),), body)
    tree = composed(static(request, "my group\r\n2"), plan())
    assert tree.find(".//GenericController").get("testname") == "TF_1_my group\r\n2"
    assert tree.find(".//*[@name='Argument.value']").text == body.decode()


def rejected(data: bytes) -> Har2LoadError:
    inputs = [(FIRST, generated(static())), (SECOND, data)]
    with pytest.raises(Har2LoadError) as error:
        compose_jmx(inputs)
    assert inputs[1][1] is data
    assert error.value.code.startswith("COMPOSE_") and error.value.exit_code == 3
    assert "PRIVATE" not in str(error.value)
    return error.value


@pytest.mark.parametrize("prefix", [b"<!DOCTYPE x>", b'<!DOCTYPE x [<!ENTITY x "PRIVATE">]>',
                                    b"<?PRIVATE data?>", b"<!--PRIVATE-->"])
def test_forbidden_xml_markup(prefix: bytes) -> None:
    rejected(prefix + generated(static()).split(b"\n", 1)[1])


@pytest.mark.parametrize("xpath,attribute,value", [
    (".//TestPlan", "enabled", "false"), (".//DebugSampler", "testclass", "PRIVATE"),
    (".//ResultCollector/stringProp[@name='filename']", "", "PRIVATE"),
    (".//ModuleController/collectionProp/stringProp[last()]", "", "PRIVATE"),
    (".//JSR223PostProcessor/stringProp[@name='script']", "", "PRIVATE"),
    (".//JSR223Sampler/stringProp[@name='parameters']", "", "PRIVATE"),
    (".//JSR223PreProcessor/stringProp[@name='filename']", "", "PRIVATE"),
])
def test_changed_profile_is_not_silently_accepted(xpath: str, attribute: str, value: str) -> None:
    tree = ET.fromstring(generated(plan()))
    node = tree.find(xpath)
    assert node is not None
    if attribute:
        node.set(attribute, value)
    else:
        node.text = value
    rejected(ET.tostring(tree))


@pytest.mark.parametrize("xpath", [".//TestPlan", ".//CookieManager", ".//HTTPSamplerProxy",
                                   ".//TransactionController", "./hashTree/hashTree"])
def test_refuse_unknown_component_or_property_even_beside_valid_ones(xpath: str) -> None:
    tree = ET.fromstring(generated(static()))
    ET.SubElement(tree.find(xpath), "PRIVATE")
    rejected(ET.tostring(tree))


def test_refuse_authored_even_when_only_in_an_event_config() -> None:
    assert rejected(generated(authored())).code == "COMPOSE_AUTHORED_UNSUPPORTED"
    tree = ET.fromstring(generated(plan()))
    node = tree.find(".//JSR223PostProcessor")
    record = config(node)
    record["extensions"]["helpers"].append({"id": "PRIVATE"})
    node.find("stringProp[@name='parameters']").text = base64.b64encode(
        json.dumps(record).encode()).decode()
    assert rejected(ET.tostring(tree)).code == "COMPOSE_AUTHORED_UNSUPPORTED"


def test_seeded_cookies_preserve_native_flags_and_locations_only_in_their_own_uc() -> None:
    one = replace(static(), initial_cookies=same_name_seeds())
    two = replace(plan(), initial_cookies=(replace(same_name_seeds()[0], value="other"),))
    branch = composed(one, two)[0][1]
    assert not branch.findall("CookieManager")
    for subtree, model in [(branch[1], one), (branch[5], two)]:
        source = ET.fromstring(generated(model)).find(".//CookieManager")
        assert ET.tostring(subtree[0]) == ET.tostring(source)
    assert b"SYNTHETIC_ROOT_COOKIE" not in ET.tostring(branch[5])


def script_fields(data: bytes) -> list[tuple[str, str]]:
    return [(prop(n, "parameters"), prop(n, "script")) for n in ET.fromstring(data).iter()
            if n.tag.startswith("JSR223")]


@pytest.mark.parametrize("candidate", [
    headers(), correlation(), plan(variable({"kind": "generated", "generator": "uuid"})),
    plan({**variable({"kind": "generated", "generator": "integer",
         "minimum": -(2**63), "maximum": 2**63 - 1}), "value_type": "integer"}),
    plan({**variable({"kind": "literal", "value": Decimal("1E-10000")}),
          "value_type": "number", "change_id": "actual-1"}),
])
def test_non_external_configs_and_scripts_are_byte_for_byte_preserved(
    candidate: RequestPlan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        pytest.fail("Composition must not run a process or access the network")
    first, other = generated(candidate), generated(static())
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    inputs = [(FIRST, first), (SECOND, other)]
    composed_bytes = compose_jmx(inputs)
    assert composed_bytes == compose_jmx(inputs)
    assert script_fields(composed_bytes) == script_fields(first)


@pytest.mark.parametrize("data", [b"<broken", b"<x/>", b"<x>" * 50 + b"</x>" * 50])
def test_depth_and_malformed_xml_are_value_free(data: bytes) -> None:
    err = rejected(data)
    assert err.code == ("COMPOSE_LIMIT" if data.startswith(b"<x>") else "COMPOSE_JMX_INVALID")


@pytest.mark.parametrize("data,code", [(b"PRIVATE malformed data", "COMPOSE_JMX_INVALID"),
                                       (generated(authored()), "COMPOSE_AUTHORED_UNSUPPORTED")])
def test_failed_second_input_identified_only_by_ordinal(data: bytes, code: str) -> None:
    err = rejected(data)
    assert err.code == code
    assert str(err) == "JMeter composition is blocked for UC input #2."


def test_aggregate_budget_is_checked_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    one = generated(static())
    monkeypatch.setattr(jmeter_composition, "MAX_GENERATED_BYTES", len(one))
    with pytest.raises(Har2LoadError) as err:
        compose_jmx([(FIRST, one), (SECOND, one)])
    assert err.value.code == "COMPOSE_LIMIT"


def test_expanded_output_budget_is_checked_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = plan(external())
    declarations = tuple({**external(key=f"k{i}"), "id": f"v{i}", "name": f"val{i}"}
                         for i in range(64))
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=declarations))
    data = generated(candidate)
    inputs = [(FIRST, data), (SECOND, data)]
    assert len(compose_jmx(inputs)) > 2 * len(data)
    monkeypatch.setattr(jmeter, "MAX_GENERATED_BYTES", 2 * len(data))
    with pytest.raises(Har2LoadError) as err:
        compose_jmx(inputs)
    assert err.value.code == "COMPOSE_LIMIT" and err.value.exit_code == 3
