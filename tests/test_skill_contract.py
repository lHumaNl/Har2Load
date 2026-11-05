"""Executable skill examples and resource checks, not proof of LLM decision quality."""

import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest

from har2load.cli import main, parser

SKILL = Path(__file__).resolve().parents[1] / "skills" / "har2load"


def hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


def invoke(capsys, *args, code=0):
    assert main(list(args)) == code
    return json.loads(capsys.readouterr().out)


def example():
    document = (SKILL / "references" / "model.md").read_text()
    return json.loads(re.search(r"```json\n(.*?)\n```", document, re.S)[1])


def test_skill_metadata_and_local_resources_are_complete():
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\nname: har2load\ndescription: ")
    metadata, body = text[4:].split("\n---\n", 1)
    assert len(metadata.splitlines()) == 2
    assert len(body.splitlines()) < 500
    assert "TODO" not in text and "[replace" not in text.lower()
    assert "references/intake.md" in body and "references/review.md" in body
    references = set()
    for path in SKILL.rglob("*.md"):
        content = path.read_text()
        assert "/Users/" not in content and "/private/var/" not in content
        assert "TODO" not in content
        for link in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in link or link.startswith("#"):
                continue
            target = (path.parent / link.split("#", 1)[0]).resolve()
            assert target.is_relative_to(SKILL.resolve()) and target.is_file(), link
            references.add(target)
    assert set((SKILL / "references").glob("*.md")) <= references
    assert not list(SKILL.rglob("*.py"))  # The skill orchestrates the CLI; no hidden runner.


def test_documented_cli_recipes_parse_with_the_current_cli(capsys):
    text = (SKILL / "references" / "cli.md").read_text()
    commands = re.search(r"```sh\n(.*?)\n```", text, re.S)[1].splitlines()
    substitutions = {"H2L": "/private/tool with spaces/har2load", "REV": "1", "NEW_REV": "2",
        "HAR": "input file.har", "WORKSPACE": "workspace one", "NEW_WORKSPACE": "workspace new",
        "CHANGESET": "change.json", "EXCHANGE_ID": "exchange-id",
        "NEW_REPORT_DIRECTORY": "new report", "NEW_OUTPUT_DIRECTORY": "new output"}
    for command in commands:
        argv = shlex.split(command)
        argv = [substitutions.get(word.removeprefix("$"), word) for word in argv]
        if argv[1] in ("--version", "--help"):
            with pytest.raises(SystemExit) as error:
                parser().parse_args(argv[1:])
            assert error.value.code == 0
        else:
            parser().parse_args(argv[1:])
    capsys.readouterr()


@pytest.mark.parametrize("operation", ["report", "history"])
def test_documented_recipes_preserve_review_identity(operation):
    text = (SKILL / "references" / "cli.md").read_text()
    commands = re.search(r"```sh\n(.*?)\n```", text, re.S)[1].splitlines()
    for command in commands:
        argv = shlex.split(command)
        if operation == "report" and argv[1] == "report":
            assert "--compare-revision" in argv
            assert argv[argv.index("--compare-revision") + 1] == "$REV"
        if operation == "history" and argv[1:3] == ["changes", "history"]:
            assert "--revision" in argv
            assert argv[argv.index("--revision") + 1] == "$REV"


