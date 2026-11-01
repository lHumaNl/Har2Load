"""Synthetic storage tests: no private captures or native runtimes."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from har2load.domain import Har2LoadError
from har2load.workspace import WorkspaceStore

RAW = b'\xef\xbb\xbf{"log":{"entries":[]}, "vendor": "exact bytes"}\n'
SCENARIO = {"schema_version": "1.0", "revision": 1, "groups": []}


def test_roundtrip_and_no_absolute_source_path(tmp_path):
    path = tmp_path / "workspace"
    store = WorkspaceStore()
    created = store.create(path, RAW, SCENARIO)
    loaded = store.load(path)
    assert loaded == created
    assert loaded.raw_bytes == RAW
    assert loaded.scenario == SCENARIO
    assert loaded.capture_sha256 == hashlib.sha256(RAW).hexdigest()
    assert str(tmp_path) not in (path / "manifest.json").read_text()


def test_existing_directory_is_never_overwritten(tmp_path):
    path = tmp_path / "existing"
    path.mkdir()
    sentinel = path / "keep.txt"
    sentinel.write_text("manual work")
    with pytest.raises(Har2LoadError) as error:
        WorkspaceStore().create(path, RAW, SCENARIO)
    assert error.value.exit_code == 4
    assert sentinel.read_text() == "manual work"
    assert not (path / "manifest.json").exists()


@pytest.mark.parametrize("filename", ["capture.har", "scenario.json"])
def test_changed_file_rejected(tmp_path, filename):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, SCENARIO)
    (path / filename).write_bytes(b'{}')
    with pytest.raises(Har2LoadError, match="integrity") as error:
        store.load(path)
    assert error.value.exit_code == 3


def test_unknown_schema_rejected(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, SCENARIO)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["schema_version"] = "99.0"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Har2LoadError) as error:
        store.load(path)
    assert error.value.code == "WORKSPACE_VERSION_UNSUPPORTED"


def test_partial_workspace_is_not_loadable(tmp_path, monkeypatch):
    path = tmp_path / "w"
    real_replace = os.replace

    def fail_manifest(source, destination):
        if Path(destination).name == "manifest.json":
            raise OSError("secret sentinel in OS error")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest)
    with pytest.raises(Har2LoadError) as error:
        WorkspaceStore().create(path, RAW, SCENARIO)
    assert "secret sentinel" not in str(error.value)
    assert not (path / "manifest.json").exists()
    with pytest.raises(Har2LoadError):
        WorkspaceStore().load(path)


def test_symbolic_link_component_rejected(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, SCENARIO)
    external = tmp_path / "external.har"
    external.write_bytes(RAW)
    (path / "capture.har").unlink()
    try:
        (path / "capture.har").symlink_to(external)
    except OSError:
        pytest.skip("Symlink permission is unavailable")
    with pytest.raises(Har2LoadError) as error:
        store.load(path)
    assert error.value.code == "WORKSPACE_UNSAFE_PATH"


def test_file_size_limit_checked_before_read(tmp_path):
    path = tmp_path / "w"
    WorkspaceStore().create(path, RAW, SCENARIO)
    with pytest.raises(Har2LoadError) as error:
        WorkspaceStore(max_capture_bytes=1).load(path)
    assert error.value.code == "INPUT_TOO_LARGE"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract only")
def test_private_permissions(tmp_path):
    path = tmp_path / "w"
    WorkspaceStore().create(path, RAW, SCENARIO)
    assert path.stat().st_mode & 0o077 == 0
    for item in path.iterdir():
        assert item.stat().st_mode & 0o077 == 0


def test_load_has_fixed_file_names_not_manifest_paths(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, SCENARIO)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["capture_path"] = "../outside.har"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Har2LoadError):
        store.load(path)


@pytest.mark.parametrize("content", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'null'])
def test_invalid_manifest_is_safe_error(tmp_path, content):
    path = tmp_path / "w"
    WorkspaceStore().create(path, RAW, SCENARIO)
    (path / "manifest.json").write_bytes(content)
    with pytest.raises(Har2LoadError) as error:
        WorkspaceStore().load(path)
    assert error.value.exit_code == 3
