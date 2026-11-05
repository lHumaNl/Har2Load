"""Opt-in JMeter 5.5/Java 8 normalized-header fixtures; no HTTP sampling or wire claim."""

import json
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from har2load.generation_types import RequestPlan
from har2load.jmeter import JMeterGenerator
from tests.test_jmeter_extensions import block, helper
from tests.test_jmeter_headers import headers
from tests.test_jmeter_runtime import correlation, extractor
from validation.header_cases import header_cases
from validation.jmeter.test_sources import native_command as source_command
from validation.support import run_isolated

type Step = dict[str, Any]
type Case = tuple[str, RequestPlan, list[Step]]
CONTROL = '${__groovy(props["h2l.header.executed"]="yes")}'


def sequence(text: str | None, value: str, *, ok: bool = True,
             transport: bool = True, **extra: Any) -> list[Step]:
    return [{"event": "iteration"}, {"event": "pre", "request": "r1"},
        {"event": "post", "request": "r1", "headers": text, "transport": transport,
         "values": {"token": value}, "states": {"v1": ok}, "warning": not ok,
         "header_reads": int(transport), **extra},
        {"event": "pre", "request": "r2", "body": {"x": value}, "warning": not ok},
        {"event": "post", "request": "r2", "headers": "invalid unrelated headers",
         "transport": False, "values": {"token": value}, "states": {"v1": ok},
         "header_reads": 0}]


def shared_cases() -> Iterator[Case]:
    for case in header_cases():
        candidate = headers(case["selector"])
        definition = {**candidate.runtime.extractors[0], "allow_empty": case["allow_empty"],
            "cardinality": "exactly_one" if case["exact"] else "occurrence",
            "occurrence": case["occurrence"]}
        candidate = replace(candidate, runtime=replace(candidate.runtime, extractors=(definition,)))
        value = case["value"] if case["ok"] else "ERROR_token"
        yield "shared_" + case["name"], candidate, sequence(case["text"], value, ok=case["ok"])


def lifecycle_cases() -> Iterator[Case]:
    for scope in ("session", "iteration"):
        steps = sequence("X-Token: first-WORKER", "first-WORKER")
        steps += [{"event": "iteration", "values": {
            "token": "first-WORKER" if scope == "session" else "ERROR_token"}},
            {"event": "pre", "request": "r1", "values": {"token": "ERROR_token"}}]
        steps += sequence("X-Other: missing", "ERROR_token", ok=False)[2:]
        steps += sequence("X-Token: recovered-WORKER", "recovered-WORKER")[1:]
        yield "lifecycle_" + scope, headers(scope=scope), steps
    steps = sequence("X-Token: retained-WORKER", "retained-WORKER")
    steps.append({"event": "pre", "request": "r2", "body": {"x": "retained-WORKER"}})
    yield "producer_unrelated_failed_response_consumer", headers(), steps


def availability_cases() -> Iterator[Case]:
    yield "native_failed_headers_present", headers(), sequence(
        "X-Token: fresh", "ERROR_token", ok=False, transport=False)
    yield "null_native_result", headers(), sequence(
        "X-Token: fresh", "ERROR_token", ok=False, missing_result=True, header_reads=0)
    yield "null_native_headers", headers(), sequence(None, "ERROR_token", ok=False)
    yield "header_only_invalid_utf8_body", headers(), sequence(
        "X-Token: independent", "independent", response_b64="/w==")
    yield "header_only_non_utf8_body_charset", headers(), sequence(
        "X-Token: independent", "independent", charset="ISO-8859-1", response="ignored")
    steps = sequence("invalid headers\r", "body", response='{"a.b":["body"]}')
    steps[2]["header_reads"] = 0
    yield "body_only_ignores_invalid_headers", correlation(), steps
    yield "function_looking_value", headers(), sequence("X-Token: " + CONTROL, CONTROL)


def typed_cases() -> Iterator[Case]:
    for value_type, text in (("integer", "001"), ("number", "1"),
                             ("boolean", "false"), ("null", "null")):
        candidate = headers()
        value = {**candidate.runtime.variables[0], "value_type": value_type}
        candidate = replace(candidate, runtime=replace(candidate.runtime, variables=(value,)))
        yield "typed_header_" + value_type, candidate, sequence(
            "X-Token: " + text, "ERROR_token", ok=False)


