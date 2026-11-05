"""Bounded dual-target artifact preparation and private manifest-last publication.

Rendering is not native validation or replay. A blocked target never leaves a
partial native file, but the other target and diagnostic reports can be published.
"""

import hashlib
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import cast

from har2load import __version__
from har2load.cookies import COOKIE_POLICY, plan_initial_cookies
from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.extension_runtime import capability_diagnostics
from har2load.extensions import TargetExtensions
from har2load.generation_types import GENERATION_PROFILE, RequestPlan, TargetBuild
from har2load.jmeter import JMeterGenerator
from har2load.loadrunner import LoadRunnerGenerator
from har2load.presentation import record
from har2load.reporting import ReportArtifact, ReportBuilder, ReportPublisher
from har2load.runtime_planning import GenerationPlanner
from har2load.runtime_sources import GENERATOR_POLICY, INPUT_POLICY, source_diagnostics
from har2load.runtime_types import (
    EXTENSION_RUNTIME_PROFILE,
    RUNTIME_PROFILE,
    RUNTIME_STATUS_POLICY,
)
from har2load.serialization import canonical_bytes
from har2load.workspace import WorkspaceSnapshot

TARGETS = ("jmeter", "loadrunner")
_FILES = {"jmeter/scenario.jmx", "loadrunner/Action.c", "loadrunner/globals.h",
          "loadrunner/vuser_init.c", "loadrunner/vuser_end.c", "loadrunner/README.txt",
          "loadrunner/native_inputs.prm"}
_INPUT_TABLE = re.compile(r"loadrunner/data/input_[0-9a-f]{64}\.dat")
_PARAMETER_KINDS = {"variable", "initialize", "reset", "extractor", "binding"}
_RUNTIME_KINDS = _PARAMETER_KINDS | {
    "code_block", "reset_outputs", "helper_definition", "helper_call",
}


def _map_key(row: dict[str, JSONValue]) -> tuple[str, str, str, str]:
    return (cast(str, row["target"]), cast(str, row["kind"]), cast(str, row["entity_id"]),
            cast(str, row.get("helper_id", "")) if row["kind"] == "helper_call" else "")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], value)


def _map_error() -> Har2LoadError:
    return Har2LoadError("GENERATION_MAP_INVALID", "Artifact mapping is inconsistent.", 3)


def _valid_locator(row: dict[str, JSONValue]) -> bool:
    locator = row.get("locator")
    if not isinstance(locator, dict):
        return False
    if row.get("target") == "jmeter":
        path, kind = locator.get("component_path"), locator.get("element_type")
        return (isinstance(path, list) and bool(path)
                and all(isinstance(item, str) and bool(item) for item in path)
                and isinstance(kind, str) and bool(kind))
    if row.get("target") == "loadrunner":
        return all(isinstance(locator.get(k), str) and bool(locator[k])
                   for k in ("marker", "function"))
    return False


def _validate_runtime_maps(
    baseline: list[dict[str, JSONValue]], mappings: list[JSONValue], statuses: dict[str, JSONValue],
) -> None:
    expected = {_map_key(r) for r in baseline
                if r["kind"] in _RUNTIME_KINDS and r["disposition"] != "removed"
                and statuses.get(cast(str, r["target"])) == "generated"}
    seen: set[tuple[str, str, str, str]] = set()
    for row in _objects(mappings):
        if row["kind"] not in _RUNTIME_KINDS:
            continue
        key = _map_key(row)
        if key not in expected or key in seen or not _valid_locator(row):
            raise _map_error()
        seen.add(key)
    if seen != expected:
        raise _map_error()


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
    if plan.runtime is not None:
        diagnostics.extend(capability_diagnostics(plan.runtime, name))
        diagnostics.extend(source_diagnostics(plan.runtime, name))
    generator = JMeterGenerator() if name == "jmeter" else LoadRunnerGenerator()
    return generator.generate(replace(plan, diagnostics=tuple(diagnostics)))


def _mappings(builds: tuple[TargetBuild, ...]) -> list[JSONValue]:
    rows: list[JSONValue] = []
    for build in builds:
        for source in build.source_map:
            path = cast(str, source["artifact"])
            if path not in build.files or not _valid_locator({**source, "target": build.target}):
                raise _map_error()
            rows.append({**source, "target": build.target, "status": "generated",
                         "artifact_sha256": _hash(build.files[path])})
    return rows


