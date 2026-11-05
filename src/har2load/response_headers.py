"""Shared capabilities for bounded native-exposed header fields, not a wire parser."""

import re
from typing import cast

from har2load.domain import Diagnostic, JSONValue
from har2load.runtime_types import RuntimePlan

RESPONSE_HEADER_POLICY = "native_response_headers_v1"
MAX_RESPONSE_HEADER_FIELDS = 1000
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")


def header_diagnostics(plan: RuntimePlan, target: str) -> list[Diagnostic]:
    """Do not narrow the historical HAR model; reject only unsupported native lowering."""
    errors = []
    for extractor in plan.extractors:
        if extractor["area"] != "response_headers":
            continue
        selector = cast(dict[str, JSONValue], extractor["selector"])
        name = selector.get("name")
        if (selector.get("dialect") != "name_v1" or not isinstance(name, str)
                or _TOKEN.fullmatch(name) is None):
            errors.append(Diagnostic("RUNTIME_RESPONSE_HEADER_SELECTOR_UNSUPPORTED", "error",
                "Native response header selection requires an ASCII token name.",
                entity_id=cast(str, extractor["id"]), target=target))
    return errors
