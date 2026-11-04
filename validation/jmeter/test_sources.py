"""Opt-in real JMeter 5.5/Java 8 UDV and source tests; no HTTP engine/sampling."""

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from har2load.generation_types import NativeGroup, RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.runtime_types import RuntimeSlot, RuntimeTemplate
from tests.test_jmeter_extensions import authored, block
from tests.test_jmeter_runtime import extractor, plan, variable
from tests.test_jmeter_sources import external
from validation.support import run_isolated

type Case = tuple[str, RequestPlan, list[dict[str, Any]]]
CONTROL = '${__groovy(props["h2l.synthetic.executed"]="yes")}'


def sequence(value: Any, *, ok: bool = True) -> list[dict[str, Any]]:
    return [{"event": "iteration", "values": {"token": value}, "states": {"v1": ok},
             "warning": not ok},
            {"event": "pre", "body": {"x": value}, "warning": not ok},
            {"event": "post", "transport": False},
            {"event": "pre", "body": {"x": value}, "warning": not ok},
            {"event": "post", "transport": True}]


def input_cases() -> Iterator[Case]:
    values = ["visible", "ERROR_token", "SYNTHETIC_PRIVATE_SENTINEL", "雪😀\t\r\n<&",
              "\\path\\", CONTROL, "\\${x}\\\\${y}", "\\\\\\${" + CONTROL + "}\\"]
    for index, value in enumerate(values):
        yield f"literal_udv_{index}", plan(external(value)), sequence(value)
    for value_type, text, result in (("integer", "9007199254740993", 9007199254740993),
            ("number", "12345678901234567890.125", "12345678901234567890.125"),
            ("boolean", "false", False), ("boolean", "true", True), ("null", "null", None)):
        candidate = plan({**external(text), "value_type": value_type})
        steps = sequence(result)
        if value_type == "number":
            for step in steps:
                if step["event"] == "pre":
                    step.pop("body")
                    step["raw_body"] = '{"x":' + text + '}'
        yield "typed_" + value_type + "_" + text, candidate, steps


def invalid_cases() -> Iterator[Case]:
    for index, (value_type, text) in enumerate([
        ("string", None), ("string", ""), ("integer", "1.25"), ("integer", "01"),
        ("number", "+1"), ("number", "NaN"), ("number", "1e-65537"),
        ("boolean", '"false"'), ("boolean", "0"), ("null", '"null"'), ("null", None),
        ("null", ""), ("integer", "1 2"), ("string", "é" * 32769), ("number", "[]"),
        ("boolean", "True"), ("integer", "1."), ("integer", "0x1"), ("integer", "--1"),
    ]):
        candidate = plan({**external(text), "value_type": value_type})
        yield f"invalid_{index}", candidate, sequence("ERROR_token", ok=False)
    allowed = external("")
    allowed["source"]["allow_empty"] = True
    yield "known_empty_allowed", plan(allowed), sequence("")
    yield "empty_not_null_even_allowed", plan({**allowed, "value_type": "null"}), sequence(
        "ERROR_token", ok=False)


def snapshot_cases() -> Iterator[Case]:
    candidate = plan(external("initial"))
    steps = sequence("initial")
    steps += [{"event": "iteration", "overwrite": {"token": "overwritten"},
               "values": {"token": "initial"}, "states": {"v1": True}},
              {"event": "pre", "body": {"x": "initial"}}]
    yield "immutable_collision_repeated_lifecycle", candidate, steps
    missing = sequence("ERROR_token", ok=False)
    missing += [missing[0], missing[1]]
    yield "missing_collision_never_becomes_marker_input", plan(external()), missing
    first = {**variable(), "id": "first", "source": {"kind": "literal", "value": "changed"}}
    second = {**external("initial", "token"), "name": "other"}
    candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(first, second)))
    steps = [{"event": "iteration", "values": {"token": "changed", "other": "initial"}},
             {"event": "pre", "body": {"x": "initial"}}]
    yield "snapshot_all_keys_before_any_mark_or_save", candidate, steps


def generated(source: dict[str, Any], *, event: str = "iteration_start") -> RequestPlan:
    declaration = variable({"kind": "generated", **source})
    declaration["value_type"] = "integer" if source["generator"] == "integer" else "string"
    declaration["initialize"] = {"event": event}
    if event == "session_start":
        declaration.update(scope="session", reset="session_start")
    if event in {"before_request", "after_response"}:
        declaration["initialize"]["exchange_id"] = "r1"
    return plan(declaration)


