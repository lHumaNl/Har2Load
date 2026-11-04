"""Opt-in synthetic authored bodies on pinned native APIs, with network denied."""

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from har2load.generation_types import RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.runtime_types import RuntimeTemplate
from tests.test_jmeter_extensions import authored, block, helper
from validation.support import run_isolated

type Case = tuple[str, RequestPlan, list[dict[str, Any]]]


def typed(source: str, value_type: str = "string") -> RequestPlan:
    candidate = authored(source)
    runtime = candidate.runtime
    assert runtime is not None
    return replace(candidate, runtime=replace(runtime, variables=(
        {**runtime.variables[0], "value_type": value_type},)))


def steps(body: str, *, warning: bool = False) -> list[dict[str, Any]]:
    return [{"event": "iteration"},
            {"event": "pre", "body": '{"x":' + body + '}', "warning": warning},
            {"event": "post", "transport": False},
            {"event": "pre", "body": '{"x":' + body + '}', "warning": warning},
            {"event": "post", "transport": True}]


def scalar_cases() -> Iterator[Case]:
    values = [("string", "'literal'", '"literal"'),
              ("integer", "9007199254740993G", "9007199254740993"),
              ("number", "9007199254740993.123456789G", "9007199254740993.123456789"),
              ("integer", "7L", "7"), ("integer", "(short) 8", "8"),
              ("integer", "(byte) 9", "9"), ("integer", "3", "3"),
              ("boolean", "false", "false"), ("null", "null", "null"),
              ("string", "'ERROR_token'", '"ERROR_token"'),
              ("string", "'${literal}<&\\\\'", '"${literal}<&\\\\"')]
    for index, (value_type, value, rendered) in enumerate(values):
        sequence = steps(rendered)
        sequence[1]["states"] = {"v1": True}
        yield f"scalar_{index}", typed(f"emit('v1', {value})", value_type), sequence
    invalid = [("string", "3"), ("integer", "'3'"), ("integer", "1.25G"),
               ("number", "Double.NaN"), ("number", "1.5d"), ("string", "null"),
               ("string", "'x' * 65537"), ("string", "'\\u00e9' * 32769"),
               ("string", "'\\ud800'"), ("number", "new BigDecimal('1e-65537')"),
               ("string", "['nested']"), ("string", '"value ${3}"')]
    for index, (value_type, value) in enumerate(invalid):
        sequence = steps('"ERROR_token"', warning=True)
        sequence[1]["states"] = {"v1": False}
        yield f"invalid_{index}", typed(f"emit('v1', {value})", value_type), sequence


def two_outputs(source: str) -> RequestPlan:
    candidate = authored(source)
    runtime = candidate.runtime
    assert runtime is not None
    second = {**runtime.variables[0], "id": "v2", "name": "other", "value_type": "integer"}
    return replace(candidate, runtime=replace(runtime, variables=(*runtime.variables, second),
        blocks=({**block(source), "outputs": ["v1", "v2"]},)))


def failure_cases() -> Iterator[Case]:
    for name, source in (
        ("missing", "// no output"), ("handled_exception", "throw new Exception('fixture')"),
        ("compile_failure", "this is not valid groovy !!!!"),
        ("undeclared_emit", "emit('missing', 'bad')"),
    ):
        yield name, authored(source), steps('"ERROR_token"', warning=True)
    sources = ["emit('v1', 'good'); emit('v2', 'wrong'); emit('v1', 'fresh')",
               "emit('v1', 'fresh'); throw new Exception('later')",
               "emit('v1', 'fresh')"]
    for index, source in enumerate(sources):
        sequence = steps('"fresh"', warning=True)
        sequence[1]["values"] = {"token": "fresh", "other": "ERROR_other"}
        yield f"partial_{index}", two_outputs(source), sequence
    source = "if (context.vars.get('again') == null) emit('v1', 'first'); " + (
        "context.vars.put('again', 'yes')")
    sequence = steps('"first"')
    sequence[3].update(body='{"x":"ERROR_token"}', warning=True)
    yield "repeat_no_stale", authored(source), sequence
    sequence = steps('"ERROR_token"', warning=True)
    sequence[1]["values"] = {"token": "ERROR_token", "other": "7"}
    yield "replacement_invalidates_only_same_slot", two_outputs(
        "emit('v1', 'valid'); emit('v2', 7); emit('v1', 9)"), sequence
    sequence = steps('"fresh"', warning=True)
    sequence[1]["values"] = {"token": "fresh", "other": "7"}
    yield "unknown_output_preserves_published_slots", two_outputs(
        "emit('v1', 'fresh'); emit('v2', 7); emit('absent', 'bad')"), sequence


