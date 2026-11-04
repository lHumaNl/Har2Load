"""Offline authored-wrapper checks; never compile or execute authored Groovy."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.jmeter_extensions import configuration as extension_configuration
from har2load.runtime_types import RuntimeTemplate
from tests.test_jmeter_runtime import config, plan, prop, resolve_path, root


def block(source: str = "emit('v1', 'fresh')") -> dict[str, JSONValue]:
    return {"id": "block", "target": "jmeter", "language": "groovy", "source": source,
            "source_digest": sha256(source.encode()).hexdigest(), "change_id": "synthetic",
            "runtime_abi": "scalar_body_v1", "behavior_id": "behavior", "order": 0,
            "placement": {"event": "before_request", "exchange_id": "r1"},
            "inputs": [], "outputs": ["v1"], "helper_calls": [], "depends_on": [],
            "dependencies": [], "side_effects": [], "purpose": "Synthetic wrapper",
            "failure_policy": "mark_error_and_continue",
            "failed_input_policy": "skip_and_mark_outputs", "reset_outputs": True}


def helper(identifier: str = "helper", source: str = "return inputs.value") -> dict[str, JSONValue]:
    return {"id": identifier, "target": "jmeter", "language": "groovy", "source": source,
            "source_digest": sha256(source.encode()).hexdigest(), "change_id": "synthetic",
            "runtime_abi": "scalar_body_v1", "namespace": "fixture", "symbol": identifier,
            "parameters": [{"name": "value", "value_type": "string"}], "return_type": "string",
            "depends_on": [], "dependencies": [], "side_effects": [], "purpose": "Synthetic",
            "stateless": True, "thread_safe": True}


def authored(source: str = "emit('v1', 'fresh')") -> RequestPlan:
    candidate = plan()
    assert candidate.runtime is not None
    output = {**candidate.runtime.variables[0],
              "source": {"kind": "agent_code", "behavior_id": "behavior"},
              "initialize": {"event": "before_request", "exchange_id": "r1"}}
    return replace(candidate, runtime=replace(candidate.runtime, variables=(output,),
                                              blocks=(block(source),)))


def test_setup_registry_precedes_debug_and_preserves_source_as_encoded_data() -> None:
    source = "emit('v1', '${literal}<&\\\\')\n// exact CRLF\r\n"
    tree = root(authored(source))
    setup = tree.find(".//SetupThreadGroup")
    assert setup is not None
    nodes = list(tree.find(".//TestPlan/.."))[1]
    tags = [n.tag for n in nodes]
    assert tags.index("SetupThreadGroup") < tags.index("ThreadGroup")
    scripts = list(tree.iter("JSR223Sampler"))
    assert config(scripts[0])["extensions"]["blocks"][0]["source"] == source
    assert all(source not in prop(node, "script") for node in scripts)
    assert all("${" not in prop(node, "script") for node in scripts)
    for node in tree.iter():
        if node.tag in {"JSR223PreProcessor", "JSR223PostProcessor"}:
            assert "source" not in config(node)["extensions"]["blocks"][0]


def test_extension_maps_are_unique_and_resolve_actual_components() -> None:
    candidate = authored()
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        helpers=(helper("first"), helper("second")),
        blocks=({**block(), "helper_calls": ["first", "second"]},)))
    build = JMeterGenerator().generate(candidate)
    tree = root(candidate)
    rows = [r for r in build.source_map if r["kind"] in {
        "code_block", "reset_outputs", "helper_definition", "helper_call"}]
    assert len(rows) == 6
    assert {r["helper_id"] for r in rows if r["kind"] == "helper_call"} == {"first", "second"}
    for row in rows:
        node = resolve_path(tree, row["locator"]["component_path"])
        assert node.tag == row["locator"]["element_type"]
    assert build.metadata["profile"] == "authored_scalar_runtime_v1"
    assert build.metadata["native_validation"] == "not_run"


@pytest.mark.parametrize("field,value,code", [
    ("runtime_abi", None, "GENERATION_EXTENSION_ABI_REQUIRED"),
    ("dependencies", [{"kind": "file", "path": "never-read"}],
     "GENERATION_EXTENSION_DEPENDENCY_UNSUPPORTED"),
])
def test_direct_generator_enforces_capability_gates(
    field: str, value: JSONValue, code: str,
) -> None:
    candidate = authored()
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        blocks=({**block(), field: value},)))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and not result.files
    assert code in {d.code for d in result.diagnostics}


def test_authored_generation_is_offline_and_metadata_never_contains_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Preparation executed source or accessed external state")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    source = "throw new Exception('PRIVATE_AUTHORED_SOURCE')"
    candidate = authored(source)
    result = JMeterGenerator().generate(candidate)
    assert result.status == "generated"
    assert result == JMeterGenerator().generate(candidate)
    assert "PRIVATE_AUTHORED_SOURCE" not in json.dumps(result.metadata)
    assert result.metadata["native_status_policy"] == "markers_only_native_status_v1"


def test_registry_namespace_is_revision_pinned_with_deterministic_direct_plan_fallback() -> None:
    runtime = authored().runtime
    first = extension_configuration(runtime)
    assert first == extension_configuration(runtime)
    changed = replace(runtime, blocks=(block("emit('v1', 'changed')"),))
    assert first["registry_key"] != extension_configuration(changed)["registry_key"]
    pinned = replace(runtime, extension_namespace="revision-model-digest")
    assert extension_configuration(pinned)["registry_key"].endswith(".revision-model-digest")


def test_other_target_declarations_do_not_leak_into_jmeter_registry_or_maps() -> None:
    candidate = authored()
    other = {**block("PRIVATE_OTHER_TARGET_SOURCE"), "id": "other", "target": "loadrunner"}
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        blocks=(*candidate.runtime.blocks, other)))
    build = JMeterGenerator().generate(candidate)
    assert build.status == "generated"
    assert not any(row["entity_id"] == "other" for row in build.source_map)
    setup = root(candidate).find(".//JSR223Sampler")
    assert len(config(setup)["extensions"]["blocks"]) == 1


def test_helper_only_and_output_free_models_have_setup_and_declaration_maps() -> None:
    candidate = authored("// Comment-only body")
    runtime = candidate.runtime
    request = replace(runtime.requests[0], body=RuntimeTemplate(('"static"',)))
    for blocks in ((), ({**block("// Comment-only body"), "outputs": []},)):
        runtime = replace(runtime, variables=(), requests=(request,), blocks=blocks,
                          helpers=(helper(),))
        build = JMeterGenerator().generate(replace(candidate, runtime=runtime))
        assert build.status == "generated"
        kinds = [row["kind"] for row in build.source_map]
        assert kinds.count("helper_definition") == 1
        assert kinds.count("code_block") == len(blocks)
        assert kinds.count("reset_outputs") == len(blocks)


def test_authored_runtime_preserves_phase_order_and_never_sets_native_status() -> None:
    tree = root(authored())
    pre = prop(tree.find(".//JSR223PreProcessor"), "script")
    assert pre.index("event('before_request')") < pre.index("extensionRun()")
    assert pre.index("extensionRun()") < pre.index("try { assemble() }")
    assert pre.index("event('after_response')") < pre.rindex("extensionRun()")
    assert "if (v.source.kind == 'agent_code') return" in pre
    for node in tree.iter():
        if node.tag.startswith("JSR223"):
            script = prop(node, "script")
            assert "setSuccessful(" not in script and "AssertionResult" not in script
    assert "parseClass(" not in pre


@pytest.mark.parametrize("declaration", [
    {**helper(), "runtime_abi": None},
    {**helper(), "dependencies": [{"kind": "jar", "path": "private.jar"}]},
])
def test_unused_unsupported_helpers_are_not_silently_dropped(declaration: dict) -> None:
    candidate = authored()
    candidate = replace(candidate, runtime=replace(candidate.runtime, helpers=(declaration,)))
    result = JMeterGenerator().generate(candidate)
    assert result.status == "blocked" and not result.files and not result.source_map
    assert "private.jar" not in json.dumps(result.metadata)
