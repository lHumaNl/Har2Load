import hashlib
import json

import pytest

from har2load.application import Application
from har2load.cli import main


def setup(tmp_path, *, value_type="string", selector=None, area="response_body", unused=False):
    app = Application()
    source = tmp_path / "source.har"
    value = {"string": "recorded", "integer": 7, "number": 1.5, "boolean": True,
             "null": None}[value_type]
    source.write_text(json.dumps({"log": {"entries": [
        {"request": {"method": "GET", "url": "https://example.invalid/source"},
         "response": {"status": 200, "headers": [{"name": "X-Token", "value": "recorded"}],
                      "content": {"mimeType": "application/json",
                                  "text": json.dumps({"id": value})}}},
        {"request": {"method": "POST", "url": "https://example.invalid/next",
                     "postData": {"mimeType": "application/json",
                                  "text": json.dumps({"id": value})}},
         "response": {"status": 200}},
    ]}}))
    workspace = tmp_path / "workspace"
    app.import_capture(source, workspace)
    entries = app.summary(workspace)["data"]["entries"]
    producer, consumer = (e["id"] for e in entries)
    operations = [{"op": "upsert_extractor", "extractor": {
        "id": "e", "exchange_id": producer, "area": area,
        "selector": selector or {"dialect": "json_path_subset_v1", "expression": "$.id"},
        "cardinality": "exactly_one", "allow_empty": False,
        "on_failure": "mark_error_and_continue"}}]
    if not unused:
        operations += [{"op": "upsert_variable", "variable": {
            "id": "v", "name": "token", "value_type": value_type,
            "source": {"kind": "extracted", "extractor_id": "e"},
            "initialize": {"event": "after_response", "exchange_id": producer},
            "scope": "iteration", "reset": "iteration_start", "purpose": "Synthetic correlation",
            "sensitive": True}}, {"op": "upsert_binding", "binding": {"id": "b", "variable_id": "v",
                "exchange_id": consumer, "location": {"area": "request_json", "pointer": "/id"}}}]
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "runtime",
        "base_revision": 1, "author": "fixture", "rationale": "Explicit scalar runtime correlation",
        "evidence": [{"exchange_id": producer, "location": "/response"}],
        "operations": operations}))
    app.changes_apply(workspace, changes)
    return app, workspace


def digest_tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("value_type", ["string", "integer", "number", "boolean", "null"])
def test_two_target_scalar_runtime_generation_and_maps(tmp_path, value_type):
    app, workspace = setup(tmp_path, value_type=value_type)
    before = digest_tree(workspace)
    checked = app.generate(workspace)
    assert checked["status"] == "ok", checked
    assert checked["data"]["profile"] == "scalar_runtime_v1"
    output = tmp_path / "generated"
    written = app.generate(workspace, output=output)
    assert written["data"]["report_sha256"] == checked["data"]["report_sha256"]
    report = json.loads((output / "report.json").read_bytes())
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert report["report_schema_version"] == "1.6"
    assert report["generation"]["failure_wire_policy"] == "error_marker_json_string_v1"
    assert report["generation"]["runtime_validation"] == "not_run"
    assert report["generation"]["native_status_policy"] == "markers_only_native_status_v1"
    assert report["generation"]["profile_version"] == "4"
    assert report["variables"][0]["lifecycle"]["runtime_implementation"] == "target_specific"
    for target in ("jmeter", "loadrunner"):
        for kind, entity in (("variable", "v"), ("initialize", "v"), ("reset", "v"),
                             ("extractor", "e"), ("binding", "b")):
            rows = [r for r in report["source_map"] if r["kind"] == kind
                    and r["entity_id"] == entity and r["target"] == target]
            assert len(rows) == 1, rows
            assert rows[0]["status"] == "generated" and rows[0]["locator"]
            assert rows[0]["artifact_sha256"] == manifest["files"][rows[0]["artifact"]]
        assert report["targets"][target]["profile"] == "scalar_runtime_v1"
        assert report["targets"][target]["profile_version"] == "4"
    assert report["targets"]["loadrunner"]["project_status"] == "incomplete"
    assert digest_tree(workspace) == before


def test_unreferenced_boundary_extractor_is_emitted_not_silently_ignored(tmp_path):
    app, workspace = setup(tmp_path, selector={"dialect": "boundary_v1", "left": '"', "right": '"'},
                           unused=True)
    output = tmp_path / "generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    report = json.loads((output / "report.json").read_bytes())
    assert len([r for r in report["source_map"] if r["kind"] == "extractor"
                and r["status"] == "generated"]) == 2