def helper_cases() -> Iterator[Case]:
    for name, source, body in (
        ("helper_success", "emit('v1', helpers.helper('fresh'))", '"fresh"'),
        ("helper_arity", "emit('v1', helpers.helper())", '"ERROR_token"'),
        ("helper_type", "emit('v1', helpers.helper(3))", '"ERROR_token"'),
        ("helper_null", "emit('v1', helpers.helper(null))", '"ERROR_token"'),
        ("helper_undeclared", "emit('v1', helpers.absent('x'))", '"ERROR_token"'),
    ):
        candidate = authored(source)
        candidate = replace(candidate, runtime=replace(candidate.runtime,
            blocks=({**block(source), "helper_calls": ["helper"]},), helpers=(helper(),)))
        yield name, candidate, steps(body, warning=body == '"ERROR_token"')
    for name, source in (("helper_throw", "throw new Exception('helper failure')"),
                         ("helper_return_type", "return 3"),
                         ("helper_compile", "not valid !!!!")):
        candidate = authored("emit('v1', helpers.helper('fresh'))")
        candidate = replace(candidate, runtime=replace(candidate.runtime,
            blocks=({**candidate.runtime.blocks[0], "helper_calls": ["helper"]},),
            helpers=(helper(source=source),)))
        yield name, candidate, steps('"ERROR_token"', warning=True)
    candidate = authored("emit('v1', helpers.outer(context.vars.get('worker')))")
    outer = {**helper("outer", "return helpers.inner(inputs.value)"), "depends_on": ["inner"]}
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        helpers=(helper("inner"), outer),
        blocks=({**candidate.runtime.blocks[0], "helper_calls": ["outer"]},)))
    sequence = steps('"WORKER"')
    yield "helper_dependencies_thread_isolation", candidate, sequence
    candidate = two_outputs("emit('v1', 'fresh'); helpers.helper('bad')")
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        helpers=(helper(source="throw new Exception('fixture')"),),
        blocks=({**candidate.runtime.blocks[0], "helper_calls": ["helper"]},)))
    sequence = steps('"fresh"', warning=True)
    sequence[1]["values"] = {"token": "fresh", "other": "ERROR_other"}
    yield "helper_failure_keeps_prior_outputs", candidate, sequence


def typed_helper_cases() -> Iterator[Case]:
    values = [("integer", "7L", "7"), ("number", "1.25G", "1.25"),
              ("boolean", "false", "false"), ("null", "null", "null")]
    for value_type, value, rendered in values:
        source = f"emit('v1', helpers.helper({value}))"
        candidate = typed(source, value_type)
        declaration = {**helper(), "parameters": [{"name": "value", "value_type": value_type}],
                       "return_type": value_type}
        candidate = replace(candidate, runtime=replace(candidate.runtime, helpers=(declaration,),
            blocks=({**candidate.runtime.blocks[0], "helper_calls": ["helper"]},)))
        yield "typed_helper_" + value_type, candidate, steps(rendered)
    for name, body, rendered in (("void_helper", "return null", '"fresh"'),
                                 ("invalid_void_helper", "return 'bad'", '"ERROR_token"')):
        candidate = authored("helpers.helper(); emit('v1', 'fresh')")
        declaration = {**helper(source=body), "parameters": [], "return_type": "void"}
        candidate = replace(candidate, runtime=replace(candidate.runtime, helpers=(declaration,),
            blocks=({**candidate.runtime.blocks[0], "helper_calls": ["helper"]},)))
        yield name, candidate, steps(rendered, warning=name.startswith("invalid"))


def phase_cases() -> Iterator[Case]:
    candidate = authored("emit('v1', inputs.seed + '-output')")
    runtime = candidate.runtime
    seed = {**runtime.variables[0], "id": "seed", "name": "seed",
            "source": {"kind": "literal", "value": "baseline"}}
    runtime = replace(runtime, variables=(seed, *runtime.variables),
                      blocks=({**runtime.blocks[0], "inputs": ["seed"]},))
    yield "baseline_before_blocks", replace(candidate, runtime=runtime), steps('"baseline-output"')
    broken = {**seed, "source": {"kind": "literal", "value": 3}}
    runtime = replace(runtime, variables=(broken, runtime.variables[1]))
    yield "failed_inputs_skip", replace(candidate, runtime=runtime), steps(
        '"ERROR_token"', warning=True)
    candidate = two_outputs("emit('v1', 'first')")
    runtime = candidate.runtime
    first = {**runtime.blocks[0], "outputs": ["v1"]}
    second = {**block("emit('v2', inputs.v1.length())"), "id": "second", "order": 1,
              "behavior_id": "second", "inputs": ["v1"], "outputs": ["v2"]}
    output = {**runtime.variables[1], "source": {"kind": "agent_code", "behavior_id": "second"}}
    runtime = replace(runtime, variables=(runtime.variables[0], output), blocks=(first, second))
    sequence = steps('"first"')
    sequence[1]["values"] = {"token": "first", "other": "5"}
    yield "same_event_chain", replace(candidate, runtime=runtime), sequence


