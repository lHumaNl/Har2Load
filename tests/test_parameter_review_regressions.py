import json

import pytest

from har2load.application import Application
from har2load.cli import main


def workspace(tmp_path, *, deep=False, body='{"id":7}'):
    entry = {
        "request": {"method": "GET", "url": "https://example.invalid/"},
        "response": {"status": 200,
                     "headers": [{"name": "Content-Type", "value": "image/png"},
                                 {"name": "X-Token", "value": "recorded-token"}],
                     "content": {"mimeType": "application/json", "text": body}},
    }
    text = json.dumps(entry)
    if deep:
        text = text[:-1] + ',"_unrelated":' + '{"x":' * 600 + "0" + "}" * 600 + "}"
    source = tmp_path / "source.har"
    source.write_text('{"log":{"entries":[' + text + ']}}')
    app = Application()
    path = tmp_path / "w"
    app.import_capture(source, path)
    entity = app.summary(path)["data"]["entries"][0]["id"]
    return app, path, entity


def apply(tmp_path, app, path, entity, *, area=None, expression="$.id"):
    operations = [{
        "op": "resolve_representation", "exchange_id": entity,
        "location": "/response/content/mimeType", "representation": "mimeType",
    }]
    if area:
        operations.append({"op": "upsert_extractor", "extractor": {
            "id": "ext_only", "exchange_id": entity, "area": area,
            "selector": ({"dialect": "json_path_subset_v1", "expression": expression}
                         if area == "response_body" else {"dialect": "name_v1", "name": "X-Token"}),
            "cardinality": "exactly_one", "allow_empty": False,
            "on_failure": "mark_error_and_continue",
        }})
    source = tmp_path / "changes.json"
    source.write_text(json.dumps({
        "schema_version": "1.0", "change_id": "choices", "base_revision": 1,
        "author": "fixture", "rationale": "Explicit synthetic fixture choice",
        "evidence": [{"exchange_id": entity, "location": "/response/content"}],
        "operations": operations,
    }))
    app.changes_apply(path, source)


def test_unreferenced_missing_extractor_is_checked(tmp_path, capsys):
    app, path, entity = workspace(tmp_path)
    apply(tmp_path, app, path, entity, area="response_body", expression="$.missing")
    assert main(["validate", "--workspace", str(path)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["extractor_count"] == result["data"]["checked_extractor_count"] == 1
    assert result["data"]["captured_failures"] == 1
    check = result["data"]["checks"][0]
    assert check["variable_id"] is None and check["extractor_id"] == "ext_only"
    assert check["status"] == "missing" and check["failure_marker"] is None
    assert result["diagnostics"][0]["entity_id"] == "ext_only"


@pytest.mark.parametrize("area", [None, "response_headers", "response_body"])
def test_deep_unrelated_extension_cannot_break_selected_body_validation(tmp_path, capsys, area):
    app, path, entity = workspace(tmp_path, deep=True)
    original = (path / "capture.har").read_bytes()
    apply(tmp_path, app, path, entity, area=area)
    assert main(["validate", "--workspace", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["check_count"] == (0 if area is None else 1)
    if area:
        assert result["data"]["checks"][0]["status"] == "matched"
    assert (path / "capture.har").read_bytes() == original


def test_projection_limit_is_unverified_not_internal_error(tmp_path, capsys):
    app, path, entity = workspace(tmp_path, body="x" * (1024 * 1024 + 1))
    apply(tmp_path, app, path, entity, area="response_body")
    assert main(["validate", "--workspace", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["captured_unverified"] == 1
    assert result["data"]["checks"][0]["status"] == "limit_exceeded"
