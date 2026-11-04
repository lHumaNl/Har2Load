"""Data-only scalar runtime generation contracts, never an interpreter or runner."""

from copy import deepcopy
from dataclasses import dataclass

from har2load.domain import JSONValue

RUNTIME_PROFILE = "scalar_runtime_v1"
EXTENSION_RUNTIME_PROFILE = "authored_scalar_runtime_v1"
EXTENSION_ABI = "scalar_body_v1"
RUNTIME_STATUS_POLICY = "markers_only_native_status_v1"
MAX_RUNTIME_VALUE_BYTES = 65536
MAX_RUNTIME_FIELD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RuntimeSlot:
    binding_id: str
    variable_id: str
    encoding: str  # text | header | path | form | json


@dataclass(frozen=True)
class RuntimeTemplate:
    parts: tuple[str | RuntimeSlot, ...]


@dataclass(frozen=True)
class RuntimeRequest:
    id: str
    url: RuntimeTemplate
    headers: tuple[tuple[str, RuntimeTemplate], ...]
    body: RuntimeTemplate | None


@dataclass(frozen=True)
class RuntimePlan:
    variables: tuple[dict[str, JSONValue], ...]
    extractors: tuple[dict[str, JSONValue], ...]
    requests: tuple[RuntimeRequest, ...]
    blocks: tuple[dict[str, JSONValue], ...] = ()
    helpers: tuple[dict[str, JSONValue], ...] = ()
    extension_namespace: str = ""


def runtime_document(plan: RuntimePlan) -> dict[str, JSONValue]:
    """Explicit private inspection view; tuples become JSON arrays, decimals stay exact."""
    def template(value: RuntimeTemplate) -> list[JSONValue]:
        result: list[JSONValue] = []
        for part in value.parts:
            if isinstance(part, str):
                result.append(part)
            else:
                result.append({"binding_id": part.binding_id, "variable_id": part.variable_id,
                               "encoding": part.encoding})
        return result

    return {"variables": [deepcopy(v) for v in plan.variables],
            "blocks": [deepcopy(b) for b in plan.blocks],
            "helpers": [deepcopy(h) for h in plan.helpers],
            "extension_namespace": plan.extension_namespace,
            "native_status_policy": RUNTIME_STATUS_POLICY,
            "extractors": [deepcopy(e) for e in plan.extractors],
            "requests": [{"id": r.id, "url": template(r.url),
                          "headers": [{"name": name, "value": template(value)}
                                      for name, value in r.headers],
                          "body": template(r.body) if r.body is not None else None}
                         for r in plan.requests]}
