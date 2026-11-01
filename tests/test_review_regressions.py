"""Synthetic cross-layer regressions from independent first-slice review."""

import json
from decimal import Decimal

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.serialization import canonical_bytes, metadata_object


def imported(content):
    raw = json.dumps({"log": {"entries": [{
        "request": {"method": "GET", "url": "https://example.invalid/"},
        "response": {"status": 200, "content": content},
    }]}}).encode()
    return HarImporter().parse(raw).exchanges[0].response_body


@pytest.mark.parametrize("mime", [None, "", "image/png", "application/octet-stream"])
@pytest.mark.parametrize("encoded", [False, True])
def test_recorded_empty_is_empty_independent_of_mime(mime, encoded):
    content = {"text": ""}
    if mime is not None:
        content["mimeType"] = mime
    if encoded:
        content["encoding"] = "base64"
    body = imported(content)
    assert body.availability == "empty"
    assert body.text == ""
    assert body.view_status == "available"


@pytest.mark.parametrize("content,state,view", [
    ({}, "unavailable", "unavailable"),
    ({"mimeType": "image/png", "text": "/w==", "encoding": "base64"}, "present", "nontext"),
    ({"text": "not_base64!", "encoding": "base64"}, "invalid", "invalid"),
    ({"text": "", "encoding": "unknown"}, "present", "unsupported"),
    ({"text": "abc", "encoding": "unknown"}, "present", "unsupported"),
])
def test_source_presence_is_separate_from_decoded_view(content, state, view):
    body = imported(content)
    assert body.availability == state
    assert body.view_status == view
    assert body.text is None


@pytest.mark.parametrize("second,groups", [
    ("05.599999999", 1), ("05.600", 2), ("05.600000001", 2),
])
def test_epoch_scale_inclusive_gap(second, groups):
    entries = [
        {"startedDateTime": "2026-11-01T00:00:00.400Z", "time": 200},
        {"startedDateTime": f"2026-11-01T00:00:{second}Z", "time": 0},
    ]
    capture = HarImporter().parse(json.dumps({"log": {"entries": entries}}).encode())
    assert len(StepGrouper().group(capture).groups) == groups


@pytest.mark.parametrize("left,right,conflict", [
    ('profile="https://example.invalid/a;x=1;y=2;z"',
     'profile="https://example.invalid/a;y=2;x=1;z"', True),
    ('profile="a;\\\"b"; charset=UTF-8', 'charset="utf-8"; profile="a;\\\"b"', False),
    ('profile="a";profile="b"', 'profile="b";profile="a"', True),
    ('charset=UTF-8;profile="a;b"', 'profile="a;b"; charset="utf-8"', False),
])
def test_mime_quoted_parameter_comparison(left, right, conflict):
    raw = {"log": {"entries": [{
        "request": {"method": "POST", "url": "https://example.invalid/",
                    "headers": [{"name": "Content-Type", "value": "application/ld+json;" + left}],
                    "postData": {"mimeType": "application/ld+json;" + right, "text": "{}"}},
        "response": {"status": 200},
    }]}}
    capture = HarImporter().parse(json.dumps(raw).encode())
    found = any(d.code == "HAR_REPRESENTATION_CONFLICT" for d in capture.diagnostics)
    assert found is conflict


def test_precision_sensitive_source_numbers_remain_exact(tmp_path):
    source = tmp_path / "source.har"
    source.write_bytes(b'{"log":{"entries":[{"_vendor":{"amount":9007199254740993.0,'
                       b'"tiny":1e-400,"decimal":0.12345678901234567890123456789}}]}}')
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    result = app.inspect(workspace, entity, pointer="/_vendor")
    precise = json.loads(result["data"]["content"], parse_float=Decimal)
    assert precise == {
        "amount": Decimal("9007199254740993.0"), "tiny": Decimal("1e-400"),
        "decimal": Decimal("0.12345678901234567890123456789"),
    }
    assert canonical_bytes(precise)  # persistence uses the same lossless encoder


def test_manifest_privacy_state_is_explicit_and_checked(tmp_path):
    source = tmp_path / "source.har"
    source.write_text('{"log":{"entries":[]}}')
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["redaction"] == "off"
    assert manifest["contains_sensitive_data"] is True
    assert manifest["privacy_warning"]
    manifest["redaction"] = "on"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(Har2LoadError):
        app.summary(workspace)


