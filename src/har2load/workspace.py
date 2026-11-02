"""Private capture storage with immutable revision objects and an atomic head.

Creation reserves a previously absent directory, writes payloads, and publishes
the manifest last. A crash may leave an incomplete directory; it is not accepted
as a workspace and is never automatically overwritten. Checksums detect accidental
modification, not a malicious owner replacing both payloads and manifest.
"""

import hashlib
import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from har2load import __version__
from har2load.domain import Har2LoadError, JSONValue
from har2load.serialization import canonical_bytes, metadata_object

LOGGER = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_REVISIONS = 1000
_MANIFEST_KEYS = {
    "schema_version", "workspace_id", "scenario_revision", "capture_sha256",
    "scenario_sha256", "producer_version",
    "redaction", "contains_sensitive_data", "privacy_warning",
}
PRIVACY_WARNING = "Unredacted private capture; may contain credentials and session data."


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _valid_digest(value: JSONValue) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _positive_revision(value: JSONValue) -> bool:
    return type(value) is int and isinstance(value, int) and 1 <= value <= MAX_REVISIONS


def _invalid() -> Har2LoadError:
    return Har2LoadError("WORKSPACE_INVALID", "Invalid or unsupported workspace metadata.", 3)


def _integrity() -> Har2LoadError:
    return Har2LoadError("WORKSPACE_INTEGRITY_FAILED", "Workspace integrity check failed.", 3)


def _change_identity(change: dict[str, JSONValue]) -> tuple[str, int, str]:
    identifier = change.get("change_id")
    revision = change.get("base_revision")
    if (
        not isinstance(identifier, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identifier) is None
        or not _positive_revision(revision)
    ):
        raise Har2LoadError("CHANGE_INVALID", "Invalid change identifier or base revision.", 3)
    return identifier, cast(int, revision), _hash(canonical_bytes(change))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_id: str
    scenario_revision: int
    capture_sha256: str
    raw_bytes: bytes
    scenario: dict[str, JSONValue]
    workspace_schema_version: str = "2.0"
    initial_scenario_sha256: str = ""
    head_sha256: str | None = None
    history: tuple[dict[str, JSONValue], ...] = ()
    latest_revision: int = 1
    initial_scenario: dict[str, JSONValue] | None = None


@dataclass(frozen=True)
class CommitResult:
    snapshot: WorkspaceSnapshot
    applied_revision: int
    replayed: bool


def read_limited(path: Path, limit: int, *, private: bool = False) -> bytes:
    """Bound allocation and reject devices/FIFOs; never expose path-bearing OSErrors."""
    try:
        if private and path.is_symlink():
            raise Har2LoadError("WORKSPACE_UNSAFE_PATH", "Linked workspace files are rejected.", 3)
        if not stat.S_ISREG(path.stat().st_mode):
            raise Har2LoadError("INPUT_NOT_REGULAR", "Expected a regular file.")
        # O_NOFOLLOW narrows the check/open race on supporting platforms. Workspaces
        # are trusted local directories, not a sandbox against hostile co-writers.
        flags = os.O_RDONLY | (getattr(os, "O_NOFOLLOW", 0) if private else 0)
        descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Har2LoadError("INPUT_NOT_REGULAR", "Expected a regular file.")
            if info.st_size > limit:
                raise Har2LoadError("INPUT_TOO_LARGE", "Input exceeds the configured size limit.")
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise Har2LoadError("INPUT_TOO_LARGE", "Input exceeds the configured size limit.")
        return raw
    except OSError as error:
        raise Har2LoadError(
            "IO_READ_FAILED", "Unable to read input or workspace file.", 5
        ) from error


