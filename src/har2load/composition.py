"""Compose trusted, manifest-published Har2Load UC outputs without touching workspaces."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from har2load import __version__
from har2load.domain import Har2LoadError, JSONValue
from har2load.generation_types import MAX_GENERATED_BYTES
from har2load.jmeter import ARTIFACT
from har2load.jmeter_composition import compose_jmx
from har2load.presentation import envelope
from har2load.reporting import ReportPublisher
from har2load.serialization import canonical_bytes, metadata_object
from har2load.shared_steps import TF_LABEL, read_shared_plan
from har2load.workspace import read_limited

_MAX_UCS = 16
_MAX_INPUT_BYTES = 128 * 1024 * 1024
_SHA = re.compile(r"[a-f0-9]{64}")
_KNOWN = frozenset({ARTIFACT, "report.md", "report.json", "loadrunner/Action.c",
                    "loadrunner/globals.h", "loadrunner/vuser_init.c", "loadrunner/vuser_end.c",
                    "loadrunner/README.txt", "loadrunner/native_inputs.prm"})
_INPUT_TABLE = re.compile(r"loadrunner/data/input_[0-9a-f]{64}\.dat")
_PROFILES = {"static_literals_v1": ("generated_static_artifacts", "1", "1.3"),
             "scalar_runtime_v1": ("generated_runtime_artifacts", "5", "1.7")}
_POLICY = "independent_debug_thread_groups_v1"


def _bad() -> Har2LoadError:
    return Har2LoadError("COMPOSE_BUNDLE_INVALID",
                         "Expected a complete, consistent Har2Load JMeter generation bundle.", 3)


def _obj(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise _bad()
    return value


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: JSONValue) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _bad()
    return value


@dataclass
class _Budget:
    remaining: int = _MAX_INPUT_BYTES
    jmx_remaining: int = MAX_GENERATED_BYTES

    def read(self, root: Path, name: str, bound: int) -> bytes:
        # Only allowlisted generation-relative paths reach this point. Reject links
        # in every component below root. Trusted local dirs, no hostile concurrent writers.
        path = root
        for part in name.split("/"):
            path = path / part
            if path.is_symlink() or path.is_junction():
                raise _bad()
        if name == ARTIFACT:
            bound = min(bound, self.jmx_remaining)
        try:
            data = read_limited(path, min(self.remaining, bound), private=True)
        except Har2LoadError as error:
            if error.code == "INPUT_TOO_LARGE":
                raise Har2LoadError(
                    "COMPOSE_LIMIT", "Composition input exceeds budget.", 3
                ) from None
            raise _bad() from None
        self.remaining -= len(data)
        if name == ARTIFACT:
            self.jmx_remaining -= len(data)
        return data


@dataclass(frozen=True)
class _Source:
    jmx: bytes
    provenance: dict[str, JSONValue]


def _metadata(data: bytes) -> dict[str, JSONValue]:
    try:
        return metadata_object(data)
    except Har2LoadError:
        raise _bad() from None


def _verify_targets(manifest: dict[str, JSONValue], report: dict[str, JSONValue],
                    requested: JSONValue, native: dict[str, JSONValue]) -> None:
    targets = _obj(manifest.get("targets"))
    recorded = _obj(report.get("targets"))
    if (requested not in (["jmeter"], ["jmeter", "loadrunner"])
            or not isinstance(requested, list) or sorted(targets) != requested
            or set(recorded) != {"jmeter", "loadrunner"}):
        raise _bad()
    partial = False
    for name in recorded:
        if name not in targets:
            if recorded[name] != {"status": "not_requested"} or any(
                p.startswith(name + "/") for p in native
            ):
                raise _bad()
            continue
        target = _obj(targets[name])
        paths = sorted(p for p in native if p.startswith(name + "/"))
        if (target != recorded[name] or target.get("status") not in ("generated", "blocked")
                or target.get("files") != paths
                or bool(paths) != (target["status"] == "generated")):
            raise _bad()
        partial |= target["status"] == "blocked"
    if manifest["generation_status"] != ("partial" if partial else "generated"):
        raise _bad()


def _source(label: str, root: Path, budget: _Budget) -> _Source:
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise _bad()
    raw_manifest = budget.read(root, "manifest.json", 1024 * 1024)
    manifest = _metadata(raw_manifest)
    profile = manifest.get("profile")
    if not isinstance(profile, str) or profile not in _PROFILES:
        raise Har2LoadError("COMPOSE_PROFILE_UNSUPPORTED",
                            "Expected generated static or bounded scalar JMeter artifacts.", 3)
    kind, version, schema = _PROFILES[profile]
    files = _obj(manifest.get("files"))
    capture = _digest(manifest.get("capture_sha256"))
    revision = manifest.get("scenario_revision")
    target = _obj(_obj(manifest.get("targets")).get("jmeter"))
    if (manifest.get("schema_version") != "1.0" or manifest.get("kind") != kind
            or manifest.get("status") != "complete"
            or manifest.get("generation_status") not in ("generated", "partial")
            or manifest.get("workspace_id") != f"ws_{capture}"
            or type(revision) is not int or revision < 1
            or target.get("status") != "generated" or target.get("profile") != profile
            or target.get("profile_version") != version or target.get("files") != [ARTIFACT]
            or not {ARTIFACT, "report.json", "report.md"} <= files.keys() or len(files) > 265):
        raise _bad()
    model = _digest(manifest.get("model_sha256"))
    retained = {}
    for name, expected_digest in files.items():
        if name not in _KNOWN and not _INPUT_TABLE.fullmatch(name):
            raise _bad()
        data = budget.read(root, name, MAX_GENERATED_BYTES)
        if _hash(data) != _digest(expected_digest):
            raise Har2LoadError("COMPOSE_INTEGRITY_FAILED", "Source artifact digest mismatch.", 3)
        if name in ("report.json", ARTIFACT):
            retained[name] = data

    report = _metadata(retained["report.json"])
    generation = _obj(report.get("generation"))
    native = {k: v for k, v in files.items() if k not in ("report.json", "report.md")}
    _verify_targets(manifest, report, generation.get("requested_targets"), native)
    if (files["report.json"] != manifest.get("report_sha256")
            or report.get("report_schema_version") != schema
            or report.get("producer_version") not in ("0.11.0a1", "0.12.0a1", "0.13.0a1")
            or report["producer_version"] != generation.get("producer_version")
            or generation.get("profile") != profile or generation.get("profile_version") != version
            or generation.get("native_artifact_digests") != native
            or type(report.get("scenario_revision")) is not int
            or any(manifest[k] != report.get(k) for k in (
                "workspace_id", "scenario_revision", "model_sha256", "capture_sha256"))):
        raise _bad()
    return _Source(retained[ARTIFACT], {
        "label": label, "workspace_id": manifest["workspace_id"], "scenario_revision": revision,
        "model_sha256": model, "capture_sha256": capture, "report_sha256": files["report.json"],
        "manifest_sha256": _hash(raw_manifest), "jmx_sha256": files[ARTIFACT],
        "producer_version": report["producer_version"], "profile": profile,
        "profile_version": version, "source_generation_status": manifest["generation_status"],
        "source_jmeter_status": target["status"],
        "recorded_extraction_status": _obj(report.get("statuses")).get("captured_extraction"),
    })


def _publish(destination: Path, files: dict[str, bytes], manifest: bytes) -> None:
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        raise Har2LoadError("COMPOSE_DESTINATION_EXISTS",
                            "Output must be a new directory.", 3) from None
    except OSError:
        raise Har2LoadError("COMPOSE_WRITE_FAILED",
                            "Cannot create composition directory.", 5) from None
    try:
        (destination / "jmeter").mkdir(mode=0o700)
        for name, data in files.items():
            ReportPublisher.write_file(destination / name, data)
        ReportPublisher.write_file(destination / "manifest.json", manifest)
    except OSError:
        raise Har2LoadError("COMPOSE_WRITE_FAILED",
                            "Preserve partial output and select a new destination.", 5) from None


def compose_jmeter(sources: list[tuple[str, Path]], output: Path | None, *,
                   shared_steps: Path | None = None) -> dict[str, JSONValue]:
    """One UC per independent debug group; check performs all work except publication."""
    if not 2 <= len(sources) <= _MAX_UCS:
        raise Har2LoadError("COMPOSE_COUNT_INVALID", "Specify between 2 and 16 UCs.", 2)
    labels = [label for label, _ in sources]
    unique = {s.lower() for s in labels}
    if any(not TF_LABEL.fullmatch(s) for s in labels) or len(unique) != len(labels):
        raise Har2LoadError("COMPOSE_LABEL_INVALID", "Expected distinct safe TF_ labels.", 2)
    if output is not None:
        try:
            dest = output.resolve()
            ancestors = [p for p in (dest, *dest.parents) if p.exists()]
            if any(path.exists() and (dest.is_relative_to(path.resolve()) or
                    any(p.samefile(path) for p in ancestors)) for _, path in sources):
                raise Har2LoadError("COMPOSE_OUTPUT_IN_SOURCE",
                                    "Output must be outside all inputs.", 3)
        except (OSError, RuntimeError):
            raise _bad() from None
    budget = _Budget()
    verified = []
    for index, (label, path) in enumerate(sources, 1):
        try:
            verified.append(_source(label, path, budget))
        except Har2LoadError as error:
            raise Har2LoadError(error.code, f"UC input #{index}: {error}", 3) from None
    inputs = [(label, uc.jmx) for label, uc in zip(labels, verified, strict=True)]
    sharing = None
    if shared_steps is not None:
        pins = {label: _digest(uc.provenance["manifest_sha256"])
                for label, uc in zip(labels, verified, strict=True)}
        sharing = read_shared_plan(shared_steps, pins)
    jmx = compose_jmx(inputs, shared_steps=sharing.blocks) if sharing else compose_jmx(inputs)
    report: dict[str, JSONValue] = {
        "schema_version": "1.0", "producer_version": __version__, "policy": _POLICY,
        "sources": [uc.provenance for uc in verified], "jmx_sha256": _hash(jmx),
        "source_maps": "source_artifacts_only", "native_validation": "not_run",
        "execution": "not_run", "redaction": "off", "contains_sensitive_data": True,
    }
    if sharing:
        # Private report only. These decisions are supplied, not conclusions of the CLI.
        report.update(schema_version="1.1", shared_steps=sharing.document,
                      shared_steps_sha256=sharing.digest, shared_steps_policy="explicit_groups_v1")
    payloads = {ARTIFACT: jmx, "composition.json": canonical_bytes(report)}
    manifest: dict[str, JSONValue] = {
        "kind": "composed_jmeter_artifacts", "schema_version": "1.0", "status": "complete",
        "policy": _POLICY, "uc_count": len(sources), "producer_version": __version__,
        "execution": "not_run", "native_validation": "not_run", "redaction": "off",
        "contains_sensitive_data": True, "files": {k: _hash(v) for k, v in payloads.items()},
    }
    if sharing:
        manifest["shared_block_count"] = len(sharing.blocks)
    if output is not None:
        _publish(output, payloads, canonical_bytes(manifest))
    return envelope("compose.jmeter", {**manifest, "write_performed": output is not None})
