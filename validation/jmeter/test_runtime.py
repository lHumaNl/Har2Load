"""Load actual JMX and execute fixed processors with synthetic responses, never HTTP."""

import base64
import json
import os
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate
from validation.support import run_isolated


def correlated(*, value_type="string", selector=None, scope="iteration", location="json",
               cardinality="exactly_one", occurrence=None, allow_empty=False):
    producer = NativeRequest("producer", "group", "GET", "https://example.invalid/source")
    consumer = NativeRequest("consumer", "group", "POST", "https://example.invalid/use",
                             (("Content-Type", "application/json"),), b'"recorded"', "json")
    variable = {"id": "v", "name": "token", "value_type": value_type,
                "source": {"kind": "extracted", "extractor_id": "e"}, "scope": scope,
                "reset": scope + "_start", "initialize": {"event": "after_response",
                "exchange_id": "producer"}, "purpose": "Synthetic fixture", "sensitive": False}
    extractor = {"id": "e", "exchange_id": "producer", "area": "response_body",
                 "selector": selector or {"dialect": "json_path_subset_v1", "expression": "$.id"},
                 "cardinality": cardinality, "allow_empty": allow_empty,
                 "on_failure": "mark_error_and_continue"}
    if occurrence is not None:
        extractor["occurrence"] = occurrence
    requests = (RuntimeRequest("producer", RuntimeTemplate((producer.url,)), (), None),
                RuntimeRequest("consumer", RuntimeTemplate((consumer.url,)),
                    (("Content-Type", RuntimeTemplate(("application/json",))),),
                    RuntimeTemplate((RuntimeSlot("binding", "v", location),))))
    return RequestPlan((producer, consumer),
                       (NativeGroup("group", "Synthetic", ("producer", "consumer")),),
                       runtime=RuntimePlan((variable,), (extractor,), requests))


def sequence(response, body, *, failed=False, transport=True, **extra):
    return [{"event": "iteration"}, {"event": "pre", "request": "producer"},
            {"event": "post", "request": "producer", "response": response,
             "transport_success": transport, "warning": failed, **extra},
            {"event": "pre", "request": "consumer", "body": body, "warning": failed},
            {"event": "post", "request": "consumer", "response": "{}"}]


