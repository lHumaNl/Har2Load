"""Synthetic contracts for opaque target declarations, never native execution."""

from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from typing import cast

import pytest

from har2load.domain import Capture, Exchange, Grouping, Har2LoadError, JSONValue, StepGroup
from har2load.extensions import TargetExtensions


def obj(value: JSONValue) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], value)


def records(model: dict[str, JSONValue], key: str) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], model[key])


def grouping(reverse: bool = False) -> Grouping:
    return Grouping((StepGroup("g", ("ex_a", "ex_b"), "Step", "annotation", "test"),),
                    ("ex_b", "ex_a") if reverse else ("ex_a", "ex_b"), 5.0)


def block(**updates: JSONValue) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {
        "id": "b", "behavior_id": "behavior", "target": "jmeter", "language": "groovy",
        "source": "\n opaque source é\r\n", "placement": {
            "event": "before_request", "exchange_id": "ex_a"}, "order": 0,
        "depends_on": [], "inputs": [], "outputs": [], "helper_calls": [],
        "dependencies": [], "side_effects": [], "failure_policy": "mark_error_and_continue",
        "failed_input_policy": "skip_and_mark_outputs", "reset_outputs": True,
        "purpose": "Synthetic behavior",
    }
    value.update(updates)
    return value


def helper(**updates: JSONValue) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {
        "id": "h", "target": "jmeter", "language": "groovy", "source": "opaque helper",
        "namespace": "ns", "symbol": "f", "parameters": [{"name": "arg", "value_type": "string"}],
        "return_type": "void", "depends_on": [], "dependencies": [], "side_effects": [],
        "stateless": True, "thread_safe": True, "purpose": "Synthetic helper",
    }
    value.update(updates)
    return value


def variable(**updates: JSONValue) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {
        "id": "v", "name": "value", "value_type": "string", "scope": "iteration",
        "source": {"kind": "agent_code", "behavior_id": "behavior"},
        "initialize": {"event": "before_request", "exchange_id": "ex_a"},
        "reset": "iteration_start", "purpose": "Synthetic output", "sensitive": False,
        "change_id": "c",
    }
    value.update(updates)
    return value


