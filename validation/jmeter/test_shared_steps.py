"""Opt-in JMeter 5.5 acceptance for two UC callers of one shared catalog range."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.jmeter import ARTIFACT, JMeterGenerator
from har2load.runtime_types import RuntimeRequest, RuntimeSlot, RuntimeTemplate
from tests.test_jmeter_cookies import seed
from tests.test_jmeter_sources import external
from validation.jmeter.test_runtime import correlated
from validation.support import run_isolated

LABELS = ("TF_Browse", "TF_Compare")
SHARED = ("g-catalog", "g-subcatalog")
BASE = "https://example.invalid/app/"


def catalog_uc(index: int) -> bytes:
    # Same exported names, selectors and request shapes; *different* source-local
    # IDs make reusing the canonical caller's processor config observably wrong.
    fixture = correlated()
    rt = fixture.runtime
    assert rt is not None
    groups = ("g-enter", *SHARED, "g-follow")
    paths = (("start-browse", "catalog", "subcatalog", "view-item"),
             ("start-compare", "catalog", "subcatalog", "compare-item"))[index]
    producer = "producer" if index == 0 else "other-producer"
    ids = ("enter", producer, "subcatalog" if index == 0 else "other-subcatalog", "follow")
    user_id, pass_id, item_id, extractor_id = (f"{k}{index}" for k in ("u", "p", "v", "e"))
    credential = {**external(f"user-{index}", "username"), "id": user_id, "name": "username"}
    secret = {**external(f"pass-{index}", "password"), "id": pass_id, "name": "password"}
    item = {**rt.variables[0], "id": item_id, "name": "item",
            "initialize": {"event": "after_response", "exchange_id": producer},
            "source": {"kind": "extracted", "extractor_id": extractor_id}}
    extract = {**rt.extractors[0], "id": extractor_id, "exchange_id": producer}
    native = []
    runtime = []
    for gid, rid, path in zip(groups, ids, paths, strict=True):
        bindings = [('"username":', RuntimeSlot("username-binding", user_id, "json")),
                    ('"password":', RuntimeSlot("password-binding", pass_id, "json"))]
        if gid in {SHARED[1], "g-follow"}:
            bindings.append(('"item":', RuntimeSlot("item-binding", item_id, "json")))
        parts: list[str | RuntimeSlot] = ["{"]
        for n, (key, slot) in enumerate(bindings):
            parts.extend((("," if n else "") + key, slot))
        headers = (("Content-Type", "application/json"),)
        native.append(NativeRequest(rid, gid, "POST", BASE + path, headers, b"{}", "json"))
        runtime.append(RuntimeRequest(rid, RuntimeTemplate((BASE + path,)),
            (("Content-Type", RuntimeTemplate(("application/json",))),),
            RuntimeTemplate((*parts, "}"))))
    plan = RequestPlan(tuple(native), tuple(NativeGroup(gid, path, (rid,))
        for gid, rid, path in zip(groups, ids, paths, strict=True)),
        runtime=replace(rt, variables=(credential, secret, item), extractors=(extract,),
                        requests=tuple(runtime)),
        initial_cookies=(replace(seed(), name="sid", value=f"seed-{index}", path_specified=True),))
    result = JMeterGenerator().generate(plan)
    assert result.status == "generated", result.diagnostics
    return result.files[ARTIFACT]


def test_shared_catalog_native(tmp_path: Path) -> None:
    from har2load.jmeter_composition import compose_jmx
    from har2load.shared_steps import SharedBlock, StepUse

    inputs = [(label, catalog_uc(i)) for i, label in enumerate(LABELS)]
    # This tests the renderer, not the CLI's source manifest pinning.
    block = SharedBlock("TF_Catalog", tuple(StepUse(label, "0" * 64, SHARED) for label in LABELS))
    jmx = compose_jmx(inputs, shared_steps=(block,))
    path = tmp_path / "catalog.jmx"
    path.write_bytes(jmx)
    home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    java = Path(os.environ["HAR2LOAD_JAVA8_HOME"]) / "bin/java"
    jars = sorted((home / "lib").glob("*.jar")) + sorted((home / "lib/ext").glob("*.jar"))
    command = [str(java), "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
               "-cp", os.pathsep.join(map(str, jars)), "groovy.ui.GroovyMain",
               str(Path(__file__).with_name("SharedStepsHarness.groovy")), str(home), str(path)]
    try:
        result = run_isolated(command, tmp_path, timeout=120)
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Shared steps harness exited {error.returncode}:\n"
                    f"{error.stdout}\n{error.stderr}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    (tmp_path / "execution-evidence.json").write_text(json.dumps(report, indent=2))
    assert report["jmeter"] == "5.5" and report["java"].startswith("1.8.")
    assert report["sha256"] == hashlib.sha256(jmx).hexdigest()
    assert report["module_targets"] == ["TF_Browse", "TF_Catalog", "TF_Catalog", "TF_Compare"]
    assert report["runs"] == [
        {"TF_Browse": "item-0", "TF_Compare": "item-1"},
        {"TF_Browse": "ERROR_item", "TF_Compare": "item-1"},
    ]
