import hashlib
import json
from copy import deepcopy

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError
from har2load.scenario import ChangeSet
from har2load.serialization import canonical_bytes
from har2load.workspace import WorkspaceStore


def setup_workspace(tmp_path):
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [{
        "comment": "Captured step", "startedDateTime": "2026-11-02T00:00:00Z", "time": 100,
        "request": {"method": "GET", "url": "https://example.invalid/?x=one",
                    "queryString": [{"name": "x", "value": "two"}]},
        "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "ok"}},
    }]}}))
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    groups = json.loads(app.inspect(workspace, section="groups")["data"]["content"])
    return app, source, workspace, entity, groups[0]["id"]


def write_change(tmp_path, entity, operations, identifier="edit1", base=1):
    path = tmp_path / f"{identifier}.json"
    path.write_text(json.dumps({
        "schema_version": "1.0", "change_id": identifier, "base_revision": base,
        "author": "fixture-author", "rationale": "Explicit synthetic decision",
        "evidence": [{"exchange_id": entity, "location": "/request"}],
        "operations": operations,
    }))
    return path


def files(path):
    return {p.name: p.read_bytes() for p in path.iterdir()}


def test_dry_run_atomic_apply_history_and_historical_inspection(tmp_path):
    app, source, workspace, entity, group = setup_workspace(tmp_path)
    change = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Edited step"},
        {"op": "resolve_representation", "exchange_id": entity,
         "location": "/request/queryString", "representation": "url"},
    ])
    before = files(workspace)
    preview = app.changes_validate(workspace, change)
    assert preview["scenario_revision"] == 1
    assert preview["data"]["proposed_revision"] == 2
    assert preview["data"]["unresolved_conflict_count"] == 0
    assert files(workspace) == before
    result = app.changes_apply(workspace, change)
    assert result["scenario_revision"] == 2
    assert result["data"]["applied_revision"] == 2
    assert result["data"]["replayed"] is False
    assert app.summary(workspace)["scenario_revision"] == 2
    assert app.summary(workspace)["data"]["selected_conflict_count"] == 1
    original = app.inspect(workspace, section="groups", revision=1)
    current = app.inspect(workspace, section="groups")
    assert json.loads(original["data"]["content"])[0]["name"] == "Captured step"
    assert json.loads(current["data"]["content"])[0]["name"] == "Edited step"
    history = app.changes_history(workspace)
    assert history["data"]["total_changes"] == 1
    assert history["data"]["entries"][0]["change_id"] == "edit1"
    assert (workspace / "capture.har").read_bytes() == source.read_bytes()
    raw_request = app.inspect(workspace, entity, section="request")
    assert json.loads(raw_request["data"]["content"])["queryString"][0]["value"] == "two"


def test_invalid_batch_and_stale_write_leave_revision_unchanged(tmp_path):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    bad = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Must not persist"},
        {"op": "rename_group", "group_id": "missing", "name": "Invalid"},
    ])
    before = files(workspace)
    with pytest.raises(Har2LoadError):
        app.changes_apply(workspace, bad)
    assert files(workspace) == before
    good = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Accepted"},
    ], identifier="good")
    app.changes_apply(workspace, good)
    before = files(workspace)
    with pytest.raises(Har2LoadError) as caught:
        app.changes_apply(workspace, bad)
    assert caught.value.code == "REVISION_CONFLICT"
    assert files(workspace) == before


def test_idempotent_apply_and_explicit_undo_are_audited(tmp_path):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    selection = {"op": "resolve_representation", "exchange_id": entity,
                 "location": "/request/queryString", "representation": "queryString"}
    change = write_change(tmp_path, entity, [selection])
    app.changes_apply(workspace, change)
    clear = write_change(tmp_path, entity, [
        {"op": "clear_resolution", "exchange_id": entity, "location": "/request/queryString"},
    ], identifier="undo", base=2)
    app.changes_apply(workspace, clear)
    result = app.changes_apply(workspace, change)
    assert result["scenario_revision"] == 3
    assert result["data"]["applied_revision"] == 2
    assert result["data"]["replayed"] is True
    assert app.summary(workspace)["data"]["unresolved_conflict_count"] == 1
    assert app.changes_history(workspace)["data"]["total_changes"] == 2


def test_continuation_pins_revision_while_later_edits_are_applied(tmp_path):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    first = app.inspect(workspace, section="groups", limit=20)
    assert first["continuation"]["revision"] == 1
    change = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Another step"},
    ])
    app.changes_apply(workspace, change)
    rest = app.inspect(workspace, section="groups", offset=20, revision=1)
    original = json.loads(first["data"]["content"] + rest["data"]["content"])
    assert original[0]["name"] == "Captured step"


