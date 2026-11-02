import hashlib
import json

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError


def setup(tmp_path, body='{"value":"{literal}"}', method="POST"):
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [{
        "comment": "Create & inspect", "request": {"method": method,
            "url": "https://example.invalid/create?x=%2F+x", "headers": [],
            "postData": {"mimeType": "application/json", "text": body}},
        "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "ok"}},
    }]}}))
    app, workspace = Application(), tmp_path / "w"
    app.import_capture(source, workspace)
    return app, workspace


def tree(workspace):
    return {p.name: p.read_bytes() for p in workspace.iterdir()}


def test_generate_check_is_read_only_then_publishes_a_digest_linked_pair(tmp_path):
    app, workspace = setup(tmp_path)
    before = tree(workspace)
    checked = app.generate(workspace)
    assert checked["data"]["write_performed"] is False
    assert checked["data"]["targets"]["jmeter"]["status"] == "generated"
    assert checked["data"]["targets"]["loadrunner"]["project_status"] == "incomplete"
    assert tree(workspace) == before
    output = tmp_path / "generated"
    written = app.generate(workspace, output=output)
    assert written["data"]["report_sha256"] == checked["data"]["report_sha256"]
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert (output / "jmeter/scenario.jmx").exists()
    assert (output / "loadrunner/Action.c").exists()
    assert not list(output.rglob("*.usr"))
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    report = json.loads((output / "report.json").read_bytes())
    assert report["targets"]["jmeter"]["status"] == "generated"
    assert report["statuses"]["native_validation"] == "not_run"
    assert report["generation"]["profile"] == "static_literals_v1"
    for row in report["source_map"]:
        if row.get("artifact") is not None:
            assert manifest["files"][row["artifact"]] == row["artifact_sha256"]
    assert tree(workspace) == before
    with pytest.raises(Har2LoadError) as error:
        app.generate(workspace, output=output)
    assert error.value.code == "GENERATION_DESTINATION_EXISTS"


def test_target_specific_rejection_keeps_other_artifacts_and_returns_nonzero(tmp_path, capsys):
    app, workspace = setup(tmp_path, '{"literal":"${PRIVATE_CAPTURED_FUNCTION}"}')
    output = tmp_path / "partial"
    assert main(["generate", "--workspace", str(workspace), "--output", str(output)]) == 3
    response = capsys.readouterr().out
    assert "PRIVATE_CAPTURED_FUNCTION" not in response
    result = json.loads(response)
    assert result["data"]["targets"]["jmeter"]["status"] == "blocked"
    assert result["data"]["targets"]["loadrunner"]["status"] == "generated"
    assert not (output / "jmeter").exists()
    assert (output / "loadrunner/Action.c").exists()
    assert (output / "report.json").exists()


def test_all_blocked_generation_writes_only_diagnostic_artifacts(tmp_path):
    app, workspace = setup(tmp_path, method="CONNECT")
    output = tmp_path / "blocked"
    result = app.generate(workspace, output=output)
    assert result["status"] == "error"
    assert all(target["status"] == "blocked" for target in result["data"]["targets"].values())
    assert {p.name for p in output.iterdir()} == {"manifest.json", "report.json", "report.md"}


def test_literal_binding_is_mapped_to_actual_request_artifacts(tmp_path):
    app, workspace = setup(tmp_path, '{"value":7}')
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    source = tmp_path / "literal.json"
    source.write_text(json.dumps({"schema_version": "1.0", "change_id": "literal",
        "base_revision": 1, "author": "fixture", "rationale": "Explicit immutable numeric constant",
        "evidence": [{"exchange_id": entity, "location": "/request/postData"}],
        "operations": [{"op": "upsert_variable", "variable": {"id": "v", "name": "value",
            "value_type": "integer", "source": {"kind": "literal", "value": 42},
            "initialize": {"event": "iteration_start"}, "scope": "iteration",
            "reset": "iteration_start", "purpose": "Constant replacement", "sensitive": False}},
            {"op": "upsert_binding", "binding": {"id": "b", "variable_id": "v",
                "exchange_id": entity,
                "location": {"area": "request_json", "pointer": "/value"}}}]}))
    app.changes_apply(workspace, source)
    output = tmp_path / "generated"
    app.generate(workspace, output=output)
    report = json.loads((output / "report.json").read_bytes())
    bindings = [row for row in report["source_map"] if row["kind"] == "binding"]
    assert len(bindings) == 2
    assert all(row["artifact"] and row["artifact_sha256"] for row in bindings)
    assert all(row["disposition"] == "constant_materialized" for row in bindings)
    assert any(d["kind"] == "literal_variable_folded" for d in report["generation"]["decisions"])


