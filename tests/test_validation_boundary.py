"""Mock-only checks of the opt-in harness; the default suite never runs native code."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from subprocess import CompletedProcess

import pytest

spec = spec_from_file_location("validation_support_test",
                              Path(__file__).parents[1] / "validation/support.py")
assert spec is not None and spec.loader is not None
support = module_from_spec(spec)
spec.loader.exec_module(support)


def test_native_runner_refuses_without_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("HAR2LOAD_NATIVE_VALIDATION", raising=False)
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: pytest.fail("Native execution"))
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        support.run_isolated(["not-executed"], tmp_path)


def test_native_runner_never_falls_back_on_unsupported_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HAR2LOAD_NATIVE_VALIDATION", "1")
    monkeypatch.setattr(support.sys, "platform", "unsupported-test-host")
    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: pytest.fail("Native execution"))
    with pytest.raises(RuntimeError, match="refusing execution"):
        support.run_isolated(["not-executed"], tmp_path)


def test_native_runner_uses_network_denial_and_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HAR2LOAD_NATIVE_VALIDATION", "1")
    monkeypatch.setenv("SYNTHETIC_PRIVATE_ENV", "must-not-inherit")
    monkeypatch.setattr(support.sys, "platform", "darwin")
    sandbox = tmp_path / "synthetic-sandbox"
    sandbox.touch()
    monkeypatch.setattr(support, "SANDBOX", sandbox)
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return CompletedProcess(command, 0, stdout="synthetic", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    support.run_isolated(["/synthetic/compiler", "input.c"], tmp_path, timeout=7)
    assert observed["command"] == [str(sandbox), "-p", support.POLICY,
                                    "/synthetic/compiler", "input.c"]
    assert "(deny network*)" in support.POLICY
    assert "SYNTHETIC_PRIVATE_ENV" not in observed["env"]
    assert observed["env"]["HOME"] == str(tmp_path)
    assert observed["timeout"] == 7 and observed["check"] is True
