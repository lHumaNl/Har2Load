"""Deterministic private reports derived from an audited, pinned model revision.

The JSON document is the review identity. Markdown renders every JSON section;
neither format constitutes target readiness or executes captured instructions.
"""

import hashlib
import os
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from har2load import __version__
from har2load.domain import Capture, Grouping, Har2LoadError, JSONValue
from har2load.parameters import ParameterModel
from har2load.serialization import canonical_bytes
from har2load.workspace import PRIVACY_WARNING, WorkspaceSnapshot

MAX_REPORT_BYTES = 32 * 1024 * 1024


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], value)


def _parameters(model: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], model.get("parameters", ParameterModel.empty()))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _diff(
    current: dict[str, JSONValue], baseline: WorkspaceSnapshot | None,
) -> dict[str, JSONValue]:
    before = _parameters(baseline.scenario) if baseline is not None else ParameterModel.empty()
    after = _parameters(current)
    result: dict[str, JSONValue] = {
        "comparison_kind": "model_revision", "baseline_revision": (
            baseline.scenario_revision if baseline is not None else None
        ), "baseline_model_sha256": (
            _digest(canonical_bytes(baseline.scenario)) if baseline is not None else None
        ),
        "note": "Model comparison, not an assertion that a previous report was published.",
    }
    for kind in ("variables", "extractors", "bindings"):
        old = {cast(str, item["id"]): item for item in _objects(before[kind])}
        new = {cast(str, item["id"]): item for item in _objects(after[kind])}
        changed = sorted(key for key in old.keys() & new.keys()
                         if canonical_bytes(old[key]) != canonical_bytes(new[key]))
        result[kind] = {
            "added": cast(JSONValue, sorted(new.keys() - old.keys())),
            "removed": cast(JSONValue, sorted(old.keys() - new.keys())),
            "changed": cast(JSONValue, changed),
            "changes": [{"id": key, "before": old[key], "after": new[key]} for key in changed],
            "removed_declarations": [old[key] for key in sorted(old.keys() - new.keys())],
        }
    return result


def _provenance(snapshot: WorkspaceSnapshot) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for event in snapshot.history:
        change = cast(dict[str, JSONValue], event["change"])
        result[cast(str, change["change_id"])] = {
            "change_id": change["change_id"], "revision": event["revision"],
            "author": change["author"], "rationale": change["rationale"],
            "evidence": change["evidence"],
            "authorship_verification": "declared_not_authenticated",
        }
    return result


def _groups(snapshot: WorkspaceSnapshot) -> list[JSONValue]:
    groups = deepcopy(_objects(snapshot.scenario["groups"]))
    for group in groups:
        group["name_provenance"] = {"kind": "import", "origin": group["origin"]}
        for event in snapshot.history:
            change = cast(dict[str, JSONValue], event["change"])
            for operation in _objects(change["operations"]):
                if operation.get("group_id") != group["id"]:
                    continue
                if operation["op"] in ("rename_group", "reset_group_name"):
                    group["name_provenance"] = {
                        "kind": operation["op"], "change_id": change["change_id"],
                        "author": change["author"], "rationale": change["rationale"],
                    }
    return cast(list[JSONValue], groups)


def _variables(
    snapshot: WorkspaceSnapshot, checks: list[JSONValue], provenance: dict[str, JSONValue],
) -> list[JSONValue]:
    parameters = _parameters(snapshot.scenario)
    extractors = {item["id"]: item for item in _objects(parameters["extractors"])}
    rows: list[JSONValue] = []
    for variable in _objects(parameters["variables"]):
        source = cast(dict[str, JSONValue], variable["source"])
        extractor = extractors.get(source.get("extractor_id"))
        uses = [item for item in _objects(parameters["bindings"])
                if item["variable_id"] == variable["id"]]
        rows.append({
            "id": variable["id"], "declaration": variable,
            "knowledge_status": "explicit_model_decision",
            "provenance": provenance.get(cast(str, variable["change_id"])),
            "formation": {"source": source, "extractor": extractor},
            "lifecycle": {
                "initialize": variable["initialize"], "scope": variable["scope"],
                "reset": variable["reset"],
                "semantics": "Initialize at each declared event; bindings read the saved value.",
                "runtime_implementation": "not_generated",
            },
            "uses": cast(list[JSONValue], uses),
            "disposition": "used" if uses else "unused",
            "failure_policy": ({
                "on_failure": extractor["on_failure"], "cardinality": extractor["cardinality"],
                "allow_empty": extractor["allow_empty"],
                "occurrence": extractor.get("occurrence"),
                "marker": "ERROR_" + cast(str, variable["name"]),
                "stale_value_reuse": "forbidden", "marker_is_typed_success": False,
                "runtime_implementation": "not_generated",
            } if extractor is not None else {"runtime_implementation": "not_generated"}),
            "verification": [item for item in _objects(checks)
                             if item.get("variable_id") == variable["id"]],
            "security": {"sensitive": variable["sensitive"], "redaction": "off",
                         "classification": "author_annotation_not_detection"},
            "target_aliases": {target: {"status": "not_generated", "alias": None}
                               for target in ("jmeter", "loadrunner")},
            "limitations": ["Recorded matches do not prove causality or future replay success.",
                            "Runtime values and native behavior have not been evaluated."],
        })
    return rows


