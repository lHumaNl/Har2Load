"""Preparation must never execute captured instructions or contact endpoints."""

import json
import socket
import subprocess

import pytest

from har2load.application import Application
from har2load.cli import main


def test_captured_instructions_remain_data(tmp_path, monkeypatch):
    sentinel = tmp_path / "must_not_exist"
    instruction = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('executed')"
    source = tmp_path / "input.har"
    source.write_text(json.dumps({"log": {"entries": [{
        "comment": instruction,
        "request": {"method": "POST", "url": "https://example.invalid/", "postData": {
            "mimeType": "text/plain", "text": instruction,
        }},
        "response": {"status": 200, "content": {"text": instruction}},
    }]}}))

    def forbidden(*args, **kwargs):
        raise AssertionError("Preparation attempted external execution or network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    app = Application()
    workspace = tmp_path / "w"
    app.import_capture(source, workspace)
    summary = app.summary(workspace)
    entity = summary["data"]["entries"][0]["id"]
    result = app.inspect(workspace, entity, section="request-body")
    assert result["data"]["content"] == instruction
    assert not sentinel.exists()


@pytest.mark.parametrize("kind", ["text", "headers", "url", "comment", "response", "time"])
def test_malformed_captured_values_never_leak_in_summary_or_errors(tmp_path, capsys, kind):
    secret = "SYNTHETIC_SECRET_VALUE_MUST_NOT_LEAK"
    entry = {"request": {"method": "GET", "url": "https://example.invalid/"},
             "response": {"status": 200, "content": {}}}
    if kind == "text":
        entry["response"]["content"]["text"] = {"private": secret}
    elif kind == "headers":
        entry["request"]["headers"] = secret
    elif kind == "url":
        entry["request"]["url"] = {"private": secret}
    elif kind == "comment":
        entry["comment"] = {"private": secret}
    elif kind == "response":
        entry["response"] = secret
    else:
        entry["time"] = secret
    source = tmp_path / "input.har"
    source.write_text(json.dumps({"log": {"entries": [entry]}}))
    workspace = tmp_path / "w"
    assert main(["import", str(source), "--workspace", str(workspace)]) == 0
    result = capsys.readouterr()
    assert secret not in result.out + result.err
    assert main(["summary", "--workspace", str(workspace)]) == 0
    result = capsys.readouterr()
    assert secret not in result.out + result.err
