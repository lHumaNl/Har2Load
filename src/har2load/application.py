"""Offline use cases joining pure interpretation to private storage."""

from pathlib import Path
from typing import cast

from har2load.binding_locations import SourceViews
from har2load.candidates import CandidateAnalyzer
from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.extensions import TargetExtensions
from har2load.generation import GenerationArtifact, GenerationBuilder, GenerationPublisher
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
from har2load.materialization import RequestMaterializer
from har2load.parameters import ParameterModel
from har2load.precision import timing_number
from har2load.presentation import (
    MAX_FRAGMENT_CHARACTERS,
    MAX_PAGE_ENTRIES,
    entry_overview,
    envelope,
    find_exchange,
    record,
    resolve_pointer,
    validate_range,
)
from har2load.reporting import ReportArtifact, ReportBuilder, ReportPublisher
from har2load.runtime_planning import GenerationPlanner
from har2load.runtime_types import runtime_document
from har2load.scenario import ChangeSet, ScenarioEditor
from har2load.serialization import canonical_bytes, metadata_object
from har2load.workspace import (
    MAX_CAPTURE_BYTES,
    PRIVACY_WARNING,
    WorkspaceSnapshot,
    WorkspaceStore,
    read_limited,
)


def initial_scenario(capture: Capture, grouping: Grouping) -> dict[str, JSONValue]:
    """Initial model; editing stays in the pure scenario domain."""
    return ScenarioEditor().initial(capture, grouping)


