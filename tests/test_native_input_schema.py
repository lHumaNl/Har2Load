"""Native input seeds are explicit opaque strings, never captured-value fallbacks."""

from copy import deepcopy
from typing import cast

import pytest
from test_extensions import block, helper
from test_parameters import capture, grouping, variable

from har2load.domain import Har2LoadError, JSONValue
from har2load.parameters import ParameterModel
from har2load.scenario import ChangeSet, ScenarioEditor

EDITOR = ScenarioEditor()
MAX_BYTES = 65536


def initial(version: str = "1.5") -> dict[str, JSONValue]:
    return EDITOR.initial(capture(), grouping(), schema_version=version)


def operation(identifier: str = "v", **source_fields: JSONValue) -> dict[str, JSONValue]:
    source: dict[str, JSONValue] = {
        "kind": "external", "key": "INPUT", "allow_empty": False, **source_fields,
    }
    return {"op": "upsert_variable", "variable": variable(
        id=identifier, name=identifier, source=source,
    )}


def apply(scenario: dict[str, JSONValue], *operations: JSONValue) -> dict[str, JSONValue]:
    change = ChangeSet.parse({
        "schema_version": "1.0", "change_id": "native-input-edit",
        "base_revision": scenario["revision"], "author": "synthetic-author",
        "rationale": "Explicit native input string",
        "evidence": [{"exchange_id": "ex_a", "location": ""}], "operations": list(operations),
    })
    return EDITOR.apply(scenario, capture(), grouping(), change)


def variables(scenario: dict[str, JSONValue]) -> list[dict[str, JSONValue]]:
    parameters = cast(dict[str, JSONValue], scenario["parameters"])
    return cast(list[dict[str, JSONValue]], parameters["variables"])


def test_upgrade_preserves_all_fields_and_old_revision() -> None:
    original = apply(initial("1.4"), operation(),
                     {"op": "upsert_code_block", "block": block()},
                     {"op": "upsert_helper", "helper": helper()})
    before = deepcopy(original)
    result = apply(original, {"op": "upgrade_native_inputs"})
    assert result == {**before, "schema_version": "1.5", "revision": 3}
    assert original == before
    assert variables(result)[0]["source"] == {
        "kind": "external", "key": "INPUT", "allow_empty": False,
    }
    EDITOR.validate(original, capture(), grouping())
    EDITOR.validate(result, capture(), grouping())


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2", "1.3", "1.5"])
def test_upgrade_requires_exact_predecessor(version: str) -> None:
    original = initial(version)
    before = deepcopy(original)
    with pytest.raises(Har2LoadError):
        apply(original, {"op": "upgrade_native_inputs"})
    assert original == before


def test_upgrade_chain_and_existing_wrapper_abi() -> None:
    original = initial("1.3")
    result = apply(original, {"op": "upgrade_extension_runtime"},
                   {"op": "upgrade_native_inputs"}, operation(initial_value="seed"),
                   {"op": "upsert_code_block", "block": block(runtime_abi="scalar_body_v1")},
                   {"op": "upsert_helper", "helper": helper(runtime_abi="scalar_body_v1")})
    assert result["schema_version"] == "1.5" and result["revision"] == 2
    assert original == initial("1.3")
    EDITOR.validate(result, capture(), grouping())


@pytest.mark.parametrize("version", ["1.1", "1.2", "1.3", "1.4"])
def test_initial_value_requires_upgrade_before_upsert(version: str) -> None:
    original = initial(version)
    before = deepcopy(original)
    with pytest.raises(Har2LoadError) as error:
        apply(original, operation(initial_value="private-sentinel"))
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    assert "private-sentinel" not in str(error.value)
    assert original == before


def test_upgrade_after_upsert_is_too_late_and_failure_is_atomic() -> None:
    original = initial("1.4")
    with pytest.raises(Har2LoadError) as error:
        apply(original, operation(initial_value="seed"), {"op": "upgrade_native_inputs"})
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    assert original == initial("1.4")
    result = apply(original, {"op": "upgrade_native_inputs"}, operation(initial_value="seed"))
    assert variables(result)[0]["change_id"] == "native-input-edit"
    assert original == initial("1.4")


@pytest.mark.parametrize("version", ["1.2", "1.3", "1.4"])
def test_stored_initial_value_is_not_legacy_compatible(version: str) -> None:
    scenario = apply(initial(), operation(initial_value="seed"))
    scenario["schema_version"] = version
    if version == "1.2":
        del scenario["target_extensions"]
    with pytest.raises(Har2LoadError) as error:
        EDITOR.validate(scenario, capture(), grouping())
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    legacy = apply(initial(version), operation())
    EDITOR.validate(legacy, capture(), grouping())