def input_lifecycle_cases() -> Iterator[Case]:
    for event in ("session_start", "iteration_start", "before_request", "after_response"):
        for supplied in (True, False):
            candidate = generated({"generator": "uuid"}, event=event)
            source = external("initial" if supplied else None)["source"]
            declaration = {**candidate.runtime.variables[0], "source": source}
            candidate = replace(candidate, runtime=replace(candidate.runtime,
                variables=(declaration,)))
            native = {"session_start": "iteration", "iteration_start": "iteration",
                      "before_request": "pre", "after_response": "post"}[event]
            steps = [{"event": "iteration"}] if native != "iteration" else []
            for transport in (False, True):
                steps.append({"event": native, "transport": transport,
                    "values": {"token": "initial" if supplied else "ERROR_token"},
                    "states": {"v1": supplied}, "warning": not supplied and (
                        event != "session_start" or not transport)})
            yield f"input_{event}_{supplied}", candidate, steps


def controlled_random_cases() -> Iterator[Case]:
    spec = {"generator": "integer", "minimum": -(2**63), "maximum": 2**63-1}
    steps = [{"event": "iteration"}]
    for fill, value in ((0, -(2**63)), (-1, 2**63-1)):
        steps += [{"event": "iteration", "random_fill": fill, "random_calls": 1,
                   "values": {"token": value}, "states": {"v1": True}, "remember": True},
                  {"event": "pre", "random_calls": 1, "reuse": True}]
    yield "integer_controlled_full_range", generated(spec), steps
    spec.update(minimum=-8, maximum=8)
    yield "integer_rejection_exhaustion", generated(spec), [
        {"event": "iteration"}, {"event": "iteration", "random_fill": -1,
         "random_calls": 128, "values": {"token": "ERROR_token"}, "states": {"v1": False},
         "warning": True}, {"event": "pre", "body": {"x": "ERROR_token"}, "warning": True},
        {"event": "iteration", "random_fill": 0, "random_calls": 1,
         "values": {"token": -8}, "states": {"v1": True}}]


def generated_steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"event": "iteration", "spec": spec, "remember": True},
            {"event": "pre", "reuse": True}, {"event": "pre", "reuse": True},
            {"event": "post", "transport": False, "reuse": True},
            {"event": "post", "transport": True, "reuse": True}]


def generator_cases() -> Iterator[Case]:
    for index, (low, high) in enumerate([(-(2**63), 2**63-1), (-(2**63), -(2**63)),
            (2**63-1, 2**63-1), (-1, 1), (0, 0), (9007199254740993, 9007199254740993)]):
        spec = {"generator": "integer", "minimum": low, "maximum": high}
        yield f"integer_{index}", generated(spec), generated_steps(spec)
    for length, alphabet in ((1, "x"), (4096, "".join(chr(c) for c in range(32, 127)))):
        spec = {"generator": "string", "length": length, "alphabet": alphabet}
        yield f"string_{length}", generated(spec), generated_steps(spec)
    spec = {"generator": "uuid"}
    yield "uuid", generated(spec), generated_steps(spec)
    for offset in (-315360000, 0, 315360000):
        spec = {"generator": "utc_datetime", "offset_seconds": offset, "format": "iso8601"}
        yield f"datetime_{offset}", generated(spec), generated_steps(spec)
    for index, (low, high) in enumerate([("0001-01-01", "9999-12-31"),
            ("2024-02-29", "2024-02-29"), ("0001-01-01", "0001-01-01"),
            ("9999-12-31", "9999-12-31"), ("2024-02-28", "2024-03-01")]):
        spec = {"generator": "utc_date_range", "minimum": low, "maximum": high,
                "format": "iso8601"}
        yield f"date_{index}", generated(spec), generated_steps(spec)


def lifecycle_cases() -> Iterator[Case]:
    for event in ("session_start", "iteration_start", "before_request", "after_response"):
        spec = {"generator": "uuid"}
        native_event = {"session_start": "iteration", "iteration_start": "iteration",
                        "before_request": "pre", "after_response": "post"}[event]
        step = {"event": native_event, "transport": False, "spec": spec, "remember": True}
        steps = [{"event": "iteration"}] if native_event != "iteration" else []
        steps += [step, {"event": native_event, "transport": True,
                        "same" if event == "session_start" else "changed": True}]
        yield "lifecycle_" + event, generated(spec, event=event), steps


