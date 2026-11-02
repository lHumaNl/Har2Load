"""Synthetic report contracts; reports are private evidence, not runtime execution."""

import hashlib
import json
from copy import deepcopy

import pytest

from har2load.domain import Har2LoadError
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.reporting import ReportBuilder, ReportPublisher
from har2load.scenario import ChangeSet, ScenarioEditor
from har2load.workspace import WorkspaceSnapshot


def inputs():
    capture = HarImporter().parse(json.dumps({"log": {"entries": [{
        "comment": "<script>untrusted</script>",
        "request": {"method": "GET", "url": "https://example.invalid/?id=7"},
        "response": {"status": 200, "content": {"mimeType": "application/json",
                                                   "text": '{"id":7}'}},
    }]}}).encode())
    grouping = StepGrouper().group(capture)
    initial = ScenarioEditor().initial(capture, grouping)
    document = {
        "schema_version": "1.0", "change_id": "declare", "base_revision": 1,
        "author": "fixture", "rationale": "Synthetic decision, not proof",
        "evidence": [{"exchange_id": capture.exchanges[0].id, "location": "/response/content"}],
        "operations": [{"op": "upsert_variable", "variable": {
            "id": "token", "name": "token", "value_type": "string",
            "source": {"kind": "external", "key": "API_TOKEN", "allow_empty": False},
            "initialize": {"event": "iteration_start"}, "scope": "iteration",
            "reset": "iteration_start", "purpose": "```\n<script>bad</script>", "sensitive": True,
        }}],
    }
    model = ScenarioEditor().apply(initial, capture, grouping, ChangeSet.parse(document))
    snapshot = WorkspaceSnapshot("ws_fixture", 2, capture.sha256, capture.raw_bytes, model,
                                 history=({"revision": 2, "change": document},))
    baseline = WorkspaceSnapshot("ws_fixture", 1, capture.sha256, capture.raw_bytes, initial)
    return snapshot, capture, grouping, baseline


def build(**overrides):
    snapshot, capture, grouping, baseline = inputs()
    arguments = dict(snapshot=snapshot, capture=capture, grouping=grouping, baseline=baseline,
                     analysis={"profile": "fixture", "candidates": [], "diagnostics": [],
                               "coverage": {"status": "complete"}, "limits": {}},
                     checks=[], diagnostics=[], conflicts=[])
    arguments.update(overrides)
    return ReportBuilder().build(**arguments)


def test_report_is_deterministic_model_backed_and_explicit_about_runtime():
    artifact = build()
    assert artifact.json_bytes == build().json_bytes
    report = json.loads(artifact.json_bytes)
    assert report["scenario_revision"] == 2
    variable = report["variables"][0]
    assert variable["declaration"]["source"]["key"] == "API_TOKEN"
    assert variable["provenance"]["author"] == "fixture"
    assert variable["knowledge_status"] == "explicit_model_decision"
    assert variable["uses"] == [] and variable["disposition"] == "unused"
    assert report["external_inputs"][0]["resolution"] == "required_at_runtime"
    assert report["statuses"]["review"] == "not_recorded"
    assert report["statuses"]["execution"] == "not_run"
    assert all(item["status"] == "not_generated" for item in report["source_map"])
    assert report["diff"]["variables"]["added"] == ["token"]
    assert artifact.digest == hashlib.sha256(artifact.json_bytes).hexdigest()
    assert artifact.digest.encode() in artifact.markdown_bytes


def test_markdown_does_not_activate_captured_markup_or_fences():
    markdown = build().markdown_bytes.decode()
    assert "\n<script>" not in markdown
    assert "\n```" not in markdown
    assert '    ' in markdown
    assert "Sensitive" in markdown and "not_generated" in markdown


def test_report_keeps_all_checks_candidates_and_never_mutates_inputs():
    snapshot, capture, grouping, baseline = inputs()
    original = deepcopy(snapshot.scenario)
    analysis = {"candidates": [{"id": "candidate", "knowledge_status": "heuristic_candidate",
                                "value": "secret", "sources": [], "consumers": []}],
                "coverage": {"status": "partial"}, "diagnostics": [], "limits": {}}
    checks = [{"extractor_id": "unused", "variable_id": None, "status": "missing"}]
    artifact = build(snapshot=snapshot, capture=capture, grouping=grouping, baseline=baseline,
                     analysis=analysis, checks=checks)
    assert artifact.document["analysis"] == analysis
    assert artifact.document["extraction_checks"] == checks
    assert artifact.document["statuses"]["captured_extraction"] == "failed"
    assert snapshot.scenario == original