def cases():
    for type_name, response, rendered in (
        ("string", '{"id":"hello"}', '"hello"'),
        ("integer", '{"id":9007199254740993}', '9007199254740993'),
        ("number", '{"id":9007199254740993.123456789}', '9007199254740993.123456789'),
        ("boolean", '{"id":false}', 'false'), ("null", '{"id":null}', 'null'),
    ):
        yield type_name, correlated(value_type=type_name), sequence(response, rendered)
    for label, response in (
        ("missing", "{}"), ("wrong_type", '{"id":3}'), ("duplicate", '{"id":"a","id":"b"}'),
        ("unused_duplicate", '{"id":"a","unused":{"x":1,"x":2}}'),
        ("container", '{"id":[]}'), ("empty", '{"id":""}'),
        ("surrogate", '{"id":"\\ud800"}'), ("trailing", '{"id":"a"}x'),
    ):
        yield label, correlated(), sequence(response, '"ERROR_token"', failed=True)
    yield "typed_failure", correlated(value_type="integer"), sequence(
        '{"id":"not an integer"}', '"ERROR_token"', failed=True)
    yield "transport_failure", correlated(), sequence(
        '{"id":"fresh"}', '"ERROR_token"', failed=True, transport=False)
    yield "charset_failure", correlated(), sequence(
        '{"id":"fresh"}', '"ERROR_token"', failed=True, charset="ISO-8859-1")
    yield "invalid_utf8", correlated(), sequence(
        "", '"ERROR_token"', failed=True, response_b64=base64.b64encode(b'\xff').decode())
    yield "missing_response", correlated(), sequence("", '"ERROR_token"', failed=True)
    yield "missing_result", correlated(), sequence(
        '{"id":"fresh"}', '"ERROR_token"', failed=True, missing_result=True)
    yield "allow_empty", correlated(allow_empty=True), sequence('{"id":""}', '""')
    yield "literal_marker_is_success", correlated(), sequence(
        '{"id":"ERROR_token"}', '"ERROR_token"')
    boundary = {"dialect": "boundary_v1", "left": "{", "right": "}"}
    yield "nested_boundaries", correlated(selector=boundary), sequence('{a{b{c}', '"a{b{c"')
    yield "multiple_boundaries", correlated(selector=boundary), sequence(
        '{a}{b}', '"ERROR_token"', failed=True)
    yield "occurrence", correlated(selector=boundary, cardinality="occurrence", occurrence=2), (
        sequence('{a}{b}', '"b"'))
    yield "root_null", correlated(value_type="null", selector={
        "dialect": "json_path_subset_v1", "expression": "$"}), sequence('null', 'null')
    yield "escaped_key", correlated(selector={"dialect": "json_path_subset_v1",
        "expression": '$["a/b~c"][0]'}), sequence('{"a/b~c":["ok"]}', '"ok"')
    for scope in ("session", "iteration"):
        steps = sequence('{"id":"first"}', '"first"')
        following = sequence('{}', '"ERROR_token"', failed=True)
        following[0]["values"] = {"token": "first" if scope == "session" else "ERROR_token"}
        following[1]["values"] = {"token": "ERROR_token"}
        steps += following
        yield "stale_" + scope, correlated(scope=scope), steps
    hostile = '${__groovy(throw new Error("executed"))}\\雪\r\n'
    yield "metasyntax_is_data", correlated(), sequence(
        json.dumps({"id": hostile}), json.dumps(hostile, ensure_ascii=True))
    plan = correlated()
    producer, consumer = plan.runtime.requests
    consumer = replace(consumer,
        url=RuntimeTemplate(("https://example.invalid/?v=", RuntimeSlot("query", "v", "form"))),
        headers=(*consumer.headers,
                 ("X-Token", RuntimeTemplate((RuntimeSlot("header", "v", "header"),)))))
    plan = replace(plan, runtime=replace(plan.runtime, requests=(producer, consumer)))
    steps = sequence('{"id":"a\\nb"}', '"a\\nb"')
    steps[3].update(url="https://example.invalid/?v=" + quote_plus("a\nb"),
                    headers={"X-Token": ["ERROR_token"]})
    steps[3].update(warning=True, values={"token": "a\nb"})
    yield "header_failure_is_local", plan, steps
    failed_transport = deepcopy(steps)
    failed_transport[4]["transport_success"] = False
    yield "header_failure_preserves_native_failure", plan, failed_transport
    for label, response in (
        ("depth_limit", '{"id":"ok","unused":' + '[' * 65 + '0' + ']' * 65 + '}'),
        ("value_limit", json.dumps({"id": "x" * 65537})),
        ("scale_limit", '{"id":1e-65537}'),
    ):
        type_name = "number" if label == "scale_limit" else "string"
        yield label, correlated(value_type=type_name), sequence(
            response, '"ERROR_token"', failed=True)
    # Root and bracket-key path semantics, integral decimals, and null/string distinction.
    for label, type_name, response, rendered in (
        ("integral_decimal", "integer", '{"id":1.00}', '1.00'),
        ("nonintegral", "integer", '{"id":1.25}', '"ERROR_token"'),
        ("null_is_not_string", "string", '{"id":null}', '"ERROR_token"'),
        ("unicode", "string", '{"id":"\\ud83d\\ude00"}', '"\\ud83d\\ude00"'),
    ):
        yield label, correlated(value_type=type_name), sequence(
            response, rendered, failed='ERROR_' in rendered)
    # Literal initialization does not depend on HTTP success, even after a failed extraction.
    plan = correlated(value_type="integer")
    literal = {**plan.runtime.variables[0], "source": {"kind": "literal", "value": 7}}
    plan = replace(plan, runtime=replace(plan.runtime, variables=(literal,)))
    steps = sequence('{}', '7', transport=False)
    yield "literal_after_http_failure", plan, steps
    plan = correlated()
    variable = {**plan.runtime.variables[0], "scope": "request", "reset": "before_request",
                "source": {"kind": "literal", "value": "fresh"},
                "initialize": {"event": "before_request", "exchange_id": "consumer"}}
    plan = replace(plan, runtime=replace(plan.runtime, variables=(variable,), extractors=()))
    steps = [{"event": "iteration"},
             {"event": "pre", "request": "producer", "values": {"token": "ERROR_token"}},
             {"event": "post", "request": "producer", "response": "{}"},
             {"event": "pre", "request": "consumer", "body": '"fresh"',
              "values": {"token": "fresh"}},
             {"event": "post", "request": "consumer", "response": "{}"}]
    yield "request_lifecycle", plan, steps + steps
    plan = correlated(location="text")
    first, second = plan.runtime.requests
    second = replace(second, body=RuntimeTemplate(("x" * (1024 * 1024 - 4),
                                                  RuntimeSlot("binding", "v", "text"))))
    plan = replace(plan, runtime=replace(plan.runtime, requests=(first, second)))
    steps = sequence('{"id":"ok"}', 'unused')
    steps[3].pop("body")
    steps[3].update(body_len=1024 * 1024 - 2, body_suffix="ok")
    following = sequence('{"id":"overflow"}', 'unused')
    following[3].pop("body")
    following[3].update(invalid_url=True, warning=True)
    following[4].update(transport_success=False)
    steps += following
    yield "field_limit_no_stale_request", plan, steps
    for order in ("bad_first", "good_first"):
        plan = correlated()
        independent = {**plan.runtime.extractors[0], "id": "bad",
                       "selector": {"dialect": "json_path_subset_v1", "expression": "$.missing"}}
        bad = {**plan.runtime.variables[0], "id": "bad", "name": "bad",
               "source": {"kind": "extracted", "extractor_id": "bad"}}
        extractors = (independent, *plan.runtime.extractors)
        if order == "good_first":
            extractors = tuple(reversed(extractors))
        plan = replace(plan, runtime=replace(plan.runtime, extractors=extractors,
                                            variables=(*plan.runtime.variables, bad)))
        steps = sequence('{"id":"independent"}', '"independent"')
        steps[2].update(warning=True,
                         values={"token": "independent", "bad": "ERROR_bad"})
        yield "independent_checks_" + order, plan, steps
    plan = correlated()
    first, second = plan.runtime.requests
    second = replace(second, url=RuntimeTemplate(("https://example.invalid/a%2Fb/",
                     RuntimeSlot("path", "v", "path"), "?keep=%2f+z&value=",
                     RuntimeSlot("query", "v", "form"))))
    plan = replace(plan, runtime=replace(plan.runtime, requests=(first, second)))
    steps = []
    for value in (" /+%雪", "changed /+%"):
        iteration = sequence(json.dumps({"id": value}), json.dumps(value))
        iteration[3]["url"] = ("https://example.invalid/a%2Fb/" + quote(value, safe="")
                                + "?keep=%2f+z&value=" + quote_plus(value, safe=""))
        steps += iteration
    yield "running_recovery_and_contextual_url_encoding", plan, steps


