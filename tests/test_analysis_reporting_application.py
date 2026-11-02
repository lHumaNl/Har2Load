import hashlib
import json

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError


def setup(tmp_path):
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [
        {"comment": "Create", "startedDateTime": "2026-11-02T00:00:00Z", "time": 100,
         "request": {"method": "POST", "url": "https://example.invalid/create"},
         "response": {"status": 200, "content": {"mimeType": "application/json",
                                                   "text": '{"token":"SYNTHETIC_PRIVATE_TOKEN"}'}}},
        {"comment": "Use", "startedDateTime": "2026-11-02T00:00:01Z", "time": 100,
         "request": {"method": "GET", "url": "https://example.invalid/use",
                     "headers": [{"name": "X-Token", "value": "SYNTHETIC_PRIVATE_TOKEN"}]},
         "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "ok"}}},
    ]}}))
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(source, workspace)
    return app, workspace


def tree(workspace):
    return {p.name: p.read_bytes() for p in workspace.iterdir()}


def test_analyze_is_value_free_read_only_and_candidates_require_explicit_inspect(tmp_path):
    app, workspace = setup(tmp_path)
    before = tree(workspace)
    result = app.analyze(workspace)
    assert result["data"]["candidate_count"] >= 1
    assert "SYNTHETIC_PRIVATE_TOKEN" not in json.dumps(result)
    assert result["data"]["model_changes_applied"] is False
    assert app.summary(workspace)["data"]["variable_count"] == 0
    explicit = app.inspect(workspace, section="candidates")
    assert "SYNTHETIC_PRIVATE_TOKEN" in explicit["data"]["content"]
    assert tree(workspace) == before


def test_report_fragments_reassemble_exported_exact_document(tmp_path):
    app, workspace = setup(tmp_path)
    before = tree(workspace)
    first = app.report(workspace, limit=173)
    chunks = [first["data"]["content"]]
    result = first
    while result["continuation"]:
        result = app.report(workspace, **result["continuation"])
        assert result["data"]["report_sha256"] == first["data"]["report_sha256"]
        chunks.append(result["data"]["content"])
    document = "".join(chunks).encode()
    assert hashlib.sha256(document).hexdigest() == first["data"]["report_sha256"]
    assert json.loads(document)["analysis"]["candidates"]
    destination = tmp_path / "report"
    exported = app.report(workspace, output=destination)
    assert exported["data"]["report_sha256"] == first["data"]["report_sha256"]
    assert (destination / "report.json").read_bytes() == document
    assert tree(workspace) == before


