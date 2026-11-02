"""Offline use cases joining pure interpretation to private storage."""

from pathlib import Path
from typing import cast

from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.grouping import StepGrouper
from har2load.importer import HarImporter
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
        expected = snapshot.scenario if legacy else self.editor.initial(capture, grouping)
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
            "targets": {"jmeter": "not_generated", "loadrunner": "not_generated"},
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
        if section in ("groups", "diagnostics", "scenario", "history", "conflicts"):
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


def error_response(operation: str, error: Har2LoadError) -> dict[str, JSONValue]:
    return envelope(
        operation, {}, diagnostics=(Diagnostic(error.code, "error", str(error)),), error=True,
        workspace_id=error.workspace_id, scenario_revision=error.scenario_revision,
    )
