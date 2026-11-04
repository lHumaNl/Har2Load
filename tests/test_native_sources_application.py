"""Native initial inputs remain private artifact data, not a custom runtime sidecar."""

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from har2load.application import Application
from har2load.domain import Har2LoadError
from har2load.generation_types import TargetBuild


def setup(tmp_path, *, initial="SYNTHETIC_INITIAL_VALUE", generated=None):
    app = Application()
    capture = tmp_path / "synthetic.har"
    capture.write_text(json.dumps({"log": {"entries": [{"request": {
        "method": "GET", "url": "https://synthetic.invalid/?input=recorded&generated=recorded"},
        "response": {"status": 200}}]}}))
    workspace = tmp_path / "workspace"
    app.import_capture(capture, workspace)
    exchange = app.store.load(workspace).scenario["ordered_exchange_ids"][0]
    external = {"kind": "external", "key": "INPUT", "allow_empty": False}
    if initial is not None:
        external["initial_value"] = initial
    operations = []
    for index, (identifier, kind, source) in enumerate((
        ("input", "string", external),
        ("generated", "integer", generated or {"kind": "generated", "generator": "integer",
                                              "minimum": 7, "maximum": 7}),
    )):
        operations += [{"op": "upsert_variable", "variable": {
            "id": identifier, "name": identifier, "value_type": kind,
            "source": source, "scope": "iteration", "reset": "iteration_start",
            "initialize": {"event": "iteration_start"}, "purpose": "Synthetic sources",
            "sensitive": identifier == "input"}}, {"op": "upsert_binding", "binding": {
                "id": "bind_" + identifier, "variable_id": identifier, "exchange_id": exchange,
                "location": {"area": "request_query", "index": index}}}]
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps({"schema_version": "1.0", "change_id": "native-sources",
        "base_revision": 1, "author": "fixture", "rationale": "Native inputs, no sidecar",
        "evidence": [{"exchange_id": exchange, "location": "/request"}],
        "operations": operations}))
    app.changes_apply(workspace, changes)
    return app, workspace


def digest_tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def test_native_input_artifacts_and_generated_sources_are_offline_and_value_private(
    tmp_path, monkeypatch,
):
    initial = "SYNTHETIC_PASSWORD_FOR_ARTIFACT_ONLY"
    app, workspace = setup(tmp_path, initial=initial)
    before = digest_tree(workspace)

    def forbidden(*args, **kwargs):
        raise AssertionError("Preparation tried to read environment or run target code")

    monkeypatch.setattr("os.getenv", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("random.randint", forbidden)
    monkeypatch.setattr("uuid.uuid4", forbidden)
    output = tmp_path / "generated"
    result = app.generate(workspace, output=output)
    assert result["status"] == "ok", result
    assert initial not in json.dumps(result)
    assert digest_tree(workspace) == before
    report = json.loads((output / "report.json").read_bytes())
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert report["report_schema_version"] == "1.5"
    assert report["generation"]["native_input_policy"] == "native_initial_snapshot_v1"
    assert report["generation"]["generator_policy"] == "per_user_generated_scalars_v1"
    assert report["generation"]["missing_input_policy"] == "marker_and_continue"
    assert report["generation"]["native_status_policy"] == "markers_only_native_status_v1"
    assert manifest["native_validation"] == manifest["execution"] == "not_run"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    root = ET.fromstring((output / "jmeter/scenario.jmx").read_bytes())
    udvs = root.findall(".//elementProp[@name='TestPlan.user_defined_variables']")
    assert any(initial in ET.tostring(node, encoding="unicode") for node in udvs)
    assert (output / "loadrunner/native_inputs.prm").is_file()
    tables = list((output / "loadrunner/data").glob("input_*.dat"))
    assert len(tables) == 1 and initial.encode() in tables[0].read_bytes()
    assert not list((output / "jmeter").rglob("*.json"))
    assert not list((output / "loadrunner").rglob("*.json"))
    assert report["targets"]["loadrunner"]["project_status"] == "incomplete"
    instruction = report["external_inputs"][0]["instructions"]
    assert "Explicit initial configuration is supplied" in instruction
    assert "JMeter" in instruction and "VuGen" in instruction
    assert initial not in instruction
    assert "future runtime configuration" not in instruction


def test_unconfigured_input_remains_declared_without_fabricated_value_or_stop(tmp_path):
    app, workspace = setup(tmp_path, initial=None)
    output = tmp_path / "generated"
    result = app.generate(workspace, output=output)
    assert result["status"] == "ok", result
    report = json.loads((output / "report.json").read_bytes())
    variable = next(v for v in report["variables"] if v["id"] == "input")
    assert "initial_value" not in variable["declaration"]["source"]
    assert report["generation"]["missing_input_policy"] == "marker_and_continue"
    instruction = report["external_inputs"][0]["instructions"]
    assert "No explicit initial configuration is supplied" in instruction
    assert "add a UDV entry" in instruction
    assert "regenerate" in instruction
    assert "ERROR_<name>" in instruction
    assert "future runtime configuration" not in instruction
    assert b"lr_abort" not in (output / "loadrunner/Action.c").read_bytes()


def test_out_of_profile_generated_integer_blocks_with_value_free_diagnostics(tmp_path):
    app, workspace = setup(tmp_path, generated={"kind": "generated", "generator": "integer",
                                               "minimum": -(2**64), "maximum": 2**64})
    result = app.generate(workspace)
    assert result["status"] == "error"
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())
    assert "SYNTHETIC_INITIAL_VALUE" not in json.dumps(result)


@pytest.mark.parametrize("initial", ["", "雪", "a,b", 'a"b', "a\nb"])
def test_native_table_limits_preserve_model_and_other_target(tmp_path, initial):
    app, workspace = setup(tmp_path, initial=initial)
    before = digest_tree(workspace)
    output = tmp_path / "partial"
    result = app.generate(workspace, output=output)
    assert result["status"] == "error"
    assert result["data"]["targets"]["jmeter"]["status"] == "generated"
    assert result["data"]["targets"]["loadrunner"]["status"] == "blocked"
    assert (output / "jmeter/scenario.jmx").is_file()
    assert not (output / "loadrunner").exists()
    assert digest_tree(workspace) == before
    report = json.loads((output / "report.json").read_bytes())
    source = report["external_inputs"][0]["declaration"]
    assert source["initial_value"] == initial
    instruction = report["external_inputs"][0]["instructions"]
    assert "Consult each target's generation status" in instruction
    variable = next(v for v in report["variables"] if v["id"] == "input")
    assert variable["formation"]["native_input"]["resolution"] == "not_run"


@pytest.mark.parametrize("path", ["loadrunner/data/../secret.dat", "loadrunner/data/input_x.dat",
                                  "loadrunner/native_inputs.json", "jmeter/scenario.jmx"])
def test_native_input_artifact_paths_are_bounded_and_target_owned(tmp_path, monkeypatch, path):
    app, workspace = setup(tmp_path)
    monkeypatch.setattr("har2load.generation.LoadRunnerGenerator.generate",
                        lambda self, plan: TargetBuild("loadrunner", files={path: b"synthetic"}))
    output = tmp_path / "refused"
    with pytest.raises(Har2LoadError) as error:
        app.generate(workspace, targets=("loadrunner",), output=output)
    assert error.value.code == "GENERATION_PATH_INVALID"
    assert not output.exists()
