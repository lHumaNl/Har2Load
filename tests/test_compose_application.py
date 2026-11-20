"""Exercise the public composition boundary with real, strictly synthetic generated bundles."""

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from har2load.application import Application
from har2load.cli import main


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def hashes(root):
    return {p.relative_to(root): digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def make_bundle(root, name, target="jmeter"):
    app = Application()
    source, ws, bundle = (root / (name + suffix) for suffix in (".har", "-ws", "-gen"))
    source.write_text(json.dumps({"log": {"entries": [{
        "request": {"method": "GET", "url": "https://synthetic.invalid/PRIVATE_" + name},
        "response": {"status": 200}}]}}))
    app.import_capture(source, ws)
    targets = ("jmeter", "loadrunner") if target == "both" else (target,)
    assert app.generate(ws, output=bundle, targets=targets)["status"] != "error"
    return bundle


def invoke(capsys, a, b, *options, code=0, labels=("TF_UC1", "TF_UC1.1")):
    actual = main(["compose", "jmeter", "--uc", labels[0], str(a), "--uc", labels[1], str(b),
                   *map(str, options)])
    public = capsys.readouterr().out
    assert "PRIVATE_" not in public and str(a) not in public
    assert actual == code, public
    return json.loads(public)


def test_cli_composition_is_deterministic_private_and_manifest_last(tmp_path, capsys, monkeypatch):
    a, b = make_bundle(tmp_path, "first", "both"), make_bundle(tmp_path, "second")
    original = hashes(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Composition must never start native processes or contact endpoints")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    check = invoke(capsys, a, b, "--check")["data"]
    assert not check["write_performed"] and hashes(tmp_path) == original
    dest = tmp_path / "combined"
    result = invoke(capsys, a, b, "--output", dest)["data"]
    assert result["write_performed"] and result["files"] == check["files"]
    manifest = json.loads((dest / "manifest.json").read_bytes())
    assert manifest["kind"] == "composed_jmeter_artifacts" and manifest["status"] == "complete"
    assert manifest["execution"] == manifest["native_validation"] == "not_run"
    assert set(manifest["files"]) == {"jmeter/scenario.jmx", "composition.json"}
    for name, expected in manifest["files"].items():
        assert digest((dest / name).read_bytes()) == expected
    report = json.loads((dest / "composition.json").read_bytes())
    assert [s["label"] for s in report["sources"]] == ["TF_UC1", "TF_UC1.1"]
    for uc, bundle in zip(report["sources"], (a, b), strict=True):
        assert uc["manifest_sha256"] == digest((bundle / "manifest.json").read_bytes())
        assert uc["jmx_sha256"] == digest((bundle / "jmeter/scenario.jmx").read_bytes())
        assert uc["scenario_revision"] == 1
    saved = hashes(dest)
    error = invoke(capsys, a, b, "--output", dest, code=3)
    assert error["diagnostics"][0]["code"] == "COMPOSE_DESTINATION_EXISTS"
    assert hashes(dest) == saved
    assert all(digest((tmp_path / p).read_bytes()) == h for p, h in original.items())
    invoke(capsys, dest, a, "--check", code=3)  # Derived products have a distinct contract.


@pytest.mark.parametrize("tamper", ["jmeter/scenario.jmx", "report.json", "report.md",
                                     "loadrunner/Action.c"])
def test_checks_every_listed_source_digest_before_writing(tmp_path, capsys, tamper):
    a, b = make_bundle(tmp_path, "a", "both"), make_bundle(tmp_path, "b")
    with (a / tamper).open("ab") as stream:
        stream.write(b"\n")
    dest = tmp_path / "failed"
    result = invoke(capsys, b, a, "--output", dest, code=3)
    assert "UC input #2" in result["diagnostics"][0]["message"]
    assert not dest.exists()


@pytest.mark.parametrize("mutation", ["path", "absent", "replaced-report", "symlink-dir",
                                      "symlink-file", "raw", "wrong-kind", "blocked-target"])
def test_rejects_nonbundles_and_inconsistent_or_unsafe_inputs(tmp_path, capsys, mutation):
    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    path = a / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if mutation == "path":
        manifest["files"]["../foreign"] = "0" * 64
    elif mutation == "absent":
        del manifest["files"]["report.json"]
    elif mutation == "replaced-report":
        raw = (b / "report.json").read_bytes()
        (a / "report.json").write_bytes(raw)
        manifest["files"]["report.json"] = manifest["report_sha256"] = digest(raw)
    elif mutation == "symlink-dir":
        (a / "jmeter").rename(a / "moved")
        (a / "jmeter").symlink_to(a / "moved", target_is_directory=True)
    elif mutation == "symlink-file":
        (a / "report.md").rename(a / "elsewhere")
        (a / "report.md").symlink_to(a / "elsewhere")
    elif mutation == "raw":
        a = a / "jmeter/scenario.jmx"
    elif mutation == "wrong-kind":
        manifest["kind"] = "composed_jmeter_artifacts"
    else:
        manifest["targets"]["jmeter"]["status"] = "blocked"
    path.write_text(json.dumps(manifest))
    invoke(capsys, a, b, "--check", code=3)


def test_output_inside_source_refused(tmp_path, capsys):
    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    invoke(capsys, a, b, "--output", a / "bad", code=3)
    assert not (a / "bad").exists()


def test_output_inside_source_via_case_alias(tmp_path, capsys):
    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    alias = a.with_name(a.name.upper())
    if not alias.exists() or not a.samefile(alias):
        pytest.skip("Filesystem is case-sensitive")
    invoke(capsys, a, b, "--output", alias / "derived", code=3)
    assert not (a / "derived").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_input_with_junction_refused(tmp_path, capsys):
    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    external = tmp_path / "other"
    (a / "jmeter").rename(external)
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(a / "jmeter"), str(external)],
                   check=True)
    invoke(capsys, a, b, "--check", code=3)


