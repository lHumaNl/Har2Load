"""Bounded dual-target artifact preparation and private manifest-last publication.

Rendering is not native validation or replay. A blocked target never leaves a
partial native file, but the other target and diagnostic reports can be published.
"""

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import cast

from har2load import __version__
from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.extensions import TargetExtensions
from har2load.generation_types import GENERATION_PROFILE, RequestPlan, TargetBuild
from har2load.jmeter import JMeterGenerator
from har2load.loadrunner import LoadRunnerGenerator
from har2load.materialization import RequestMaterializer
from har2load.presentation import record
from har2load.reporting import ReportArtifact, ReportBuilder, ReportPublisher
from har2load.serialization import canonical_bytes
from har2load.workspace import WorkspaceSnapshot

TARGETS = ("jmeter", "loadrunner")
_FILES = {"jmeter/scenario.jmx", "loadrunner/Action.c", "loadrunner/globals.h",
          "loadrunner/vuser_init.c", "loadrunner/vuser_end.c", "loadrunner/README.txt"}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], value)


def _target(
    name: str, plan: RequestPlan, scenario: dict[str, JSONValue],
) -> TargetBuild:
    extensions = cast(
        dict[str, JSONValue], scenario.get("target_extensions", TargetExtensions.empty())
    )
    parameters = cast(dict[str, JSONValue], scenario.get("parameters", {
        "variables": [], "extractors": [], "bindings": [],
    }))
    diagnostics = [replace(d, target=name) for d in plan.diagnostics]
    diagnostics.extend(d for d in TargetExtensions.diagnostics(extensions, parameters)
                       if d.target == name and d.severity == "error")
    if any(item["target"] == name for kind in ("blocks", "helpers")
           for item in _objects(extensions[kind])):
        diagnostics.append(Diagnostic("GENERATION_EXTENSION_UNSUPPORTED", "error",
            "Code/helper runtime wrappers are outside this generation profile.", target=name))
    generator = JMeterGenerator() if name == "jmeter" else LoadRunnerGenerator()
    return generator.generate(replace(plan, diagnostics=tuple(diagnostics)))


def _mappings(builds: tuple[TargetBuild, ...]) -> list[JSONValue]:
    rows: list[JSONValue] = []
    for build in builds:
        for source in build.source_map:
            path = cast(str, source["artifact"])
            if path not in build.files:
                raise Har2LoadError(
                    "GENERATION_MAP_INVALID", "Artifact mapping is inconsistent.", 3
                )
            rows.append({**source, "target": build.target, "status": "generated",
                         "artifact_sha256": _hash(build.files[path])})
    return rows


def _parameter_map(
    baseline: list[dict[str, JSONValue]], mappings: list[JSONValue], scenario: dict[str, JSONValue],
    statuses: dict[str, JSONValue],
) -> list[JSONValue]:
    result: list[JSONValue] = []
    parameters = cast(dict[str, JSONValue], scenario.get("parameters", {"bindings": []}))
    bindings = {item["id"]: item for item in _objects(parameters["bindings"])}
    requests = {(item["target"], item["entity_id"]): item for item in _objects(mappings)
                if item["kind"] == "request"}
    for row in baseline:
        target = cast(str, row["target"])
        if statuses.get(target) != "generated" or row["disposition"] == "removed":
            result.append(row if row["disposition"] == "removed" else {
                **row, "reason": "Target blocked" if statuses.get(target) == "blocked"
                else "Target not requested",
            })
            continue
        if row["kind"] == "binding":
            binding = bindings[row["entity_id"]]
            request = requests[(target, binding["exchange_id"])]
            result.append({**row, "status": "generated", "disposition": "constant_materialized",
                "artifact": request["artifact"], "artifact_sha256": request["artifact_sha256"],
                "locator": request["locator"], "binding_location": binding["location"],
                "reason": "Literal value materialized in this request component"})
        else:
            result.append({**row, "status": "not_emitted", "artifact": None, "locator": None,
                "disposition": "unused" if row["disposition"] == "unused" else "constant_folded",
                "reason": "Immutable literal folded; no runtime variable/lifecycle action emitted"})
    return result


@dataclass(frozen=True)
class GenerationArtifact:
    files: dict[str, bytes]
    report: ReportArtifact
    manifest: dict[str, JSONValue]
    diagnostics: tuple[Diagnostic, ...]
    blocked: bool


