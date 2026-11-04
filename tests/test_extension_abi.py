"""Explicit runtime ABI declarations never parse, execute or reinterpret source."""

from copy import deepcopy

import pytest
from test_extensions import block, grouping, helper, model, obj, parameters, records

from har2load.domain import Capture, Exchange, Har2LoadError, JSONValue
from har2load.extensions import TargetExtensions
from har2load.scenario import ChangeSet, ScenarioEditor

ABI = "scalar_body_v1"
EDITOR = ScenarioEditor()
CAPTURE = Capture("cap", "digest", b"", (Exchange("ex_a", 0, {}), Exchange("ex_b", 1, {})))


def initial(version: str = "1.3") -> dict[str, JSONValue]:
    return EDITOR.initial(CAPTURE, grouping(), schema_version=version)


def apply(scenario: dict[str, JSONValue], *operations: JSONValue) -> dict[str, JSONValue]:
    change = ChangeSet.parse({
        "schema_version": "1.0", "change_id": "abi-edit", "base_revision": scenario["revision"],
        "author": "synthetic-author", "rationale": "Explicit versioned wrapper contract",
        "evidence": [{"exchange_id": "ex_a", "location": ""}], "operations": list(operations),
    })
    return EDITOR.apply(scenario, CAPTURE, grouping(), change)


def test_upgrade_preserves_every_opaque_field_and_old_model() -> None:
    original = initial()
    original["target_extensions"] = model(block(helper_calls=["h"]), helper())
    before = deepcopy(original)
    upgraded = apply(original, {"op": "upgrade_extension_runtime"})
    assert upgraded == {**before, "schema_version": "1.4", "revision": 2}
    assert original == before
    EDITOR.validate(original, CAPTURE, grouping())
    EDITOR.validate(upgraded, CAPTURE, grouping())


@pytest.mark.parametrize("kind", ["block", "helper"])
@pytest.mark.parametrize("target,language", [("jmeter", "groovy"), ("loadrunner", "c")])
def test_opt_in_requires_upgrade_before_upsert(kind: str, target: str, language: str) -> None:
    definition = block(runtime_abi=ABI) if kind == "block" else helper(runtime_abi=ABI)
    definition.update(target=target, language=language)
    operation: dict[str, JSONValue] = {
        "op": "upsert_code_block" if kind == "block" else "upsert_helper", kind: definition,
    }
    original = initial()
    with pytest.raises(Har2LoadError) as error:
        apply(original, operation)
    assert error.value.code == "SCENARIO_UPGRADE_REQUIRED"
    with pytest.raises(Har2LoadError):
        apply(original, operation, {"op": "upgrade_extension_runtime"})
    result = apply(original, {"op": "upgrade_extension_runtime"}, operation)
    assert records(obj(result["target_extensions"]), kind + "s")[0]["runtime_abi"] == ABI
    assert original == initial()


@pytest.mark.parametrize("kind", ["block", "helper"])
@pytest.mark.parametrize("abi", [None, "", "scalar_body_v2", "opaque", True, 1, [], {}])
def test_unknown_or_wrong_type_abi_is_rejected(kind: str, abi: JSONValue) -> None:
    definition = block(runtime_abi=abi) if kind == "block" else helper(runtime_abi=abi)
    with pytest.raises(Har2LoadError):
        model(definition)


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2", "1.4"])
def test_upgrade_only_accepts_schema_13(version: str) -> None:
    with pytest.raises(Har2LoadError):
        apply(initial(version), {"op": "upgrade_extension_runtime"})


def test_three_upgrades_share_one_revision() -> None:
    result = apply(initial("1.1"), {"op": "upgrade_parameters"}, {"op": "upgrade_extensions"},
                   {"op": "upgrade_extension_runtime"},
                   {"op": "upsert_code_block", "block": block(runtime_abi=ABI)})
    assert result["schema_version"] == "1.4" and result["revision"] == 2


def test_upgrade_operation_rejects_extra_fields() -> None:
    with pytest.raises(Har2LoadError):
        apply(initial(), {"op": "upgrade_extension_runtime", "runtime_abi": ABI})