def test_documented_changeset_and_pinned_cli_round_trip(tmp_path, capsys):
    source = tmp_path / "recording one.har"
    source.write_text(json.dumps({"log": {"entries": [
        {"request": {"method": "GET", "url": "https://example.invalid/start"},
         "response": {"status": 200, "headers": [{"name": "X-Token", "value": "captured"}]}},
        {"request": {"method": "POST", "url": "https://example.invalid/finish",
            "postData": {"mimeType": "application/json", "text": '{"token":"captured"}'}},
         "response": {"status": 200}},
    ]}}))
    workspace = tmp_path / "workspace one"
    imported = invoke(capsys, "import", str(source), "--workspace", str(workspace))
    assert imported["scenario_revision"] == 1
    before = hashes(workspace)
    summary = invoke(capsys, "summary", "--workspace", str(workspace), "--revision", "1")
    producer, consumer = [row["id"] for row in summary["data"]["entries"]]
    invoke(capsys, "analyze", "--workspace", str(workspace), "--revision", "1")
    invoke(capsys, "inspect", "--workspace", str(workspace), "--section", "candidates",
           "--revision", "1", "--limit", "256")
    assert hashes(workspace) == before
    change = example()
    text = json.dumps(change).replace("PRODUCER_ID", producer).replace("CONSUMER_ID", consumer)
    changeset = tmp_path / "change.json"
    changeset.write_text(text)
    invoke(capsys, "changes", "validate", str(changeset), "--workspace", str(workspace))
    assert hashes(workspace) == before
    applied = invoke(capsys, "changes", "apply", str(changeset), "--workspace", str(workspace))
    assert applied["scenario_revision"] == 2
    after = hashes(workspace)
    invoke(capsys, "changes", "apply", str(changeset), "--workspace", str(workspace))
    assert hashes(workspace) == after
    invoke(capsys, "validate", "--workspace", str(workspace), "--revision", "2")
    invoke(capsys, "inspect", "--workspace", str(workspace), "--section", "runtime-plan",
           "--revision", "2")
    invoke(capsys, "report", "--workspace", str(workspace), "--revision", "2",
           "--compare-revision", "1")
    checked = invoke(capsys, "generate", "--workspace", str(workspace), "--revision", "2",
                     "--target", "both", "--check")
    output = tmp_path / "generated"
    generated = invoke(capsys, "generate", "--workspace", str(workspace), "--revision", "2",
                       "--target", "both", "--output", str(output))
    assert checked["data"]["report_sha256"] == generated["data"]["report_sha256"]
    assert hashes(workspace) == after
    manifest = json.loads((output / "manifest.json").read_bytes())
    for path, digest in manifest["files"].items():
        assert hashlib.sha256((output / path).read_bytes()).hexdigest() == digest
    assert manifest["native_validation"] == manifest["execution"] == "not_run"
    assert (workspace / "capture.har").read_bytes() == source.read_bytes()
    preserved = hashes(output)
    invoke(capsys, "generate", "--workspace", str(workspace), "--revision", "2",
           "--target", "both", "--output", str(output), code=3)
    assert hashes(output) == preserved
    groups = invoke(capsys, "inspect", "--workspace", str(workspace), "--revision", "2",
                    "--section", "groups")
    group_id = json.loads(groups["data"]["content"])[0]["id"]
    rename = tmp_path / "rename.json"
    rename.write_text(json.dumps({"schema_version": "1.0", "change_id": "rename-for-comparison",
        "base_revision": 2, "author": "fixture", "rationale": "Advance HEAD for report identity",
        "evidence": [{"exchange_id": producer, "location": "/request"}],
        "operations": [{"op": "rename_group", "group_id": group_id, "name": "Renamed"}]}))
    invoke(capsys, "changes", "apply", str(rename), "--workspace", str(workspace))
    viewed = invoke(capsys, "report", "--workspace", str(workspace), "--revision", "3",
                    "--compare-revision", "1")
    recipe = next(line for line in (SKILL / "references/cli.md").read_text().splitlines()
                  if line.startswith('"$H2L" report ') and "--output" in line)
    substitutions = {"$WORKSPACE": str(workspace), "$NEW_REV": "3", "$REV": "1",
                     "$NEW_REPORT_DIRECTORY": str(tmp_path / "published-report")}
    arguments = [substitutions.get(word, word) for word in shlex.split(recipe)[1:]]
    published = invoke(capsys, *arguments)
    assert published["data"]["compare_revision"] == 1
    assert published["data"]["report_sha256"] == viewed["data"]["report_sha256"]
    historical = invoke(capsys, "changes", "history", "--workspace", str(workspace),
                        "--revision", "2")
    assert historical["scenario_revision"] == 2
    replay_check = invoke(capsys, "changes", "validate", str(changeset), "--workspace",
                          str(workspace))
    assert replay_check["scenario_revision"] == 3  # HEAD-only, even for the old idempotent change.


def test_import_each_recording_separately_does_not_merge_ids_or_captures(tmp_path, capsys):
    workspaces = []
    for number in range(2):
        raw = json.dumps({"log": {"entries": [{
            "request": {"method": "GET", "url": f"https://example.invalid/{number}"},
            "response": {"status": 200}}]}}).encode()
        source, workspace = tmp_path / f"capture-{number}.har", tmp_path / f"w{number}"
        source.write_bytes(raw)
        result = invoke(capsys, "import", str(source), "--workspace", str(workspace))
        workspaces.append(result["workspace_id"])
        assert (workspace / "capture.har").read_bytes() == raw
        summary = invoke(capsys, "summary", "--workspace", str(workspace))
        assert len(summary["data"]["entries"]) == 1
    assert workspaces[0] != workspaces[1]
