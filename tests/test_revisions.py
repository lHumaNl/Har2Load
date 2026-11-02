"""Transactional storage tests use independently authored synthetic documents."""

import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from har2load.domain import Har2LoadError
from har2load.workspace import WorkspaceStore

RAW = b'{"log":{"entries":[]}}'
INITIAL = {"schema_version": "1.1", "revision": 1, "groups": []}


def change(identifier="c1", base=1):
    return {
        "schema_version": "1.0", "change_id": identifier, "base_revision": base,
        "author": "synthetic-author", "rationale": "Synthetic change",
        "evidence": [{"exchange_id": "synthetic", "location": ""}],
        "operations": [{"op": "test-storage-callback"}],
    }


def advance(snapshot):
    model = deepcopy(snapshot.scenario)
    model["revision"] += 1
    return model


def test_commit_history_and_historical_read(tmp_path):
    store = WorkspaceStore()
    path = tmp_path / "w"
    initial = store.create(path, RAW, INITIAL)
    committed = store.transact(path, change(), advance)
    assert committed.applied_revision == 2 and committed.replayed is False
    assert committed.snapshot.scenario_revision == 2
    assert store.load(path).scenario == committed.snapshot.scenario
    assert store.load(path, revision=1).scenario == initial.scenario
    history = store.load(path).history
    assert len(history) == 1
    assert history[0]["change"] == change()
    assert history[0]["revision"] == 2 and history[0]["created_at"].endswith("Z")
    assert (path / "capture.har").read_bytes() == RAW
    assert json.loads((path / "scenario.json").read_bytes()) == INITIAL