def parameters(*variables: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return {"variables": list(variables), "extractors": [], "bindings": []}


def model(*definitions: dict[str, JSONValue]) -> dict[str, JSONValue]:
    result = TargetExtensions.empty()
    for definition in definitions:
        kind = "code_block" if "behavior_id" in definition else "helper"
        key = "block" if kind == "code_block" else "helper"
        assert TargetExtensions.apply_operation(result, {"op": "upsert_" + kind, key: definition},
                                                "c.1-0")
    return result


def validate(extensions: JSONValue, params: dict[str, JSONValue] | None = None,
             reverse: bool = False) -> None:
    capture = Capture("cap", "digest", b"", (Exchange("ex_a", 0, {}), Exchange("ex_b", 1, {})))
    TargetExtensions.validate(extensions, parameters() if params is None else params,
                              capture, grouping(reverse))


def rejects(extensions: JSONValue, params: dict[str, JSONValue] | None = None) -> None:
    with pytest.raises(Har2LoadError) as error:
        validate(extensions, params)
    assert error.value.code == "EXTENSION_INVALID"
    assert error.value.exit_code == 3
    assert str(error.value) == "Invalid target extension declaration."


def test_empty_and_unknown_operations() -> None:
    assert TargetExtensions.empty() == {"blocks": [], "helpers": []}
    validate(TargetExtensions.empty())
    assert TargetExtensions.diagnostics(TargetExtensions.empty(), parameters()) == []
    assert not TargetExtensions.validate_operation({"op": "other"})
    assert not TargetExtensions.apply_operation(TargetExtensions.empty(), {"op": "other"}, "c")


@pytest.mark.parametrize("updates", [
    {"extra": True}, {"source_digest": "spoof"}, {"change_id": "spoof"},
    {"code_semantics": "verified"}, {"id": "bad-id"}, {"id": "é"}, {"id": "x" * 65},
    {"behavior_id": "bad.id"}, {"language": "c"}, {"target": "other"},
    {"source": " "}, {"source": "\ud800"}, {"source": "x\0y"}, {"source": "é" * 32769},
    {"purpose": " "}, {"purpose": "\ud800"}, {"purpose": "x" * 1025},
    {"order": True}, {"order": Decimal("1")}, {"order": -1}, {"order": 10001},
    {"depends_on": ["a", "a"]}, {"inputs": ["v", "v"]}, {"inputs": ["v"], "outputs": ["v"]},
    {"placement": {"event": "setup", "exchange_id": "ex_a"}},
    {"placement": {"event": "before_request", "exchange_id": "ex_a", "group_id": "g"}},
    {"failure_policy": "abort"}, {"failed_input_policy": "continue"}, {"reset_outputs": 1},
    {"side_effects": ["network", "network"]}, {"side_effects": ["other"]},
    {"depends_on": [f"b{i}" for i in range(129)]},
    {"inputs": [f"v{i}" for i in range(257)]},
    {"outputs": [f"v{i}" for i in range(257)]},
    {"helper_calls": [f"h{i}" for i in range(65)]},
    {"dependencies": [{"kind": "file", "path": "file"}] * 33},
])
def test_invalid_block_shapes(updates: dict[str, JSONValue]) -> None:
    with pytest.raises(Har2LoadError):
        model(block(**updates))


@pytest.mark.parametrize("updates", [
    {"namespace": "ns.f"}, {"symbol": "é"}, {"parameters": [{"name": "x", "value_type": "list"}]},
    {"parameters": [{"name": "x", "value_type": "string", "extra": 1}]},
    {"parameters": [{"name": "x", "value_type": "string"}] * 2},
    {"parameters": [{"name": f"a{i}", "value_type": "string"} for i in range(17)]},
    {"return_type": "array"}, {"stateless": False}, {"stateless": 1}, {"thread_safe": 1},
    {"thread_safe": False}, {"side_effects": ["shared_state"]}, {"inputs": []},
])
def test_invalid_helper_shapes(updates: dict[str, JSONValue]) -> None:
    with pytest.raises(Har2LoadError):
        model(helper(**updates))


@pytest.mark.parametrize("value", [None, [], {}, True, 1, "x"])
def test_wrong_field_types_fail_safely(value: JSONValue) -> None:
    for factory in (block, helper):
        for key in factory():
            text_fields = {"id", "behavior_id", "source", "purpose", "namespace", "symbol"}
            array_fields = {"depends_on", "inputs", "outputs", "helper_calls", "dependencies",
                            "side_effects", "parameters"}
            valid = (key in text_fields and value == "x"
                     or key in array_fields and isinstance(value, list) and not value
                     or key == "order" and type(value) is int and value == 1
                     or key in {"reset_outputs", "stateless", "thread_safe"} and value is True)
            if valid:
                validate(model(factory(**{key: value})))
            else:
                with pytest.raises(Har2LoadError) as error:
                    validate(model(factory(**{key: value})))
                assert str(error.value) == "Invalid target extension declaration."


@pytest.mark.parametrize("path", ["", "/x", "C:x", "a\\b", "a//b", "a/./b", "a/../b",
                                  "a/", "a\0b", "a\nb", "a\x7fb", "a\x85b", "x" * 257])
def test_dependency_paths(path: str) -> None:
    with pytest.raises(Har2LoadError):
        model(block(dependencies=[{"kind": "file", "path": path}]))


def test_dependencies_declaration_and_target_rules() -> None:
    validate(model(block(dependencies=[{"kind": "jar", "path": "lib/a.jar"},
                                       {"kind": "csv", "path": "data/a.csv"}])))
    with pytest.raises(Har2LoadError):
        model(block(target="loadrunner", language="c", dependencies=[{"kind": "jar", "path": "a"}]))
    for dependency in [{"kind": "url", "path": "a"}, {"kind": "file", "path": "a", "x": 0}]:
        with pytest.raises(Har2LoadError):
            model(helper(dependencies=[dependency]))


def test_source_exact_digest_sorting_and_no_aliasing() -> None:
    authored = block(id="z")
    extensions = model(authored, block(id="a", behavior_id="other", order=1))
    stored = records(extensions, "blocks")
    assert [item["id"] for item in stored] == ["a", "z"]
    assert stored[1]["source"] == authored["source"]
    assert stored[1]["source_digest"] == sha256(str(authored["source"]).encode()).hexdigest()
    assert stored[1]["change_id"] == "c.1-0"
    obj(authored["placement"])["exchange_id"] = "mutated"
    validate(extensions)
    stored[1]["source"] = "tampered"
    rejects(extensions)


@pytest.mark.parametrize("key,value", [("source_digest", "a" * 64), ("change_id", "bad/id"),
                                       ("extra", None), ("code_semantics", "verified")])
def test_stored_shape_and_provenance(key: str, value: JSONValue) -> None:
    for definition, collection in [(block(), "blocks"), (helper(), "helpers")]:
        extensions = model(definition)
        records(extensions, collection)[0][key] = value
        rejects(extensions)


def test_final_batch_references_removal_and_replacement() -> None:
    extensions = model(block(helper_calls=["h"]), helper())
    validate(extensions)
    TargetExtensions.apply_operation(extensions, {"op": "remove_helper", "id": "h"}, "c")
    assert len(records(extensions, "blocks")) == 1
    rejects(extensions)
    TargetExtensions.apply_operation(extensions, {"op": "upsert_code_block", "block": block()},
                                     "c2")
    validate(extensions)
    assert records(extensions, "blocks")[0]["change_id"] == "c2"
    TargetExtensions.apply_operation(extensions, {"op": "remove_code_block", "id": "b"}, "c")
    assert extensions == TargetExtensions.empty()


@pytest.mark.parametrize("kind", ["code_block", "helper"])
def test_remove_unknown_and_operation_shape(kind: str) -> None:
    extensions = model(block())
    before = deepcopy(extensions)
    for operation in [{"op": "remove_" + kind, "id": "missing"}, {"op": "upsert_" + kind},
                      {"op": "remove_" + kind, "id": "b", "extra": 1}]:
        with pytest.raises(Har2LoadError):
            TargetExtensions.apply_operation(extensions, operation, "c")
    assert extensions == before


@pytest.mark.parametrize("updates", [
    {"depends_on": ["missing"]}, {"depends_on": ["b"]}, {"helper_calls": ["missing"]},
    {"inputs": ["missing"]}, {"outputs": ["missing"]},
    {"placement": {"event": "before_request", "exchange_id": "missing"}},
])
def test_dangling_references(updates: dict[str, JSONValue]) -> None:
    rejects(model(block(**updates)))


def test_global_record_id_uniqueness() -> None:
    rejects(model(block(), helper(id="b")))
    for key in ("variables", "extractors", "bindings"):
        params = parameters(variable(id="b", source={"kind": "literal", "value": "x"}))
        if key != "variables":
            params = parameters()
            params[key] = [{"id": "b"}]
        rejects(model(block()), params)


def test_output_source_and_counterpart_contracts() -> None:
    producer = block(outputs=["v"])
    params = parameters(variable())
    validate(model(producer), params)
    counterpart = block(id="lr", target="loadrunner", language="c")
    rejects(model(producer, counterpart), params)
    counterpart["outputs"] = ["v"]
    validate(model(producer, counterpart), params)
    for source in [{"kind": "literal", "value": "x"},
                   {"kind": "agent_code", "behavior_id": "other"}]:
        rejects(model(producer), parameters(variable(source=source)))
    rejects(model(), params)
    rejects(model(producer), parameters(variable(initialize={"event": "iteration_start"})))


def test_missing_target_is_diagnostic_not_structural_error() -> None:
    extensions = model(block(outputs=["v"]), block(id="consumer", behavior_id="consumer",
                       target="loadrunner", language="c", order=1, inputs=["v"]), helper())
    params = parameters(variable())
    validate(extensions, params)
    diagnostics = TargetExtensions.diagnostics(extensions, params)
    errors = [item for item in diagnostics if item.severity == "error"]
    assert [(item.entity_id, item.target) for item in errors] == [
        ("behavior", "loadrunner"), ("consumer", "jmeter")]
    assert {item.code for item in errors} == {"TARGET_IMPLEMENTATION_MISSING"}
    warnings = [item for item in diagnostics if item.severity == "warning"]
    assert len(warnings) == 3
    assert {item.code for item in warnings} == {"EXTENSION_CODE_UNVERIFIED"}
    assert all("opaque source" not in item.message for item in diagnostics)


@pytest.mark.parametrize("source", [
    {"kind": "literal", "value": "x"}, {"kind": "external", "key": "X", "allow_empty": False},
    {"kind": "generated", "generator": "uuid"}, {"kind": "extracted", "extractor_id": "e"},
])
def test_baseline_inputs_available_at_equal_event(source: JSONValue) -> None:
    placement: JSONValue = {"event": "after_response", "exchange_id": "ex_a"}
    params = parameters(variable(source=source, initialize=placement))
    params["extractors"] = [{"id": "e", "exchange_id": "ex_a", "area": "response_body",
                             "selector": {"dialect": "json_path_subset_v1", "expression": "$.x"},
                             "cardinality": "exactly_one", "allow_empty": False,
                             "on_failure": "mark_error_and_continue", "change_id": "c"}]
    validate(model(block(inputs=["v"], placement=placement)), params)


@pytest.mark.parametrize("initialize,scope,valid", [
    ({"event": "session_start"}, "session", True),
    ({"event": "iteration_start"}, "iteration", True),
    ({"event": "before_request", "exchange_id": "ex_a"}, "request", True),
    ({"event": "before_request", "exchange_id": "ex_b"}, "iteration", False),
    ({"event": "after_response", "exchange_id": "ex_a"}, "iteration", False),
    ({"event": "before_request", "exchange_id": "ex_b"}, "request", False),
])
def test_input_availability(initialize: JSONValue, scope: str, valid: bool) -> None:
    params = parameters(variable(source={"kind": "literal", "value": "x"},
                                 initialize=initialize, scope=scope))
    if valid:
        validate(model(block(inputs=["v"])), params)
    else:
        rejects(model(block(inputs=["v"])), params)


def test_request_scope_cannot_cross_exchange_and_group_order_is_used() -> None:
    consumer = block(inputs=["v"], placement={"event": "before_request", "exchange_id": "ex_b"})
    params = parameters(variable(source={"kind": "literal", "value": "x"}))
    validate(model(consumer), params)
    with pytest.raises(Har2LoadError):
        validate(model(consumer), params, reverse=True)
    records(params, "variables")[0]["scope"] = "request"
    rejects(model(consumer), params)


def test_agent_input_same_event_requires_earlier_target_producer() -> None:
    producer = block(outputs=["v"], order=1)
    consumer = block(id="consumer", behavior_id="consume", inputs=["v"], order=2)
    validate(model(producer, consumer), parameters(variable()))
    consumer["order"] = 0
    rejects(model(producer, consumer), parameters(variable()))
    consumer["placement"] = {"event": "after_response", "exchange_id": "ex_a"}
    validate(model(producer, consumer), parameters(variable()))


def test_block_dependency_order_and_behavior_uniqueness() -> None:
    first = block()
    second = block(id="second", behavior_id="other", order=1, depends_on=["b"])
    validate(model(first, second))
    for updates in [{"order": 0}, {"behavior_id": "behavior"},
                    {"target": "loadrunner", "language": "c"}]:
        rejects(model(first, {**second, **updates}))
    first["depends_on"] = ["second"]
    rejects(model(first, second))


def test_helper_dag_and_symbols() -> None:
    first = helper(id="z", symbol="z")
    second = helper(id="a", depends_on=["z"])
    validate(model(second, first, block(helper_calls=["a"])))
    rejects(model(first, {**second, "symbol": "z"}))
    validate(model(first, {**second, "symbol": "z", "namespace": "other"}))
    rejects(model({**first, "depends_on": ["a"]}, second))
    rejects(model(helper(depends_on=["h"])))
    rejects(model(helper(depends_on=["missing"])))
    rejects(model(helper(), block(target="loadrunner", language="c", helper_calls=["h"])))
    rejects(model(first, {**second, "target": "loadrunner", "language": "c"}))
    rejects(model(helper(target="loadrunner", language="c"), helper(
        id="h2", target="loadrunner", language="c", namespace="other")))


def test_plan_is_deterministic_declaration_only_and_not_aliased() -> None:
    extensions = model(helper(id="z", symbol="z"), helper(id="a", depends_on=["z"]),
                       block(order=2), block(id="early", behavior_id="early", order=1))
    plan = TargetExtensions.plan(extensions, grouping())
    assert plan["code_semantics"] == "unverified"
    targets = obj(plan["targets"])
    jmeter = obj(targets["jmeter"])
    assert jmeter["helper_placement"] == "setup_registry"
    assert [item["id"] for item in records(jmeter, "helpers")] == ["z", "a"]
    assert [item["id"] for item in records(jmeter, "blocks")] == ["early", "b"]
    assert records(jmeter, "blocks")[0]["group_id"] == "g"
    assert obj(targets["loadrunner"])["helper_placement"] == "helper_definitions"
    obj(records(jmeter, "blocks")[0]["placement"])["event"] = "changed"
    validate(extensions)


@pytest.mark.parametrize("kind,maximum", [("blocks", 128), ("helpers", 64)])
def test_record_count_limits(kind: str, maximum: int) -> None:
    definitions = [block(id=f"b{i}", behavior_id=f"behavior{i}", order=i) if kind == "blocks"
                   else helper(id=f"h{i}", symbol=f"f{i}") for i in range(maximum)]
    extensions = model(*definitions)
    validate(extensions)
    extra = block(id="overflow", behavior_id="overflow", order=maximum) if kind == "blocks" else (
        helper(id="overflow", symbol="overflow"))
    operation: dict[str, JSONValue] = {"op": "upsert_code_block", "block": extra} if (
        kind == "blocks") else {"op": "upsert_helper", "helper": extra}
    TargetExtensions.apply_operation(extensions, operation, "c")
    rejects(extensions)


def test_source_byte_and_aggregate_budgets() -> None:
    source = "é" * 32768
    definitions = [block(id=f"b{i}", behavior_id=f"behavior{i}", order=i, source=source)
                   for i in range(32)]
    extensions = model(*definitions)
    validate(extensions)
    TargetExtensions.apply_operation(extensions, {"op": "upsert_helper", "helper": helper()}, "c")
    rejects(extensions)


def test_opaque_source_cannot_execute_or_access_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "open('/must-not-read').read(); __import__('os').system('exit 1')"
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Opaque preparation attempted execution or resource access")
    with monkeypatch.context() as patch:
        for target in ["builtins.open", "builtins.eval", "builtins.exec", "builtins.compile",
                       "socket.socket", "subprocess.run", "os.system", "os.getenv"]:
            patch.setattr(target, forbidden)
        extensions = model(block(source=source, dependencies=[{"kind": "file", "path": "absent"}]),
                           helper(source=source))
        validate(extensions)
        TargetExtensions.diagnostics(extensions, parameters())
        TargetExtensions.plan(extensions, grouping())


@pytest.mark.parametrize("value", [None, [], {}, {"blocks": [], "helpers": [], "extra": []}])
def test_top_level_strict_shape(value: JSONValue) -> None:
    rejects(value)


def test_every_field_required_and_duplicate_stored_ids_rejected() -> None:
    for definition, kind, collection in [(block(), "block", "blocks"),
                                          (helper(), "helper", "helpers")]:
        for key in definition:
            incomplete = {name: value for name, value in definition.items() if name != key}
            operation = "upsert_code_block" if kind == "block" else "upsert_helper"
            with pytest.raises(Har2LoadError):
                TargetExtensions.validate_operation({"op": operation, kind: incomplete})
        for key in ("source_digest", "change_id"):
            extensions = model(definition)
            del records(extensions, collection)[0][key]
            rejects(extensions)
        extensions = model(definition)
        records(extensions, collection).append(deepcopy(records(extensions, collection)[0]))
        rejects(extensions)


@pytest.mark.parametrize("change_id", ["", "bad/id", "x" * 65, "_leading"])
def test_invalid_change_id_does_not_mutate(change_id: str) -> None:
    extensions = model(block())
    before = deepcopy(extensions)
    with pytest.raises(Har2LoadError):
        TargetExtensions.apply_operation(extensions, {"op": "remove_code_block", "id": "b"},
                                         change_id)
    assert extensions == before


@pytest.mark.parametrize("scalar", ["string", "integer", "number", "boolean", "null", "void"])
def test_helper_scalar_contracts_and_valid_boundaries(scalar: str) -> None:
    value_type = "null" if scalar == "void" else scalar
    args: list[JSONValue] = [{"name": f"arg{i}", "value_type": value_type} for i in range(16)]
    validate(model(helper(parameters=args, return_type=scalar), block(
        id="_" + "a" * 63, order=10000, purpose="x" * 1024,
        side_effects=["network", "filesystem", "process", "environment", "shared_state", "unknown"],
    )))


def test_request_scoped_output_and_producer_removal_do_not_cascade() -> None:
    params = parameters(variable(scope="request", reset="before_request"))
    extensions = model(block(outputs=["v"]))
    validate(extensions, params)
    TargetExtensions.apply_operation(extensions, {"op": "remove_code_block", "id": "b"}, "c")
    rejects(extensions, params)
    assert len(records(params, "variables")) == 1
    validate(extensions, parameters())


def test_counterparts_can_declare_independent_inputs_helpers_and_source() -> None:
    params = parameters(variable(), variable(id="input", name="input",
                                            source={"kind": "literal", "value": "x"}))
    extensions = model(block(outputs=["v"], inputs=["input"], helper_calls=["h"]), helper(),
                       block(id="lr", target="loadrunner", language="c", source="distinct source",
                             outputs=["v"], side_effects=["network"]))
    validate(extensions, params)
    diagnostics = TargetExtensions.diagnostics(extensions, params)
    assert all(item.severity == "warning" for item in diagnostics)


def test_dependency_positions_and_plan_follow_logical_exchange_order() -> None:
    first = block(order=10000)
    later = block(id="later", behavior_id="later", depends_on=["b"],
                  placement={"event": "before_request", "exchange_id": "ex_b"})
    extensions = model(later, first)
    validate(extensions)
    with pytest.raises(Har2LoadError):
        validate(extensions, reverse=True)
    plan = TargetExtensions.plan(extensions, grouping(reverse=True))
    assert [item["id"] for item in records(obj(obj(plan["targets"])["jmeter"]), "blocks")] == [
        "later", "b"]


def test_topological_order_uses_sorted_available_ids() -> None:
    extensions = model(helper(id="c", symbol="c"), helper(id="b", symbol="b"),
                       helper(id="a", symbol="a", depends_on=["b"]))
    validate(extensions)
    plan = TargetExtensions.plan(extensions, grouping())
    assert [item["id"] for item in records(obj(obj(plan["targets"])["jmeter"]), "helpers")] == [
        "b", "a", "c"]


def test_missing_counterpart_earlier_input_emits_only_one_behavior_error() -> None:
    consumer = block(id="consumer", behavior_id="consumer", target="loadrunner", language="c",
                     inputs=["v"], placement={"event": "before_request", "exchange_id": "ex_b"})
    extensions = model(block(outputs=["v"]), consumer)
    params = parameters(variable())
    validate(extensions, params)
    errors = [item for item in TargetExtensions.diagnostics(extensions, params)
              if item.code == "TARGET_IMPLEMENTATION_MISSING" and item.entity_id == "behavior"]
    assert len(errors) == 1
    assert errors[0].target == "loadrunner"
