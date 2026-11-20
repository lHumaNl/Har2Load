"""Opt-in boundaries through actual synthetic HAR import, generation and the public CLI."""

import json
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from har2load.application import Application
from har2load.cli import main
from tests.test_compose_application import digest, hashes

LABELS = ["TF_Buy", "TF_Filter"]
SHARED = "TF_OpenCatalog"


def make_inputs(tmp_path, *, different_region=False):
    app = Application()
    uses = []
    cmd = ["compose", "jmeter"]
    for index, label in enumerate(LABELS):
        paths = [f"/entry{index}", "/catalog", "/catalog/electronics", f"/next{index}"]
        if different_region and index == 1:
            paths[2] += "?region=EU"
        entries = [{"comment": f"Step {j}", "request": {"method": "GET",
                    "url": "https://shop.invalid" + path}, "response": {"status": 200}}
                   for j, path in enumerate(paths)]
        source = tmp_path / (label + ".har")
        source.write_text(json.dumps({"log": {"entries": entries}}))
        ws, out = tmp_path / ("ws" + label), tmp_path / label
        app.import_capture(source, ws)
        assert app.generate(ws, output=out, targets=("jmeter",))["status"] != "error"
        report = json.loads((out / "report.json").read_bytes())
        ids = [g["id"] for g in report["scenario"]["groups"]]
        assert len(ids) == 4
        uses.append({"uc": label, "groups": ids[1:3],
                     "manifest_sha256": digest((out / "manifest.json").read_bytes())})
        cmd.extend(["--uc", label, str(out)])
    doc = {"schema_version": "1.0", "blocks": [{"name": SHARED, "uses": uses,
           "decision": "user_confirmed", "reason": "PRIVATE: same two browsing actions"}]}
    return cmd, doc


def run(cmd, capsys, code=0):
    actual = main(cmd)
    text = capsys.readouterr().out
    assert "PRIVATE" not in text
    assert actual == code, text
    return json.loads(text)


def test_plain_is_default_and_explicit_plan_is_private_and_pinned(tmp_path, capsys):
    cmd, doc = make_inputs(tmp_path)
    p = tmp_path / "decision.json"
    p.write_text(json.dumps(doc))
    plain = tmp_path / "separate"
    run([*cmd, "--output", str(plain)], capsys)
    plain_xml = ET.parse(plain / "jmeter/scenario.jmx")
    assert len(list(plain_xml.iter("TestFragmentController"))) == 2
    sources = hashes(tmp_path)
    checked = run([*cmd, "--shared-steps", str(p), "--check"], capsys)["data"]
    assert checked["shared_block_count"] == 1 and not checked["write_performed"]
    assert hashes(tmp_path) == sources
    dest = tmp_path / "shared"
    written = run([*cmd, "--shared-steps", str(p), "--output", str(dest)], capsys)["data"]
    assert written["files"] == checked["files"]
    for f, sha in written["files"].items():
        assert digest((dest / f).read_bytes()) == sha
    report = json.loads((dest / "composition.json").read_bytes())
    assert report["shared_steps"] == doc
    assert report["shared_steps_sha256"] == digest(p.read_bytes())
    assert report["execution"] == report["native_validation"] == "not_run"
    tree = ET.parse(dest / "jmeter/scenario.jmx")
    assert [t.get("testname") for t in tree.iter("TestFragmentController")].count(SHARED) == 1
    paths = [t.text for t in tree.findall(".//stringProp[@name='HTTPSampler.path']")]
    assert paths.count("/catalog") == paths.count("/catalog/electronics") == 1
    assert all(paths.count(p) == 1 for p in ("/entry0", "/next0", "/entry1", "/next1"))
    module_paths = tree.findall(".//collectionProp[@name='ModuleController.node_path']")
    assert sum(p[-1].text == SHARED for p in module_paths) == 2
    assert all(digest((tmp_path / f).read_bytes()) == sha for f, sha in sources.items())


def test_documented_shared_plan_and_both_cli_recipes(tmp_path, capsys):
    _, real = make_inputs(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    guide = (repo / "skills/har2load/references/jmeter-composition.md").read_text()
    section = guide.split("## Write and check the plan", 1)[1].split("\n## ", 1)[0]
    template = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S)[1])
    paths = {"generated/catalog": str(tmp_path / LABELS[0]),
             "generated/search": str(tmp_path / LABELS[1]),
             "generated/common-shared": str(tmp_path / "derived"),
             "shared.json": str(tmp_path / "example")}
    block = template["blocks"][0]
    for example, source in zip(block["uses"], real["blocks"][0]["uses"], strict=True):
        paths[example["uc"]] = source["uc"]
        assert set(example) == set(source) and len(example["groups"]) == len(source["groups"])
        example.update(source)
    Path(paths["shared.json"]).write_text(json.dumps(template))
    commands = re.search(r"```sh\n(.*?)\n```", section, re.S)[1].splitlines()
    assert len(commands) == 2
    checked, published = (run([paths.get(word, word) for word in shlex.split(line)[1:]], capsys)
                          for line in commands)
    assert not checked["data"]["write_performed"] and published["data"]["write_performed"]
    assert checked["data"]["files"] == published["data"]["files"]


@pytest.mark.parametrize("mode", ["region", "noncontiguous", "reversed", "unknown", "stale",
                                  "undecided", "duplicate_flag"])
def test_refused_candidate_never_publishes_or_silently_falls_back(tmp_path, capsys, mode):
    cmd, doc = make_inputs(tmp_path, different_region=mode == "region")
    use = doc["blocks"][0]["uses"][1]
    if mode == "noncontiguous":
        report = json.loads((tmp_path / LABELS[1] / "report.json").read_bytes())
        groups = report["scenario"]["groups"]
        use["groups"] = [groups[0]["id"], groups[2]["id"]]
    elif mode == "reversed":
        use["groups"].reverse()
    elif mode == "unknown":
        use["groups"][1] = "no_such_group"
    elif mode == "stale":
        use["manifest_sha256"] = "e" * 64
    elif mode == "undecided":
        doc["blocks"][0]["decision"] = "possible"
    p = tmp_path / "pending.json"
    p.write_text(json.dumps(doc))
    flag = ["--shared-steps", str(p)]
    dest = tmp_path / "refused"
    before = hashes(tmp_path)
    denied = run([*cmd, *(flag * (2 if mode == "duplicate_flag" else 1)), "--output", str(dest)],
                 capsys, code=2 if mode in ("undecided", "duplicate_flag") else 3)
    assert denied["diagnostics"][0]["code"].startswith("COMPOSE_SHARED_")
    assert not dest.exists() and hashes(tmp_path) == before