def test_same_change_is_idempotent_even_after_later_revision(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    store.transact(path, change(), advance)
    store.transact(path, change("c2", 2), advance)
    before = {p.name: p.read_bytes() for p in path.iterdir()}

    def forbidden(snapshot):
        raise AssertionError("Idempotent retry must not call the transform")

    result = store.transact(path, change(), forbidden)
    assert result.replayed and result.applied_revision == 2
    assert result.snapshot.scenario_revision == 3
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


def test_stale_or_reused_identifier_conflicts(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    store.transact(path, change(), advance)
    for document, code in [(change("new", 1), "REVISION_CONFLICT"),
                           (change("c1", 2), "CHANGE_ID_CONFLICT")]:
        with pytest.raises(Har2LoadError) as caught:
            store.transact(path, document, advance)
        assert caught.value.code == code and caught.value.exit_code == 4
    assert store.load(path).scenario_revision == 2


def test_transform_failure_leaves_every_published_file_unchanged(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    before = {p.name: p.read_bytes() for p in path.iterdir()}

    def reject(snapshot):
        raise Har2LoadError("CHANGE_INVALID", "Synthetic rejection.", 3)

    with pytest.raises(Har2LoadError):
        store.transact(path, change(), reject)
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


def test_publication_failure_preserves_old_head_and_retry_works(tmp_path, monkeypatch):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    manifest = (path / "manifest.json").read_bytes()
    replace = os.replace

    def fail_publication(source, destination):
        raise OSError("SECRET_PATH_SENTINEL")

    monkeypatch.setattr(os, "replace", fail_publication)
    with pytest.raises(Har2LoadError) as caught:
        store.transact(path, change(), advance)
    assert "SECRET_PATH_SENTINEL" not in str(caught.value)
    assert (path / "manifest.json").read_bytes() == manifest
    assert store.load(path).scenario_revision == 1
    assert not (path / ".changes.lock").exists()
    monkeypatch.setattr(os, "replace", replace)
    assert store.transact(path, change(), advance).snapshot.scenario_revision == 2


def test_failure_after_publication_is_recoverable_by_same_id(tmp_path, monkeypatch):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    replace = os.replace

    def publish_then_fail(source, destination):
        replace(source, destination)
        raise OSError("Injected post-publication failure")

    monkeypatch.setattr(os, "replace", publish_then_fail)
    with pytest.raises(Har2LoadError):
        store.transact(path, change(), advance)
    monkeypatch.setattr(os, "replace", replace)
    result = store.transact(path, change(), advance)
    assert result.replayed and result.applied_revision == 2


def test_concurrent_different_changes_have_one_winner(tmp_path):
    path = tmp_path / "w"
    WorkspaceStore().create(path, RAW, INITIAL)
    barrier = Barrier(2)

    def worker(identifier):
        barrier.wait()
        try:
            return WorkspaceStore().transact(path, change(identifier), advance).applied_revision
        except Har2LoadError as error:
            assert error.exit_code == 4
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["left", "right"]))
    assert results.count(2) == 1
    assert any(item in {"WORKSPACE_BUSY", "REVISION_CONFLICT"} for item in results)
    assert len(WorkspaceStore().load(path).history) == 1


def test_stale_lock_does_not_block_read_and_is_never_automatically_deleted(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    (path / ".changes.lock").write_text("simulated-dead-writer")
    assert store.load(path).scenario_revision == 1
    with pytest.raises(Har2LoadError) as caught:
        store.transact(path, change(), advance)
    assert caught.value.code == "WORKSPACE_BUSY"
    assert (path / ".changes.lock").read_text() == "simulated-dead-writer"


def test_corrupted_historical_object_blocks_load(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    store.transact(path, change(), advance)
    store.transact(path, change("c2", 2), advance)
    historical = next(path.glob("revision-000002-*.json"))
    historical.write_text("{}")
    with pytest.raises(Har2LoadError) as caught:
        store.load(path)
    assert caught.value.code == "WORKSPACE_INTEGRITY_FAILED"


def test_legacy_workspace_reads_but_never_auto_migrates(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["schema_version"] = "1.0"
    manifest["producer_version"] = "0.1.0a1"
    del manifest["initial_scenario_sha256"], manifest["head_sha256"]
    (path / "manifest.json").write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    assert store.load(path).workspace_schema_version == "1.0"
    with pytest.raises(Har2LoadError) as caught:
        store.transact(path, change(), advance)
    assert caught.value.code == "WORKSPACE_READ_ONLY"
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


@pytest.mark.parametrize("revision", [0, -1, True, 2, "1"])
def test_invalid_or_absent_historical_revision_is_rejected(tmp_path, revision):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    with pytest.raises(Har2LoadError):
        store.load(path, revision=revision)


def test_manifest_head_is_digest_not_an_external_path(tmp_path):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["head_sha256"] = "../../outside"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Har2LoadError):
        store.load(path)
    assert hashlib.sha256((path / "capture.har").read_bytes()).digest()


def _process_writer(path, barrier, results, identifier):
    from pathlib import Path

    barrier.wait(timeout=10)
    try:
        result = WorkspaceStore().transact(Path(path), change(identifier), advance)
        results.put(result.applied_revision)
    except Har2LoadError as error:
        results.put(error.code)


def test_writer_lock_is_cross_process_not_only_thread_local(tmp_path):
    path = tmp_path / "w"
    WorkspaceStore().create(path, RAW, INITIAL)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_process_writer, args=(str(path), barrier, results, identifier))
        for identifier in ("process-left", "process-right")
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert outcomes.count(2) == 1
        assert any(item in {"WORKSPACE_BUSY", "REVISION_CONFLICT"} for item in outcomes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()
    assert len(WorkspaceStore().load(path).history) == 1


def test_history_limit_is_checked_before_head_publication(tmp_path, monkeypatch):
    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    monkeypatch.setattr("har2load.workspace.MAX_HISTORY_BYTES", 1)
    with pytest.raises(Har2LoadError) as caught:
        store.transact(path, change(), advance)
    assert caught.value.code == "HISTORY_LIMIT"
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


def test_reader_observes_old_or_new_head_never_partial_revision(tmp_path, monkeypatch):
    from threading import Event

    path = tmp_path / "w"
    store = WorkspaceStore()
    store.create(path, RAW, INITIAL)
    ready, proceed = Event(), Event()
    replace = os.replace

    def pause_before_publication(source, destination):
        ready.set()
        assert proceed.wait(timeout=10)
        replace(source, destination)

    monkeypatch.setattr(os, "replace", pause_before_publication)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(store.transact, path, change(), advance)
        try:
            assert ready.wait(timeout=10)
            observed = store.load(path)
            assert observed.scenario_revision == 1 and observed.history == ()
        finally:
            proceed.set()
        assert writer.result(timeout=10).applied_revision == 2
    assert store.load(path).scenario_revision == 2
