"""Header sources use the existing explicit correlation model, never name heuristics."""

import hashlib
import json

import pytest
from test_runtime_application import digest_tree, setup


@pytest.mark.parametrize("name", ["X-Token", "x-token", "X-TOKEN"])
def test_header_source_generation_maps_privacy_and_revision_are_preserved(
    tmp_path, monkeypatch, name,
):
    app, workspace = setup(tmp_path, selector={"dialect": "name_v1", "name": name},
                           area="response_headers")
    before = digest_tree(workspace)
    model = app.store.load(workspace).scenario

    def forbidden(*args, **kwargs):
        pytest.fail("Preparation must not resolve or execute native header sources")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    output = tmp_path / "generated"
    checked = app.generate(workspace, revision=2)
    result = app.generate(workspace, output=output, revision=2)
    assert checked["status"] == result["status"] == "ok", result
    assert checked["data"]["report_sha256"] == result["data"]["report_sha256"]
    assert "recorded" not in json.dumps(result)
    assert digest_tree(workspace) == before
    report = json.loads((output / "report.json").read_bytes())
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert report["report_schema_version"] == "1.7"
    assert report["generation"]["response_header_policy"] == "native_response_headers_v1"
    assert report["generation"]["native_status_policy"] == "markers_only_native_status_v1"
    assert report["generation"]["runtime_validation"] == "not_run"
    assert report["generation"]["profile_version"] == "5"
    for target in ("jmeter", "loadrunner"):
        assert report["targets"][target]["profile_version"] == "5"
        assert report["targets"][target]["response_header_policy"] == "native_response_headers_v1"
        rows = [r for r in report["source_map"]
                if r["target"] == target and r["kind"] == "extractor"]
        assert len(rows) == 1 and rows[0]["status"] == "generated"
        assert rows[0]["artifact_sha256"] == manifest["files"][rows[0]["artifact"]]
        for path in (output / target).rglob("*"):
            if path.is_file():
                assert b"recorded" not in path.read_bytes()
    for path, digest in manifest["files"].items():
        assert hashlib.sha256((output / path).read_bytes()).hexdigest() == digest
    source = report["variables"][0]["formation"]["native_header"]
    assert source["refresh"] == "declared_producer_attempt"
    assert source["cookie_jar_used"] is False and source["har_parity"] == "unverified"
    assert model == app.store.load(workspace).scenario
    assert app.generate(workspace, revision=1)["status"] == "ok"


def test_unused_header_extractor_is_not_dropped(tmp_path):
    app, workspace = setup(tmp_path, selector={"dialect": "name_v1", "name": "X-Token"},
                           area="response_headers", unused=True)
    output = tmp_path / "generated"
    assert app.generate(workspace, output=output)["status"] == "ok"
    report = json.loads((output / "report.json").read_bytes())
    assert len([r for r in report["source_map"] if r["kind"] == "extractor"
                and r["status"] == "generated"]) == 2


def test_cookie_extraction_remains_separate_from_native_cookie_jar(tmp_path):
    app, workspace = setup(tmp_path, selector={"dialect": "name_v1", "name": "SID"},
                           area="response_cookies")
    result = app.generate(workspace)
    assert result["status"] == "error"
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())


def test_out_of_native_header_profile_does_not_mutate_valid_model(tmp_path):
    app, workspace = setup(tmp_path, selector={"dialect": "name_v1", "name": "X Token"},
                           area="response_headers")
    before = digest_tree(workspace)
    result = app.generate(workspace)
    assert result["status"] == "error"
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())
    assert digest_tree(workspace) == before