class Application:
    """Public application facade; no tokenizer, HTTP client, LLM or native runner."""

    def __init__(self) -> None:
        self.importer = HarImporter()
        self.grouper = StepGrouper()
        self.store = WorkspaceStore()
        self.editor = ScenarioEditor()

    def import_capture(
        self, source: Path, workspace: Path, step_gap_seconds: float = 5.0
    ) -> dict[str, JSONValue]:
        capture = self.importer.parse(read_limited(source, MAX_CAPTURE_BYTES))
        grouping = self.grouper.group(capture, step_gap_seconds)
        snapshot = self.store.create(
            workspace, capture.raw_bytes, initial_scenario(capture, grouping)
        )
        return envelope(
            "import", self._counts(capture, grouping, snapshot.scenario),
            workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            diagnostics=capture.diagnostics + grouping.diagnostics,
        )

    def _load(
        self, workspace: Path, revision: int | None = None,
    ) -> tuple[WorkspaceSnapshot, Capture, Grouping]:
        snapshot = self.store.load(workspace, revision)
        capture = self.importer.parse(snapshot.raw_bytes)
        threshold = timing_number(snapshot.scenario.get("step_gap_seconds"))
        if threshold is None or threshold < 0:
            raise Har2LoadError("SCENARIO_INVALID", "Invalid initial scenario settings.", 3)
        grouping = self.grouper.group(capture, float(threshold))
        legacy = snapshot.workspace_schema_version == "1.0"
        if legacy != (snapshot.scenario.get("schema_version") == "1.0"):
            raise Har2LoadError("SCENARIO_INVALID", "Scenario and workspace versions differ.", 3)
        self.editor.validate(snapshot.scenario, capture, grouping)
        initial = snapshot.initial_scenario
        if initial is None:
            raise Har2LoadError("SCENARIO_INVALID", "Initial scenario is unavailable.", 3)
        version = initial.get("schema_version")
        if not isinstance(version, str):
            raise Har2LoadError("SCENARIO_INVALID", "Initial scenario version is invalid.", 3)
        expected = self.editor.initial(capture, grouping, schema_version=version)
        if canonical_bytes(initial) != canonical_bytes(expected):
            raise Har2LoadError(
                "SCENARIO_AUDIT_MISMATCH", "Initial scenario differs from source.", 3
            )
        for event in snapshot.history:
            change = ChangeSet.parse(event["change"])
            expected = self.editor.apply(expected, capture, grouping, change)
        if canonical_bytes(snapshot.scenario) != canonical_bytes(expected):
            raise Har2LoadError(
                "SCENARIO_AUDIT_MISMATCH", "Scenario differs from its recorded changes.", 3
            )
        return snapshot, capture, grouping

    def _conflict_counts(
        self, capture: Capture, scenario: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        conflicts = self.editor.conflicts(scenario, capture)
        selected = sum(
            isinstance(item, dict) and item.get("status") == "selected" for item in conflicts
        )
        return {
            "unresolved_conflict_count": len(conflicts) - selected,
            "selected_conflict_count": selected,
        }

    def _counts(
        self, capture: Capture, grouping: Grouping, scenario: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        return {
            **self._conflict_counts(capture, scenario),
            **self._parameter_counts(scenario),
            "capture_id": capture.id,
            "capture_sha256": capture.sha256,
            "capture_bytes": len(capture.raw_bytes),
            "exchange_count": len(capture.exchanges),
            "group_count": len(grouping.groups),
            "step_gap_seconds": grouping.step_gap_seconds,
            "diagnostic_count": len(capture.diagnostics) + len(grouping.diagnostics),
            "redaction": "off",
            "contains_sensitive_data": True,
            "privacy_warning": PRIVACY_WARNING,
            "model_status": "draft",
            "targets": {target: "blocked" if status == "blocked" else "not_generated"
                        for target, status in self._target_contracts(scenario).items()},
            "native_validation": "not_run",
            "execution": "not_run",
        }

    def summary(
        self, workspace: Path, offset: int = 0, limit: int = 20, revision: int | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_PAGE_ENTRIES)
        snapshot, capture, grouping = self._load(workspace, revision)
        exchanges = {exchange.id: exchange for exchange in capture.exchanges}
        ids = grouping.ordered_exchange_ids[offset:offset + limit]
        continuation: JSONValue = None
        if offset + len(ids) < len(capture.exchanges):
            continuation = {
                "offset": offset + len(ids), "limit": limit, "revision": snapshot.scenario_revision,
            }
        return envelope(
            "summary",
            {**self._counts(capture, grouping, snapshot.scenario), "offset": offset,
             "latest_revision": snapshot.latest_revision,
             "entries": [entry_overview(exchanges[entity_id]) for entity_id in ids]},
            workspace_id=snapshot.workspace_id,
            scenario_revision=snapshot.scenario_revision,
            diagnostics=capture.diagnostics + grouping.diagnostics,
            continuation=continuation,
        )

    def inspect(
        self, workspace: Path, exchange_id: str | None = None, *, section: str = "entry",
        pointer: str = "", offset: int = 0, limit: int = MAX_FRAGMENT_CHARACTERS,
        revision: int | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_FRAGMENT_CHARACTERS)
        if len(pointer) > 256:
            raise Har2LoadError(
                "LOCATION_TOO_LONG",
                "Pointer exceeds 256 characters; inspect a shorter parent or the entry in chunks.",
            )
        snapshot, capture, grouping = self._load(workspace, revision)
        diagnostics = capture.diagnostics + grouping.diagnostics
        value: JSONValue
        metadata: dict[str, JSONValue] = {}
        text: str | None = None
        format_name = "json-text"
        if section in (
            "groups", "diagnostics", "scenario", "history", "conflicts",
            "parameters", "variables", "extractors", "bindings", "extraction-checks",
            "validation-diagnostics", "analysis", "candidates", "analysis-diagnostics",
            "target-extensions", "code-blocks", "helpers", "extension-plan",
            "materialized-requests", "generation-diagnostics", "runtime-plan",
        ):
            if exchange_id is not None:
                raise Har2LoadError("VIEW_ARGUMENT_CONFLICT", "This section is workspace-scoped.")
            if section == "groups":
                value = snapshot.scenario["groups"]
            elif section == "scenario":
                value = snapshot.scenario
            elif section == "history":
                value = [cast(JSONValue, item) for item in snapshot.history]
            elif section == "conflicts":
                value = self.editor.conflicts(snapshot.scenario, capture)
            elif section == "parameters":
                value = self._parameters(snapshot.scenario)
            elif section in ("variables", "extractors", "bindings"):
                value = self._parameters(snapshot.scenario)[section]
            elif section == "extraction-checks":
                value = self._extraction_checks(snapshot.scenario, capture)
            elif section == "validation-diagnostics":
                checks = self._extraction_checks(snapshot.scenario, capture)
                value = [record(item) for item in self._validation_findings(
                    snapshot.scenario, capture, cast(list[dict[str, JSONValue]], checks)
                )]
            elif section in ("analysis", "candidates", "analysis-diagnostics"):
                analysis = CandidateAnalyzer().analyze(capture, grouping, snapshot.scenario)
                value = analysis if section == "analysis" else analysis[
                    "candidates" if section == "candidates" else "diagnostics"
                ]
            elif section in ("target-extensions", "code-blocks", "helpers", "extension-plan"):
                extensions = self._extensions(snapshot.scenario)
                if section == "extension-plan":
                    value = TargetExtensions.plan(extensions, grouping)
                elif section == "target-extensions":
                    value = extensions
                else:
                    value = extensions["blocks" if section == "code-blocks" else "helpers"]
            elif section == "materialized-requests":
                plan = RequestMaterializer().build(capture, grouping, snapshot.scenario)
                value = {"complete": not any(d.severity == "error" for d in plan.diagnostics),
                         "diagnostics": [record(d) for d in plan.diagnostics],
                         "decisions": list(plan.decisions), "requests": [{
                             "id": item.id, "group_id": item.group_id, "method": item.method,
                             "url": item.url, "headers": [{"name": name, "value": value}
                                                           for name, value in item.headers],
                             "body": item.body.decode("utf-8") if item.body is not None else None,
                             "body_kind": item.body_kind,
                         } for item in plan.requests]}
            elif section == "generation-diagnostics":
                generated = self._generation_artifact(workspace, snapshot, capture, grouping,
                                                      ("jmeter", "loadrunner"))
                value = {target: [record(d) for d in generated.diagnostics if d.target == target]
                         for target in ("jmeter", "loadrunner")}
            elif section == "runtime-plan":
                plan = GenerationPlanner().build(capture, grouping, snapshot.scenario)
                metadata = {"contains_sensitive_data": True, "redaction": "off",
                            "privacy_warning": PRIVACY_WARNING}
                value = {"runtime": (runtime_document(plan.runtime)
                                     if plan.runtime is not None else None),
                         "diagnostics": [record(d) for d in plan.diagnostics],
                         "decisions": list(plan.decisions), "execution": "not_run"}
            else:
                value = [record(item) for item in diagnostics]
        else:
            exchange = find_exchange(capture, exchange_id)
            if section in ("request-body", "response-body"):
                if pointer:
                    raise Har2LoadError(
                        "VIEW_ARGUMENT_CONFLICT", "Body views do not accept a pointer."
                    )
                body = (
                    exchange.request_body if section == "request-body" else exchange.response_body
                )
                text = body.text
                format_name = "text"
                metadata = {
                    "availability": body.availability,
                    "view_status": body.view_status,
                    "representation": body.representation,
                    "mime_type": body.mime_type[:200] if body.mime_type else None,
                    "encoding": body.encoding[:80] if body.encoding else None,
                    "raw_location": "/request/postData" if section == "request-body"
                    else "/response/content",
                }
                # A missing decoded view is not an empty body; content remains null.
                value = None
            elif section == "entry":
                value = exchange.raw
            elif section in ("request", "response"):
                value = resolve_pointer(exchange.raw, f"/{section}")
            else:
                raise Har2LoadError("VIEW_SECTION_INVALID", "Unknown inspection section.")
        if format_name == "json-text":
            value = resolve_pointer(value, pointer)
            text = canonical_bytes(value).decode("utf-8")
        length = len(text) if text is not None else 0
        continuation: JSONValue = None
        if text is not None and offset + limit < length:
            continuation = {
                "section": section, "exchange_id": exchange_id,
                "pointer": pointer, "offset": offset + limit, "limit": limit,
                "revision": snapshot.scenario_revision,
            }
        return envelope(
            "inspect",
            {**metadata, "section": section, "exchange_id": exchange_id,
             "pointer": pointer, "format": format_name, "offset": offset,
             "total_characters": length,
             "content": text[offset:offset + limit] if text is not None else None,
             "capture_sha256": capture.sha256},
            workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            continuation=continuation,
        )

    @staticmethod
    def _read_change(source: Path) -> ChangeSet:
        raw = read_limited(source, 1024 * 1024)
        try:
            document = metadata_object(raw)
        except Har2LoadError:
            raise Har2LoadError(
                "CHANGE_INVALID_JSON", "Changeset must contain a strict JSON object.", 2
            ) from None
        return ChangeSet.parse(document)

    def changes_validate(self, workspace: Path, source: Path) -> dict[str, JSONValue]:
        change = self._read_change(source)
        snapshot, capture, grouping = self._load(workspace)
        applied = self.store.check_change(snapshot, change.document)
        preview = snapshot.scenario if applied is not None else self.editor.apply(
            snapshot.scenario, capture, grouping, change
        )
        return envelope(
            "changes.validate", {
                **self._conflict_counts(capture, preview), "change_id": change.change_id,
                **self._parameter_counts(preview),
                "base_revision": change.base_revision,
                "proposed_revision": (
                    applied if applied is not None else snapshot.scenario_revision + 1
                ),
                "replayed": applied is not None, "write_performed": False,
                "validation_scope": "changeset_and_supported_scenario_contract",
                "target_readiness": "not_evaluated",
            }, workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
        )

    def changes_apply(self, workspace: Path, source: Path) -> dict[str, JSONValue]:
        change = self._read_change(source)
        snapshot, capture, grouping = self._load(workspace)
        self.store.check_change(snapshot, change.document)
        result = self.store.transact(
            workspace, change.document,
            lambda current: self.editor.apply(current.scenario, capture, grouping, change),
        )
        return envelope(
            "changes.apply", {
                **self._conflict_counts(capture, result.snapshot.scenario),
                **self._parameter_counts(result.snapshot.scenario),
                "change_id": change.change_id, "applied_revision": result.applied_revision,
                "replayed": result.replayed, "write_performed": not result.replayed,
                "target_readiness": "not_evaluated",
            }, workspace_id=result.snapshot.workspace_id,
            scenario_revision=result.snapshot.scenario_revision,
        )

    def changes_history(
        self, workspace: Path, offset: int = 0, limit: int = 20, revision: int | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_PAGE_ENTRIES)
        snapshot, _, _ = self._load(workspace, revision)
        page = snapshot.history[offset:offset + limit]
        entries: list[JSONValue] = []
        for event in page:
            change = cast(dict[str, JSONValue], event["change"])
            entries.append({
                "revision": event["revision"], "change_id": change["change_id"],
                "change_sha256": event["change_sha256"], "created_at": event["created_at"],
                "operation_count": len(cast(list[JSONValue], change["operations"])),
            })
        continuation: JSONValue = None
        if offset + len(page) < len(snapshot.history):
            continuation = {
                "offset": offset + len(page), "limit": limit,
                "revision": snapshot.scenario_revision,
            }
        return envelope(
            "changes.history", {
                "entries": entries, "total_changes": len(snapshot.history), "offset": offset,
                "latest_revision": snapshot.latest_revision,
            }, workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            continuation=continuation,
        )

    @staticmethod
    def _parameters(scenario: dict[str, JSONValue]) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], scenario.get("parameters", ParameterModel.empty()))

    @staticmethod
    def _extensions(scenario: dict[str, JSONValue]) -> dict[str, JSONValue]:
        return cast(
            dict[str, JSONValue], scenario.get("target_extensions", TargetExtensions.empty())
        )

    def _extension_diagnostics(self, scenario: dict[str, JSONValue]) -> list[Diagnostic]:
        return TargetExtensions.diagnostics(self._extensions(scenario), self._parameters(scenario))

    def _target_contracts(self, scenario: dict[str, JSONValue]) -> dict[str, JSONValue]:
        diagnostics = self._extension_diagnostics(scenario)
        return {target: "blocked" if any(d.severity == "error" and d.target == target
                                         for d in diagnostics) else "valid"
                for target in ("jmeter", "loadrunner")}

    def _parameter_counts(self, scenario: dict[str, JSONValue]) -> dict[str, JSONValue]:
        parameters = self._parameters(scenario)
        counts: dict[str, JSONValue] = {
            kind[:-1] + "_count": len(cast(list[JSONValue], parameters[kind]))
            for kind in ("variables", "extractors", "bindings")
        }
        sources = [cast(dict[str, JSONValue], item["source"])["kind"] for item in
                   cast(list[dict[str, JSONValue]], parameters["variables"])]
        counts["external_input_count"] = sources.count("external")
        counts["generated_variable_count"] = sources.count("generated")
        counts["agent_code_variable_count"] = sources.count("agent_code")
        extensions = self._extensions(scenario)
        counts["code_block_count"] = len(cast(list[JSONValue], extensions["blocks"]))
        counts["helper_count"] = len(cast(list[JSONValue], extensions["helpers"]))
        counts["declared_dependency_count"] = sum(
            len(cast(list[JSONValue], item["dependencies"])) for key in ("blocks", "helpers")
            for item in cast(list[dict[str, JSONValue]], extensions[key])
        )
        return counts

    def _extraction_checks(
        self, scenario: dict[str, JSONValue], capture: Capture,
    ) -> list[JSONValue]:
        selections = cast(list[JSONValue], scenario.get("representation_selections", []))
        parameters = self._parameters(scenario)
        extractors = cast(list[dict[str, JSONValue]], parameters["extractors"])
        if not extractors:
            return []
        views = SourceViews(capture, selections)
        body_producers = {cast(str, item["exchange_id"]) for item in extractors
                          if item["area"] == "response_body"}
        results = ParameterModel.checks(parameters, views.for_extraction(body_producers))
        for item in cast(list[dict[str, JSONValue]], results):
            evidence = cast(dict[str, JSONValue], item["evidence"])
            item["source_view"] = "captured"
            if evidence["area"] == "response_body":
                try:
                    selected = views.selection(
                        cast(str, evidence["exchange_id"]), "/response/content/mimeType"
                    )
                    if selected is not None:
                        item["source_view"] = "scenario_selected"
                except Har2LoadError:
                    item["source_view"] = "unresolved"
        return results

    def _temporal_diagnostics(
        self, scenario: dict[str, JSONValue], capture: Capture,
    ) -> list[Diagnostic]:
        parameters = self._parameters(scenario)
        variables = {item["id"]: item for item in
                     cast(list[dict[str, JSONValue]], parameters["variables"])}
        exchanges = {item.id: item for item in capture.exchanges}
        diagnostics = []
        for binding in cast(list[dict[str, JSONValue]], parameters["bindings"]):
            variable = variables[binding["variable_id"]]
            if cast(dict[str, JSONValue], variable["source"])["kind"] != "extracted":
                continue
            producer_id = cast(dict[str, JSONValue], variable["initialize"])["exchange_id"]
            producer = exchanges[cast(str, producer_id)]
            consumer = exchanges[cast(str, binding["exchange_id"])]
            if (producer.end_time is None or consumer.start_time is None
                    or producer.end_time > consumer.start_time):
                diagnostics.append(Diagnostic(
                    "CAPTURE_DEPENDENCY_UNPROVEN", "warning",
                    "Recorded timing does not prove response availability despite scenario order.",
                    entity_id=cast(str, binding["id"]),
                ))
        return diagnostics

    def _validation_findings(
        self, scenario: dict[str, JSONValue], capture: Capture, checks: list[dict[str, JSONValue]],
    ) -> list[Diagnostic]:
        diagnostics = self._temporal_diagnostics(scenario, capture)
        for item in checks:
            if item["status"] == "matched":
                continue
            partial = item["status"] in ("unavailable", "limit_exceeded")
            diagnostics.append(Diagnostic(
                "CAPTURED_EXTRACTION_UNVERIFIED" if partial else "CAPTURED_EXTRACTION_FAILED",
                "warning" if partial else "error", "See recorded-response check status.",
                entity_id=cast(str, item["variable_id"] or item["extractor_id"]),
            ))
        conflict_counts = self._conflict_counts(capture, scenario)
        if conflict_counts["unresolved_conflict_count"]:
            diagnostics.append(Diagnostic(
                "SOURCE_REPRESENTATIONS_UNRESOLVED", "warning",
                "Original representation conflicts remain; target readiness is not established.",
            ))
        diagnostics.extend(self._extension_diagnostics(scenario))
        return diagnostics

    def validate(
        self, workspace: Path, offset: int = 0, limit: int = 20, revision: int | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_PAGE_ENTRIES)
        snapshot, capture, _ = self._load(workspace, revision)
        checks = cast(
            list[dict[str, JSONValue]], self._extraction_checks(snapshot.scenario, capture)
        )
        failures = sum(item["status"] not in ("matched", "unavailable", "limit_exceeded")
                       for item in checks)
        unverified = sum(item["status"] in ("unavailable", "limit_exceeded") for item in checks)
        diagnostics = self._validation_findings(snapshot.scenario, capture, checks)
        parameter_counts = self._parameter_counts(snapshot.scenario)
        target_contracts = self._target_contracts(snapshot.scenario)
        safe_checks: list[JSONValue] = [
            {key: item[key] for key in ("variable_id", "extractor_id", "status", "match_count",
                                       "failure_marker", "verification", "source_view",
                                       "check_kind")}
            for item in checks[offset:offset + limit]
        ]
        continuation: JSONValue = None
        if offset + len(safe_checks) < len(checks):
            continuation = {"offset": offset + len(safe_checks), "limit": limit,
                            "revision": snapshot.scenario_revision}
        return envelope(
            "validate", {
                **parameter_counts, **self._conflict_counts(capture, snapshot.scenario),
                "model_contract": "valid", "captured_failures": failures,
                "captured_unverified": unverified, "check_count": len(checks),
                "checked_extractor_count": len({item["extractor_id"] for item in checks}),
                "checks": safe_checks, "offset": offset,
                "validation_scope": "model_contract_and_recorded_extractors",
                "target_readiness": "not_evaluated", "native_validation": "not_run",
                "target_contracts": target_contracts,
                "code_semantics": (
                    "unverified" if parameter_counts["code_block_count"] or parameter_counts[
                        "helper_count"] else "not_applicable"
                ),
                "execution": "not_run", "runtime_inputs": (
                    "required_at_runtime" if (parameter_counts["external_input_count"]
                                              or parameter_counts["declared_dependency_count"])
                    else "not_evaluated"
                ),
            }, workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            diagnostics=tuple(diagnostics),
            error=failures > 0 or "blocked" in target_contracts.values(), continuation=continuation,
            diagnostics_section="validation-diagnostics",
        )

    def analyze(
        self, workspace: Path, offset: int = 0, limit: int = 20, revision: int | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_PAGE_ENTRIES)
        snapshot, capture, grouping = self._load(workspace, revision)
        analysis = CandidateAnalyzer().analyze(capture, grouping, snapshot.scenario)
        candidates = cast(list[dict[str, JSONValue]], analysis["candidates"])
        page: list[JSONValue] = [{
            "id": item["id"], "knowledge_status": item["knowledge_status"],
            "priority": item["priority"], "ambiguity": item["ambiguity"],
            "source_count": len(cast(list[JSONValue], item["sources"])),
            "consumer_count": len(cast(list[JSONValue], item["consumers"])),
        } for item in candidates[offset:offset + limit]]
        continuation: JSONValue = None
        if offset + len(page) < len(candidates):
            continuation = {"offset": offset + len(page), "limit": limit,
                            "revision": snapshot.scenario_revision}
        return envelope(
            "analyze", {"profile": analysis["profile"], "coverage": analysis["coverage"],
                        "limits": analysis["limits"], "candidate_count": len(candidates),
                        "candidates": page, "offset": offset, "model_changes_applied": False,
                        "diagnostic_count": len(cast(list[JSONValue], analysis["diagnostics"])),
                        "diagnostics_view": {"section": "analysis-diagnostics",
                                             "revision": snapshot.scenario_revision},
                        "values": "explicit_inspect_only"},
            workspace_id=snapshot.workspace_id, scenario_revision=snapshot.scenario_revision,
            continuation=continuation,
        )

    def report(
        self, workspace: Path, *, format: str = "json", offset: int = 0,
        limit: int = MAX_FRAGMENT_CHARACTERS, revision: int | None = None,
        compare_revision: int | None = None, output: Path | None = None,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_FRAGMENT_CHARACTERS)
        if format not in ("json", "markdown"):
            raise Har2LoadError("REPORT_FORMAT_INVALID", "Unknown report format.")
        if output is not None and (offset != 0 or limit != MAX_FRAGMENT_CHARACTERS
                                   or format != "json"):
            raise Har2LoadError("REPORT_ARGUMENT_CONFLICT", "Export cannot use fragment options.")
        snapshot, capture, grouping = self._load(workspace, revision)
        baseline_revision = compare_revision
        if baseline_revision is None and snapshot.scenario_revision > 1:
            baseline_revision = snapshot.scenario_revision - 1
        if baseline_revision is not None and (
            type(baseline_revision) is not int
            or not 1 <= baseline_revision < snapshot.scenario_revision
        ):
            raise Har2LoadError("REPORT_BASELINE_INVALID", "Baseline must be an earlier revision.")
        baseline = (
            self._load(workspace, baseline_revision)[0] if baseline_revision is not None else None
        )
        artifact = self._report_artifact(snapshot, capture, grouping, baseline)
        metadata: dict[str, JSONValue] = {
            "report_sha256": artifact.digest, "compare_revision": baseline_revision,
            "redaction": "off", "contains_sensitive_data": True,
            "privacy_warning": PRIVACY_WARNING, "review": "not_recorded",
            "statuses": artifact.document["statuses"],
        }
        continuation: JSONValue = None
        if output is not None:
            metadata.update(ReportPublisher().publish(artifact, output))
            metadata["write_performed"] = True
        else:
            text = (artifact.json_bytes if format == "json" else artifact.markdown_bytes).decode()
            metadata.update({"content": text[offset:offset + limit], "format": format,
                             "offset": offset, "total_characters": len(text),
                             "write_performed": False})
            if offset + limit < len(text):
                continuation = {"format": format, "offset": offset + limit, "limit": limit,
                                "revision": snapshot.scenario_revision,
                                "compare_revision": baseline_revision}
        return envelope(
            "report", metadata, workspace_id=snapshot.workspace_id,
            scenario_revision=snapshot.scenario_revision, continuation=continuation,
        )

    def _report_artifact(
        self, snapshot: WorkspaceSnapshot, capture: Capture, grouping: Grouping,
        baseline: WorkspaceSnapshot | None,
    ) -> ReportArtifact:
        checks = self._extraction_checks(snapshot.scenario, capture)
        diagnostics = list(capture.diagnostics + grouping.diagnostics) + self._validation_findings(
            snapshot.scenario, capture, cast(list[dict[str, JSONValue]], checks)
        )
        return ReportBuilder().build(
            snapshot=snapshot, capture=capture, grouping=grouping, baseline=baseline,
            checks=checks, diagnostics=[record(item) for item in diagnostics],
            analysis=CandidateAnalyzer().analyze(capture, grouping, snapshot.scenario),
            conflicts=self.editor.conflicts(snapshot.scenario, capture),
        )

    def generate(
        self, workspace: Path, *, output: Path | None = None, revision: int | None = None,
        targets: tuple[str, ...] = ("jmeter", "loadrunner"),
        initial_cookies: tuple[str, ...] = (),
    ) -> dict[str, JSONValue]:
        snapshot, capture, grouping = self._load(workspace, revision)
        artifact = self._generation_artifact(
            workspace, snapshot, capture, grouping, targets, initial_cookies,
        )
        if output is not None:
            GenerationPublisher().publish(artifact, output)
        result = envelope(
            "generate", {**artifact.manifest, "write_performed": output is not None,
                         "diagnostic_count": len(artifact.diagnostics),
                         "diagnostics_complete_in": "report.json:generation.diagnostics",
                         "privacy_warning": PRIVACY_WARNING}, workspace_id=snapshot.workspace_id,
            scenario_revision=snapshot.scenario_revision, error=artifact.blocked,
            diagnostics=artifact.diagnostics, diagnostics_section="generation-diagnostics",
        )
        if len(artifact.diagnostics) > 10:
            data = cast(dict[str, JSONValue], result["data"])
            if initial_cookies:
                # inspect recomputes default options, not this flag-specific artifact set.
                data["diagnostics_continuation"] = None
                data["diagnostics_complete_in"] = (
                    "report.json:generation.diagnostics; for --check, repeat with the same "
                    "options and --output NEW_DIRECTORY")
            else:
                continuation = cast(dict[str, JSONValue], data["diagnostics_continuation"])
                continuation["pointer"] = "/" + targets[0] if len(targets) == 1 else ""
        return result

    def _generation_artifact(
        self, workspace: Path, snapshot: WorkspaceSnapshot, capture: Capture, grouping: Grouping,
        targets: tuple[str, ...],
        initial_cookies: tuple[str, ...] = (),
    ) -> GenerationArtifact:
        baseline = (self._load(workspace, snapshot.scenario_revision - 1)[0]
                    if snapshot.scenario_revision > 1 else None)
        return GenerationBuilder().build(
            snapshot, capture, grouping,
            self._report_artifact(snapshot, capture, grouping, baseline), targets, initial_cookies,
        )


def error_response(operation: str, error: Har2LoadError) -> dict[str, JSONValue]:
    return envelope(
        operation, {}, diagnostics=(Diagnostic(error.code, "error", str(error)),), error=True,
        workspace_id=error.workspace_id, scenario_revision=error.scenario_revision,
    )