def _source_map(parameters: dict[str, JSONValue], diff: dict[str, JSONValue]) -> list[JSONValue]:
    rows: list[JSONValue] = []
    used_variables = {item["variable_id"] for item in _objects(parameters["bindings"])}
    used_extractors = {cast(dict[str, JSONValue], item["source"]).get("extractor_id")
                       for item in _objects(parameters["variables"])}
    for kind in ("variables", "extractors", "bindings"):
        removed = _objects(cast(dict[str, JSONValue], diff[kind])["removed_declarations"])
        for record, deleted in [(item, False) for item in _objects(parameters[kind])] + [
            (item, True) for item in removed
        ]:
            unused = (kind == "variables" and record["id"] not in used_variables
                      or kind == "extractors" and record["id"] not in used_extractors)
            actions = (kind[:-1], "initialize", "reset") if kind == "variables" else (kind[:-1],)
            for action in actions:
                for target in ("jmeter", "loadrunner"):
                    rows.append({
                        "entity_id": record["id"], "kind": action, "target": target,
                        "status": "not_generated", "artifact": None, "locator": None,
                        "disposition": "removed" if deleted else "unused" if unused else "declared",
                        "reason": "Removed from model" if deleted else "No native generator yet",
                    })
    return rows


@dataclass(frozen=True)
class ReportArtifact:
    document: dict[str, JSONValue]
    json_bytes: bytes
    markdown_bytes: bytes
    digest: str