def initialization_cases() -> Iterator[tuple[str, RequestPlan, list[dict[str, object]]]]:
    for event in ("session_start", "iteration_start"):
        plan = correlated()
        variable = {**plan.runtime.variables[0], "source": {"kind": "literal", "value": 7},
                    "initialize": {"event": event}}
        plan = replace(plan, runtime=replace(plan.runtime, variables=(variable,), extractors=()))
        steps = sequence("{}", '"ERROR_token"')
        steps[0].update(warning=True, values={"token": "ERROR_token"})
        steps[3].update(warning=True)
        yield "failed_initializer_" + event, plan, steps


def policy_control_cases() -> Iterator[tuple[str, RequestPlan, list[dict[str, object]]]]:
    for label, transport in (("force_failure", True), ("force_success", False)):
        yield label + "_negative_control", correlated(), sequence(
            "{}", '"ERROR_token"', failed=True, transport=transport)
    plan, steps = next((p, s) for _, p, s in initialization_cases())
    yield "initializer_status_negative_control", plan, steps


def mutate_status_control(data: bytes, name: str) -> tuple[bytes, str | None]:
    mutations = {
        "force_failure_negative_control": ("JSR223PostProcessor", "prev.setSuccessful(false)",
                                           "response status"),
        "force_success_negative_control": ("JSR223PostProcessor", "prev.setSuccessful(true)",
                                           "response status"),
        "initializer_status_negative_control": (
            "JSR223Sampler", "SampleResult.setSuccessful(false)", "initializer"),
    }
    if name not in mutations:
        return data, None
    tag, mutation, rejection = mutations[name]
    tree = ET.fromstring(data)
    node = tree.find(f".//{tag}/stringProp[@name='script']")
    assert node is not None and node.text.endswith("\nreturn null\n")
    node.text = node.text.removesuffix("return null\n") + mutation + "\nreturn null\n"
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True), rejection


