"""Native cookie opt-in is explicit, private, revision-pinned and never live preparation."""

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from har2load.application import Application
from har2load.cli import main
from har2load.domain import Har2LoadError

SECRET = "SYNTHETIC_COOKIE_SEED"


def hashes(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def setup(tmp_path, runtime=False, *, host_only=False, expires=None, same_site=None):
    app = Application()
    capture = tmp_path / "synthetic.har"
    cookie = {"name": "session", "value": SECRET, "domain": "example.invalid",
              "path": "/", "secure": True, "httpOnly": True}
    if host_only:
        cookie.pop("domain")
    if expires is not None:
        cookie["expires"] = expires
    if same_site is not None:
        cookie["sameSite"] = same_site
    capture.write_text(json.dumps({"log": {"entries": [
        {"request": {"method": "GET", "url": "https://example.invalid/login"},
         "response": {"status": 200, "cookies": [cookie]}},
        {"request": {"method": "GET", "url": "https://example.invalid/account",
                     "headers": [{"name": "Cookie", "value": "session=" + SECRET}],
                     "cookies": [cookie]}, "response": {"status": 200}},
    ]}}))
    workspace = tmp_path / "workspace"
    app.import_capture(capture, workspace)
    producer, consumer = app.store.load(workspace).scenario["ordered_exchange_ids"]
    if runtime:
        change = tmp_path / "runtime.json"
        change.write_text(json.dumps({"schema_version": "1.0", "change_id": "runtime",
            "base_revision": 1, "author": "fixture", "rationale": "Cookie runtime integration",
            "evidence": [{"exchange_id": producer, "location": "/response"}],
            "operations": [{"op": "upsert_variable", "variable": {
                "id": "v", "name": "v", "value_type": "string",
                "source": {"kind": "generated", "generator": "uuid"},
                "initialize": {"event": "iteration_start"}, "scope": "iteration",
                "reset": "iteration_start", "purpose": "Synthetic runtime", "sensitive": False}}]}))
        app.changes_apply(workspace, change)
    return app, workspace, producer, consumer


@pytest.mark.parametrize("runtime", [False, True])
def test_default_native_jar_is_empty_and_recorded_cookie_is_not_replayed(tmp_path, runtime):
    app, workspace, _, _ = setup(tmp_path, runtime)
    before = hashes(workspace)
    output = tmp_path / "default"
    result = app.generate(workspace, output=output)
    assert result["status"] != "error", result
    assert result["data"]["cookie_policy"] == "native_cookie_jar_v1"
    assert result["data"]["initial_cookie_count"] == 0
    assert SECRET not in json.dumps(result)
    root = ET.fromstring((output / "jmeter/scenario.jmx").read_bytes())
    managers = root.findall(".//CookieManager")
    assert len(managers) == 1 and managers[0].get("enabled") == "true"
    assert len(managers[0].find("collectionProp[@name='CookieManager.cookies']")) == 0
    for path in list((output / "jmeter").rglob("*")) + list((output / "loadrunner").rglob("*")):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
    assert b"web_add_cookie(" not in (output / "loadrunner/Action.c").read_bytes()
    report = json.loads((output / "report.json").read_bytes())
    assert report["generation"]["initial_cookies"] == []
    assert report["generation"]["cookie_authentication"] == "unverified"
    assert hashes(workspace) == before


@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("area", ["request", "response"])
def test_selected_cookies_publish_native_configuration_without_mutating_model(
    tmp_path, monkeypatch, runtime, area,
):
    app, workspace, producer, consumer = setup(tmp_path, runtime)
    revision = 2 if runtime else 1
    before = hashes(workspace)
    exchange = producer if area == "response" else consumer
    selection = exchange + f":/{area}/cookies/0"

    def forbidden(*args, **kwargs):
        pytest.fail("Cookie preparation must not access live time, network or runtime")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    output = tmp_path / "seeded"
    result = app.generate(workspace, output=output, revision=revision,
                          initial_cookies=(selection,))
    assert result["status"] != "error", result
    assert result["data"]["initial_cookie_count"] == 1
    assert SECRET not in json.dumps(result) and selection not in json.dumps(result)
    assert hashes(workspace) == before
    report = json.loads((output / "report.json").read_bytes())
    assert report["report_schema_version"] == ("1.7" if runtime else "1.3")
    seed = report["generation"]["initial_cookies"][0]
    assert seed["value"] == SECRET and seed["source_exchange_id"] == exchange
    assert seed["source_area"] == area and seed["source_index"] == 0
    assert report["generation"]["cookie_initialization"] == "iteration_start_after_clear"
    manifest = json.loads((output / "manifest.json").read_bytes())
    for path, digest in manifest["files"].items():
        assert hashlib.sha256((output / path).read_bytes()).hexdigest() == digest
    assert manifest["execution"] == manifest["native_validation"] == "not_run"
    root = ET.fromstring((output / "jmeter/scenario.jmx").read_bytes())
    cookies = root.findall(".//CookieManager/collectionProp/elementProp")
    assert len(cookies) == 1
    assert cookies[0].find("stringProp[@name='Cookie.value']").text == SECRET
    assert SECRET.encode() in (output / "loadrunner/Action.c").read_bytes()
    assert b'web_add_header("Cookie"' not in (output / "loadrunner/Action.c").read_bytes()
    assert app.generate(workspace, revision=revision)["data"]["initial_cookie_count"] == 0


def test_cookie_selection_cli_is_repeatable_opt_in_without_values_on_command_line(tmp_path, capsys):
    app, workspace, producer, _ = setup(tmp_path)
    selection = producer + ":/response/cookies/0"
    assert main(["generate", "--workspace", str(workspace), "--check",
                 "--initial-cookie", selection]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["initial_cookie_count"] == 1
    assert SECRET not in json.dumps(result)
    before = hashes(workspace)
    output = tmp_path / "rejected"
    with pytest.raises(Har2LoadError):
        app.generate(workspace, output=output, initial_cookies=(selection, selection))
    assert not output.exists() and hashes(workspace) == before


def test_flag_specific_diagnostics_do_not_offer_default_only_inspection(tmp_path, monkeypatch):
    from har2load.domain import Diagnostic
    from har2load.generation_types import TargetBuild

    app, workspace, producer, _ = setup(tmp_path)
    errors = tuple(Diagnostic("COOKIE_UNSUPPORTED", "error", "Unsupported cookie capability.")
                   for _ in range(12))
    monkeypatch.setattr("har2load.generation.JMeterGenerator.generate",
                        lambda self, plan: TargetBuild("jmeter", diagnostics=errors))
    result = app.generate(workspace, targets=("jmeter",),
                          initial_cookies=(producer + ":/response/cookies/0",))
    assert result["status"] == "error"
    assert result["data"]["diagnostics_truncated"]
    assert result["data"]["diagnostics_continuation"] is None
    assert "same options" in result["data"]["diagnostics_complete_in"]


def test_cookie_seed_selection_remains_pinned_after_head_advances(tmp_path):
    app, workspace, producer, _ = setup(tmp_path)
    selections = (producer + ":/response/cookies/0",)
    old = app.generate(workspace, revision=1, initial_cookies=selections)
    model = app.store.load(workspace).scenario
    change = tmp_path / "rename.json"
    change.write_text(json.dumps({"schema_version": "1.0", "change_id": "rename",
        "base_revision": 1, "author": "fixture", "rationale": "Advance editable head",
        "evidence": [{"exchange_id": producer, "location": "/request"}],
        "operations": [{"op": "rename_group", "group_id": model["groups"][0]["id"],
                        "name": "Renamed after first generation"}]}))
    app.changes_apply(workspace, change)
    before = hashes(workspace)
    pinned = app.generate(workspace, revision=1, initial_cookies=selections)
    assert pinned["data"]["report_sha256"] == old["data"]["report_sha256"]
    assert pinned["scenario_revision"] == 1
    assert app.generate(workspace, initial_cookies=selections)["scenario_revision"] == 2
    assert app.generate(workspace, revision=1)["data"]["report_sha256"] != (
        pinned["data"]["report_sha256"])
    assert hashes(workspace) == before


@pytest.mark.parametrize("options,generated,blocked", [
    ({"host_only": True}, "jmeter", "loadrunner"),
    ({"expires": "2030-01-01T00:00:00Z"}, "loadrunner", "jmeter"),
])
def test_native_cookie_capabilities_refuse_scope_changes_but_keep_other_target(
    tmp_path, options, generated, blocked,
):
    app, workspace, producer, _ = setup(tmp_path, **options)
    before = hashes(workspace)
    output = tmp_path / "partial"
    result = app.generate(workspace, output=output,
                          initial_cookies=(producer + ":/response/cookies/0",))
    assert result["status"] == "error"
    assert result["data"]["generation_status"] == "partial"
    assert result["data"]["targets"][generated]["status"] == "generated"
    assert result["data"]["targets"][blocked]["status"] == "blocked"
    assert (output / generated).is_dir() and not (output / blocked).exists()
    assert SECRET not in json.dumps(result)
    assert hashes(workspace) == before
    report = json.loads((output / "report.json").read_bytes())
    assert report["generation"]["initial_cookies"][0]["value"] == SECRET


def test_samesite_is_not_silently_dropped_and_default_does_not_import_it(tmp_path):
    app, workspace, producer, _ = setup(tmp_path, same_site="Strict")
    assert app.generate(workspace)["status"] != "error"
    result = app.generate(workspace, initial_cookies=(producer + ":/response/cookies/0",))
    assert result["status"] == "error"
    assert all(t["status"] == "blocked" for t in result["data"]["targets"].values())


def test_cli_selects_same_name_cookies_with_distinct_paths(tmp_path, capsys):
    app = Application()
    source = tmp_path / "synthetic.har"
    source.write_text(json.dumps({"log": {"entries": [{
        "request": {"method": "GET", "url": "https://example.invalid/app/login"},
        "response": {"status": 200, "cookies": [
            {"name": "sid", "value": "root_seed", "domain": "example.invalid", "path": "/"},
            {"name": "sid", "value": "app_seed", "domain": "example.invalid", "path": "/app"},
        ]}}]}}))
    workspace, output = tmp_path / "workspace", tmp_path / "seeded"
    app.import_capture(source, workspace)
    exchange = app.store.load(workspace).scenario["ordered_exchange_ids"][0]
    assert main(["generate", "--workspace", str(workspace), "--output", str(output),
                 "--initial-cookie", exchange + ":/response/cookies/0",
                 "--initial-cookie", exchange + ":/response/cookies/1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["initial_cookie_count"] == 2
    assert "root_seed" not in json.dumps(result) and "app_seed" not in json.dumps(result)
    root = ET.fromstring((output / "jmeter/scenario.jmx").read_bytes())
    cookies = root.findall(".//CookieManager/collectionProp/elementProp")
    assert len(cookies) == 2 and [c.get("testname") for c in cookies] == ["sid", "sid"]
    assert [c.find("stringProp[@name='Cookie.path']").text for c in cookies] == ["/", "/app"]
    assert (output / "loadrunner/Action.c").read_bytes().count(b"web_add_cookie(") == 2