def two_sources(*, body: bool = False, reverse: bool = False) -> RequestPlan:
    candidate = headers()
    runtime = candidate.runtime
    second = {**runtime.variables[0], "id": "v2", "name": "other",
              "source": {"kind": "extracted", "extractor_id": "e2"}}
    definition = ({**extractor(), "selector": {"dialect": "boundary_v1", "left": "[",
                    "right": "]"}} if body else {**runtime.extractors[0],
                    "selector": {"dialect": "name_v1", "name": "X-Other"}})
    declarations = (runtime.extractors[0], {**definition, "id": "e2"})
    return replace(candidate, runtime=replace(runtime, variables=(*runtime.variables, second),
        extractors=tuple(reversed(declarations)) if reverse else declarations))


def mixed_steps(text: str, token: str, other: str, *, good: tuple[bool, bool],
                **extra: Any) -> list[Step]:
    steps = sequence(text, token, ok=good[0], **extra)
    for index in (2, 4):
        steps[index].update(values={"token": token, "other": other},
                            states={"v1": good[0], "v2": good[1]})
    steps[2]["warning"] = not all(good)
    return steps


def mixed_cases() -> Iterator[Case]:
    for reverse in (False, True):
        candidate = two_sources(body=True, reverse=reverse)
        yield f"mixed_bad_body_{reverse}", candidate, mixed_steps(
            "X-Token: good", "good", "ERROR_other", good=(True, False), response_b64="/w==")
        yield f"mixed_bad_headers_{reverse}", candidate, mixed_steps(
            "invalid\r", "ERROR_token", "body", good=(False, True), response="[body]")
        definitions = tuple({**e, "selector": {"dialect": "json_path_subset_v1",
            "expression": "$.id"}} if e["area"] == "response_body" else e
            for e in candidate.runtime.extractors)
        json_candidate = replace(candidate, runtime=replace(
            candidate.runtime, extractors=definitions))
        yield f"mixed_bad_body_json_{reverse}", json_candidate, mixed_steps(
            "X-Token: good", "good", "ERROR_other", good=(True, False), response="invalid json")


def two_header_cases() -> Iterator[Case]:
    for text, token, other, good in (
        ("X-Token: good", "good", "ERROR_other", (True, False)),
        ("X-Token: good\nX-Other: 雪", "good", "ERROR_other", (True, False)),
        ("X-Token: 雪\nX-Other: good", "ERROR_token", "good", (False, True)),
        ("X-Token: " + "a" * 65537 + "\nX-Other: good", "ERROR_token", "good", (False, True)),
        ("invalid", "ERROR_token", "ERROR_other", (False, False)),
    ):
        yield "two_headers_" + str(len(text)) + "_" + token[:8], two_sources(), mixed_steps(
            text, token, other, good=good)


def second_producer_case() -> Case:
    candidate = headers()
    runtime = candidate.runtime
    definition = {**runtime.extractors[0], "exchange_id": "r2"}
    value = {**runtime.variables[0], "initialize": {"event": "after_response", "exchange_id": "r2"}}
    requests = (replace(runtime.requests[0], body=runtime.requests[1].body),
                replace(runtime.requests[1], body=runtime.requests[0].body))
    candidate = replace(candidate, runtime=replace(runtime, variables=(value,),
        extractors=(definition,), requests=requests))
    steps = sequence("X-Token: second-WORKER", "second-WORKER")
    for step in steps:
        if "request" in step:
            step["request"] = "r2" if step["request"] == "r1" else "r1"
    return "explicit_second_request_producer", candidate, steps