class GenerationBuilder:
    def build(
        self, snapshot: WorkspaceSnapshot, capture: Capture, grouping: Grouping,
        base_report: ReportArtifact, targets: tuple[str, ...],
    ) -> GenerationArtifact:
        if (not targets or len(set(targets)) != len(targets)
                or any(t not in TARGETS for t in targets)):
            raise Har2LoadError("GENERATION_TARGET_INVALID", "Invalid target selection.", 2)
        selected = tuple(target for target in TARGETS if target in targets)
        plan = RequestMaterializer().build(capture, grouping, snapshot.scenario)
        builds = tuple(_target(target, plan, snapshot.scenario) for target in selected)
        files = {path: data for build in builds for path, data in build.files.items()}
        if any(path not in _FILES for path in files):
            raise Har2LoadError("GENERATION_PATH_INVALID", "Invalid generated artifact path.", 3)
        statuses: dict[str, JSONValue] = {build.target: build.status for build in builds}
        target_metadata: dict[str, JSONValue] = {
            build.target: {**build.metadata, "status": build.status,
                           "files": cast(JSONValue, sorted(build.files))} for build in builds
        }
        diagnostics = tuple(d for build in builds for d in build.diagnostics)
        document = deepcopy(base_report.document)
        document["report_schema_version"] = "1.2"
        document["targets"] = {target: target_metadata.get(target, {"status": "not_requested"})
                               for target in TARGETS}
        jmeter_generated = statuses.get("jmeter") == "generated"
        document["debug_profile"] = {
            "status": "target_specific", "runtime_results_may_contain_secrets": True,
            "listeners": ([{"target": "jmeter", "name": "View Results Tree", "enabled": True}]
                          if jmeter_generated else []),
            "targets": {
                "jmeter": {"status": statuses.get("jmeter", "not_requested"),
                           "thread_group": "DebugTG" if jmeter_generated else None,
                           "threads": 1 if jmeter_generated else None,
                           "iterations": 1 if jmeter_generated else None,
                           "debug_sampler": jmeter_generated,
                           "display_runtime_variables": jmeter_generated,
                           "display_system_properties": False, "display_jmeter_properties": False},
                "loadrunner": {"status": statuses.get("loadrunner", "not_requested"),
                               "runtime_settings": "template_pending_unverified",
                               "continue_on_error_emitted": (
                                   statuses.get("loadrunner") == "generated")},
            },
        }
        maps = _mappings(builds)
        document["source_map"] = _parameter_map(
            _objects(document["source_map"]), maps, snapshot.scenario, statuses
        ) + maps
        document["generation"] = {
            "profile": GENERATION_PROFILE, "profile_version": "1", "producer_version": __version__,
            "requested_targets": list(selected), "decisions": list(plan.decisions),
            "diagnostics": [record(d) for d in diagnostics],
            "native_artifact_digests": {path: _hash(data) for path, data in sorted(files.items())},
            "native_validation": "not_run", "execution": "not_run", "wire_parity": "unverified",
            "publication": "prepared_artifacts", "contains_sensitive_data": True,
            "limitations": [
                "Static requests and literal folding only; not the complete dual-target product.",
                "LoadRunner output is C source scaffold, not a verified runnable VuGen project.",
                "No target process, captured endpoint or native loader was invoked.",
                "Recorded cookies/authentication and runtime producers require future support.",
            ],
        }
        report = ReportBuilder().render(document)
        files.update({"report.json": report.json_bytes, "report.md": report.markdown_bytes})
        blocked = any(build.status == "blocked" for build in builds)
        manifest: dict[str, JSONValue] = {
            "schema_version": "1.0", "kind": "generated_static_artifacts", "status": "complete",
            "workspace_id": snapshot.workspace_id, "scenario_revision": snapshot.scenario_revision,
            "capture_sha256": capture.sha256, "model_sha256": document["model_sha256"],
            "report_sha256": report.digest, "profile": GENERATION_PROFILE,
            "targets": target_metadata, "generation_status": (
                "partial" if blocked and any(build.files for build in builds)
                else "blocked" if blocked else "generated"
            ), "files": {path: _hash(data) for path, data in sorted(files.items())},
            "redaction": "off", "contains_sensitive_data": True,
            "native_validation": "not_run", "execution": "not_run",
        }
        return GenerationArtifact(files, report, manifest, diagnostics, blocked)


class GenerationPublisher:
    """Never overwrite: reserve private directory, publish completion manifest last."""

    def publish(self, artifact: GenerationArtifact, destination: Path) -> None:
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError:
            raise Har2LoadError("GENERATION_DESTINATION_EXISTS",
                               "Generation destination must not exist.", 3) from None
        except OSError:
            raise Har2LoadError(
                "GENERATION_WRITE_FAILED", "Cannot create output directory.", 5
            ) from None
        try:
            for name, data in sorted(artifact.files.items()):
                path = destination.joinpath(*PurePosixPath(name).parts)
                if path.parent != destination:
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                ReportPublisher.write_file(path, data)
            ReportPublisher.write_file(
                destination / "manifest.json", canonical_bytes(artifact.manifest)
            )
        except OSError:
            raise Har2LoadError("GENERATION_WRITE_FAILED",
                "Preserve partial output, verify any manifest, and retry with a new directory.",
                5) from None
