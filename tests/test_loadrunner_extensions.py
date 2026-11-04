"""Synthetic opted-in C fixtures; the ordinary suite never compiles or executes them."""

from dataclasses import replace
from hashlib import sha256
from typing import cast

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from har2load.loadrunner_runtime import symbol
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate

ACTION = "loadrunner/Action.c"


def variable(entity: str = "value", kind: str = "string", **changes: JSONValue
             ) -> dict[str, JSONValue]:
    return {"id": entity, "name": entity, "value_type": kind,
            "source": {"kind": "agent_code", "behavior_id": "produce"},
            "initialize": {"event": "before_request", "exchange_id": "producer"},
            "scope": "session", "reset": "session_start", **changes}


def block(source: str = "/* Synthetic output-free body. */", **changes: JSONValue
          ) -> dict[str, JSONValue]:
    return {"id": "block", "target": "loadrunner", "runtime_abi": "scalar_body_v1",
            "behavior_id": "produce", "source": source, "dependencies": [], "depends_on": [],
            "source_digest": sha256(source.encode()).hexdigest(), "inputs": [], "outputs": [],
            "helper_calls": [], "order": 0,
            "placement": {"event": "before_request", "exchange_id": "producer"}, **changes}


def helper(source: str = "/* Synthetic void body. */", **changes: JSONValue
           ) -> dict[str, JSONValue]:
    return {"id": "helper", "target": "loadrunner", "runtime_abi": "scalar_body_v1",
            "source": source, "source_digest": sha256(source.encode()).hexdigest(),
            "dependencies": [], "depends_on": [], "parameters": [], "return_type": "void",
            "namespace": "synthetic", "symbol": "return", **changes}


def authored(source: str = 'h2l_emit_text(frame, "value", "fresh");', kind: str = "string",
             *, variables: tuple[dict[str, JSONValue], ...] | None = None,
             blocks: tuple[dict[str, JSONValue], ...] | None = None,
             helpers: tuple[dict[str, JSONValue], ...] = ()) -> RequestPlan:
    native = tuple(NativeRequest(name, "first" if name == "producer" else "later", "POST",
                                 "https://synthetic.invalid/" + name)
                   for name in ("producer", "consumer"))
    live = tuple(RuntimeRequest(r.id, RuntimeTemplate((r.url,)), (),
                                RuntimeTemplate((RuntimeSlot(r.id + "-body", "value", "json"),)))
                 for r in native)
    model = RuntimePlan((variable(kind=kind),) if variables is None else variables, (), live,
                        (block(source, outputs=["value"]),) if blocks is None else blocks, helpers)
    return RequestPlan(native, (NativeGroup("first", "First", ("producer",)),
                                NativeGroup("later", "Later", ("consumer",))), runtime=model)


def test_opted_body_is_opaque_unicode_preserved_and_metadata_is_public_safe() -> None:
    source = '/* SYNTHETIC_PRIVATE_TEXT é */\nnot valid C;'
    build = LoadRunnerGenerator().generate(authored(source))
    assert build.status == "generated" and source in build.files[ACTION].decode()
    assert build.metadata["profile"] == "authored_scalar_runtime_v1"
    assert build.metadata["native_status_policy"] == "markers_only_native_status_v1"
    assert build.metadata["output_commit_policy"] == "per_output_validated_v1"
    assert "SYNTHETIC_PRIVATE_TEXT" not in str(build.metadata)
    assert build.metadata["native_validation"] == "not_run"


@pytest.mark.parametrize("kind", ["blocks", "helpers"])
@pytest.mark.parametrize("change,code", [({"runtime_abi": None}, "ABI_REQUIRED"),
    ({"dependencies": [{"path": "never-read"}]}, "DEPENDENCY_UNSUPPORTED")])
def test_direct_generator_checks_every_declaration(kind: str, change: dict[str, JSONValue],
                                                  code: str) -> None:
    plan = authored()
    assert plan.runtime is not None
    records = (block(outputs=["value"], **change),) if kind == "blocks" else (helper(**change),)
    build = LoadRunnerGenerator().generate(replace(plan, runtime=replace(
        plan.runtime, **{kind: records})))
    assert build.status == "blocked" and not build.files
    assert any(d.code == "GENERATION_EXTENSION_" + code for d in build.diagnostics)


def test_missing_producer_and_other_target_source_do_not_enable_agent_code() -> None:
    plan = authored(blocks=(block(target="jmeter", outputs=["value"]),))
    build = LoadRunnerGenerator().generate(plan)
    assert build.status == "blocked"
    assert any(d.code == "GENERATION_EXTENSION_PRODUCER_UNSUPPORTED" for d in build.diagnostics)


def test_agent_code_initializer_is_never_scheduled_and_phase_order_is_preserved() -> None:
    second = block(id="after", outputs=[],
                   placement={"event": "after_response", "exchange_id": "producer"})
    plan = authored(blocks=(block(outputs=["value"]), second))
    source = LoadRunnerGenerator().generate(plan).files[ACTION].decode()
    source = source.split("int Action(void)")[1]
    assert symbol("INIT_FN", "value") not in source
    assert source.index(symbol("BLOCK_RUN", "block")) < source.index("h2l_slot(")
    assert source.index('"REQ_0001"') < source.index(symbol("BLOCK_RUN", "after"))
    assert source.index(symbol("BLOCK_RUN", "after")) < source.index('"REQ_0002"')


def test_every_declaration_has_unique_real_map_including_unused_helpers() -> None:
    helpers = (helper(id="one"), helper(id="two", depends_on=["one"]))
    plan = authored(blocks=(block(outputs=["value"], helper_calls=["one", "two"]),),
                    helpers=helpers)
    build = LoadRunnerGenerator().generate(plan)
    assert build == LoadRunnerGenerator().generate(plan)
    for row in build.source_map:
        artifact, marker = cast(str, row["artifact"]), cast(str, row["marker"])
        assert build.files[artifact].count(marker.encode()) == 1
    calls = [row for row in build.source_map if row["kind"] == "helper_call"]
    assert {row["helper_id"] for row in calls} == {"one", "two"}
    assert all(row["entity_id"] == "block" for row in calls)
    assert all(row["callsite_status"] == "declared_not_execution_proof" for row in calls)


def test_helper_only_and_output_free_blocks_render_without_executing_source() -> None:
    plan = authored(variables=(), blocks=(block(),), helpers=(helper(),))
    assert plan.runtime is not None
    requests = tuple(replace(r, body=None) for r in plan.runtime.requests)
    model = replace(plan.runtime, requests=requests)
    build = LoadRunnerGenerator().generate(replace(plan, runtime=model))
    assert build.status == "generated"
    assert b"static int return(" not in build.files[ACTION]
    assert {row["kind"] for row in build.source_map} >= {
        "code_block", "reset_outputs", "helper_definition"}
    helper_only = replace(model, blocks=())
    assert LoadRunnerGenerator().generate(replace(plan, runtime=helper_only)).status == "generated"
