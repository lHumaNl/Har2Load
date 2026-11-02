"""Pure request-plan and target-artifact contracts; never a runner or native loader."""

from dataclasses import dataclass, field

from har2load.domain import Diagnostic, JSONValue

GENERATION_PROFILE = "static_literals_v1"
MAX_GENERATED_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class NativeRequest:
    id: str
    group_id: str
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    body_kind: str = "none"


@dataclass(frozen=True)
class NativeGroup:
    id: str
    name: str
    exchange_ids: tuple[str, ...]


@dataclass(frozen=True)
class RequestPlan:
    requests: tuple[NativeRequest, ...]
    groups: tuple[NativeGroup, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
    decisions: tuple[JSONValue, ...] = ()


@dataclass(frozen=True)
class TargetBuild:
    target: str
    files: dict[str, bytes] = field(default_factory=dict)
    source_map: tuple[dict[str, JSONValue], ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "blocked" if any(d.severity == "error" for d in self.diagnostics) else "generated"
