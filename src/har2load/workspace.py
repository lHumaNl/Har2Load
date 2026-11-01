"""Private, single-capture workspaces. No mutation API or runtime execution.

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
from dataclasses import dataclass
from pathlib import Path

from har2load import __version__
from har2load.domain import Har2LoadError, JSONValue
from har2load.serialization import canonical_bytes, metadata_object

LOGGER = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
_MANIFEST_KEYS = {
    "schema_version", "workspace_id", "scenario_revision", "capture_sha256",
    "scenario_sha256", "producer_version",
    "redaction", "contains_sensitive_data", "privacy_warning",
}
PRIVACY_WARNING = "Unredacted private capture; may contain credentials and session data."


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_id: str
    scenario_revision: int
    capture_sha256: str
    raw_bytes: bytes
    scenario: dict[str, JSONValue]


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
    """Persist immutable initial snapshots independently of HAR interpretation."""

    def __init__(self, max_capture_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self.max_capture_bytes = max_capture_bytes

    def create(
        self, path: Path, raw_bytes: bytes, scenario: dict[str, JSONValue]
    ) -> WorkspaceSnapshot:
        if len(raw_bytes) > self.max_capture_bytes:
            raise Har2LoadError("INPUT_TOO_LARGE", "Input exceeds the configured size limit.")
        scenario_bytes = canonical_bytes(scenario)
        if len(scenario_bytes) > MAX_METADATA_BYTES:
            raise Har2LoadError("INPUT_TOO_LARGE", "Scenario metadata exceeds the size limit.")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        workspace_id = f"ws_{digest}"
        manifest: dict[str, JSONValue] = {
            "schema_version": "1.0",
            "workspace_id": workspace_id,
            "scenario_revision": 1,
            "capture_sha256": digest,
            "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
            "producer_version": __version__,
            "redaction": "off",
            "contains_sensitive_data": True,
            "privacy_warning": PRIVACY_WARNING,
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
            workspace_id, 1, digest, raw_bytes, metadata_object(scenario_bytes)
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

    def load(self, path: Path) -> WorkspaceSnapshot:
        if path.is_symlink():
            raise Har2LoadError("WORKSPACE_UNSAFE_PATH", "Linked workspace roots are rejected.", 3)
        manifest = metadata_object(read_limited(path / "manifest.json", 8192, private=True))
        if manifest.get("schema_version") != "1.0":
            raise Har2LoadError(
                "WORKSPACE_VERSION_UNSUPPORTED", "Unsupported workspace version.", 3
            )
        if set(manifest) != _MANIFEST_KEYS:
            raise Har2LoadError("WORKSPACE_INVALID", "Invalid workspace manifest fields.", 3)
        digest = manifest.get("capture_sha256")
        scenario_digest = manifest.get("scenario_sha256")
        if (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(scenario_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", scenario_digest)
            or manifest.get("workspace_id") != f"ws_{digest}"
            or type(manifest.get("scenario_revision")) is not int
            or manifest["scenario_revision"] != 1
            or manifest.get("producer_version") != __version__
            or manifest.get("redaction") != "off"
            or manifest.get("contains_sensitive_data") is not True
            or manifest.get("privacy_warning") != PRIVACY_WARNING
        ):
            raise Har2LoadError(
                "WORKSPACE_INVALID", "Invalid or unsupported workspace metadata.", 3
            )
        raw = read_limited(path / "capture.har", self.max_capture_bytes, private=True)
        scenario_bytes = read_limited(path / "scenario.json", MAX_METADATA_BYTES, private=True)
        if (
            hashlib.sha256(raw).hexdigest() != digest
            or hashlib.sha256(scenario_bytes).hexdigest() != scenario_digest
        ):
            raise Har2LoadError(
                "WORKSPACE_INTEGRITY_FAILED", "Workspace integrity check failed.", 3
            )
        return WorkspaceSnapshot(
            f"ws_{digest}", 1, digest, raw, metadata_object(scenario_bytes)
        )
