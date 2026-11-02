"""Small, explicit contracts shared by import, grouping, storage and presentation.

Raw JSON is retained independently of interpreted views. Frozen records prevent
attribute reassignment, not mutation of nested raw JSON; consumers must treat raw
values as read-only. Original bytes, rather than reserialized JSON, are archival.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

type JSONValue = None | bool | int | float | Decimal | str | list[JSONValue] | dict[str, JSONValue]
type Availability = Literal["present", "empty", "unavailable", "invalid"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    entity_id: str | None = None
    location: str | None = None
    target: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class Body:
    availability: Availability
    text: str | None = None
    mime_type: str | None = None
    encoding: str | None = None
    representation: Literal["text", "params", "none"] = "none"
    diagnostics: tuple[Diagnostic, ...] = ()
    view_status: Literal[
        "available", "unavailable", "unsupported", "nontext", "invalid", "limit_exceeded"
    ] = (
        "unavailable"
    )


@dataclass(frozen=True)
class Exchange:
    id: str
    index: int
    raw: JSONValue
    request_body: Body = field(default_factory=lambda: Body("unavailable"))
    response_body: Body = field(default_factory=lambda: Body("unavailable"))
    start_time: Decimal | float | None = None
    end_time: Decimal | float | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Capture:
    id: str
    sha256: str
    raw_bytes: bytes
    exchanges: tuple[Exchange, ...]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class StepGroup:
    id: str
    exchange_ids: tuple[str, ...]
    name: str
    origin: Literal["annotation", "timing_heuristic"]
    boundary_reason: str


@dataclass(frozen=True)
class Grouping:
    groups: tuple[StepGroup, ...]
    ordered_exchange_ids: tuple[str, ...]
    step_gap_seconds: float
    diagnostics: tuple[Diagnostic, ...] = ()


class Har2LoadError(Exception):
    """Expected application failure with a safe, data-independent message."""

    def __init__(
        self, code: str, message: str, exit_code: int = 2, *,
        workspace_id: str | None = None, scenario_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        # Safe snapshot context, captured at rejection rather than reread later.
        self.workspace_id = workspace_id
        self.scenario_revision = scenario_revision
