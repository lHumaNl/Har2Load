"""Extension lowering and map identity use synthetic data only."""

import json
from copy import deepcopy
from hashlib import sha256

import pytest

from har2load.domain import Har2LoadError
from har2load.extension_runtime import capability_diagnostics
from har2load.generation import _parameter_map, _validate_runtime_maps
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.runtime_planning import GenerationPlanner
from har2load.runtime_types import runtime_document
from har2load.scenario import ScenarioEditor


def model_with_extensions(kind="blocks", bad_request=False):
    capture = HarImporter().parse(json.dumps({"log": {"entries": [{"request": {
        "method": "GET", "url": "broken" if bad_request else "https://synthetic.invalid/"},
        "response": {"status": 200}}]}}).encode())
    grouping = StepGrouper().group(capture)
    model = ScenarioEditor().initial(capture, grouping)
    source = "// synthetic body\n"
    common = {"id": "body", "target": "jmeter", "language": "groovy", "source": source,
              "source_digest": sha256(source.encode()).hexdigest(), "change_id": "fixture",
              "purpose": "Synthetic", "depends_on": [], "dependencies": [], "side_effects": []}
    if kind == "blocks":
        declaration = {**common, "behavior_id": "behavior", "order": 0,
            "placement": {"event": "before_request", "exchange_id": capture.exchanges[0].id},
            "inputs": [], "outputs": [], "helper_calls": [], "reset_outputs": True,
            "failed_input_policy": "skip_and_mark_outputs",
            "failure_policy": "mark_error_and_continue"}
    else:
        declaration = {**common, "namespace": "test", "symbol": "helper", "parameters": [],
                       "return_type": "void", "stateless": True, "thread_safe": True}
    model["target_extensions"][kind] = [declaration]
    return capture, grouping, model


@pytest.mark.parametrize("kind", ["blocks", "helpers"])
@pytest.mark.parametrize("bad_request", [False, True])
def test_extensions_force_runtime_and_survive_diagnostic_paths(kind, bad_request):
    args = model_with_extensions(kind, bad_request)
    original = deepcopy(args[2])
    plan = GenerationPlanner().build(*args)
    assert plan.runtime is not None
    assert getattr(plan.runtime, kind)[0]["id"] == "body"
    assert runtime_document(plan.runtime)[kind][0]["source"] == "// synthetic body\n"
    assert args[2] == original
    getattr(plan.runtime, kind)[0]["purpose"] = "Changed"
    assert args[2] == original


def test_two_helper_call_maps_do_not_collide_or_get_dropped():
    baseline = [{"target": "jmeter", "kind": "helper_call", "entity_id": "block",
                 "helper_id": helper, "disposition": "not_generated"}
                for helper in ("first", "second")]
    mappings = [{**row, "artifact": "jmeter/scenario.jmx", "locator": {
        "component_path": ["Test Plan", row["helper_id"]], "element_type": "JSR223PreProcessor"}}
        for row in baseline]
    result = _parameter_map(baseline, mappings, {}, {"jmeter": "generated"}, True)
    assert [r["locator"]["component_path"][-1] for r in result] == ["first", "second"]
    for invalid in (mappings[:1], [*mappings, mappings[0]], [*mappings, {
        **mappings[0], "helper_id": "undeclared"}]):
        with pytest.raises(Har2LoadError, match="mapping"):
            _validate_runtime_maps(baseline, invalid, {"jmeter": "generated"})


def test_explicit_abi_and_dependencies_are_target_specific_capabilities():
    plan = GenerationPlanner().build(*model_with_extensions()).runtime
    assert [d.code for d in capability_diagnostics(plan, "jmeter")] == [
        "GENERATION_EXTENSION_ABI_REQUIRED"]
    assert not capability_diagnostics(plan, "loadrunner")
    plan.blocks[0]["runtime_abi"] = "scalar_body_v1"
    assert not capability_diagnostics(plan, "jmeter")
    plan.blocks[0]["dependencies"] = [{"kind": "file", "path": "private/no-read"}]
    assert [d.code for d in capability_diagnostics(plan, "jmeter")] == [
        "GENERATION_EXTENSION_DEPENDENCY_UNSUPPORTED"]


def test_planner_reuses_canonical_block_order_and_helper_dag_not_storage_order():
    capture, grouping, model = model_with_extensions()
    block = model["target_extensions"]["blocks"][0]
    helper = model_with_extensions("helpers")[2]["target_extensions"]["helpers"][0]
    model["target_extensions"] = {
        "blocks": [{**deepcopy(block), "id": name, "behavior_id": name, "order": order,
                    "depends_on": dependencies} for name, order, dependencies in (
                        ("last", 30, ["middle"]), ("first", 10, []), ("middle", 20, ["first"]))],
        "helpers": [{**deepcopy(helper), "id": name, "symbol": name,
                     "depends_on": dependencies} for name, dependencies in (
                         ("helper_m", ["helper_a"]), ("helper_a", ["helper_z"]),
                         ("helper_z", []))],
    }
    runtime = GenerationPlanner().build(capture, grouping, model).runtime
    assert [b["id"] for b in runtime.blocks] == ["first", "middle", "last"]
    assert [h["id"] for h in runtime.helpers] == ["helper_z", "helper_a", "helper_m"]