def test_report_cli_metadata_hides_private_values_and_export_does_not_overwrite(tmp_path, capsys):
    app, workspace = setup(tmp_path)
    destination = tmp_path / "report"
    args = ["report", "--workspace", str(workspace), "--output", str(destination)]
    assert main(args) == 0
    assert "SYNTHETIC_PRIVATE_TOKEN" not in capsys.readouterr().out
    assert main(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"][0]["code"] == "REPORT_DESTINATION_EXISTS"


def test_report_revision_and_comparison_are_pinned_despite_later_edit(tmp_path):
    app, workspace = setup(tmp_path)
    first = app.report(workspace, limit=80)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    group = json.loads(app.inspect(workspace, section="groups")["data"]["content"])[0]["id"]
    change = tmp_path / "change.json"
    change.write_text(json.dumps({
        "schema_version": "1.0", "change_id": "rename", "base_revision": 1,
        "author": "fixture", "rationale": "Synthetic rename",
        "evidence": [{"exchange_id": entity, "location": "/comment"}],
        "operations": [{"op": "rename_group", "group_id": group, "name": "Changed"}],
    }))
    app.changes_apply(workspace, change)
    next_page = app.report(workspace, **first["continuation"])
    assert next_page["scenario_revision"] == 1
    assert next_page["data"]["report_sha256"] == first["data"]["report_sha256"]
    assert app.report(workspace)["data"]["report_sha256"] != first["data"]["report_sha256"]
    with pytest.raises(Har2LoadError) as error:
        app.report(workspace, revision=1, compare_revision=2)
    assert error.value.code == "REPORT_BASELINE_INVALID"
    second = app.report(workspace, limit=80)
    assert second["continuation"]["compare_revision"] == 1
    document = json.loads(change.read_text())
    document.update(change_id="rename-again", base_revision=2)
    document["operations"][0]["name"] = "Changed again"
    change.write_text(json.dumps(document))
    app.changes_apply(workspace, change)
    pinned = app.report(workspace, **second["continuation"])
    assert pinned["scenario_revision"] == 2
    assert pinned["data"]["compare_revision"] == 1
    assert pinned["data"]["report_sha256"] == second["data"]["report_sha256"]


def test_reports_and_analysis_never_open_network_or_execute_code(tmp_path, monkeypatch):
    app, workspace = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected execution")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    app.analyze(workspace)
    app.report(workspace, output=tmp_path / "report")


def test_report_explains_lifecycle_provenance_uses_and_recorded_failure(tmp_path):
    app, workspace = setup(tmp_path)
    entries = app.summary(workspace)["data"]["entries"]
    producer, consumer = [item["id"] for item in entries]
    change = tmp_path / "parameters.json"
    change.write_text(json.dumps({
        "schema_version": "1.0", "change_id": "parameters", "base_revision": 1,
        "author": "test-agent", "rationale": "Explicit uncertain extraction",
        "evidence": [{"exchange_id": producer, "location": "/response/content"}],
        "operations": [
            {"op": "upsert_extractor", "extractor": {
                "id": "ext", "exchange_id": producer, "area": "response_body",
                "selector": {"dialect": "json_path_subset_v1", "expression": "$.missing"},
                "cardinality": "exactly_one", "allow_empty": False,
                "on_failure": "mark_error_and_continue",
            }},
            {"op": "upsert_variable", "variable": {
                "id": "token", "name": "token", "value_type": "string",
                "source": {"kind": "extracted", "extractor_id": "ext"},
                "initialize": {"event": "after_response", "exchange_id": producer},
                "scope": "iteration", "reset": "iteration_start",
                "purpose": "Unverified future token", "sensitive": True,
            }},
            {"op": "upsert_binding", "binding": {
                "id": "use", "variable_id": "token", "exchange_id": consumer,
                "location": {"area": "request_header", "index": 0},
            }},
        ],
    }))
    app.changes_apply(workspace, change)
    destination = tmp_path / "report"
    response = app.report(workspace, output=destination)
    assert response["data"]["statuses"]["captured_extraction"] == "failed"
    report = json.loads((destination / "report.json").read_bytes())
    row = report["variables"][0]
    assert row["lifecycle"]["initialize"]["exchange_id"] == producer
    assert row["provenance"]["author"] == "test-agent"
    assert row["uses"][0]["location"] == {"area": "request_header", "index": 0}
    assert row["failure_policy"]["marker"] == "ERROR_token"
    assert row["verification"][0]["status"] == "missing"
    assert report["diff"]["variables"]["added"] == ["token"]
    assert report["statuses"]["review"] == "not_recorded"
    assert {item["kind"] for item in report["source_map"]} == {
        "variable", "initialize", "reset", "extractor", "binding",
    }


@pytest.mark.parametrize("options", [
    {"format": "unknown"}, {"offset": -1}, {"limit": 0}, {"compare_revision": True},
])
def test_report_invalid_arguments_write_nothing(tmp_path, options):
    app, workspace = setup(tmp_path)
    before = tree(workspace)
    with pytest.raises(Har2LoadError):
        app.report(workspace, **options)
    assert tree(workspace) == before


def test_report_export_rejects_fragment_options_without_creating_directory(tmp_path):
    app, workspace = setup(tmp_path)
    destination = tmp_path / "report"
    with pytest.raises(Har2LoadError):
        app.report(workspace, output=destination, format="markdown")
    assert not destination.exists()


@pytest.mark.parametrize("schema,producer", [("1.0", "0.1.0a1"), ("1.1", "0.2.0a1")])
def test_legacy_scenario_reports_do_not_upgrade_or_write_workspace(tmp_path, schema, producer):
    from har2load.serialization import canonical_bytes

    app, workspace = setup(tmp_path)
    scenario_path = workspace / "scenario.json"
    model = json.loads(scenario_path.read_bytes())
    model["schema_version"] = schema
    del model["parameters"]
    del model["target_extensions"]
    if schema == "1.0":
        del model["representation_selections"]
    encoded = canonical_bytes(model)
    scenario_path.write_bytes(encoded)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.update(producer_version=producer, scenario_sha256=hashlib.sha256(encoded).hexdigest())
    if schema == "1.0":
        manifest["schema_version"] = "1.0"
        del manifest["head_sha256"], manifest["initial_scenario_sha256"]
    else:
        manifest["initial_scenario_sha256"] = manifest["scenario_sha256"]
    manifest_path.write_bytes(canonical_bytes(manifest))
    before = tree(workspace)
    app.analyze(workspace)
    app.report(workspace, output=tmp_path / "report")
    report = json.loads((tmp_path / "report" / "report.json").read_bytes())
    assert report["scenario_schema_version"] == schema
    assert report["variables"] == []
    assert tree(workspace) == before