def test_report_diff_removal_and_binding_change_are_not_lost():
    snapshot, capture, grouping, baseline = inputs()
    old = deepcopy(snapshot.scenario)
    old["parameters"]["bindings"] = [{"id": "b", "variable_id": "token",
                                       "location": {"area": "request_query", "index": 0}}]
    baseline = WorkspaceSnapshot("ws_fixture", 1, capture.sha256, capture.raw_bytes, old)
    result = build(snapshot=snapshot, baseline=baseline).document["diff"]
    assert result["bindings"]["removed"] == ["b"]
    assert result["variables"]["added"] == []


def test_publish_creates_private_digest_checked_pair_without_overwriting(tmp_path):
    artifact = build()
    destination = tmp_path / "report"
    result = ReportPublisher().publish(artifact, destination)
    assert result["report_sha256"] == artifact.digest
    assert (destination / "report.json").read_bytes() == artifact.json_bytes
    assert (destination / "report.md").read_bytes() == artifact.markdown_bytes
    manifest = json.loads((destination / "manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    with pytest.raises(Har2LoadError) as error:
        ReportPublisher().publish(artifact, destination)
    assert error.value.code == "REPORT_DESTINATION_EXISTS"
    assert (destination / "report.json").read_bytes() == artifact.json_bytes


def test_failed_publish_has_no_completion_manifest(tmp_path, monkeypatch):
    from pathlib import Path

    original = Path.open

    def fail_markdown(self, *args, **kwargs):
        if self.name == "report.md":
            raise OSError("private path must not be echoed")
        return original(self, *args, **kwargs)

    destination = tmp_path / "report"
    monkeypatch.setattr(Path, "open", fail_markdown)
    with pytest.raises(Har2LoadError) as error:
        ReportPublisher().publish(build(), destination)
    assert error.value.code == "REPORT_WRITE_FAILED"
    assert "private path" not in str(error.value)
    assert not (destination / "manifest.json").exists()


def test_report_budget_failure_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("har2load.reporting.MAX_REPORT_BYTES", 20)
    with pytest.raises(Har2LoadError) as error:
        build()
    assert error.value.code == "REPORT_LIMIT"
    assert list(tmp_path.iterdir()) == []


def test_existing_symlink_or_file_destination_is_never_touched(tmp_path):
    target = tmp_path / "manual.txt"
    target.write_text("manual content")
    link = tmp_path / "alias"
    link.symlink_to(target)
    for destination in (target, link):
        with pytest.raises(Har2LoadError) as error:
            ReportPublisher().publish(build(), destination)
        assert error.value.code == "REPORT_DESTINATION_EXISTS"
    assert target.read_text() == "manual content"


def test_markdown_sections_preserve_exact_decimal_and_json_content():
    from decimal import Decimal

    artifact = build(checks=[{"extractor_id": "e", "variable_id": None, "status": "matched",
                             "value": Decimal("1234567890123456789.123456789")}])
    text = artifact.markdown_bytes.decode()
    sections = text.split("\n## ")[1:]
    reconstructed = {}
    for section in sections:
        heading, content = section.split("\n", 1)
        # Lists render each item independently for readability; nonlists are exact JSON.
        key = heading.lower().replace(" ", "_")
        if not isinstance(artifact.document[key], list):
            reconstructed[key] = json.loads("\n".join(
                line[4:] for line in content.splitlines() if line.startswith("    ")
            ), parse_float=Decimal)
    assert reconstructed["parameters"] == artifact.document["parameters"]
    assert "1234567890123456789.123456789" in text


def test_concurrent_report_publishers_have_one_exclusive_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    artifact = build()

    def publish():
        try:
            return ReportPublisher().publish(artifact, tmp_path / "report")["status"]
        except Har2LoadError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(results) == ["REPORT_DESTINATION_EXISTS", "complete"]
    assert (tmp_path / "report" / "report.json").read_bytes() == artifact.json_bytes


def test_removed_variable_and_lifecycle_have_explicit_source_map_dispositions():
    snapshot, capture, grouping, baseline = inputs()
    old = WorkspaceSnapshot("ws_fixture", 1, capture.sha256, capture.raw_bytes, snapshot.scenario)
    empty = deepcopy(baseline.scenario)
    empty["revision"] = 2
    current = WorkspaceSnapshot("ws_fixture", 2, capture.sha256, capture.raw_bytes, empty)
    report = build(snapshot=current, baseline=old).document
    assert report["diff"]["variables"]["removed"] == ["token"]
    assert len(report["source_map"]) == 6
    assert all(row["disposition"] == "removed" for row in report["source_map"])


def test_json_budget_stops_before_serializing_all_repeated_evidence(monkeypatch):
    import har2load.reporting as reporting

    value = "x" * 100_000
    original = reporting.canonical_bytes
    visited = 0

    def counted(item):
        nonlocal visited
        if item == value:
            visited += 1
        return original(item)

    monkeypatch.setattr(reporting, "MAX_REPORT_BYTES", 350_000)
    monkeypatch.setattr(reporting, "canonical_bytes", counted)
    checks = [{"extractor_id": f"e{i}", "variable_id": None, "status": "matched", "value": value}
              for i in range(20)]
    with pytest.raises(Har2LoadError, match="budget"):
        build(checks=checks)
    assert 0 < visited <= 4


def test_markdown_budget_stops_before_fully_rendering_oversized_evidence(monkeypatch):
    import har2load.reporting as reporting

    def huge_lines(value):
        yield "x" * 300_000
        yield "y" * 300_000
        raise AssertionError("Renderer consumed evidence beyond the output budget")

    monkeypatch.setattr(reporting, "MAX_REPORT_BYTES", 500_000)
    monkeypatch.setattr(reporting, "_json_lines", huge_lines)
    with pytest.raises(Har2LoadError, match="budget"):
        ReportBuilder._markdown({"scenario_revision": 1}, "digest")


def test_changed_binding_location_is_included_before_and_after():
    snapshot, capture, _, _ = inputs()
    old = deepcopy(snapshot.scenario)
    new = deepcopy(snapshot.scenario)
    binding = {"id": "b", "variable_id": "token", "exchange_id": capture.exchanges[0].id,
               "location": {"area": "request_query", "index": 0}}
    old["parameters"]["bindings"] = [binding]
    new["parameters"]["bindings"] = [{**binding, "location": {"area": "request_query", "index": 1}}]
    baseline = WorkspaceSnapshot("ws_fixture", 1, capture.sha256, capture.raw_bytes, old)
    current = WorkspaceSnapshot("ws_fixture", 2, capture.sha256, capture.raw_bytes, new)
    diff = build(snapshot=current, baseline=baseline).document["diff"]["bindings"]
    assert diff["changed"] == ["b"]
    assert diff["changes"][0]["before"]["location"]["index"] == 0
    assert diff["changes"][0]["after"]["location"]["index"] == 1


def test_private_permissions_are_set_before_content_write(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    if os.name != "posix":
        pytest.skip("POSIX permission evidence only")
    original = Path.chmod
    calls = []

    def checked(path, mode, **kwargs):
        assert path.stat().st_size == 0
        assert path.parent.stat().st_mode & 0o777 == 0o700
        result = original(path, mode, **kwargs)
        assert path.stat().st_mode & 0o777 == 0o600
        calls.append(path.name)
        return result

    monkeypatch.setattr(Path, "chmod", checked)
    ReportPublisher().publish(build(), tmp_path / "private")
    assert calls == ["report.json", "report.md", "manifest.json"]


@pytest.mark.parametrize("phase", ["open", "write", "flush", "fsync"])
def test_manifest_stage_failures_preserve_artifacts(tmp_path, monkeypatch, phase):
    import os
    from pathlib import Path

    original_open, original_fsync = Path.open, os.fsync
    artifact = build()
    manifest_fd = None

    class FailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def write(self, data):
            if phase == "write":
                self.stream.write(data[:10])
                raise OSError("private")
            return self.stream.write(data)

        def flush(self):
            if phase == "flush":
                raise OSError("private")
            self.stream.flush()

    def opening(path, *args, **kwargs):
        nonlocal manifest_fd
        if path.name != "manifest.json":
            return original_open(path, *args, **kwargs)
        if phase == "open":
            raise OSError("private")
        stream = original_open(path, *args, **kwargs)
        manifest_fd = stream.fileno()
        return FailingStream(stream)

    def syncing(fd):
        if phase == "fsync" and fd == manifest_fd:
            raise OSError("private")
        original_fsync(fd)

    destination = tmp_path / "report"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opening)
        patch.setattr(os, "fsync", syncing)
        with pytest.raises(Har2LoadError) as error:
            ReportPublisher().publish(artifact, destination)
        assert error.value.code == "REPORT_WRITE_FAILED"
    assert (destination / "report.json").read_bytes() == artifact.json_bytes
    assert (destination / "report.md").read_bytes() == artifact.markdown_bytes
    with pytest.raises(Har2LoadError) as error:
        ReportPublisher().publish(artifact, destination)
    assert error.value.code == "REPORT_DESTINATION_EXISTS"


def test_streamed_report_encoding_equals_canonical_json_exactly():
    from har2load.serialization import canonical_bytes

    artifact = build()
    assert artifact.json_bytes == canonical_bytes(artifact.document)
