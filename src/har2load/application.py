"""Offline use cases joining pure interpretation to private storage."""

from pathlib import Path

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
from har2load.serialization import canonical_bytes
from har2load.workspace import (
    MAX_CAPTURE_BYTES,
    PRIVACY_WARNING,
    WorkspaceSnapshot,
    WorkspaceStore,
    read_limited,
)


def initial_scenario(capture: Capture, grouping: Grouping) -> dict[str, JSONValue]:
    """Versioned initial scaffold, not yet a variable/binding scenario editor."""
    return {
        "schema_version": "1.0",
        "revision": 1,
        "source_capture": capture.id,
        "step_gap_seconds": grouping.step_gap_seconds,
        "groups": [{
            "id": group.id, "exchange_ids": list(group.exchange_ids), "name": group.name,
            "origin": group.origin, "boundary_reason": group.boundary_reason,
        } for group in grouping.groups],
        "ordered_exchange_ids": list(grouping.ordered_exchange_ids),
        "status": "draft",
        "targets": {"jmeter": "not_generated", "loadrunner": "not_generated"},
        "native_validation": "not_run",
        "execution": "not_run",
    }


class Application:
    """Public application facade; no tokenizer, HTTP client, LLM or native runner."""

    def __init__(self) -> None:
        self.importer = HarImporter()
        self.grouper = StepGrouper()
        self.store = WorkspaceStore()

    def import_capture(
        self, source: Path, workspace: Path, step_gap_seconds: float = 5.0
    ) -> dict[str, JSONValue]:
        capture = self.importer.parse(read_limited(source, MAX_CAPTURE_BYTES))
        grouping = self.grouper.group(capture, step_gap_seconds)
        snapshot = self.store.create(
            workspace, capture.raw_bytes, initial_scenario(capture, grouping)
        )
        return envelope(
            "import", self._counts(capture, grouping), workspace_id=snapshot.workspace_id,
            diagnostics=capture.diagnostics + grouping.diagnostics,
        )

    def _load(self, workspace: Path) -> tuple[WorkspaceSnapshot, Capture, Grouping]:
        snapshot = self.store.load(workspace)
        capture = self.importer.parse(snapshot.raw_bytes)
        threshold = timing_number(snapshot.scenario.get("step_gap_seconds"))
        if threshold is None or threshold < 0:
            raise Har2LoadError("SCENARIO_INVALID", "Invalid initial scenario settings.", 3)
        grouping = self.grouper.group(capture, float(threshold))
        # This release supports only immutable initial scenarios. Arbitrary manual
        # edits must not silently be ignored or labelled as validated scenarios.
        expected = canonical_bytes(initial_scenario(capture, grouping))
        if canonical_bytes(snapshot.scenario) != expected:
            raise Har2LoadError(
                "SCENARIO_INVALID", "Initial scenario differs from its source and settings.", 3
            )
        return snapshot, capture, grouping

    @staticmethod
    def _counts(capture: Capture, grouping: Grouping) -> dict[str, JSONValue]:
        return {
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

    def summary(self, workspace: Path, offset: int = 0, limit: int = 20) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_PAGE_ENTRIES)
        snapshot, capture, grouping = self._load(workspace)
        exchanges = {exchange.id: exchange for exchange in capture.exchanges}
        ids = grouping.ordered_exchange_ids[offset:offset + limit]
        continuation: JSONValue = None
        if offset + len(ids) < len(capture.exchanges):
            continuation = {"offset": offset + len(ids), "limit": limit}
        return envelope(
            "summary",
            {**self._counts(capture, grouping), "offset": offset,
             "entries": [entry_overview(exchanges[entity_id]) for entity_id in ids]},
            workspace_id=snapshot.workspace_id,
            diagnostics=capture.diagnostics + grouping.diagnostics,
            continuation=continuation,
        )

    def inspect(
        self, workspace: Path, exchange_id: str | None = None, *, section: str = "entry",
        pointer: str = "", offset: int = 0, limit: int = MAX_FRAGMENT_CHARACTERS,
    ) -> dict[str, JSONValue]:
        validate_range(offset, limit, MAX_FRAGMENT_CHARACTERS)
        if len(pointer) > 256:
            raise Har2LoadError(
                "LOCATION_TOO_LONG",
                "Pointer exceeds 256 characters; inspect a shorter parent or the entry in chunks.",
            )
        snapshot, capture, grouping = self._load(workspace)
        diagnostics = capture.diagnostics + grouping.diagnostics
        value: JSONValue
        metadata: dict[str, JSONValue] = {}
        text: str | None = None
        format_name = "json-text"
        if section in ("groups", "diagnostics"):
            if exchange_id is not None:
                raise Har2LoadError("VIEW_ARGUMENT_CONFLICT", "This section is workspace-scoped.")
            value = (
                snapshot.scenario["groups"] if section == "groups"
                else [record(item) for item in diagnostics]
            )
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
            }
        return envelope(
            "inspect",
            {**metadata, "section": section, "exchange_id": exchange_id,
             "pointer": pointer, "format": format_name, "offset": offset,
             "total_characters": length,
             "content": text[offset:offset + limit] if text is not None else None,
             "capture_sha256": capture.sha256},
            workspace_id=snapshot.workspace_id, continuation=continuation,
        )


def error_response(operation: str, error: Har2LoadError) -> dict[str, JSONValue]:
    return envelope(
        operation, {}, diagnostics=(Diagnostic(error.code, "error", str(error)),), error=True
    )