def test_runtime_plan_inspection_is_private_and_revision_pinned(tmp_path, capsys):
    app, workspace = setup(tmp_path)
    assert main(["inspect", "--workspace", str(workspace), "--section", "runtime-plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scenario_revision"] == 2
    assert result["data"]["contains_sensitive_data"] is True
    assert "runtime" in json.loads(result["data"]["content"])
    assert app.generate(workspace, revision=1)["data"]["profile"] == "static_literals_v1"


def test_runtime_check_does_not_execute_native_or_read_external_inputs(tmp_path, monkeypatch):
    app, workspace = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Preparation must not execute or read external inputs")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    assert app.generate(workspace)["status"] == "ok"


def test_model_only_report_does_not_claim_runtime_emission(tmp_path):
    app, workspace = setup(tmp_path)
    output = tmp_path / "model-report"
    app.report(workspace, output=output)
    document = json.loads((output / "report.json").read_bytes())
    assert document["report_schema_version"] == "1.1"
    assert document["variables"][0]["lifecycle"]["runtime_implementation"] == "not_generated"


def test_bound_recorded_value_is_absent_from_native_runtime_files(tmp_path):
    app, workspace = setup(tmp_path)
    output = tmp_path / "generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    assert b"recorded" not in (output / "jmeter/scenario.jmx").read_bytes()
    assert b"recorded" not in (output / "loadrunner/Action.c").read_bytes()
    # The accompanying evidence report deliberately remains private and unredacted.
    assert b"recorded" in (output / "report.json").read_bytes()


def test_missing_runtime_mapping_refuses_publication(tmp_path, monkeypatch):
    from har2load.domain import Har2LoadError
    from har2load.generation_types import TargetBuild

    app, workspace = setup(tmp_path)
    monkeypatch.setattr(
        "har2load.generation.JMeterGenerator.generate", lambda self, plan: TargetBuild(
            "jmeter", files={"jmeter/scenario.jmx": b"synthetic-incomplete-mapping"}))
    output = tmp_path / "generated"
    with pytest.raises(Har2LoadError) as error:
        app.generate(workspace, targets=("jmeter",), output=output)
    assert error.value.code == "GENERATION_MAP_INVALID"
    assert not output.exists()


def test_generated_runtime_report_keeps_removed_mapping_history(tmp_path):
    app, workspace = setup(tmp_path)
    before = app.generate(workspace)["data"]["report_sha256"]
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    changes = tmp_path / "remove.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "remove",
        "base_revision": 2, "author": "fixture", "rationale": "Keep unused extractor diagnostics",
        "evidence": [{"exchange_id": entity, "location": "/response"}],
        "operations": [{"op": "remove_binding", "id": "b"}, {"op": "remove_variable", "id": "v"}]}))
    app.changes_apply(workspace, changes)
    output = tmp_path / "generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    report = json.loads((output / "report.json").read_bytes())
    removed = [r for r in report["source_map"] if r["entity_id"] in {"b", "v"}]
    assert len(removed) == 8 and all(r["disposition"] == "removed" for r in removed)
    assert all(r["artifact"] is None for r in removed)
    assert app.generate(workspace, revision=2)["data"]["report_sha256"] == before


@pytest.mark.parametrize("source", [
    {"kind": "external", "key": "PRIVATE_INPUT", "allow_empty": False},
    {"kind": "generated", "generator": "uuid"}])
def test_native_sources_render_without_reading_or_fabricating_live_values(tmp_path, source):
    app, workspace = setup(tmp_path)
    model = app.store.load(workspace).scenario
    variable = {k: v for k, v in model["parameters"]["variables"][0].items() if k != "change_id"}
    variable["source"] = source
    variable["initialize"] = {"event": "iteration_start"}
    entity = model["ordered_exchange_ids"][0]
    changes = tmp_path / "source.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "source",
        "base_revision": 2, "author": "fixture", "rationale": "Unsupported producer is explicit",
        "evidence": [{"exchange_id": entity, "location": "/response"}],
        "operations": [{"op": "upsert_variable", "variable": variable}]}))
    app.changes_apply(workspace, changes)
    output = tmp_path / "prepared"
    result = app.generate(workspace, output=output)
    assert result["status"] == "ok"
    assert all(t["status"] == "generated" for t in result["data"]["targets"].values())
    assert "PRIVATE_INPUT" not in json.dumps(result)
    assert {p.name for p in output.iterdir()} == {
        "manifest.json", "report.json", "report.md", "jmeter", "loadrunner"}


@pytest.mark.parametrize("fault", ["duplicate", "empty_locator", "unknown_entity"])
def test_runtime_source_maps_reject_conflicting_or_unattributed_emission(fault):
    from har2load.domain import Har2LoadError
    from har2load.generation import _parameter_map

    baseline = [{"target": "jmeter", "kind": "binding", "entity_id": "b",
                 "disposition": "declared"}]
    row = {"target": "jmeter", "kind": "binding", "entity_id": "b", "artifact": "x",
           "locator": {"component_path": ["Har2Load", "Consumer"],
                       "element_type": "JSR223PreProcessor"}}
    mappings = [row]
    if fault == "duplicate":
        mappings.append({**row, "locator": {"component_path": ["Wrong"], "element_type": "X"}})
    elif fault == "empty_locator":
        row["locator"] = {}
    else:
        mappings.append({**row, "entity_id": "missing"})
    with pytest.raises(Har2LoadError) as error:
        _parameter_map(baseline, mappings, {"parameters": {"bindings": []}},
                       {"jmeter": "generated"}, runtime=True)
    assert error.value.code == "GENERATION_MAP_INVALID"


def test_runtime_inspection_continuation_keeps_original_revision_after_edit(tmp_path):
    app, workspace = setup(tmp_path)
    first = app.inspect(workspace, section="runtime-plan", limit=100)
    content = first["data"]["content"]
    continuation = first["continuation"]
    assert continuation["revision"] == 2
    model = app.store.load(workspace).scenario
    variable = {k: v for k, v in model["parameters"]["variables"][0].items() if k != "change_id"}
    variable["name"] = "renamed_after_first_page"
    entity = model["ordered_exchange_ids"][0]
    changes = tmp_path / "rename.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "rename",
        "base_revision": 2, "author": "fixture", "rationale": "Edit after first page",
        "evidence": [{"exchange_id": entity, "location": "/response"}],
        "operations": [{"op": "upsert_variable", "variable": variable}]}))
    app.changes_apply(workspace, changes)
    while continuation:
        page = app.inspect(workspace, **continuation)
        assert page["scenario_revision"] == 2
        content += page["data"]["content"]
        continuation = page["continuation"]
    assert json.loads(content)["runtime"]["variables"][0]["name"] == "token"
    latest = app.inspect(workspace, section="runtime-plan", pointer="/runtime/variables")
    assert latest["scenario_revision"] == 3
    assert json.loads(latest["data"]["content"])[0]["name"] == "renamed_after_first_page"
