"""Bounded, explicit views. These are not redacted or safe-handoff snapshots."""

import re
from dataclasses import asdict
from typing import cast

from har2load.domain import Capture, Diagnostic, Exchange, Har2LoadError, JSONValue

MAX_FRAGMENT_CHARACTERS = 4096
MAX_PAGE_ENTRIES = 100


def record(value: Diagnostic) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], asdict(value))


def validate_range(offset: int, limit: int, maximum: int) -> None:
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= maximum:
        raise Har2LoadError("VIEW_RANGE_INVALID", "Invalid view offset or limit.")


def resolve_pointer(value: JSONValue, pointer: str) -> JSONValue:
    if not pointer:
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise Har2LoadError("LOCATION_INVALID", "Expected an RFC 6901 JSON pointer.")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            if len(token) > 10 or int(token) >= len(value):
                raise Har2LoadError("LOCATION_NOT_FOUND", "Selected location does not exist.")
            value = value[int(token)]
        else:
            raise Har2LoadError("LOCATION_NOT_FOUND", "Selected location does not exist.")
    return value


def find_exchange(capture: Capture, exchange_id: str | None) -> Exchange:
    for exchange in capture.exchanges:
        if exchange.id == exchange_id:
            return exchange
    raise Har2LoadError("EXCHANGE_NOT_FOUND", "Select an exchange ID from summary.")


def entry_overview(exchange: Exchange) -> JSONValue:
    raw = exchange.raw if isinstance(exchange.raw, dict) else {}
    request = raw.get("request")
    response = raw.get("response")
    method = request.get("method") if isinstance(request, dict) else None
    status = response.get("status") if isinstance(response, dict) else None
    return {
        "id": exchange.id,
        "source_index": exchange.index,
        "method": method[:16] if isinstance(method, str) else None,
        "status": status if type(status) is int and 0 <= status <= 999 else None,
        "request_body": exchange.request_body.availability,
        "response_body": exchange.response_body.availability,
        "request_body_view": exchange.request_body.view_status,
        "response_body_view": exchange.response_body.view_status,
        "diagnostic_count": (
            len(exchange.diagnostics)
            + len(exchange.request_body.diagnostics)
            + len(exchange.response_body.diagnostics)
        ),
    }


def envelope(
    operation: str,
    data: dict[str, JSONValue],
    *,
    workspace_id: str | None = None,
    scenario_revision: int | None = 1,
    diagnostics_section: str = "diagnostics",
    diagnostics: tuple[Diagnostic, ...] = (),
    continuation: JSONValue = None,
    error: bool = False,
) -> dict[str, JSONValue]:
    if len(diagnostics) > 10:
        data = {
            **data,
            "diagnostics_truncated": True,
            "diagnostics_continuation": {
                "section": diagnostics_section, "offset": 0, "revision": scenario_revision,
            },
        }
    return {
        "schema_version": "1.0",
        "operation": operation,
        "workspace_id": workspace_id,
        "scenario_revision": scenario_revision if workspace_id else None,
        "status": "error" if error else ("completed_with_diagnostics" if diagnostics else "ok"),
        "data": data,
        "diagnostics": [record(item) for item in diagnostics[:10]],
        "truncated": continuation is not None or len(diagnostics) > 10,
        "continuation": continuation,
    }
