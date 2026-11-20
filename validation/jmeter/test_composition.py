"""Opt-in, network-denied native object tests. Never start an engine or sample HTTP."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.jmeter import JMeterGenerator
from har2load.runtime_types import RuntimeSlot, RuntimeTemplate
from tests.test_jmeter import ARTIFACT, plan
from tests.test_jmeter_cookies import seed
from tests.test_jmeter_sources import external
from validation.jmeter.test_runtime import correlated
from validation.support import run_isolated

LABELS = ("TF_UC1", "TF_UC1.1")
ESCAPED = '雪😀\\${token}\\\\${__groovy(props["h2l.compose.executed"]="yes")}<&'


def source(index: int, scalar: bool, supplied: str | None) -> tuple[bytes, dict]:
    # Deliberately collide in every plan-local identifier; only URL, seed and data differ.
    prefix = f"https://example.invalid/app/{index}"
    candidate: RequestPlan
    if scalar:
        candidate = correlated()
        rt = candidate.runtime
        assert rt is not None
        req = tuple(replace(r, group_id=r.id, url=prefix + "/" + r.id) for r in candidate.requests)
        producer, consumer = (replace(r, url=RuntimeTemplate((q.url,)))
                              for r, q in zip(rt.requests, req, strict=True))
        consumer = replace(consumer, body=RuntimeTemplate((
            '{"input":', RuntimeSlot("input-binding", "v1", "json"),
            ',"response":', RuntimeSlot("binding", "v", "json"), '}')))
        extracted = {**rt.variables[0], "name": "response_token"}
        groups = tuple(NativeGroup(r.id, "Same step", (r.id,)) for r in req)
        initial = external(supplied)
        if supplied == "":
            initial["source"]["allow_empty"] = True
        candidate = replace(candidate, groups=groups, requests=req, runtime=replace(rt,
            variables=(initial, extracted), requests=(producer, consumer)))
    else:
        candidate = plan(NativeRequest("producer", "g1", "GET", prefix + "/producer"),
                         name="Synthetic")
    cookie = replace(seed(), name="sid", value=f"seed{index}", path_specified=True)
    build = JMeterGenerator().generate(replace(candidate, initial_cookies=(cookie,)))
    assert build.status == "generated", build.diagnostics
    return build.files[ARTIFACT], {
        "label": LABELS[index], "scalar": scalar, "input": supplied, "prefix": prefix,
        "seed": cookie.value, "ids": [r.id for r in candidate.requests],
    }


def write_cases(directory: Path) -> Path:
    from har2load.jmeter_composition import compose_jmx

    cases = []
    for name, profiles in (
        ("distinct", ((True, ESCAPED), (True, "second"))),
        ("missing", ((True, None), (True, ESCAPED))),
        ("empty_and_missing", ((True, ""), (True, None))),
        ("mixed", ((False, None), (True, "second"))),
    ):
        docs = []
        groups = []
        for index, (scalar, supplied) in enumerate(profiles):
            jmx, expected = source(index, scalar, supplied)
            docs.append((LABELS[index], jmx))
            groups.append(expected)
        composed = compose_jmx(docs)
        path = directory / (name + ".jmx")
        path.write_bytes(composed)
        cases.append({"name": name, "path": str(path), "groups": groups,
                      "sha256": hashlib.sha256(composed).hexdigest()})
    manifest = directory / "cases.json"
    manifest.write_text(json.dumps(cases, indent=2), encoding="utf-8")
    return manifest


def test_composed_native_groups(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    java = Path(os.environ["HAR2LOAD_JAVA8_HOME"]) / "bin/java"
    jars = sorted((home / "lib").glob("*.jar")) + sorted((home / "lib/ext").glob("*.jar"))
    command = [str(java), "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
               "-cp", os.pathsep.join(map(str, jars)), "groovy.ui.GroovyMain",
               str(Path(__file__).with_name("CompositionHarness.groovy")), str(home), str(manifest)]
    try:
        result = run_isolated(command, tmp_path, timeout=120)
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Composition harness exited {error.returncode}:\n"
                    f"{error.stdout}\n{error.stderr}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
    assert report["jmeter"] == "5.5" and report["java"].startswith("1.8.")
    assert report["processor_error_control"] == "passed"
    expected = {c["name"]: c["sha256"] for c in json.loads(manifest.read_text())}
    assert {c["name"]: c["sha256"] for c in report["cases"]} == expected
    assert all(c["status"] == "passed" for c in report["cases"]), report["cases"]