def wrapper_case() -> Case:
    candidate = headers()
    placement = {"event": "after_response", "exchange_id": "r1"}
    output = {**candidate.runtime.variables[0], "id": "out", "name": "wrapped",
              "source": {"kind": "agent_code", "behavior_id": "behavior"}}
    producer = {**block("emit('out', helpers.helper(inputs.v1))"), "inputs": ["v1"],
        "outputs": ["out"], "placement": placement, "helper_calls": ["helper"]}
    candidate = replace(candidate, runtime=replace(candidate.runtime,
        variables=(*candidate.runtime.variables, output), blocks=(producer,), helpers=(helper(),)))
    steps = sequence("X-Token: header-WORKER", "header-WORKER")
    for index in (2, 4):
        steps[index]["values"]["wrapped"] = "header-WORKER"
        steps[index]["states"]["out"] = True
    return "header_consumed_by_code_and_helper", candidate, steps


def edge_cases() -> Iterator[Case]:
    for index, text in enumerate(("\n\n", "HTTP/1.1 600 Bad\nX-Token: a",
            "HTTP/1.1 200\tOK\nX-Token: a", "HTTP/1.1 200 雪\nX-Token: a",
            "X-Token: a\n \n", "X-Other: \ud800\nX-Token: a")):
        yield f"framing_extra_{index}", headers(), sequence(text, "ERROR_token", ok=False)
    yield "trailing_blank_lines", headers(), sequence("X-Token: a\n\n\n", "a")
    yield "unselected_occurrence_unicode", headers(), sequence("X-Other: 雪\nX-Token: a", "a")
    yield "unselected_large_value", headers(), sequence("X-Other: " + "a" * 65537 +
        "\nX-Token: good", "good")
    yield "block_utf8_byte_budget", headers(), sequence("X-Other: " + "雪" * 349526 +
        "\nX-Token: good", "ERROR_token", ok=False)


def bound_cases() -> Iterator[Case]:
    prefix, suffix = "X-Other: ", "\nX-Token: good"
    text = prefix + "a" * (1024 * 1024 - len(prefix + suffix)) + suffix
    yield "capture_at_byte_limit", headers(), sequence(text, "good")
    candidate = headers()
    definition = {**candidate.runtime.extractors[0], "occurrence": 2, "cardinality": "occurrence"}
    candidate = replace(candidate, runtime=replace(candidate.runtime, extractors=(definition,)))
    yield "other_occurrence_unicode", candidate, sequence("X-Token: 雪\nX-Token: good", "good")
    huge = {**definition, "occurrence": 10**30}
    candidate = replace(candidate, runtime=replace(candidate.runtime, extractors=(huge,)))
    yield "huge_occurrence_not_truncated", candidate, sequence(
        "X-Token: a", "ERROR_token", ok=False)


def write_cases(directory: Path) -> Path:
    manifest = []
    for name, candidate, steps in (*shared_cases(), *lifecycle_cases(), *availability_cases(),
            *typed_cases(), *mixed_cases(), *two_header_cases(), second_producer_case(),
            wrapper_case(), *edge_cases(), *bound_cases()):
        build = JMeterGenerator().generate(candidate)
        assert build.status == "generated", build.diagnostics
        path = directory / (name + ".jmx")
        with path.open("xb") as stream:
            stream.write(build.files["jmeter/scenario.jmx"])
        manifest.append({"name": name, "jmx": str(path), "steps": steps})
    path = directory / "cases.json"
    with path.open("x") as stream:
        json.dump(manifest, stream)
    return path


def test_native_response_headers(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    command = source_command(manifest)
    command[-3] = str(Path(__file__).with_name("HeaderHarness.groovy"))
    try:
        result = run_isolated(command, tmp_path, timeout=300)
    except Exception as error:
        (tmp_path / "failure-stdout.txt").write_text(str(getattr(error, "stdout", "")))
        (tmp_path / "failure-stderr.txt").write_text(str(getattr(error, "stderr", "")))
        pytest.fail(f"Header harness {type(error).__name__}; evidence at {tmp_path}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["function_control"] == report["processor_error_control"] == "passed"
    assert len(report["cases"]) == len(json.loads(manifest.read_text()))
    assert all(case["status"] == "passed" for case in report["cases"])
    fixture_bytes = json.dumps(header_cases(), sort_keys=True).encode()
    report["shared_fixture_sha256"] = sha256(fixture_bytes).hexdigest()
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
