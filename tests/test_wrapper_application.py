"""Offline ABI generation, history, source-map and target isolation acceptance."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET

import pytest

from har2load.application import Application


def setup(tmp_path, *, abi=True, targets=("jmeter", "loadrunner"), helpers=True):
    app = Application()
    source = tmp_path / "synthetic.har"
    source.write_text(json.dumps({"log": {"entries": [{"request": {
        "method": "GET", "url": "https://synthetic.invalid/source"},
        "response": {"status": 200}}]}}))
    workspace = tmp_path / "workspace"
    app.import_capture(source, workspace)
    exchange = app.store.load(workspace).scenario["ordered_exchange_ids"][0]
    operations = []
    for target in targets:
        common = {"target": target, "language": "groovy" if target == "jmeter" else "c",
                  "source": "// Synthetic body, not captured source.\n", "depends_on": [],
                  "dependencies": [], "side_effects": [], "purpose": "Synthetic ABI acceptance"}
        if abi:
            common["runtime_abi"] = "scalar_body_v1"
        helper_ids = [f"{target}_first", f"{target}_second"] if helpers else []
        for identifier in helper_ids:
            operations.append({"op": "upsert_helper", "helper": {
                **common, "id": identifier, "namespace": "synthetic", "symbol": identifier,
                "parameters": [], "return_type": "void", "stateless": True, "thread_safe": True}})
        operations.append({"op": "upsert_code_block", "block": {
            **common, "id": target + "_block", "behavior_id": "synthetic_behavior",
            "placement": {"event": "before_request", "exchange_id": exchange}, "order": 0,
            "inputs": [], "outputs": [], "helper_calls": helper_ids,
            "failure_policy": "mark_error_and_continue",
            "failed_input_policy": "skip_and_mark_outputs", "reset_outputs": True}})
    changes = tmp_path / "authored.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "authored",
        "base_revision": 1, "author": "fixture", "rationale": "Explicit authored ABI",
        "evidence": [{"exchange_id": exchange, "location": "/request"}],
        "operations": operations}))
    app.changes_apply(workspace, changes)
    return app, workspace


def tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def assert_c_locator(text, locator):
    # Only a test oracle for emitted fixture C, not a production source parser.
    tokens = r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    masked = re.sub(tokens, lambda match: " " * len(match[0]), text)
    definition = re.search(r"\b" + re.escape(locator["function"]) + r"\s*\([^;{}]*\)\s*\{",
                           masked)
    assert definition is not None, "Missing function definition"
    start, depth = definition.end(), 1
    for index in range(start, len(masked)):
        depth += (masked[index] == "{") - (masked[index] == "}")
        if depth == 0:
            assert locator["marker"] in text[start:index], "Marker outside claimed function"
            return
    raise AssertionError("Unclosed function body")


def test_c_locator_oracle_rejects_marker_in_another_function_or_in_only_a_call():
    source = 'void first(void) { /* marker */ } void second(void) { first(); }'
    assert_c_locator(source, {"function": "first", "marker": "marker"})
    with pytest.raises(AssertionError, match="outside claimed function"):
        assert_c_locator(source, {"function": "second", "marker": "marker"})
    with pytest.raises(AssertionError, match="Missing function definition"):
        assert_c_locator('void second(void) { /* marker */ first(); }',
                         {"function": "first", "marker": "marker"})


def assert_real_locators(output, rows):
    paths = {}
    root = ET.fromstring((output / "jmeter/scenario.jmx").read_bytes())

    def walk(branch, prefix):
        elements = list(branch)
        for node, children in zip(elements[::2], elements[1::2], strict=True):
            path = (*prefix, node.attrib["testname"])
            paths[path] = node.tag
            walk(children, path)

    walk(root.find("hashTree"), ())
    for row in rows:
        if row.get("status") != "generated":
            continue
        locator = row["locator"]
        if row["target"] == "jmeter":
            assert paths[tuple(locator["component_path"])] == locator["element_type"]
            for path in locator.get("additional_component_paths", []):
                assert tuple(path) in paths
        else:
            text = (output / row["artifact"]).read_text()
            assert_c_locator(text, locator)


def test_opted_in_output_free_blocks_and_helper_maps_are_generated_once(tmp_path, monkeypatch):
    app, workspace = setup(tmp_path)
    before = tree(workspace)

    def forbidden(*args, **kwargs):
        pytest.fail("Ordinary generation attempted native execution or networking")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    output = tmp_path / "generated"
    result = app.generate(workspace, output=output)
    assert result["status"] == "ok", result
    report = json.loads((output / "report.json").read_bytes())
    assert report["report_schema_version"] == "1.6"
    assert report["generation"]["profile"] == "authored_scalar_runtime_v1"
    assert report["generation"]["native_status_policy"] == "markers_only_native_status_v1"
    assert report["generation"]["output_commit_policy"] == "per_output_validated_v1"
    assert_real_locators(output, report["source_map"])
    for target in ("jmeter", "loadrunner"):
        assert report["targets"][target]["profile_version"] == "3"
        assert report["targets"][target]["output_commit_policy"] == "per_output_validated_v1"
        rows = [r for r in report["source_map"] if r["target"] == target]
        calls = [r for r in rows if r["kind"] == "helper_call"]
        assert {r["helper_id"] for r in calls} == {target + "_first", target + "_second"}
        assert len(calls) == 2
        for kind, count in (("code_block", 1), ("reset_outputs", 1), ("helper_definition", 2)):
            assert len([r for r in rows if r["kind"] == kind]) == count
        for row in rows:
            assert row["artifact_sha256"] == hashlib.sha256(
                (output / row["artifact"]).read_bytes()).hexdigest()
        assert report["targets"][target]["native_validation"] == "not_run"
    assert tree(workspace) == before
    assert app.generate(workspace, revision=1)["data"]["profile"] == "static_literals_v1"


def test_absent_abi_never_implicitly_enables_old_source(tmp_path):
    app, workspace = setup(tmp_path, abi=False)
    output = tmp_path / "blocked"
    result = app.generate(workspace, output=output)
    assert result["status"] == "error"
    assert not (output / "jmeter").exists() and not (output / "loadrunner").exists()
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())
    assert "GENERATION_EXTENSION_ABI_REQUIRED" in (output / "report.json").read_text()


def test_missing_counterpart_blocks_only_that_target(tmp_path):
    app, workspace = setup(tmp_path, targets=("jmeter",), helpers=False)
    output = tmp_path / "partial"
    result = app.generate(workspace, output=output)
    assert result["status"] == "error"
    assert (output / "jmeter/scenario.jmx").is_file()
    assert not (output / "loadrunner").exists()
    report = json.loads((output / "report.json").read_bytes())
    assert report["targets"]["jmeter"]["status"] == "generated"
    assert report["targets"]["loadrunner"]["status"] == "blocked"


def edit(app, workspace, path, operations):
    snapshot = app.store.load(workspace)
    path.write_text(json.dumps({"schema_version": "1.0", "change_id": path.stem,
        "base_revision": snapshot.scenario_revision, "author": "fixture",
        "rationale": "Synthetic extension history",
        "evidence": [{"exchange_id": snapshot.scenario["ordered_exchange_ids"][0],
                      "location": "/request"}], "operations": operations}))
    app.changes_apply(workspace, path)


def authored(record):
    return {k: v for k, v in record.items() if k not in {"source_digest", "change_id"}}


def test_removed_helper_call_preserves_removed_row_and_emits_unused_definition(tmp_path):
    app, workspace = setup(tmp_path)
    block = next(b for b in app.store.load(workspace).scenario["target_extensions"]["blocks"]
                 if b["target"] == "jmeter")
    edit(app, workspace, tmp_path / "remove-call.json", [{"op": "upsert_code_block", "block": {
        **authored(block), "helper_calls": ["jmeter_first"]}}])
    output = tmp_path / "generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    rows = json.loads((output / "report.json").read_bytes())["source_map"]
    removed = [r for r in rows if r["kind"] == "helper_call"
               and r.get("helper_id") == "jmeter_second"]
    assert len(removed) == 1 and removed[0]["disposition"] == "removed"
    assert removed[0]["artifact"] is None
    definition = [r for r in rows if r["kind"] == "helper_definition"
                  and r["entity_id"] == "jmeter_second"]
    assert len(definition) == 1 and definition[0]["status"] == "generated"
    assert definition[0]["declaration_disposition"] == "unused"


def test_helper_only_models_emit_unused_definitions_and_retarget_history(tmp_path):
    app, workspace = setup(tmp_path)
    model = app.store.load(workspace).scenario
    operations = [{"op": "remove_code_block", "id": b["id"]}
                  for b in model["target_extensions"]["blocks"]]
    edit(app, workspace, tmp_path / "helpers-only.json", operations)
    output = tmp_path / "helpers-generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    document = json.loads((output / "report.json").read_bytes())
    definitions = [r for r in document["source_map"] if r["kind"] == "helper_definition"]
    assert len(definitions) == 4
    assert all(r["declaration_disposition"] == "unused" for r in definitions)
    helper = next(h for h in model["target_extensions"]["helpers"] if h["id"] == "jmeter_first")
    edit(app, workspace, tmp_path / "retarget-helper.json", [{"op": "upsert_helper", "helper": {
        **authored(helper), "target": "loadrunner", "language": "c"}}])
    output = tmp_path / "retargeted-generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    rows = json.loads((output / "report.json").read_bytes())["source_map"]
    definitions = [r for r in rows if r["kind"] == "helper_definition"
                   and r["entity_id"] == "jmeter_first"]
    assert len(definitions) == 2
    old = next(r for r in definitions if r["target"] == "jmeter")
    new = next(r for r in definitions if r["target"] == "loadrunner")
    assert old["disposition"] == "removed" and old["artifact"] is None
    assert new["disposition"] == "runtime_emitted" and new["status"] == "generated"