def manual_input_cases() -> Iterator[Case]:
    steps = sequence("worker-WORKER")
    steps[0]["overwrite"] = {"token": "worker-WORKER"}
    yield "native_worker_input_isolation", plan(external()), steps
    candidate = plan(external("initial"))
    second = {**external(), "id": "v2", "name": "other"}
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        variables=(*candidate.runtime.variables, second)))
    steps = sequence("initial")
    steps[0]["values"]["other"] = "initial"
    steps[0]["states"]["v2"] = True
    yield "shared_key_absent_plus_provided", candidate, steps


def wrapper_cases() -> Iterator[Case]:
    for source in ({"kind": "external", "key": "seed", "allow_empty": False,
                    "initial_value": "from_udv"},
                   {"kind": "generated", "generator": "integer", "minimum": 7, "maximum": 7}):
        candidate = authored("emit('v1', inputs.seed.toString() + '-output')")
        seed = {**variable(source), "id": "seed", "name": "seed",
                "value_type": "integer" if source["kind"] == "generated" else "string",
                "initialize": {"event": "before_request", "exchange_id": "r1"}}
        runtime = replace(candidate.runtime, variables=(*candidate.runtime.variables, seed),
            blocks=({**candidate.runtime.blocks[0], "inputs": ["seed"]},))
        steps = sequence("7-output" if source["kind"] == "generated" else "from_udv-output")
        steps[0] = {"event": "iteration"}
        yield "wrapper_" + source["kind"], replace(candidate, runtime=runtime), steps


def dispatch_sources() -> Iterator[tuple[str, dict[str, Any], str, int]]:
    yield "literal", {"kind": "literal", "value": "source"}, "string", 0
    yield "external", external("source", "native_seed")["source"], "string", 0
    yield "extracted", {"kind": "extracted", "extractor_id": "e1"}, "string", 0
    for spec, value_type, draws in (
        ({"generator": "integer", "minimum": 0, "maximum": 1}, "integer", 1),
        ({"generator": "string", "length": 3, "alphabet": "ab"}, "string", 3),
        ({"generator": "uuid"}, "string", 4),
        ({"generator": "utc_datetime", "offset_seconds": 0, "format": "iso8601"}, "string", 0),
        ({"generator": "utc_date_range", "minimum": "2024-02-28", "maximum": "2024-02-29",
          "format": "iso8601"}, "string", 1),
    ):
        yield spec["generator"], {"kind": "generated", **spec}, value_type, draws


def request_pair(candidate: RequestPlan) -> RequestPlan:
    first = replace(candidate.runtime.requests[0], body=RuntimeTemplate(('{"unrelated":true}',)))
    second = replace(first, id="r2", body=RuntimeTemplate(('{"x":',
        RuntimeSlot("seed_binding", "v1", "json"), ',"wrapped":',
        RuntimeSlot("output_binding", "out", "json"), '}')))
    requests = (*candidate.requests, replace(candidate.requests[0], id="r2"))
    return replace(candidate, requests=requests,
        groups=(NativeGroup("g1", "Safe", ("r1", "r2")),),
        runtime=replace(candidate.runtime, requests=(first, second)))


def dispatch_plan(source: dict[str, Any], value_type: str, event: str) -> RequestPlan:
    candidate = request_pair(plan())
    placement = {"event": event, "exchange_id": "r2"}
    seed = {**variable(source), "value_type": value_type, "initialize": placement}
    output = {**variable({"kind": "agent_code", "behavior_id": "behavior"}),
              "id": "out", "name": "wrapped", "initialize": placement}
    producer = {**block("emit('out', inputs.v1.toString())"), "inputs": ["v1"],
                "outputs": ["out"], "placement": placement}
    extractors = ({**extractor(), "exchange_id": "r2"},) if source["kind"] == "extracted" else ()
    requests = candidate.runtime.requests
    if extractors:
        requests = (replace(requests[0], body=requests[1].body), requests[1])
    return replace(candidate, runtime=replace(candidate.runtime,
        variables=(seed, output), blocks=(producer,), extractors=extractors, requests=requests))