def test_genuine_partial_accepted_but_aggregate_status_must_be_consistent(tmp_path, capsys):
    from tests.test_cookie_application import setup

    app, ws, producer, _ = setup(tmp_path, host_only=True)
    a = tmp_path / "partial"
    result = app.generate(ws, output=a, initial_cookies=(producer + ":/response/cookies/0",))
    assert result["data"]["generation_status"] == "partial"
    b = make_bundle(tmp_path, "other")
    dest = tmp_path / "composed"
    invoke(capsys, a, b, "--output", dest)
    report = json.loads((dest / "composition.json").read_bytes())
    assert report["sources"][0]["source_generation_status"] == "partial"
    path = a / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["generation_status"] = "generated"
    path.write_text(json.dumps(manifest))
    invoke(capsys, a, b, "--check", code=3)


def test_partial_write_has_no_completion_manifest_and_retry_uses_new_path(tmp_path, capsys,
                                                                         monkeypatch):
    from har2load.reporting import ReportPublisher

    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    dest = tmp_path / "incomplete"
    write = ReportPublisher.write_file

    def fail_report(path, data):
        if path.name == "composition.json":
            raise OSError("injected failure")
        write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(ReportPublisher, "write_file", staticmethod(fail_report))
        invoke(capsys, a, b, "--output", dest, code=5)
    assert (dest / "jmeter/scenario.jmx").is_file() and not (dest / "manifest.json").exists()
    invoke(capsys, a, b, "--output", dest, code=3)
    invoke(capsys, a, b, "--output", tmp_path / "retry")


def test_aggregate_source_budget_fails_closed(tmp_path, capsys, monkeypatch):
    from har2load.composition import _Budget

    a, b = make_bundle(tmp_path, "a"), make_bundle(tmp_path, "b")
    size = sum(p.stat().st_size for p in a.rglob("*") if p.is_file())
    monkeypatch.setattr("har2load.composition._Budget", lambda: _Budget(remaining=size))
    assert invoke(capsys, a, b, "--check", code=3)["diagnostics"][0]["code"] == "COMPOSE_LIMIT"


@pytest.mark.parametrize("label", ["TF_UC1", "tf_UC1", "TF_uc1", "TF_../a", "TF_${x}"])
def test_labels_rejected_before_reading_inputs(tmp_path, capsys, label):
    error = invoke(capsys, Path("not-read"), Path("not-read2"), "--check",
                   labels=("TF_UC1", label), code=2)
    assert error["diagnostics"][0]["code"] == "COMPOSE_LABEL_INVALID"


def test_documented_compose_recipes_work_with_generated_inputs(tmp_path, capsys, monkeypatch):
    """The README example paths are relative to the user's working directory."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    examples = [shlex.split(line)[1:] for line in readme.splitlines()
                if line.startswith("har2load compose jmeter ")]
    assert len(examples) == 2
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    for name in ("catalog", "search"):
        make_bundle(root, name).rename(root / name)
    for args in examples:
        assert main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["data"]["uc_count"] == 2
    assert (root / "common-debug" / "manifest.json").is_file()