def _parameter_map(
    baseline: list[dict[str, JSONValue]], mappings: list[JSONValue], scenario: dict[str, JSONValue],
    statuses: dict[str, JSONValue], runtime: bool = False,
) -> list[JSONValue]:
    result: list[JSONValue] = []
    parameters = cast(dict[str, JSONValue], scenario.get("parameters", {"bindings": []}))
    bindings = {item["id"]: item for item in _objects(parameters["bindings"])}
    if runtime:
        _validate_runtime_maps(baseline, mappings, statuses)
    requests = {(item["target"], item["entity_id"]): item for item in _objects(mappings)
                if item["kind"] == "request"}
    implementations = {_map_key(item): item
                       for item in _objects(mappings)}
    for row in baseline:
        target = cast(str, row["target"])
        if statuses.get(target) != "generated" or row["disposition"] == "removed":
            result.append(row if row["disposition"] == "removed" else {
                **row, "reason": "Target blocked" if statuses.get(target) == "blocked"
                else "Target not requested",
            })
            continue
        if runtime:
            implementation = implementations.get(_map_key(row))
            if implementation is None:
                raise Har2LoadError("GENERATION_MAP_INVALID",
                                   "Runtime declaration has no artifact mapping.", 3)
            result.append({**row, **implementation, "disposition": "runtime_emitted",
                           "declaration_disposition": row["disposition"],
                           "reason": ("Declared helper access emitted; source calls unverified"
                                      if row["kind"] == "helper_call" else
                                      "Runtime action emitted; native behavior unverified")})
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