def dispatch_steps(event: str, source: dict[str, Any], draws: int) -> list[dict[str, Any]]:
    absent = {"uninitialized": ["v1", "out"], "draws": 0}
    steps = [{"event": "observe", "uninitialized": ["v1", "out"]},
        {"event": "iteration", "uninitialized": ["v1", "out"]},
        {"event": "pre", "request": "r1", "tracking_random": True, **absent},
        {"event": "post", "request": "r1", "transport": True, **absent}]
    if event == "after_response":
        steps.append({"event": "pre", "request": "r2", **absent, "warning": True})
    steps.append({"event": "pre" if event == "before_request" else "post", "request": "r2",
        "transport": True, "response": "[source]", "states": {"v1": True, "out": True},
        "draws": draws, "spec": source if source["kind"] == "generated" else None,
        "remember": True, "wrapped": True})
    steps += [{"event": "post", "request": "r1", "transport": True, "draws": draws, "same": True},
              {"event": "pre", "request": "r1", "draws": draws, "same": True}]
    if event == "after_response":
        # A new producer request intentionally resets extracted values; bind on its consumer.
        consumer = "r1" if source["kind"] == "extracted" else "r2"
        steps.append({"event": "pre", "request": consumer, "draws": draws, "wrapped": True})
    return steps


def dispatcher_cases() -> Iterator[Case]:
    for label, source, value_type, draws in dispatch_sources():
        for event in ("before_request", "after_response"):
            if label == "extracted" and event != "after_response":
                continue
            yield f"dispatch_{label}_{event}", dispatch_plan(source, value_type, event), (
                dispatch_steps(event, source, draws))


def session_dispatch_cases() -> Iterator[Case]:
    for label, source, value_type, draws in dispatch_sources():
        if label == "extracted":
            continue
        for event in ("session_start", "iteration_start"):
            seed = {**variable(source), "value_type": value_type, "initialize": {"event": event},
                    "scope": "session" if event == "session_start" else "iteration", "reset": event}
            spec = source if source["kind"] == "generated" else None
            steps = [{"event": "observe", "uninitialized": ["v1"]},
                {"event": "iteration", "states": {"v1": True}, "spec": spec, "remember": True},
                {"event": "pre", "tracking_random": True, "draws": 0, "reuse": True},
                {"event": "post", "transport": True, "draws": 0, "same": True},
                {"event": "iteration", "draws": 0 if event == "session_start" else draws,
                 "states": {"v1": True}, "same": event == "session_start",
                 "spec": spec if event == "iteration_start" else None}]
            yield f"dispatch_{label}_{event}", plan(seed), steps


def reserved_name_gates() -> int:
    count = 0
    for name in ("__jm__DebugTG__idx", "__jmv_SAME_USER"):
        declarations = [external(value, name) for value in (None, "configured")]
        declarations.append({**variable(), "name": name})
        for declaration in declarations:
            build = JMeterGenerator().generate(plan(declaration))
            assert build.status == "blocked" and not build.files
            assert {d.code for d in build.diagnostics} == {"JMETER_NATIVE_NAME_RESERVED"}
            count += 1
    return count


def write_cases(directory: Path) -> Path:
    manifest = []
    for name, candidate, steps in (*input_cases(), *invalid_cases(), *snapshot_cases(),
            *generator_cases(), *lifecycle_cases(), *input_lifecycle_cases(),
            *manual_input_cases(), *controlled_random_cases(), *wrapper_cases(),
            *dispatcher_cases(), *session_dispatch_cases()):
        build = JMeterGenerator().generate(candidate)
        assert build.status == "generated", build.diagnostics
        path = directory / (name + ".jmx")
        path.write_bytes(build.files["jmeter/scenario.jmx"])
        manifest.append({"name": name, "jmx": str(path), "steps": steps})
    path = directory / "cases.json"
    path.write_text(json.dumps(manifest))
    return path


def native_command(manifest: Path) -> list[str]:
    home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    java = Path(os.environ["HAR2LOAD_JAVA8_HOME"]) / "bin/java"
    jars = sorted((home / "lib").glob("*.jar")) + sorted((home / "lib/ext").glob("*.jar"))
    return [str(java), "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
            "-cp", os.pathsep.join(map(str, jars)), "groovy.ui.GroovyMain",
            str(Path(__file__).with_name("SourceHarness.groovy")), str(home), str(manifest)]


def test_native_udv_and_generated_sources(tmp_path: Path) -> None:
    gate_count = reserved_name_gates()
    manifest = write_cases(tmp_path)
    try:
        result = run_isolated(native_command(manifest), tmp_path, timeout=180)
    except Exception as error:
        pytest.fail(f"Source harness failed: {getattr(error, 'stdout', '')}\n"
                    f"{getattr(error, 'stderr', '')}\n{error}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["udv_function_negative_control"] == "passed"
    assert report["controller_collision_control"] == "passed"
    assert report["same_user_namespace_control"] == "passed"
    report["reserved_name_gate_cases"] = gate_count
    assert len(report["cases"]) == len(json.loads(manifest.read_text()))
    assert all(case["status"] == "passed" for case in report["cases"])
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