class WorkspaceStore:
    """Publish only a complete revision under a short exclusive writer lock.

    The lock is never automatically stolen. A dead writer may leave a lock or an
    unreferenced object; readers ignore these. Hostile co-writers and network FS
    locking semantics are outside the trusted-local-directory contract.
    """

    def __init__(self, max_capture_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self.max_capture_bytes = max_capture_bytes

    def create(
        self, path: Path, raw_bytes: bytes, scenario: dict[str, JSONValue]
    ) -> WorkspaceSnapshot:
        if len(raw_bytes) > self.max_capture_bytes:
            raise Har2LoadError("INPUT_TOO_LARGE", "Input exceeds the configured size limit.")
        scenario_bytes = canonical_bytes(scenario)
        if type(scenario.get("revision")) is not int or scenario["revision"] != 1:
            raise _invalid()
        if len(scenario_bytes) > MAX_METADATA_BYTES:
            raise Har2LoadError("INPUT_TOO_LARGE", "Scenario metadata exceeds the size limit.")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        workspace_id = f"ws_{digest}"
        manifest: dict[str, JSONValue] = {
            "schema_version": "2.0",
            "workspace_id": workspace_id,
            "scenario_revision": 1,
            "capture_sha256": digest,
            "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
            "producer_version": __version__,
            "redaction": "off",
            "contains_sensitive_data": True,
            "privacy_warning": PRIVACY_WARNING,
            "initial_scenario_sha256": _hash(scenario_bytes),
            "head_sha256": None,
        }
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as error:
            raise Har2LoadError(
                "WORKSPACE_EXISTS", "Destination already exists; choose a new workspace.", 4
            ) from error
        except OSError as error:
            raise Har2LoadError(
                "IO_WRITE_FAILED", "Unable to create workspace directory.", 5
            ) from error
        try:
            self._write(path / "capture.har", raw_bytes)
            self._write(path / "scenario.json", scenario_bytes)
            self._write(path / ".manifest.pending", canonical_bytes(manifest))
            self._sync_directory(path)
            os.replace(path / ".manifest.pending", path / "manifest.json")
            self._sync_directory(path)
        except OSError as error:
            # Leave evidence of incomplete creation, without deleting user data.
            raise Har2LoadError(
                "IO_WRITE_FAILED", "Workspace creation failed; destination may be incomplete.", 5
            ) from error
        LOGGER.info("Workspace created (private capture; redaction disabled).")
        return WorkspaceSnapshot(
            workspace_id, 1, digest, raw_bytes, metadata_object(scenario_bytes),
            initial_scenario_sha256=_hash(scenario_bytes),
            initial_scenario=metadata_object(scenario_bytes),
        )

    @staticmethod
    def _sync_directory(path: Path) -> None:
        # POSIX supports directory fsync. Windows relies on file flush + replace;
        # no cross-platform power-loss durability guarantee is made.
        if os.name == "posix":
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())

    @staticmethod
    def _manifest(path: Path) -> dict[str, JSONValue]:
        if path.is_symlink():
            raise Har2LoadError("WORKSPACE_UNSAFE_PATH", "Linked workspace roots are rejected.", 3)
        manifest = metadata_object(read_limited(path / "manifest.json", 8192, private=True))
        version = manifest.get("schema_version")
        if version not in ("1.0", "2.0"):
            raise Har2LoadError(
                "WORKSPACE_VERSION_UNSUPPORTED", "Unsupported workspace version.", 3
            )
        expected = _MANIFEST_KEYS | (
            {"initial_scenario_sha256", "head_sha256"} if version == "2.0" else set()
        )
        if set(manifest) != expected:
            raise Har2LoadError("WORKSPACE_INVALID", "Invalid workspace manifest fields.", 3)
        digest = manifest.get("capture_sha256")
        scenario_digest = manifest.get("scenario_sha256")
        if (
            not _valid_digest(digest) or not _valid_digest(scenario_digest)
            or manifest.get("workspace_id") != f"ws_{digest}"
            or not _positive_revision(manifest.get("scenario_revision"))
            or manifest.get("producer_version") not in (
                ("0.1.0a1",) if version == "1.0" else (
                    "0.2.0a1", "0.3.0a1", "0.4.0a1", __version__
                )
            )
            or manifest.get("redaction") != "off"
            or manifest.get("contains_sensitive_data") is not True
            or manifest.get("privacy_warning") != PRIVACY_WARNING
        ):
            raise _invalid()
        if version == "1.0" and manifest["scenario_revision"] != 1:
            raise _invalid()
        if version == "2.0" and (
            not _valid_digest(manifest["initial_scenario_sha256"])
            or (manifest["scenario_revision"] == 1 and manifest["head_sha256"] is not None)
            or (manifest["scenario_revision"] != 1 and not _valid_digest(manifest["head_sha256"]))
        ):
            raise _invalid()
        return manifest

    def load(self, path: Path, revision: int | None = None) -> WorkspaceSnapshot:
        manifest = self._manifest(path)  # one atomic read, then only immutable payloads
        latest = cast(int, manifest["scenario_revision"])
        selected = latest if revision is None else revision
        if not _positive_revision(selected) or selected > latest:
            raise Har2LoadError("REVISION_NOT_FOUND", "Requested revision is not available.", 2)
        raw = read_limited(path / "capture.har", self.max_capture_bytes, private=True)
        scenario_bytes = read_limited(path / "scenario.json", MAX_METADATA_BYTES, private=True)
        initial_digest = cast(
            str, manifest.get("initial_scenario_sha256", manifest["scenario_sha256"])
        )
        if (
            _hash(raw) != manifest["capture_sha256"] or _hash(scenario_bytes) != initial_digest
        ):
            raise _integrity()
        scenario = metadata_object(scenario_bytes)
        if type(scenario.get("revision")) is not int or scenario["revision"] != 1:
            raise _invalid()
        if latest == 1 and manifest["scenario_sha256"] != initial_digest:
            raise _integrity()
        initial_scenario = scenario
        scenario, head, history = self._history(path, manifest, selected, scenario)
        return WorkspaceSnapshot(
            cast(str, manifest["workspace_id"]), selected, cast(str, manifest["capture_sha256"]),
            raw, scenario, cast(str, manifest["schema_version"]), initial_digest, head,
            history, latest, initial_scenario,
        )

    @staticmethod
    def _object_path(path: Path, revision: int, digest: str) -> Path:
        return path / f"revision-{revision:06d}-{digest}.json"

    def _history(
        self, path: Path, manifest: dict[str, JSONValue], selected: int,
        initial: dict[str, JSONValue],
    ) -> tuple[dict[str, JSONValue], str | None, tuple[dict[str, JSONValue], ...]]:
        parent = manifest.get("head_sha256")
        selected_head: str | None = None
        selected_model = initial
        history: list[dict[str, JSONValue]] = []
        identifiers: set[str] = set()
        total_bytes = 0
        latest = cast(int, manifest["scenario_revision"])
        for revision in range(latest, 1, -1):
            if not _valid_digest(parent):
                raise _invalid()
            digest = cast(str, parent)
            raw = read_limited(self._object_path(path, revision, digest), MAX_METADATA_BYTES,
                               private=True)
            total_bytes += len(raw)
            if total_bytes > MAX_HISTORY_BYTES:
                raise Har2LoadError("HISTORY_LIMIT", "History exceeds the supported size limit.", 3)
            if _hash(raw) != digest:
                raise _integrity()
            event = metadata_object(raw)
            model, change = self._event(event, revision)
            identifier, _, change_digest = _change_identity(change)
            if identifier in identifiers or change_digest != event["change_sha256"]:
                raise _integrity()
            identifiers.add(identifier)
            scenario_digest = _hash(canonical_bytes(model))
            if revision == latest and scenario_digest != manifest["scenario_sha256"]:
                raise _integrity()
            if revision == selected:
                selected_model, selected_head = model, digest
            if revision <= selected:
                history.append({
                    "revision": revision, "change": change, "change_sha256": change_digest,
                    "created_at": event["created_at"], "scenario_sha256": scenario_digest,
                    "object_sha256": digest, "parent_sha256": event["parent_sha256"],
                })
            parent = event["parent_sha256"]
        if parent is not None:
            raise _invalid()
        return selected_model, selected_head, tuple(reversed(history))

    @staticmethod
    def _event(
        event: dict[str, JSONValue], revision: int,
    ) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
        model, change = event.get("scenario"), event.get("change")
        if (
            set(event) != {"schema_version", "revision", "parent_sha256", "scenario", "change",
                           "change_sha256", "created_at"}
            or event["schema_version"] != "1.0"
            or type(event["revision"]) is not int or event["revision"] != revision
            or not isinstance(model, dict) or type(model.get("revision")) is not int
            or model.get("revision") != revision or not isinstance(change, dict)
            or type(change.get("base_revision")) is not int
            or change.get("base_revision") != revision - 1
            or not _valid_digest(event["change_sha256"])
            or not isinstance(event["created_at"], str)
            or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z",
                            event["created_at"]) is None
            or (revision == 2 and event["parent_sha256"] is not None)
            or (revision > 2 and not _valid_digest(event["parent_sha256"]))
        ):
            raise _invalid()
        return model, change

    @staticmethod
    def check_change(snapshot: WorkspaceSnapshot, change: dict[str, JSONValue]) -> int | None:
        """Check idempotence BEFORE rejecting an old base revision."""
        if snapshot.workspace_schema_version != "2.0":
            raise Har2LoadError(
                "WORKSPACE_READ_ONLY", "Legacy workspace is read-only; re-import into a new one.", 3
            )
        identifier, base, digest = _change_identity(change)
        for event in snapshot.history:
            recorded = cast(dict[str, JSONValue], event["change"])
            if recorded["change_id"] == identifier:
                if digest != event["change_sha256"]:
                    raise Har2LoadError(
                        "CHANGE_ID_CONFLICT", "Change ID has different content.", 4,
                        workspace_id=snapshot.workspace_id,
                        scenario_revision=snapshot.scenario_revision,
                    )
                return cast(int, event["revision"])
        if base != snapshot.scenario_revision:
            raise Har2LoadError(
                "REVISION_CONFLICT", "Change base revision is stale.", 4,
                workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            )
        if base >= MAX_REVISIONS:
            raise Har2LoadError("HISTORY_LIMIT", "Maximum supported revision count reached.", 3)
        return None

    @contextmanager
    def _lock(self, path: Path) -> Iterator[None]:
        if path.is_symlink():
            raise Har2LoadError("WORKSPACE_UNSAFE_PATH", "Linked workspace roots are rejected.", 3)
        lock_path = path / ".changes.lock"
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise Har2LoadError(
                "WORKSPACE_BUSY", "Workspace is locked; retry or inspect a stale lock manually.", 4
            ) from error
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(uuid4().hex)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            lock_path.unlink()

    def transact(
        self, path: Path, change: dict[str, JSONValue],
        transform: Callable[[WorkspaceSnapshot], dict[str, JSONValue]],
    ) -> CommitResult:
        """Atomically publish a validated model and its audit event, or neither."""
        try:
            with self._lock(path):
                current = self.load(path)
                applied = self.check_change(current, change)
                if applied is not None:
                    return CommitResult(current, applied, True)
                model = transform(current)
                revision = current.scenario_revision + 1
                if type(model.get("revision")) is not int or model["revision"] != revision:
                    raise _invalid()
                event: dict[str, JSONValue] = {
                    "schema_version": "1.0", "revision": revision,
                    "parent_sha256": current.head_sha256, "scenario": model, "change": change,
                    "change_sha256": _hash(canonical_bytes(change)),
                    "created_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                }
                self._publish(path, current, event)
                return CommitResult(self.load(path), revision, False)
        except OSError as error:
            raise Har2LoadError(
                "IO_WRITE_FAILED",
                "Commit outcome may be uncertain; inspect history and retry the same change ID.", 5,
            ) from error

    def _publish(
        self, path: Path, current: WorkspaceSnapshot, event: dict[str, JSONValue],
    ) -> None:
        payload = canonical_bytes(event)
        history_size = sum(
            self._object_path(path, cast(int, item["revision"]),
                              cast(str, item["object_sha256"])).stat().st_size
            for item in current.history
        )
        if len(payload) > MAX_METADATA_BYTES or history_size + len(payload) > MAX_HISTORY_BYTES:
            raise Har2LoadError("HISTORY_LIMIT", "Revision exceeds the history size limit.", 3)
        digest = _hash(payload)
        revision = cast(int, event["revision"])
        target = self._object_path(path, revision, digest)
        try:
            self._write(target, payload)
        except FileExistsError:
            if read_limited(target, MAX_METADATA_BYTES, private=True) != payload:
                raise _integrity() from None
        manifest = self._manifest(path)
        manifest.update({
            "scenario_revision": revision, "head_sha256": digest,
            "scenario_sha256": _hash(canonical_bytes(event["scenario"])),
            "producer_version": __version__,
        })
        pending = path / f".manifest-{uuid4().hex}.pending"
        try:
            self._write(pending, canonical_bytes(manifest))
            self._sync_directory(path)
            os.replace(pending, path / "manifest.json")
            self._sync_directory(path)
        finally:
            pending.unlink(missing_ok=True)