def test_large_finite_exponents_are_not_silently_converted_to_infinity():
    capture = HarImporter().parse(b'{"log":{"entries":[{"_vendor":1e9999}]}}')
    raw = capture.exchanges[0].raw
    assert raw["_vendor"] == Decimal("1e9999")
    assert b'1E+9999' in canonical_bytes(raw)


def test_unrepresentable_decimal_exponent_is_a_safe_typed_error():
    raw = b'{"log":{"entries":[1e999999999999999999999999999999999]}}'
    with pytest.raises(Har2LoadError):
        HarImporter().parse(raw)


def test_fractional_timezone_offsets_are_diagnosed_not_rounded():
    raw = {"log": {"entries": [{
        "startedDateTime": "2026-11-01T00:00:00+00:00:00.5", "time": 0,
    }]}}
    capture = HarImporter().parse(json.dumps(raw).encode())
    assert capture.exchanges[0].start_time is None
    assert any(item.code == "HAR_INVALID_TIMING" for item in capture.diagnostics)


def test_decimal_serialization_remains_valid_json_with_escaping():
    value = {"z\"": [Decimal("1e-400"), "\ud800", "雪\n"], "a": Decimal("-0.0")}
    encoded = canonical_bytes(value)
    assert encoded.startswith(b'{"a":-0.0,')
    assert json.loads(encoded, parse_float=Decimal) == value
    assert metadata_object(encoded) == value
    with pytest.raises(ValueError):
        canonical_bytes(Decimal("NaN"))


def test_unrepresentable_metadata_decimal_is_safe():
    with pytest.raises(Har2LoadError) as error:
        metadata_object(b'{"a":1e999999999999999999999999999999999}')
    assert error.value.code == "WORKSPACE_INVALID"


def test_maximum_unicode_locator_and_fragment_fit_response_budget(tmp_path, capsys):
    key = "😀" * 255
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [{key: "😀" * 5000}]}}))
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    assert main(["inspect", "--workspace", str(workspace), "--exchange", entity,
                 "--pointer", "/" + key, "--limit", "4096"]) == 0
    result = capsys.readouterr()
    assert len(result.out.encode()) <= 65536
    assert json.loads(result.out)["continuation"] is not None


@pytest.mark.parametrize("gap", [1e-6, 1e-7, 1e20, 0.1, 1.25, 0, 5.0])
def test_non_default_threshold_workspace_roundtrip(tmp_path, gap):
    source = tmp_path / "source.har"
    source.write_text('{"log":{"entries":[]}}')
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace, gap)
    assert app.summary(workspace)["data"]["step_gap_seconds"] == gap
    assert app.inspect(workspace, section="groups")["data"]["content"] == "[]"
    scenario = {"step_gap_seconds": gap}
    encoded = canonical_bytes(scenario)
    assert canonical_bytes(metadata_object(encoded)) == encoded


@pytest.mark.parametrize("mime", [
    "application/x-www-form-urlencoded; charset=ISO-8859-1", "multipart/form-data",
])
def test_recorded_nonfile_params_retain_source_presence_without_reconstruction(mime):
    source = {"log": {"entries": [{
        "request": {"method": "POST", "url": "https://example.invalid/", "postData": {
            "mimeType": mime, "params": [{"name": "x", "value": "é"}],
        }},
        "response": {"status": 200},
    }]}}
    capture = HarImporter().parse(json.dumps(source).encode())
    body = capture.exchanges[0].request_body
    assert body.availability == "present"
    assert body.representation == "params"
    assert body.view_status == "unsupported"
    assert body.text is None


def test_long_pointer_rejected_with_truthful_recovery(tmp_path, capsys):
    key = "x" * 70000
    source = tmp_path / "source.har"
    source.write_text(json.dumps({"log": {"entries": [{key: "v"}]}}))
    workspace = tmp_path / "w"
    app = Application()
    app.import_capture(source, workspace)
    entity = app.summary(workspace)["data"]["entries"][0]["id"]
    result = main(["inspect", "--workspace", str(workspace), "--exchange", entity,
                   "--pointer", "/" + key, "--limit", "1"])
    captured = capsys.readouterr()
    assert result == 2
    assert len(captured.out.encode()) < 65536
    diagnostic = json.loads(captured.out)["diagnostics"][0]
    assert diagnostic["code"] == "LOCATION_TOO_LONG"
    assert "parent" in diagnostic["message"].lower()
    assert "smaller view" not in diagnostic["message"]