def after_response_case() -> Case:
    candidate = authored("emit('v1', inputs.seed + '-post')")
    runtime = candidate.runtime
    placement = {"event": "after_response", "exchange_id": "r1"}
    seed = {**runtime.variables[0], "id": "seed", "name": "seed", "initialize": placement,
            "source": {"kind": "extracted", "extractor_id": "extract"}}
    extractor = {"id": "extract", "exchange_id": "r1", "area": "response_body",
        "selector": {"dialect": "json_path_subset_v1", "expression": "$.seed"},
        "cardinality": "exactly_one", "allow_empty": False, "on_failure": "mark_error_and_continue"}
    runtime = replace(runtime, variables=(seed, {**runtime.variables[0], "initialize": placement}),
        extractors=(extractor,), blocks=({**runtime.blocks[0], "inputs": ["seed"],
                                         "placement": placement},))
    sequence = [{"event": "iteration"}, {"event": "post", "transport": True,
        "response": '{"seed":"recorded"}', "values": {"token": "recorded-post"}},
        {"event": "pre", "body": '{"x":"recorded-post"}'},
        {"event": "post", "transport": False, "response": '{"seed":"ignored"}',
         "values": {"token": "ERROR_token"}, "warning": True}]
    return "extract_before_blocks_status_snapshot", replace(candidate, runtime=runtime), sequence


def empty_cases() -> Iterator[Case]:
    candidate = authored("context.vars.put('output_free', 'ran')")
    runtime = candidate.runtime
    request = replace(runtime.requests[0], body=RuntimeTemplate(('"static"',)))
    runtime = replace(runtime, variables=(), requests=(request,),
                      blocks=({**runtime.blocks[0], "outputs": []},))
    yield "output_free", replace(candidate, runtime=runtime), [
        {"event": "iteration"}, {"event": "pre", "body": '"static"',
                                 "values": {"output_free": "ran"}}]
    runtime = replace(runtime, blocks=(), helpers=(helper(),))
    yield "helper_only", replace(candidate, runtime=runtime), [
        {"event": "iteration"}, {"event": "pre", "body": '"static"'}]


def write_cases(directory: Path) -> Path:
    manifest = []
    cases = [*scalar_cases(), *failure_cases(), *helper_cases(), *typed_helper_cases(),
             *phase_cases(), after_response_case(), *empty_cases()]
    for name, candidate, sequence in cases:
        artifact = JMeterGenerator().generate(candidate)
        assert artifact.status == "generated", artifact.diagnostics
        path = directory / (name + ".jmx")
        path.write_bytes(artifact.files["jmeter/scenario.jmx"])
        manifest.append({"name": name, "jmx": str(path), "steps": sequence})
    path = directory / "cases.json"
    path.write_text(json.dumps(manifest))
    return path


def native_command(manifest: Path) -> list[str]:
    java_home = Path(os.environ["HAR2LOAD_JAVA8_HOME"])
    jmeter_home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    jars = sorted((jmeter_home / "lib").glob("*.jar"))
    jars += sorted((jmeter_home / "lib/ext").glob("*.jar"))
    return [str(java_home / "bin/java"), "-Xmx512m", "-Djava.awt.headless=true",
            "-Dfile.encoding=UTF-8", "-cp", os.pathsep.join(map(str, jars)),
            "groovy.ui.GroovyMain", str(Path(__file__).with_name("WrapperHarness.groovy")),
            str(jmeter_home), str(manifest)]


def test_authored_wrappers_on_native_jmeter(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    try:
        result = run_isolated(native_command(manifest), tmp_path, timeout=180)
    except Exception as error:
        pytest.fail(f"Native wrapper harness failed: {getattr(error, 'stdout', '')}\n"
                    f"{getattr(error, 'stderr', '')}\n{error}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(case["status"] == "passed" for case in report["cases"])
    assert len(report["cases"]) == len(json.loads(manifest.read_text()))
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