def test_cli_changes_commands_emit_json_and_correct_revision(tmp_path, capsys):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    change = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Edited"},
    ])
    for verb, expected in [("validate", 1), ("apply", 2), ("apply", 2)]:
        assert main(["changes", verb, str(change), "--workspace", str(workspace)]) == 0
        result = capsys.readouterr()
        assert json.loads(result.out)["scenario_revision"] == expected
        assert result.err == ""
    assert main(["changes", "history", "--workspace", str(workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["total_changes"] == 1
    assert main(["summary", "--workspace", str(workspace), "--revision", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["scenario_revision"] == 1


def test_old_workspace_is_readable_but_apply_does_not_migrate(tmp_path):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    scenario_path = workspace / "scenario.json"
    scenario = json.loads(scenario_path.read_bytes())
    scenario["schema_version"] = "1.0"
    del scenario["representation_selections"]
    del scenario["parameters"]
    encoded = canonical_bytes(scenario)
    scenario_path.write_bytes(encoded)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.update(schema_version="1.0", producer_version="0.1.0a1",
                    scenario_sha256=hashlib.sha256(encoded).hexdigest())
    del manifest["head_sha256"], manifest["initial_scenario_sha256"]
    manifest_path.write_bytes(canonical_bytes(manifest))
    before = files(workspace)
    assert app.summary(workspace)["scenario_revision"] == 1
    change = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "New"},
    ])
    with pytest.raises(Har2LoadError) as caught:
        app.changes_apply(workspace, change)
    assert caught.value.code == "WORKSPACE_READ_ONLY"
    assert files(workspace) == before


@pytest.mark.parametrize("raw", [b'{"private":', b'[]', b'{"a":1,"a":2}'])
def test_invalid_changeset_json_does_not_leak_or_write(tmp_path, raw):
    app, _, workspace, _, _ = setup_workspace(tmp_path)
    source = tmp_path / "secret_name.json"
    source.write_bytes(raw)
    before = files(workspace)
    with pytest.raises(Har2LoadError) as caught:
        app.changes_validate(workspace, source)
    assert caught.value.code == "CHANGE_INVALID_JSON"
    assert "private" not in str(caught.value) and "secret_name" not in str(caught.value)
    assert files(workspace) == before


def test_cli_stale_conflict_returns_observed_revision_without_writes(tmp_path, capsys):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    operation = {"op": "rename_group", "group_id": group, "name": "Edited"}
    winner = write_change(tmp_path, entity, [operation], identifier="winner")
    stale = write_change(tmp_path, entity, [operation], identifier="stale")
    app.changes_apply(workspace, winner)
    before = files(workspace)
    assert main(["changes", "apply", str(stale), "--workspace", str(workspace)]) == 4
    response = json.loads(capsys.readouterr().out)
    assert response["diagnostics"][0]["code"] == "REVISION_CONFLICT"
    assert response["scenario_revision"] == 2
    assert response["workspace_id"] == app.summary(workspace)["workspace_id"]
    assert files(workspace) == before


def test_under_lock_conflict_keeps_observed_revision_not_later_head(tmp_path, monkeypatch, capsys):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    _, capture, grouping = app._load(workspace)
    operation = {"op": "rename_group", "group_id": group, "name": "Edited"}
    target = write_change(tmp_path, entity, [operation], identifier="race-target")
    winner = json.loads(
        write_change(tmp_path, entity, [operation], identifier="winner").read_bytes()
    )
    later = json.loads(write_change(tmp_path, entity, [operation], identifier="later", base=2)
                       .read_bytes())
    transact = WorkspaceStore.transact

    def commit_document(store, path, document):
        return transact(store, path, document, lambda snapshot: app.editor.apply(
            snapshot.scenario, capture, grouping, ChangeSet.parse(document)
        ))

    def race(store, path, document, transform):
        assert document["change_id"] == "race-target"
        commit_document(store, path, winner)  # occurs after application's precheck
        try:
            return transact(store, path, document, transform)  # under-lock rejection at revision 2
        except Har2LoadError:
            commit_document(store, path, later)  # revision 3 exists before the error is formatted
            raise

    monkeypatch.setattr(WorkspaceStore, "transact", race)
    assert main(["changes", "apply", str(target), "--workspace", str(workspace)]) == 4
    response = json.loads(capsys.readouterr().out)
    assert response["scenario_revision"] == 2
    assert response["workspace_id"] == app.summary(workspace)["workspace_id"]
    assert app.summary(workspace)["scenario_revision"] == 3
    assert [e["change_id"] for e in app.changes_history(workspace)["data"]["entries"]] == [
        "winner", "later",
    ]


def test_semantic_audit_mismatch_is_rejected_even_with_valid_hashes(tmp_path):
    app, _, workspace, entity, group = setup_workspace(tmp_path)
    path = write_change(tmp_path, entity, [
        {"op": "rename_group", "group_id": group, "name": "Expected"},
    ])
    document = json.loads(path.read_bytes())

    def inconsistent_transform(snapshot):
        model = deepcopy(snapshot.scenario)
        model["revision"] = 2
        model["groups"][0]["name"] = "Different"
        return model

    app.store.transact(workspace, document, inconsistent_transform)
    assert app.store.load(workspace).scenario_revision == 2  # storage-level integrity passes
    before = files(workspace)
    with pytest.raises(Har2LoadError) as caught:
        app.summary(workspace)
    assert caught.value.code == "SCENARIO_AUDIT_MISMATCH"
    assert files(workspace) == before
