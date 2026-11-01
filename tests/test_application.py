import json
import socket

import pytest

from har2load.application import Application
from har2load.domain import Har2LoadError


def capture(tmp_path, count=3, body="a%2Fb", comment="Step"):
    entries = [
        {
            "startedDateTime": f"2026-01-01T00:00:{i:02d}Z",
            "time": 100,
            "comment": comment,
            "request": {"method": "POST", "url": "https://example.invalid/path?token=SECRET",
                        "queryString": [{"name": "token", "value": "SECRET"}],
                        "postData": {"mimeType": "text/plain", "text": body}},
            "response": {"status": 200, "content": {"mimeType": "text/plain", "text": body}},
            "vendor/key": {"~value": "sentinel"},
        }
        for i in range(count)
    ]
    source = tmp_path / "synthetic.har"
    source.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return source


def test_import_summary_and_explicit_inspection(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Network access forbidden")

    monkeypatch.setattr(socket, "socket", no_network)
    source = capture(tmp_path)
    workspace = tmp_path / "w"
    app = Application()
    imported = app.import_capture(source, workspace)
    assert imported["data"]["exchange_count"] == 3
    assert imported["data"]["group_count"] == 1
    assert "SECRET" not in json.dumps(imported)
    assert "example.invalid" not in json.dumps(imported)
    summary = app.summary(workspace, offset=0, limit=2)
    assert len(summary["data"]["entries"]) == 2
    assert summary["truncated"]
    assert summary["continuation"]["offset"] == 2
    entry_id = summary["data"]["entries"][0]["id"]
    fragment = app.inspect(workspace, entry_id, section="request-body", offset=1, limit=3)
    assert fragment["data"]["content"] == "%2F"
    assert fragment["data"]["total_characters"] == 5
    assert fragment["data"]["format"] == "text"
    assert fragment["continuation"]["offset"] == 4
    selected = app.inspect(workspace, entry_id, pointer="/vendor~1key/~0value")
    assert json.loads(selected["data"]["content"]) == "sentinel"
    assert (workspace / "capture.har").read_bytes() == source.read_bytes()


def test_large_content_can_be_reconstructed(tmp_path):
    body = "λ😀${not_code}" * 1000
    source = capture(tmp_path, count=1, body=body)
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    entry_id = app.summary(workspace)["data"]["entries"][0]["id"]
    fragments = []
    offset = 0
    while True:
        result = app.inspect(workspace, entry_id, section="response-body", offset=offset, limit=511)
        fragments.append(result["data"]["content"])
        assert len(json.dumps(result).encode()) <= 65536
        if result["continuation"] is None:
            break
        offset = result["continuation"]["offset"]
    assert "".join(fragments) == body


@pytest.mark.parametrize("pointer", ["bad", "/~2", "/missing", "/request/url/0"])
def test_bad_pointer_has_safe_error(tmp_path, pointer):
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(capture(tmp_path), workspace)
    entry_id = app.summary(workspace)["data"]["entries"][0]["id"]
    with pytest.raises(Har2LoadError):
        app.inspect(workspace, entry_id, pointer=pointer)


def test_invalid_import_does_not_create_workspace(tmp_path):
    source = tmp_path / "secret_name.har"
    source.write_text("private data not JSON")
    workspace = tmp_path / "w"
    with pytest.raises(Har2LoadError) as error:
        Application().import_capture(source, workspace)
    assert not workspace.exists()
    assert "private data" not in str(error.value)


def test_groups_and_diagnostics_are_selectively_readable(tmp_path):
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(capture(tmp_path), workspace)
    groups = app.inspect(workspace, section="groups")
    assert json.loads(groups["data"]["content"])[0]["name"] == "Step"
    diagnostics = app.inspect(workspace, section="diagnostics")
    assert isinstance(json.loads(diagnostics["data"]["content"]), list)


def test_summary_bounds_untrusted_strings_and_diagnostics(tmp_path):
    app = Application()
    source = capture(tmp_path, comment="X" * 100000)
    workspace = tmp_path / "w"
    imported = app.import_capture(source, workspace)
    assert len(json.dumps(imported).encode()) < 65536
    assert len(json.dumps(app.summary(workspace)).encode()) < 65536


@pytest.mark.parametrize("offset,limit", [(-1, 20), (0, 0), (0, 101)])
def test_summary_page_range_validation(tmp_path, offset, limit):
    with pytest.raises(Har2LoadError):
        Application().summary(tmp_path, offset=offset, limit=limit)


def test_bad_duration_does_not_discard_valid_ordering_timestamp(tmp_path):
    source = capture(tmp_path, count=3, comment="")
    raw = json.loads(source.read_text())
    entries = raw["log"]["entries"]
    entries[0]["time"] = None
    entries.reverse()
    source.write_text(json.dumps(raw))
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(source, workspace)
    result = app.summary(workspace)
    assert [item["source_index"] for item in result["data"]["entries"]] == [2, 1, 0]
    assert result["data"]["group_count"] == 1  # unknown interval prevents later gaps


def test_entry_diagnostic_count_includes_body_issues(tmp_path):
    source = capture(tmp_path, count=1)
    raw = json.loads(source.read_text())
    raw["log"]["entries"][0]["response"]["content"]["encoding"] = "unknown"
    source.write_text(json.dumps(raw))
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(source, workspace)
    result = app.summary(workspace)
    assert result["data"]["entries"][0]["diagnostic_count"] > 0