def test_jmeter_load_and_synthetic_runtime(tmp_path):
    java_home = Path(os.environ["HAR2LOAD_JAVA8_HOME"])
    jmeter_home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    java = java_home / "bin/java"
    assert java.is_file() and (jmeter_home / "bin/jmeter.properties").is_file()
    manifest = []
    static = replace(correlated(), runtime=None)
    renamed = replace(static,
        requests=(replace(static.requests[0], group_id="first"),
                  replace(static.requests[1], group_id="second")),
        groups=(NativeGroup("first", "A & 雪", ("producer",)),
                NativeGroup("second", "Renamed second fragment", ("consumer",))))
    all_cases = [("static_load_only", static, None),
                 ("renamed_multiple_modules_load_only", renamed, None),
                 ("duplicate_module_target_negative_control", renamed, None),
                 ("runtime_load_only", correlated(), None), *cases(), *initialization_cases(),
                 *policy_control_cases()]
    for name, plan, steps in all_cases:
        artifact = JMeterGenerator().generate(plan)
        assert artifact.status == "generated", artifact.diagnostics
        path = tmp_path / (name + ".jmx")
        data = artifact.files["jmeter/scenario.jmx"]
        data, expected_error = mutate_status_control(data, name)
        if name == "duplicate_module_target_negative_control":
            tree = ET.fromstring(data)
            paths = tree.findall(".//ModuleController/collectionProp")
            assert len(paths) == 2
            paths[1][:] = deepcopy(list(paths[0]))
            data = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
            expected_error = "module group order/coverage mismatch"
        path.write_bytes(data)
        if steps is not None:
            urls = {r.id: r.url for r in plan.requests}
            for step in steps:
                if step["event"] == "pre" and not step.get("invalid_url"):
                    step.setdefault("url", urls[step["request"]])
        manifest.append({"name": name, "jmx": str(path), "requests": len(plan.requests),
                         "groups": {g.id: list(g.exchange_ids) for g in plan.groups},
                         "group_order": [g.id for g in plan.groups],
                         "expected_error": expected_error,
                         "running_version": name.startswith("running_recovery"),
                         "mode": "load_only" if steps is None else "runtime", "steps": steps})
    manifest_path = tmp_path / "cases.json"
    manifest_path.write_text(json.dumps(manifest))
    jars = (sorted((jmeter_home / "lib").glob("*.jar"))
            + sorted((jmeter_home / "lib/ext").glob("*.jar")))
    command = [str(java), "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
               "-cp", os.pathsep.join(map(str, jars)), "groovy.ui.GroovyMain",
               str(Path(__file__).with_name("Harness.groovy")),
               str(jmeter_home), str(manifest_path)]
    try:
        result = run_isolated(command, tmp_path, timeout=120)
    except Exception as error:
        pytest.fail(f"Native JMeter harness failed: {getattr(error, 'stdout', '')}\n"
                    f"{getattr(error, 'stderr', '')}\n{error}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["processor_error_negative_control"] == "passed"
    assert len(report["cases"]) == len(all_cases)
    assert all(c["status"] == "passed" for c in report["cases"])
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