def test_generation_never_calls_native_or_network(tmp_path, monkeypatch):
    app, workspace = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Generation must not execute")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("os.system", forbidden)
    app.generate(workspace, output=tmp_path / "generated")


@pytest.mark.parametrize("targets", [(), ("invalid-private",), ("jmeter", "jmeter")])
def test_invalid_target_selection_writes_nothing(tmp_path, targets):
    app, workspace = setup(tmp_path)
    output = tmp_path / "generated"
    with pytest.raises(Har2LoadError):
        app.generate(workspace, targets=targets, output=output)
    assert not output.exists()


def test_failed_native_write_leaves_no_completion_manifest(tmp_path, monkeypatch):
    from pathlib import Path

    app, workspace = setup(tmp_path)
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.name == "scenario.jmx":
            raise OSError("PRIVATE_IO_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    destination = tmp_path / "failed"
    with pytest.raises(Har2LoadError) as error:
        app.generate(workspace, output=destination)
    assert error.value.code == "GENERATION_WRITE_FAILED"
    assert "PRIVATE_IO_ERROR" not in str(error.value)
    assert not (destination / "manifest.json").exists()
    with pytest.raises(Har2LoadError) as repeated:
        app.generate(workspace, output=destination)
    assert repeated.value.code == "GENERATION_DESTINATION_EXISTS"


def test_concurrent_generators_never_overwrite_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    app, workspace = setup(tmp_path)
    output = tmp_path / "generated"

    def generate():
        try:
            return app.generate(workspace, output=output)["data"]["generation_status"]
        except Har2LoadError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: generate(), range(2)))
    assert sorted(results) == ["GENERATION_DESTINATION_EXISTS", "generated"]
    manifest = json.loads((output / "manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


def test_generation_historical_revision_keeps_artifact_identity(tmp_path):
    app, workspace = setup(tmp_path)
    before = app.generate(workspace)
    snapshot = app.store.load(workspace)
    source = tmp_path / "rename.json"
    source.write_text(json.dumps({
        "schema_version": "1.0", "change_id": "rename", "base_revision": 1,
        "author": "fixture", "rationale": "Rename without losing original artifact identity",
        "evidence": [{"exchange_id": snapshot.scenario["ordered_exchange_ids"][0],
                      "location": "/comment"}],
        "operations": [{"op": "rename_group", "group_id": snapshot.scenario["groups"][0]["id"],
                        "name": "Renamed & step"}]}))
    app.changes_apply(workspace, source)
    historical = app.generate(workspace, revision=1)
    assert historical["data"]["report_sha256"] == before["data"]["report_sha256"]
    assert app.generate(workspace)["data"]["report_sha256"] != before["data"]["report_sha256"]


def test_long_generation_diagnostics_are_revision_and_target_pinned(tmp_path):
    app, workspace = setup(tmp_path)
    capture = json.loads((workspace / "capture.har").read_bytes())
    capture["log"]["entries"] *= 12
    for entry in capture["log"]["entries"]:
        entry["request"]["method"] = "CONNECT"
    source = tmp_path / "many.har"
    source.write_text(json.dumps(capture))
    destination = tmp_path / "many"
    app.import_capture(source, destination)
    result = app.generate(destination, targets=("jmeter",))
    assert result["truncated"] is True
    continuation = result["data"]["diagnostics_continuation"]
    assert continuation == {"section": "generation-diagnostics", "offset": 0,
                            "revision": 1, "pointer": "/jmeter"}
    inspected = json.loads(app.inspect(destination, **continuation)["data"]["content"])
    assert len(inspected) == 12
    assert all(item["target"] == "jmeter" for item in inspected)


def test_generated_debug_report_matches_jmeter_components(tmp_path):
    app, workspace = setup(tmp_path)
    output = tmp_path / "generated"
    app.generate(workspace, output=output)
    report = json.loads((output / "report.json").read_bytes())
    profile = report["debug_profile"]
    assert profile["targets"]["jmeter"]["status"] == "generated"
    assert profile["targets"]["jmeter"]["thread_group"] == "DebugTG"
    assert {item["name"] for item in profile["listeners"]} == {"View Results Tree"}
    ordinary = tmp_path / "ordinary"
    app.report(workspace, output=ordinary)
    ordinary_report = json.loads((ordinary / "report.json").read_bytes())
    assert ordinary_report["debug_profile"]["status"] == "not_generated"


def test_bodyless_entity_request_without_content_type_blocks_jmeter_only(tmp_path):
    app, workspace = setup(tmp_path)
    source = tmp_path / "delete.har"
    source.write_text(json.dumps({"log": {"entries": [{"request": {
        "method": "DELETE", "url": "https://example.invalid/item"},
        "response": {"status": 204}}]}}))
    destination = tmp_path / "delete"
    app.import_capture(source, destination)
    output = tmp_path / "generated"
    result = app.generate(destination, output=output)
    assert result["data"]["targets"]["jmeter"]["status"] == "blocked"
    assert result["data"]["targets"]["loadrunner"]["status"] == "generated"
    assert any(d["code"] == "JMETER_CONTENT_TYPE_REQUIRED" for d in result["diagnostics"])
    assert not (output / "jmeter/scenario.jmx").exists()
    report = json.loads((output / "report.json").read_bytes())
    assert report["debug_profile"]["listeners"] == []
    assert report["debug_profile"]["targets"]["jmeter"]["status"] == "blocked"


def test_selected_empty_query_is_preserved_in_both_generated_targets(tmp_path):
    import xml.etree.ElementTree as ET

    app, _ = setup(tmp_path)
    source = tmp_path / "query.har"
    source.write_text(json.dumps({"log": {"entries": [{"request": {
        "method": "GET", "url": "https://example.invalid/a?",
        "queryString": [{"name": "discard", "value": "1"}]}, "response": {"status": 200}}]}}))
    workspace = tmp_path / "query"
    app.import_capture(source, workspace)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    change = tmp_path / "choice.json"
    change.write_text(json.dumps({
        "schema_version": "1.0", "change_id": "choice", "base_revision": 1,
        "author": "fixture", "rationale": "Keep explicitly selected empty URL query",
        "evidence": [{"exchange_id": entity, "location": "/request/url"}],
        "operations": [{"op": "resolve_representation", "exchange_id": entity,
                        "location": "/request/queryString", "representation": "url"}]}))
    app.changes_apply(workspace, change)
    output = tmp_path / "generated"
    result = app.generate(workspace, output=output)
    assert result["status"] == "ok"
    root = ET.parse(output / "jmeter/scenario.jmx").getroot()
    assert root.find(".//stringProp[@name='HTTPSampler.path']").text == "/a?"
    assert 'URL=https://example.invalid/a\\077' in (output / "loadrunner/Action.c").read_text()


def test_connection_nominated_compression_produces_no_native_artifacts(tmp_path):
    app, _ = setup(tmp_path)
    source = tmp_path / "compressed.har"
    source.write_text(json.dumps({"log": {"entries": [{"request": {
        "method": "POST", "url": "https://example.invalid/", "headers": [
            {"name": "Connection", "value": "Content-Encoding"},
            {"name": "Content-Encoding", "value": "gzip"}],
        "postData": {"mimeType": "text/plain", "text": "payload"}},
        "response": {"status": 200}}]}}))
    workspace, output = tmp_path / "compressed", tmp_path / "generated"
    app.import_capture(source, workspace)
    result = app.generate(workspace, output=output)
    assert result["status"] == "error"
    assert {p.name for p in output.iterdir()} == {"report.json", "report.md", "manifest.json"}
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())
