import json
import os
import subprocess
import sys

import pytest

from har2load.cli import main


def test_help_with_no_optional_dependencies_or_network():
    result = subprocess.run(
        [sys.executable, "-m", "har2load", "--help"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "import" in result.stdout
    assert result.stderr == ""


def test_machine_readable_error_does_not_echo_input(tmp_path, capsys):
    code = main(["import", str(tmp_path / "secret.har"), "--workspace", str(tmp_path / "w")])
    result = capsys.readouterr()
    assert code == 5
    assert result.err == ""
    assert "secret.har" not in result.out
    assert json.loads(result.out)["status"] == "error"


@pytest.mark.parametrize("args", [[], ["unknown-secret"], ["summary"], ["import", "a", "--bad"]])
def test_argument_errors_are_json_and_sanitized(args, capsys):
    assert main(args) == 2
    result = capsys.readouterr()
    assert result.err == ""
    assert "unknown-secret" not in result.out
    assert json.loads(result.out)["diagnostics"][0]["code"] == "CLI_ARGUMENT_ERROR"


def test_full_cli_cycle_without_site_packages(tmp_path):
    source = tmp_path / "empty.har"
    source.write_text('{"log":{"version":"1.2","entries":[]}}')
    workspace = tmp_path / "workspace"
    env = {**os.environ, "PYTHONPATH": os.path.abspath("src")}
    for args in [
        ["import", str(source), "--workspace", str(workspace)],
        ["summary", "--workspace", str(workspace)],
        ["inspect", "--workspace", str(workspace), "--section", "groups"],
    ]:
        run = subprocess.run(
            [sys.executable, "-S", "-m", "har2load", *args], env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert json.loads(run.stdout)["scenario_revision"] == 1
        assert run.stderr == ""
