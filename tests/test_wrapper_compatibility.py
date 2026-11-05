"""Cross-producer workspace reads must remain valid after the package version bump."""

import json

import pytest

from har2load.application import Application
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.scenario import ScenarioEditor


@pytest.mark.parametrize("producer", [
    "0.5.0a1", "0.6.0a1", "0.7.0a1", "0.8.0a1", "0.9.0a1", "0.10.0a1",
])
def test_previous_extension_producers_remain_readable_and_explicitly_upgradable(
    tmp_path, monkeypatch, producer,
):
    raw = json.dumps({"log": {"entries": [{
        "request": {"method": "GET", "url": "https://synthetic.invalid/"},
        "response": {"status": 200}}]}}).encode()
    capture = HarImporter().parse(raw)
    model = ScenarioEditor().initial(capture, StepGrouper().group(capture), schema_version="1.3")
    app, workspace = Application(), tmp_path / "workspace"
    with monkeypatch.context() as patch:
        patch.setattr("har2load.workspace.__version__", producer)
        app.store.create(workspace, raw, model)
    before = {p.name: p.read_bytes() for p in workspace.iterdir()}
    assert app.store.load(workspace).scenario["schema_version"] == "1.3"
    assert app.generate(workspace)["status"] == "ok"
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == before
    changes = tmp_path / "upgrade.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "upgrade",
        "base_revision": 1, "author": "fixture", "rationale": "Explicit runtime upgrade",
        "evidence": [{"exchange_id": capture.exchanges[0].id, "location": "/request"}],
        "operations": [{"op": "upgrade_extension_runtime"}]}))
    app.changes_apply(workspace, changes)
    assert app.store.load(workspace).scenario["schema_version"] == "1.4"
    assert app.store.load(workspace, 1).scenario == model
    assert (workspace / "capture.har").read_bytes() == raw
    assert (workspace / "scenario.json").read_bytes() == before["scenario.json"]
