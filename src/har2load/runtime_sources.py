"""Pure native-input/generator capabilities, not live input resolution or generation."""

from typing import cast

from har2load.domain import Diagnostic, JSONValue
from har2load.runtime_types import RuntimePlan

MIN_GENERATED_INTEGER = -(2**63)
MAX_GENERATED_INTEGER = 2**63 - 1
GENERATORS = frozenset({"integer", "string", "uuid", "utc_datetime", "utc_date_range"})
INPUT_POLICY = "native_initial_snapshot_v1"
GENERATOR_POLICY = "per_user_generated_scalars_v1"


def initial_values(plan: RuntimePlan) -> dict[str, str]:
    """Return only explicitly supplied native strings; capability checks reject conflicts."""
    result: dict[str, str] = {}
    for variable in plan.variables:
        source = cast(dict[str, JSONValue], variable["source"])
        if source["kind"] == "external" and "initial_value" in source:
            result.setdefault(cast(str, source["key"]), cast(str, source["initial_value"]))
    return dict(sorted(result.items()))


def source_diagnostics(plan: RuntimePlan, target: str) -> list[Diagnostic]:
    """Value-free diagnostics for the shared bounded generation profile."""
    errors: list[Diagnostic] = []
    supplied = initial_values(plan)
    for variable in plan.variables:
        source = cast(dict[str, JSONValue], variable["source"])
        code: str | None = None
        if source["kind"] == "external" and "initial_value" in source:
            if source["initial_value"] != supplied[cast(str, source["key"])]:
                code = "RUNTIME_INPUT_INITIAL_CONFLICT"
        elif source["kind"] == "generated":
            generator = source.get("generator")
            if not isinstance(generator, str) or generator not in GENERATORS:
                code = "RUNTIME_GENERATOR_UNSUPPORTED"
            elif source["generator"] == "integer":
                low, high = source.get("minimum"), source.get("maximum")
                if not (type(low) is int and type(high) is int
                        and MIN_GENERATED_INTEGER <= low <= high <= MAX_GENERATED_INTEGER):
                    code = "RUNTIME_GENERATOR_RANGE_UNSUPPORTED"
        if code is not None:
            errors.append(Diagnostic(code, "error",
                "Declaration is outside the bounded native input/generator profile.",
                entity_id=cast(str, variable["id"]), target=target))
    return errors