def test_low_level_api_requires_permission_but_shape_validation_is_schemaless() -> None:
    parameters = ParameterModel.empty()
    op = operation(initial_value="seed")
    assert ParameterModel.validate_operation(op)
    with pytest.raises(Har2LoadError) as error:
        ParameterModel.apply_operation(parameters, op, "seed-edit")
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    assert parameters == ParameterModel.empty()
    ParameterModel.apply_operation(parameters, op, "seed-edit", allow_native_inputs=True)
    with pytest.raises(Har2LoadError) as error:
        ParameterModel.validate(parameters, capture(), grouping())
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    ParameterModel.validate(parameters, capture(), grouping(), allow_native_inputs=True)


@pytest.mark.parametrize("seed", ["", "x" * MAX_BYTES, "é" * (MAX_BYTES // 2),
                                 "😀" * (MAX_BYTES // 4), "\r\n\t", "${__groovy(throw 1)}"],
                         ids=["empty", "ascii-limit", "two-byte-limit", "four-byte-limit",
                              "whitespace", "opaque-metasyntax"])
def test_valid_strings_are_preserved_exactly(seed: str) -> None:
    op = operation(initial_value=seed)
    before = deepcopy(op)
    scenario = apply(initial(), op)
    assert variables(scenario)[0]["source"] == cast(dict[str, JSONValue], before["variable"])[
        "source"]
    assert op == before
    parameters = cast(dict[str, JSONValue], scenario["parameters"])
    assert ParameterModel.checks(parameters, capture()) == []


@pytest.mark.parametrize("seed", [None, True, 12, {}, [], "\x00", "a\x00b", "\ud800", "\udfff",
                                 "x" * (MAX_BYTES + 1), "é" * (MAX_BYTES // 2) + "x",
                                 "😀" * (MAX_BYTES // 4) + "x"],
                         ids=["null", "boolean", "integer", "object", "array", "nul",
                              "embedded-nul", "high-surrogate", "low-surrogate",
                              "ascii-overflow", "two-byte-overflow", "four-byte-overflow"])
def test_invalid_initial_value_shape_fails_without_value_disclosure(seed: JSONValue) -> None:
    with pytest.raises(Har2LoadError) as error:
        ParameterModel.validate_operation(operation(initial_value=seed))
    assert error.value.code == "PARAMETER_INVALID"
    assert str(error.value) == "Invalid parameter declaration."


@pytest.mark.parametrize("value_type", ["string", "integer", "number", "boolean", "null"])
@pytest.mark.parametrize("allow_empty", [False, True])
@pytest.mark.parametrize("seed", ["", "not-a-typed-value"])
def test_strings_are_not_coerced_or_runtime_validated(
    value_type: str, allow_empty: bool, seed: str,
) -> None:
    op = operation(initial_value=seed, allow_empty=allow_empty)
    cast(dict[str, JSONValue], op["variable"])["value_type"] = value_type
    scenario = apply(initial(), op)
    assert cast(dict[str, JSONValue], variables(scenario)[0]["source"])["initial_value"] == seed


@pytest.mark.parametrize("second", ["same", None])
@pytest.mark.parametrize("reverse", [False, True])
def test_shared_key_accepts_identical_or_absent_seed(second: str | None, reverse: bool) -> None:
    other = operation("other", **({"initial_value": second} if second is not None else {}))
    operations = [operation(initial_value="same"), other]
    result = apply(initial(), *reversed(operations) if reverse else operations)
    assert len(variables(result)) == 2


@pytest.mark.parametrize("first,second", [("a", "b"), ("", "a"), ("1", "1.0"), ("é", "é")])
def test_shared_key_conflicts_reject_final_batch_without_mutation(first: str, second: str) -> None:
    original = initial()
    with pytest.raises(Har2LoadError) as error:
        apply(original, operation(initial_value=first), operation("other", initial_value=second))
    assert error.value.code == "PARAMETER_INVALID"
    assert str(error.value) == "Invalid parameter declaration."
    assert original == initial()


def test_final_batch_can_repair_temporary_conflicting_seed() -> None:
    result = apply(initial(), operation(initial_value="first"),
                   operation("other", initial_value="second"), operation(initial_value="second"))
    assert len(variables(result)) == 2
    removed = apply(result, operation(initial_value="changed"),
                    {"op": "remove_variable", "id": "other"})
    assert len(variables(removed)) == 1


def test_removing_opt_in_has_no_fallback_and_does_not_alias() -> None:
    original = apply(initial(), operation(initial_value="private-sentinel"))
    before = deepcopy(original)
    restored = apply(original, operation())
    assert "initial_value" not in cast(dict[str, JSONValue], variables(restored)[0]["source"])
    assert original == before
    variables(restored)[0]["purpose"] = "Detached mutation"
    assert original == before


def test_initial_strings_are_not_executed_or_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Native input schema attempted execution or I/O")

    op = operation(initial_value="${__groovy(throw 1)}; /never/read; ${ENV_SECRET}")
    cast(dict[str, JSONValue], op["variable"])["sensitive"] = True
    with monkeypatch.context() as patch:
        for name in ("builtins.open", "builtins.eval", "builtins.exec", "builtins.compile",
                     "os.getenv", "os.system", "subprocess.run", "socket.socket"):
            patch.setattr(name, forbidden)
        result = apply(initial(), op)
        authored = cast(dict[str, JSONValue], op["variable"])
        assert variables(result)[0]["source"] == authored["source"]
        assert variables(result)[0]["sensitive"] is True


@pytest.mark.parametrize("extra", [{"default": "seed"}, {"file": "input.json"},
                                  {"initial_value": "seed", "extra": True}])
def test_external_exact_fields_remain_strict(extra: dict[str, JSONValue]) -> None:
    with pytest.raises(Har2LoadError):
        ParameterModel.validate_operation(operation(**extra))


def test_upgrade_shape_is_exact() -> None:
    with pytest.raises(Har2LoadError):
        apply(initial("1.4"), {"op": "upgrade_native_inputs", "initial_value": "seed"})


def test_full_upgrade_chain_is_one_copy_on_success_revision() -> None:
    original = initial("1.1")
    result = apply(original, {"op": "upgrade_parameters"}, {"op": "upgrade_extensions"},
                   {"op": "upgrade_extension_runtime"}, {"op": "upgrade_native_inputs"},
                   operation(initial_value="seed"))
    assert result["schema_version"] == "1.5" and result["revision"] == 2
    assert original == initial("1.1")


def test_legacy_source_variants_are_unchanged_by_upgrade() -> None:
    sources: list[JSONValue] = [
        {"kind": "literal", "value": "literal"},
        {"kind": "generated", "generator": "uuid"},
        {"kind": "external", "key": "INPUT", "allow_empty": True},
    ]
    operations: list[JSONValue] = [{"op": "upsert_variable", "variable": variable(
        id=f"v{index}", name=f"v{index}", source=source,
    )} for index, source in enumerate(sources)]
    original = apply(initial("1.4"), *operations)
    result = apply(original, {"op": "upgrade_native_inputs"})
    assert [item["source"] for item in variables(result)] == sources
    assert result == {**original, "schema_version": "1.5", "revision": 3}


def test_stored_conflicts_cannot_bypass_final_batch_validation() -> None:
    scenario = apply(initial(), operation(initial_value="seed"), operation("other"))
    cast(dict[str, JSONValue], variables(scenario)[0]["source"])["initial_value"] = "different"
    with pytest.raises(Har2LoadError) as error:
        EDITOR.validate(scenario, capture(), grouping())
    assert error.value.code == "PARAMETER_INVALID"


def test_different_keys_have_independent_defaults() -> None:
    scenario = apply(initial(), operation(initial_value="first"),
                     operation("other", key="SECOND", initial_value="second"))
    EDITOR.validate(scenario, capture(), grouping())
    assert len(variables(scenario)) == 2


def test_permissions_are_independent() -> None:
    parameters = ParameterModel.empty()
    ParameterModel.apply_operation(parameters, operation(initial_value="seed"), "c",
                                   allow_native_inputs=True)
    with pytest.raises(Har2LoadError) as error:
        ParameterModel.validate(parameters, capture(), grouping(), allow_agent_code=True)
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    other = ParameterModel.empty()
    ParameterModel.apply_operation(other, {"op": "upsert_variable", "variable": variable(
        source={"kind": "agent_code", "behavior_id": "behavior"},
    )}, "c")
    with pytest.raises(Har2LoadError):
        ParameterModel.validate(other, capture(), grouping(), allow_native_inputs=True)


@pytest.mark.parametrize("key", ["bad-key", "", "é", "x" * 65, None, True])
def test_seed_does_not_relax_native_key_constraints(key: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        ParameterModel.validate_operation(operation(key=key, initial_value="seed"))