@pytest.mark.parametrize("kind", ["block", "helper"])
def test_abi_only_edit_keeps_source_digest_and_can_remove_opt_in(kind: str) -> None:
    definition = block() if kind == "block" else helper()
    operation = "upsert_code_block" if kind == "block" else "upsert_helper"
    original = initial("1.4")
    original["target_extensions"] = model(definition)
    result = apply(original, {"op": operation, kind: {**definition, "runtime_abi": ABI}})
    old = records(obj(original["target_extensions"]), kind + "s")[0]
    edited = records(obj(result["target_extensions"]), kind + "s")[0]
    assert edited == {**old, "runtime_abi": ABI, "change_id": "abi-edit"}
    restored = apply(result, {"op": operation, kind: definition})
    assert "runtime_abi" not in records(obj(restored["target_extensions"]), kind + "s")[0]
    assert "runtime_abi" not in old


@pytest.mark.parametrize("kind", ["block", "helper"])
def test_stored_abi_is_forbidden_before_schema_14(kind: str) -> None:
    definition = block(runtime_abi=ABI) if kind == "block" else helper(runtime_abi=ABI)
    operation = "upsert_code_block" if kind == "block" else "upsert_helper"
    scenario = apply(initial("1.4"), {"op": operation, kind: definition})
    scenario["schema_version"] = "1.3"
    with pytest.raises(Har2LoadError):
        EDITOR.validate(scenario, CAPTURE, grouping())


def test_plan_exposes_only_explicit_opt_in_without_readiness_claim() -> None:
    scenario = apply(initial("1.4"),
                     {"op": "upsert_code_block", "block": block(runtime_abi=ABI)},
                     {"op": "upsert_helper", "helper": helper(runtime_abi=ABI)})
    extensions = obj(scenario["target_extensions"])
    plan = TargetExtensions.plan(extensions, grouping())
    target = obj(obj(plan["targets"])["jmeter"])
    assert all(records(target, kind)[0]["runtime_abi"] == ABI for kind in ("blocks", "helpers"))
    assert plan["code_semantics"] == "unverified"
    assert scenario["native_validation"] == scenario["execution"] == "not_run"
    assert any(item.code == "EXTENSION_CODE_UNVERIFIED"
               for item in TargetExtensions.diagnostics(extensions, parameters()))
    legacy = obj(obj(TargetExtensions.plan(model(block(), helper()), grouping())["targets"])[
        "jmeter"])
    assert all("runtime_abi" not in records(legacy, kind)[0] for kind in ("blocks", "helpers"))


def test_opt_in_does_not_parse_compile_execute_or_read_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Declaration preparation attempted execution or resource access")

    with monkeypatch.context() as patch:
        for name in ("builtins.open", "builtins.eval", "builtins.exec", "builtins.compile",
                     "socket.socket", "subprocess.run", "os.system", "os.getenv"):
            patch.setattr(name, forbidden)
        scenario = apply(initial("1.4"), {"op": "upsert_code_block", "block": block(
            runtime_abi=ABI, source="not { valid code !!! é\r\n",
            dependencies=[{"kind": "file", "path": "not-present"}])},
            {"op": "upsert_helper", "helper": helper(runtime_abi=ABI, source="invalid {{{")})
        TargetExtensions.plan(obj(scenario["target_extensions"]), grouping())


def test_low_level_api_requires_explicit_runtime_permission() -> None:
    operation: dict[str, JSONValue] = {"op": "upsert_code_block", "block": block(runtime_abi=ABI)}
    extensions = TargetExtensions.empty()
    assert TargetExtensions.validate_operation(operation)
    with pytest.raises(Har2LoadError):
        TargetExtensions.apply_operation(extensions, operation, "opt-in")
    assert extensions == TargetExtensions.empty()
    TargetExtensions.apply_operation(extensions, operation, "opt-in", allow_runtime_abi=True)
    with pytest.raises(Har2LoadError):
        TargetExtensions.validate(extensions, parameters(), CAPTURE, grouping())
    TargetExtensions.validate(extensions, parameters(), CAPTURE, grouping(), allow_runtime_abi=True)


@pytest.mark.parametrize("kind", ["blocks", "helpers"])
def test_unknown_stored_abi_cannot_bypass_validation_or_planning(kind: str) -> None:
    extensions = model(block(), helper())
    records(extensions, kind)[0]["runtime_abi"] = "scalar_body_v2"
    with pytest.raises(Har2LoadError):
        TargetExtensions.validate(extensions, parameters(), CAPTURE, grouping(),
                                  allow_runtime_abi=True)
    with pytest.raises(Har2LoadError):
        TargetExtensions.plan(extensions, grouping())
    with pytest.raises(Har2LoadError):
        TargetExtensions.diagnostics(extensions, parameters())
