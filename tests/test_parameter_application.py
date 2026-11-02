import json

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError
from har2load.serialization import canonical_bytes


def setup(tmp_path, body='{"id":7}', target=7, producer_time=100):
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [
        {"comment": "Create", "startedDateTime": "2026-11-02T00:00:00Z", "time": producer_time,
         "request": {"method": "POST", "url": "https://example.invalid/create",
                     "postData": {"mimeType": "application/json", "text": "{}"}},
         "response": {"status": 200, "content": {"mimeType": "application/json", "text": body}}},
        {"comment": "Use", "startedDateTime": "2026-11-02T00:00:01Z", "time": 100,
         "request": {"method": "POST", "url": "https://example.invalid/use",
                     "postData": {"mimeType": "application/json",
                                  "text": json.dumps({"orderId": target})}},
         "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "ok"}}},
    ]}}))
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(source, workspace)
    entries = app.summary(workspace)["data"]["entries"]
    return app, workspace, entries[0]["id"], entries[1]["id"]


def declarations(producer, consumer):
    extractor = {
        "id": "ext_id", "exchange_id": producer, "area": "response_body",
        "selector": {"dialect": "json_path_subset_v1", "expression": "$.id"},
        "cardinality": "exactly_one", "allow_empty": False,
        "on_failure": "mark_error_and_continue",
    }
    variable = {
        "id": "var_id", "name": "order_id", "value_type": "integer",
        "source": {"kind": "extracted", "extractor_id": "ext_id"},
        "initialize": {"event": "after_response", "exchange_id": producer},
        "scope": "iteration", "reset": "iteration_start", "purpose": "Recorded order identifier",
        "sensitive": False,
    }
    binding = {
        "id": "bind_id", "variable_id": "var_id", "exchange_id": consumer,
        "location": {"area": "request_json", "pointer": "/orderId"},
    }
    return [
        {"op": "upsert_binding", "binding": binding},  # final batch validation, not per-op refs
        {"op": "upsert_variable", "variable": variable},
        {"op": "upsert_extractor", "extractor": extractor},
    ]


def changes(tmp_path, producer, operations, identifier="params", base=1):
    path = tmp_path / f"{identifier}.json"
    path.write_text(json.dumps({
        "schema_version": "1.0", "change_id": identifier, "base_revision": base,
        "author": "synthetic-author", "rationale": "Bind captured response ID to exact JSON value",
        "evidence": [{"exchange_id": producer, "location": "/response/content"}],
        "operations": operations,
    }))
    return path


def files(workspace):
    return {p.name: p.read_bytes() for p in workspace.iterdir()}


def test_full_parameter_batch_dry_run_apply_check_and_historical_read(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path)
    source = changes(tmp_path, producer, declarations(producer, consumer))
    before = files(workspace)
    assert app.changes_validate(workspace, source)["data"]["proposed_revision"] == 2
    assert files(workspace) == before
    app.changes_apply(workspace, source)
    result = app.validate(workspace)
    assert result["scenario_revision"] == 2
    assert result["data"]["captured_failures"] == 0
    assert result["data"]["checks"][0]["status"] == "matched"
    assert "value" not in result["data"]["checks"][0]
    inspected = json.loads(app.inspect(workspace, section="extraction-checks")["data"]["content"])
    assert inspected[0]["value"] == 7
    assert inspected[0]["verification"] == "recorded_response_only"
    assert app.summary(workspace)["data"]["variable_count"] == 1
    assert app.summary(workspace, revision=1)["data"]["variable_count"] == 0
    assert app.changes_apply(workspace, source)["data"]["replayed"] is True


def test_use_before_producer_is_rejected_without_publication(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path)
    operations = declarations(producer, producer)
    before = files(workspace)
    with pytest.raises(Har2LoadError):
        app.changes_apply(workspace, changes(tmp_path, producer, operations))
    assert files(workspace) == before


def test_exact_destination_type_cannot_be_changed_to_quoted_string(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path)
    operations = declarations(producer, consumer)
    operations[1]["variable"]["value_type"] = "string"
    before = files(workspace)
    with pytest.raises(Har2LoadError) as error:
        app.changes_apply(workspace, changes(tmp_path, producer, operations))
    assert error.value.code == "BINDING_TYPE_MISMATCH"
    assert files(workspace) == before


def test_dangling_references_block_removal_but_whole_batch_can_remove(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path)
    app.changes_apply(workspace, changes(tmp_path, producer, declarations(producer, consumer)))
    before = files(workspace)
    with pytest.raises(Har2LoadError):
        app.changes_apply(workspace, changes(tmp_path, producer, [
            {"op": "remove_variable", "id": "var_id"},
        ], "bad-removal", 2))
    assert files(workspace) == before
    app.changes_apply(workspace, changes(tmp_path, producer, [
        {"op": "remove_variable", "id": "var_id"},
        {"op": "remove_extractor", "id": "ext_id"},
        {"op": "remove_binding", "id": "bind_id"},
    ], "remove-all", 2))
    assert app.summary(workspace)["data"]["binding_count"] == 0