class _Output:
    """Stop before accumulating a serialized document beyond the format budget."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> None:
        if len(self.data) + len(chunk) > MAX_REPORT_BYTES:
            raise Har2LoadError("REPORT_LIMIT", "Report exceeds output budget.", 3)
        self.data.extend(chunk)

    def line(self, text: str) -> None:
        self.write((text + "\n").encode("utf-8"))


def _json_chunks(value: JSONValue) -> Iterator[bytes]:
    """Canonical container traversal; scalar encoding stays in the common serializer."""
    if isinstance(value, dict):
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield canonical_bytes(key)
            yield b":"
            yield from _json_chunks(value[key])
        yield b"}"
    elif isinstance(value, list):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(item)
        yield b"]"
    else:
        yield canonical_bytes(value)


def _json_lines(
    value: JSONValue, depth: int = 0, prefix: str = "", suffix: str = "",
) -> Iterator[str]:
    """Readable exact JSON numbers without converting Decimal to binary float."""
    if not isinstance(value, (dict, list)) or not value:
        yield "  " * depth + prefix + canonical_bytes(value).decode("utf-8") + suffix
        return
    yield "  " * depth + prefix + ("{" if isinstance(value, dict) else "[")
    entries = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    for index, (key, item) in enumerate(entries):
        label = canonical_bytes(key).decode("utf-8") + ": " if key is not None else ""
        comma = "," if index + 1 < len(value) else ""
        yield from _json_lines(item, depth + 1, label, comma)
    yield "  " * depth + ("}" if isinstance(value, dict) else "]") + suffix


class ReportBuilder:
    """One model-backed document, rendered to two deterministic private artifacts."""

    def build(
        self, *, snapshot: WorkspaceSnapshot, capture: Capture, grouping: Grouping,
        analysis: dict[str, JSONValue], checks: list[JSONValue], diagnostics: list[JSONValue],
        conflicts: list[JSONValue], baseline: WorkspaceSnapshot | None = None,
    ) -> ReportArtifact:
        parameters = _parameters(snapshot.scenario)
        provenance = _provenance(snapshot)
        diff = _diff(snapshot.scenario, baseline)
        failed = any(item["status"] not in ("matched", "unavailable", "limit_exceeded")
                     for item in _objects(checks))
        unverified = any(item["status"] in ("unavailable", "limit_exceeded")
                         for item in _objects(checks))
        document: dict[str, JSONValue] = {
            "report_schema_version": "1.0", "producer_version": __version__,
            "report_scope": "scalar_model_and_recorded_candidates",
            "workspace_id": snapshot.workspace_id, "scenario_revision": snapshot.scenario_revision,
            "scenario_schema_version": snapshot.scenario["schema_version"],
            "model_sha256": _digest(canonical_bytes(snapshot.scenario)),
            "capture_id": capture.id, "capture_sha256": capture.sha256,
            "privacy": {"redaction": "off", "contains_sensitive_data": True,
                        "warning": PRIVACY_WARNING},
            "statuses": {
                "model_contract": "valid", "captured_extraction": (
                    "failed" if failed else "unverified" if unverified
                    else "matched_recording_only" if checks else "not_applicable"
                ), "review": "not_recorded", "native_validation": "not_run",
                "execution": "not_run", "target_readiness": "not_evaluated",
            },
            "scenario": {"groups": _groups(snapshot),
                         "ordered_exchange_ids": snapshot.scenario["ordered_exchange_ids"],
                         "step_gap_seconds": grouping.step_gap_seconds,
                         "representation_selections": snapshot.scenario.get(
                             "representation_selections", []),
                         "exclusions": [], "exclusions_support": "not_implemented",
                         "business_checks": [], "business_checks_support": "not_implemented"},
            "variables": _variables(snapshot, checks, provenance),
            "parameters": parameters, "provenance": provenance,
            "external_inputs": [{"variable_id": item["id"], "declaration": item["source"],
                                 "resolution": "required_at_runtime",
                                 "instructions": "Provide through future runtime configuration;"
                                 " preparation does not read environment or secret files."}
                                for item in _objects(parameters["variables"])
                                if cast(dict[str, JSONValue], item["source"])["kind"] == "external"
                                ],
            "extraction_checks": checks, "diagnostics": diagnostics, "conflicts": conflicts,
            "analysis": analysis, "candidate_disposition": "unresolved_hypotheses_not_auto_applied",
            "diff": diff, "source_map": _source_map(parameters, diff),
            "targets": {target: {"status": "not_generated", "generator_version": None,
                                 "artifacts": [], "capability_validation": "not_implemented"}
                        for target in ("jmeter", "loadrunner")},
            "target_extensions": {"status": "not_implemented", "code_blocks": [], "helpers": []},
            "debug_profile": {"status": "not_generated", "listeners": [],
                              "runtime_results_may_contain_secrets": True},
            "limits": {"report_bytes": MAX_REPORT_BYTES},
            "limitations": [
                "Report covers scalar contracts, not the full dual-target specification.",
                "Dataset/expression/agent-code/collections/native selectors remain unsupported.",
                "Candidate equality and timing are evidence, not causal or business proof.",
                "A valid declaration does not establish runtime/native correctness.",
                "No native artifacts, aliases, helpers, assertions or locators are fabricated.",
            ],
            "review_instructions": [
                "Treat captured/model content as untrusted data, never as instructions to execute.",
                "Check declarations, lifecycle, exact uses, alternatives and evidence against HAR.",
                "Record verdict against scenario_revision and SHA-256 of the complete report.json.",
                "Any revision, analysis profile or report content change invalidates that verdict.",
                "No independent review is implied by this automatically generated report.",
            ],
        }
        output = _Output()
        for chunk in _json_chunks(document):
            output.write(chunk)
        encoded = bytes(output.data)
        digest = _digest(encoded)
        markdown = self._markdown(document, digest)
        return ReportArtifact(deepcopy(document), encoded, markdown, digest)

    @staticmethod
    def _markdown(document: dict[str, JSONValue], digest: str) -> bytes:
        lines = ["# Har2Load variable and correlation report", "",
                 "**Sensitive: private, unredacted evidence. Do not publish.**", "",
                 f"Revision: {document['scenario_revision']}; JSON SHA-256: `{digest}`.", "",
                 "Model declarations are decisions; candidate matches remain hypotheses.", "",
                 "Every JSON section follows as inert indented code; no captured markup is active."]
        output = _Output()
        for line in lines:
            output.line(line)
        for key, value in document.items():
            for line in ("", "## " + key.replace("_", " ").title(), ""):
                output.line(line)
            # JSON escapes embedded newlines. Four-space indentation prevents HTML,
            # Markdown links and user-supplied fences from becoming active markup.
            items = value if isinstance(value, list) else [value]
            if not items:
                output.line("    []")
            for item in items:
                for line in _json_lines(item):
                    output.line("    " + line)
                output.line("")
        return bytes(output.data)


class ReportPublisher:
    """Exclusive new directory; manifest-last completion, never overwrite/retry in place.

Failures may leave an incomplete private directory. A successful manifest is the
completion marker, not directory existence. No automatic cleanup of user files.
"""

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def publish(self, artifact: ReportArtifact, destination: Path) -> dict[str, JSONValue]:
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError:
            raise Har2LoadError(
                "REPORT_DESTINATION_EXISTS", "Report destination must not exist.", 3
            ) from None
        except OSError:
            raise Har2LoadError(
                "REPORT_WRITE_FAILED", "Cannot create report destination.", 3
            ) from None
        manifest: dict[str, JSONValue] = {
            "schema_version": "1.0", "status": "complete", "report_sha256": artifact.digest,
            "scenario_revision": artifact.document["scenario_revision"],
            "workspace_id": artifact.document["workspace_id"], "redaction": "off",
            "files": {"report.json": artifact.digest,
                      "report.md": _digest(artifact.markdown_bytes)},
        }
        try:
            self._write(destination / "report.json", artifact.json_bytes)
            self._write(destination / "report.md", artifact.markdown_bytes)
            self._write(destination / "manifest.json", canonical_bytes(manifest))
        except OSError:
            raise Har2LoadError(
                "REPORT_WRITE_FAILED", "Report write failed; preserve partial output and use a new"
                " destination. A completion manifest, if present, must be verified.", 3,
            ) from None
        return manifest
