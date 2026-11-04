"""Target-neutral ABI capability checks; source remains opaque, never executed."""

from typing import cast

from har2load.domain import Diagnostic, JSONValue
from har2load.runtime_types import EXTENSION_ABI, RuntimePlan


def declarations(plan: RuntimePlan, target: str, kind: str) -> tuple[dict[str, JSONValue], ...]:
    records = plan.blocks if kind == "blocks" else plan.helpers
    return tuple(record for record in records if record["target"] == target)


def capability_diagnostics(plan: RuntimePlan, target: str) -> list[Diagnostic]:
    """Check explicit opt-in and producer coverage, without guessing source semantics."""
    diagnostics: list[Diagnostic] = []

    def error(code: str, entity: str, message: str) -> None:
        diagnostics.append(Diagnostic(code, "error", message, entity_id=entity, target=target))

    blocks, helpers = (declarations(plan, target, kind) for kind in ("blocks", "helpers"))
    for record in (*blocks, *helpers):
        identifier = cast(str, record["id"])
        if record.get("runtime_abi") != EXTENSION_ABI:
            error("GENERATION_EXTENSION_ABI_REQUIRED", identifier,
                  "Authored runtime generation requires an explicitly supported ABI.")
        if record["dependencies"]:
            error("GENERATION_EXTENSION_DEPENDENCY_UNSUPPORTED", identifier,
                  "Declared runtime dependencies are not bundled or resolved by this profile.")
    for variable in plan.variables:
        source = cast(dict[str, JSONValue], variable["source"])
        if source["kind"] == "agent_code" and not any(
            b["behavior_id"] == source["behavior_id"]
            and variable["id"] in cast(list[JSONValue], b["outputs"])
            and b.get("runtime_abi") == EXTENSION_ABI for b in blocks
        ):
            error("GENERATION_EXTENSION_PRODUCER_UNSUPPORTED", cast(str, variable["id"]),
                  "Agent-code variable lacks an opted-in producer for this target.")
    return diagnostics