def test_recorded_failure_has_marker_and_nonzero_validate_not_fake_success(tmp_path, capsys):
    app, workspace, producer, consumer = setup(tmp_path, body='{"other":7}')
    app.changes_apply(workspace, changes(tmp_path, producer, declarations(producer, consumer)))
    assert main(["validate", "--workspace", str(workspace)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["captured_failures"] == 1
    assert result["data"]["checks"][0]["failure_marker"] == "ERROR_order_id"
    assert result["data"]["target_readiness"] == "not_evaluated"
    assert app.summary(workspace)["scenario_revision"] == 2


def test_explicit_upgrade_preserves_old_revision_and_audit(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path)
    # Recreate the previous release's initial model, not a silent upgrade.
    import hashlib

    scenario_path = workspace / "scenario.json"
    model = json.loads(scenario_path.read_bytes())
    model["schema_version"] = "1.1"
    del model["parameters"]
    scenario_path.write_bytes(canonical_bytes(model))
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["producer_version"] = "0.2.0a1"
    digest = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
    manifest["scenario_sha256"] = manifest["initial_scenario_sha256"] = digest
    manifest_path.write_bytes(canonical_bytes(manifest))
    before = files(workspace)
    assert app.summary(workspace)["data"]["variable_count"] == 0
    with pytest.raises(Har2LoadError):
        app.changes_apply(workspace, changes(tmp_path, producer, declarations(producer, consumer)))
    assert files(workspace) == before
    upgrade = changes(tmp_path, producer, [
        {"op": "upgrade_parameters"}, *declarations(producer, consumer),
    ], "upgrade")
    app.changes_apply(workspace, upgrade)
    old = json.loads(app.inspect(workspace, section="scenario", revision=1)["data"]["content"])
    assert old["schema_version"] == "1.1" and "parameters" not in old
    assert app.validate(workspace)["data"]["captured_failures"] == 0
    assert len(app.store.load(workspace).history) == 1


def test_default_validation_does_not_leak_captured_secret(tmp_path, capsys):
    secret = "PRIVATE_SYNTHETIC_TOKEN_123456789"
    app, workspace, producer, consumer = setup(tmp_path, json.dumps({"id": secret}), secret)
    ops = declarations(producer, consumer)
    ops[1]["variable"]["value_type"] = "string"
    ops[1]["variable"]["sensitive"] = True
    app.changes_apply(workspace, changes(tmp_path, producer, ops))
    assert main(["validate", "--workspace", str(workspace)]) == 0
    result = capsys.readouterr()
    assert secret not in result.out + result.err
    explicit = app.inspect(workspace, section="extraction-checks")
    assert json.loads(explicit["data"]["content"])[0]["value"] == secret


def test_external_inputs_and_unproven_captured_timing(tmp_path, monkeypatch):
    app, workspace, producer, consumer = setup(tmp_path, producer_time=2000)
    ops = declarations(producer, consumer)
    app.changes_apply(workspace, changes(tmp_path, producer, ops))
    checked = app.validate(workspace)
    assert checked["data"]["captured_failures"] == 0
    assert any(d["code"] == "CAPTURE_DEPENDENCY_UNPROVEN" for d in checked["diagnostics"])
    variable = ops[1]["variable"].copy()
    variable["source"] = {"kind": "external", "key": "PRIVATE_INPUT", "allow_empty": False}
    variable["initialize"] = {"event": "iteration_start"}
    with monkeypatch.context() as patch:
        patch.setattr("os.getenv", lambda *args: (_ for _ in ()).throw(AssertionError("Env read")))
        app.changes_apply(workspace, changes(tmp_path, producer, [
            {"op": "upsert_variable", "variable": variable},
        ], "external", 2))
        result = app.validate(workspace)
    assert result["data"]["runtime_inputs"] == "required_at_runtime"
    assert result["data"]["checks"][0]["check_kind"] == "extractor"
    assert result["data"]["checks"][0]["variable_id"] is None


def test_validation_diagnostic_continuation_points_to_correct_revision_and_section(tmp_path):
    app, workspace, producer, consumer = setup(tmp_path, body="{}")
    template = declarations(producer, consumer)
    operations = [template[2]]
    for index in range(12):
        variable = {**template[1]["variable"], "id": f"var_{index}", "name": f"value_{index}"}
        operations.append({"op": "upsert_variable", "variable": variable})
    app.changes_apply(workspace, changes(tmp_path, producer, operations))
    checked = app.validate(workspace)
    continuation = checked["data"]["diagnostics_continuation"]
    assert continuation == {"section": "validation-diagnostics", "offset": 0, "revision": 2}
    all_diagnostics = json.loads(app.inspect(workspace, **continuation)["data"]["content"])
    assert len(all_diagnostics) == 12
    assert all(item["code"] == "CAPTURED_EXTRACTION_FAILED" for item in all_diagnostics)