def _runtime_report(document: dict[str, JSONValue], statuses: dict[str, JSONValue]) -> None:
    target_statuses = {target: statuses.get(target, "not_requested") for target in TARGETS}
    input_definitions: dict[str, list[JSONValue]] = {}
    for variable in _objects(document["variables"]):
        declaration = cast(dict[str, JSONValue], variable["declaration"])
        source = cast(dict[str, JSONValue], declaration["source"])
        if source["kind"] == "external" and "initial_value" in source:
            input_definitions.setdefault(cast(str, source["key"]), []).append(variable["id"])
    for variable in _objects(document["variables"]):
        for key in ("lifecycle", "failure_policy"):
            section = cast(dict[str, JSONValue], variable[key])
            section["runtime_implementation"] = "target_specific"
            section["target_implementations"] = target_statuses.copy()
        failure = cast(dict[str, JSONValue], variable["failure_policy"])
        failure.update({"failure_wire_policy": "error_marker_json_string_v1",
                        "marker": "ERROR_" + cast(str, cast(dict[str, JSONValue],
                                                           variable["declaration"])["name"]),
                         "marker_is_typed_success": False, "stale_value_reuse": "forbidden",
                         "native_status_policy": RUNTIME_STATUS_POLICY})
        declaration = cast(dict[str, JSONValue], variable["declaration"])
        source = cast(dict[str, JSONValue], declaration["source"])
        formation = cast(dict[str, JSONValue], variable["formation"])
        if source["kind"] == "external":
            definitions = input_definitions.get(cast(str, source["key"]), [])
            formation["native_input"] = {
                "key": source["key"], "policy": INPUT_POLICY,
                "initial_value_present": bool(definitions),
                "initial_definition_variable_ids": list(definitions),
                "missing_input_policy": "marker_and_continue", "resolution": "not_run",
                "custom_runtime_json_file": False,
            }
        elif source["kind"] == "generated":
            formation["native_generator"] = {
                "policy": GENERATOR_POLICY, "evaluation": "declared_initialization_event",
                "per_binding_regeneration": False, "runtime_value": "not_generated",
                "security_token_guarantee": False, "cross_target_identical_stream": False,
            }
    for item in _objects(document["external_inputs"]):
        source = cast(dict[str, JSONValue], item["declaration"])
        supplied = bool(input_definitions.get(cast(str, source["key"])))
        item["instructions"] = (
            ("Explicit initial configuration is supplied for this shared input key. "
             "For generated targets, edit literal-encoded JMeter Test Plan UDV or integrate "
             "native_inputs.prm/data files into a real VuGen project and edit its parameters. "
             if supplied else
             "No explicit initial configuration is supplied for this shared input key. "
             "For JMeter, add a UDV entry using jmeter_udv_literal_v1 encoding. "
             "For VuGen, declare source.initial_value and regenerate before integrating "
             "native parameter files; undeclared native keys are not probed. ")
            + "Consult each target's generation status and encoding limitations; runtime "
            "resolution remains unverified. Missing/invalid inputs produce ERROR_<name> "
            "and continue without overriding native status. No custom runtime JSON file."
        )
    extensions = cast(dict[str, JSONValue], document["target_extensions"])
    extensions.update({"runtime_implementation": "target_specific",
                       "target_implementations": target_statuses.copy(),
                       "output_commit_policy": "per_output_validated_v1",
                       "native_status_policy": RUNTIME_STATUS_POLICY,
                       "wrapper_failure_handling": "reported_errors_only_not_fault_containment",
                       "helper_calls": "author_declared_not_observed"})
    document["limitations"] = [
        "Emitted wrappers do not prove authored code semantics, safety or target parity.",
        "Successful validated outputs survive later failures; other outputs remain markers.",
        "Correlation errors never override native sample/transaction status.",
        "Ordinary preparation never executes source or resolves runtime dependencies.",
        "Explicit native initial strings are preserved with redaction off, including secrets.",
        "Generated values are runtime-only; no global uniqueness or cryptographic promise.",
        "Native crashes, undefined behavior, infinite loops and side effects are not contained.",
    ]


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
        initial_cookies: tuple[str, ...] = (),
    ) -> GenerationArtifact:
        if (not targets or len(set(targets)) != len(targets)
                or any(t not in TARGETS for t in targets)):
            raise Har2LoadError("GENERATION_TARGET_INVALID", "Invalid target selection.", 2)
        selected = tuple(target for target in TARGETS if target in targets)
        plan = GenerationPlanner().build(capture, grouping, snapshot.scenario)
        plan = replace(plan, initial_cookies=plan_initial_cookies(capture, initial_cookies))
        runtime = plan.runtime is not None
        authored = bool(plan.runtime and (plan.runtime.blocks or plan.runtime.helpers))
        profile = (EXTENSION_RUNTIME_PROFILE if authored else RUNTIME_PROFILE if runtime
                   else GENERATION_PROFILE)
        builds = tuple(_target(target, plan, snapshot.scenario) for target in selected)
        files = {path: data for build in builds for path, data in build.files.items()}
        if any(not path.startswith(build.target + "/")
               or (path not in _FILES and _INPUT_TABLE.fullmatch(path) is None)
               for build in builds for path in build.files):
            raise Har2LoadError("GENERATION_PATH_INVALID", "Invalid generated artifact path.", 3)
        statuses: dict[str, JSONValue] = {build.target: build.status for build in builds}
        target_metadata: dict[str, JSONValue] = {
            build.target: {**build.metadata, "status": build.status,
                           "files": cast(JSONValue, sorted(build.files))} for build in builds
        }
        diagnostics = tuple(d for build in builds for d in build.diagnostics)
        document = deepcopy(base_report.document)
        document["report_schema_version"] = "1.6" if runtime else "1.3"
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
            _objects(document["source_map"]), maps, snapshot.scenario, statuses, runtime
        ) + [row for row in maps if not runtime or cast(dict[str, JSONValue], row)["kind"]
              not in _RUNTIME_KINDS]
        if runtime:
            _runtime_report(document, statuses)
        document["generation"] = {
            "profile": profile, "profile_version": "3" if authored else "4" if runtime else "1",
            "producer_version": __version__,
            "cookie_policy": COOKIE_POLICY,
            "initial_cookies": [cast(JSONValue, asdict(cookie)) for cookie in plan.initial_cookies],
            "cookie_initialization": "iteration_start_after_clear",
            "cookie_authentication": "unverified",
            "requested_targets": list(selected), "decisions": list(plan.decisions),
            "diagnostics": [record(d) for d in diagnostics],
            "native_artifact_digests": {path: _hash(data) for path, data in sorted(files.items())},
            "native_validation": "not_run", "execution": "not_run", "wire_parity": "unverified",
            "publication": "prepared_artifacts", "contains_sensitive_data": True,
            "limitations": [
                ("Scalar runtime artifacts; not the complete dual-target product." if runtime else
                 "Static requests and literal folding only; not the complete dual-target product."),
                "LoadRunner output is C source scaffold, not a verified runnable VuGen project.",
                "No target process, captured endpoint or native loader was invoked.",
                "Captured cookies are not replayed as fixed headers; native session is unverified.",
                "Without explicit seeds, normal scenario authentication must establish cookies.",
                "Recorded Authorization headers remain unsupported; no automatic auth inference.",
            ],
        }
        if runtime:
            generation = cast(dict[str, JSONValue], document["generation"])
            generation.update({"failure_wire_policy": "error_marker_json_string_v1",
                                "native_input_policy": INPUT_POLICY,
                                "generator_policy": GENERATOR_POLICY,
                                "missing_input_policy": "marker_and_continue",
                                "native_status_policy": RUNTIME_STATUS_POLICY,
                                "output_commit_policy": "per_output_validated_v1",
                               "runtime_validation": "not_run",
                               "failure_changes_json_type": True,
                               "success_preserves_declared_type": True})
        report = ReportBuilder().render(document)
        files.update({"report.json": report.json_bytes, "report.md": report.markdown_bytes})
        blocked = any(build.status == "blocked" for build in builds)
        manifest: dict[str, JSONValue] = {
            "schema_version": "1.0", "kind": (
                "generated_runtime_artifacts" if runtime else "generated_static_artifacts"),
            "status": "complete",
            "workspace_id": snapshot.workspace_id, "scenario_revision": snapshot.scenario_revision,
            "capture_sha256": capture.sha256, "model_sha256": document["model_sha256"],
            "report_sha256": report.digest, "profile": profile,
            "targets": target_metadata, "generation_status": (
                "partial" if blocked and any(build.files for build in builds)
                else "blocked" if blocked else "generated"
            ), "files": {path: _hash(data) for path, data in sorted(files.items())},
            "redaction": "off", "contains_sensitive_data": True,
            "cookie_policy": COOKIE_POLICY, "initial_cookie_count": len(plan.initial_cookies),
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
