"""Read-only selected source views and exact request destination validation.

These projections validate addresses and types; they are NOT emitted HTTP requests
or runtime substitutions. Capture bytes/raw entries are never modified.
"""

import math
import re
from dataclasses import replace
from decimal import Decimal
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from har2load.domain import Body, Capture, Exchange, Har2LoadError, JSONValue
from har2load.importer import HarImporter
from har2load.media import mime_signature
from har2load.presentation import resolve_pointer
from har2load.serialization import canonical_bytes
from har2load.values import MAX_TEXT_BYTES, check_text, matches_type, parse_json_text


def _bad(code: str = "BINDING_TARGET_INVALID") -> Har2LoadError:
    return Har2LoadError(code, "Binding destination is unavailable, incompatible or ambiguous.", 3)


def _object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise _bad()
    return value


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _bad()
    return cast(list[dict[str, JSONValue]], value)


def _pairs(value: JSONValue) -> list[tuple[str, str]]:
    result = []
    size = 0
    for item in _objects(value):
        if not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
            raise _bad()
        name, text = cast(str, item["name"]), cast(str, item["value"])
        size += max(1, _bounded(name) + _bounded(text))
        if size > MAX_TEXT_BYTES:
            raise _bad("BINDING_VIEW_LIMIT")
        result.append((name, text))
    return result


def _form(text: str) -> list[tuple[str, str]]:
    _bounded(text)
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise _bad()
    try:
        return parse_qsl(text, keep_blank_values=True, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise _bad() from None


def _bounded(text: str) -> int:
    try:
        return check_text(text)
    except Har2LoadError as error:
        raise _bad(
            "BINDING_VIEW_LIMIT" if error.code == "JSON_LIMIT_EXCEEDED"
            else "BINDING_TARGET_INVALID"
        ) from None


def _body_metadata(
    data: dict[str, JSONValue], representation: JSONValue, mime_override: str | None,
) -> dict[str, JSONValue]:
    """Copy only known scalar decoder inputs, never arbitrary nested extensions."""
    result: dict[str, JSONValue] = {}
    size = 0
    for key in ("mimeType", "encoding", "text", "comment"):
        if key == "mimeType" and mime_override is not None:
            size += _bounded(mime_override)
            result[key] = mime_override
            continue
        if key not in data or key == "text" and representation == "params":
            continue
        value = data[key]
        if not isinstance(value, str):
            raise _bad()
        size += _bounded(value)
        result[key] = value
    for key in ("size", "compression"):
        if key in data:
            value = data[key]
            if (type(value) not in (int, float, Decimal)
                    or not math.isfinite(cast(float, value))):
                raise _bad()
            result[key] = value
    if "params" in data and representation != "text":
        parameters: list[JSONValue] = []
        for item in _objects(data["params"]):
            parameter: dict[str, JSONValue] = {}
            for key in ("name", "value", "fileName", "contentType", "comment"):
                if key not in item:
                    continue
                value = item[key]
                if not isinstance(value, str):
                    raise _bad()
                size += max(1, _bounded(value))
                parameter[key] = value
            parameters.append(parameter)
            size += 1
            if size > MAX_TEXT_BYTES:
                raise _bad("BINDING_VIEW_LIMIT")
        result["params"] = parameters
    if size > MAX_TEXT_BYTES:
        raise _bad("BINDING_VIEW_LIMIT")
    return result


def _content_headers(section: dict[str, JSONValue]) -> list[JSONValue]:
    result: list[JSONValue] = []
    size = 0
    headers = section.get("headers", [])
    if not isinstance(headers, list):
        raise _bad()
    for item in headers:
        # Other headers and their vendor extensions cannot affect body decoding.
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        if cast(str, item["name"]).lower() != "content-type":
            continue
        value = item.get("value")
        if not isinstance(value, str):
            raise _bad()
        size += max(1, _bounded(value))
        if size > MAX_TEXT_BYTES:
            raise _bad("BINDING_VIEW_LIMIT")
        result.append({"name": "Content-Type", "value": value})
    return result


class SourceViews:
    """Per-validation cached projections of explicitly selected representations."""

    def __init__(self, capture: Capture, selections: list[JSONValue]) -> None:
        self.capture = capture
        self.exchanges = {exchange.id: exchange for exchange in capture.exchanges}
        self.selections = {
            (item["exchange_id"], item["location"]): item
            for item in _objects(selections)
        }
        self.conflicts = {
            (item.entity_id, item.location) for item in capture.diagnostics
            if item.code == "HAR_REPRESENTATION_CONFLICT"
        }
        self._bodies: dict[tuple[str, str], Body] = {}

    def exchange(self, entity_id: str) -> Exchange:
        if entity_id not in self.exchanges:
            raise _bad()
        return self.exchanges[entity_id]

    def selection(self, entity_id: str, location: str) -> dict[str, JSONValue] | None:
        result = self.selections.get((entity_id, location))
        if result is None and (entity_id, location) in self.conflicts:
            raise _bad("BINDING_REPRESENTATION_CONFLICT")
        return result

    def body(self, entity_id: str, side: str) -> Body:
        key = entity_id, side
        if key in self._bodies:
            return self._bodies[key]
        exchange = self.exchange(entity_id)
        field = "postData" if side == "request" else "content"
        parent = f"/{side}/{field}"
        choice = self.selection(entity_id, parent + "/params")
        mime = self.selection(entity_id, parent + "/mimeType")
        original = exchange.request_body if side == "request" else exchange.response_body
        if choice is None and mime is None:
            self._bodies[key] = original
            return original
        source_section = _object(_object(exchange.raw).get(side))
        source_data = _object(source_section.get(field))
        mime_override: str | None = None
        if mime is not None:
            selected = source_data.get("mimeType")
            if mime["representation"] == "header":
                headers = source_section.get("headers", [])
                index = cast(int, mime["header_index"])
                if not isinstance(headers, list) or not 0 <= index < len(headers):
                    raise _bad()
                selected = _object(headers[index]).get("value")
            if not isinstance(selected, str):
                raise _bad()
            _bounded(selected)
            mime_override = selected
            projected_headers: list[JSONValue] = [{"name": "Content-Type", "value": selected}]
        else:
            projected_headers = _content_headers(source_section)
        try:
            data = _body_metadata(
                source_data, choice["representation"] if choice else None, mime_override
            )
        except (RecursionError, OverflowError):
            raise _bad("BINDING_VIEW_LIMIT") from None
        # Minimal decoder envelope, not a reconstructed/emitted HTTP request.
        section: dict[str, JSONValue] = {field: data, "headers": projected_headers}
        raw: dict[str, JSONValue] = {
            "request": {"method": "GET", "url": "https://har2load.invalid/"},
            "response": {"status": 0}, side: section,
        }
        try:
            payload = canonical_bytes({"log": {"entries": [raw]}})
            projected = HarImporter().parse(payload).exchanges[0]
        except (RecursionError, OverflowError):
            raise _bad("BINDING_VIEW_LIMIT") from None
        result = projected.request_body if side == "request" else projected.response_body
        self._bodies[key] = result
        return result

    def media(self, entity_id: str) -> str:
        body = self.body(entity_id, "request")
        mime = body.mime_type
        if not mime:
            request = _object(_object(self.exchange(entity_id).raw).get("request"))
            candidates = [value for name, value in _pairs(request.get("headers", []))
                          if name.lower() == "content-type"]
            mime = candidates[0] if candidates else ""
        signature = mime_signature(mime)
        if signature is None or any(
            name == "charset" and value not in {"utf-8", "utf8"} for name, value in signature[1]
        ):
            raise _bad("BINDING_CAPABILITY_UNSUPPORTED")
        return signature[0]

    def for_extraction(self, body_producers: set[str] | None = None) -> Capture:
        if body_producers == set():
            return self.capture
        derived = []
        for exchange in self.capture.exchanges:
            if body_producers is not None and exchange.id not in body_producers:
                derived.append(exchange)
                continue
            try:
                body = self.body(exchange.id, "response")
            except Har2LoadError as error:
                if error.code == "BINDING_VIEW_LIMIT":
                    body = replace(exchange.response_body, text=None, view_status="limit_exceeded")
                elif error.code == "BINDING_REPRESENTATION_CONFLICT":
                    body = Body("unavailable")
                else:
                    body = Body("invalid", view_status="invalid")
            derived.append(replace(exchange, response_body=body))
        return replace(self.capture, exchanges=tuple(derived))


class BindingLocations:
    """Validate locations on the selected source representation, without mutation."""

    def validate(
        self, parameters: dict[str, JSONValue], capture: Capture, selections: list[JSONValue],
    ) -> None:
        variables = {item["id"]: item for item in _objects(parameters.get("variables", []))}
        bindings = _objects(parameters.get("bindings", []))
        views = SourceViews(capture, selections)
        json_cache: dict[str, JSONValue] = {}
        for binding in bindings:
            variable = variables.get(binding.get("variable_id"))
            if variable is None:
                raise _bad()
            value = self._target(binding, views, json_cache)
            location = _object(binding["location"])
            value_type = cast(str, variable["value_type"])
            if location["area"] == "request_json":
                if not matches_type(value, value_type):
                    raise _bad("BINDING_TYPE_MISMATCH")
            elif value_type == "null":
                raise _bad("BINDING_TYPE_MISMATCH")
        for index, left in enumerate(bindings):
            for right in bindings[index + 1:]:
                if left["exchange_id"] == right["exchange_id"] and self._overlap(
                    _object(left["location"]), _object(right["location"])
                ):
                    raise _bad("BINDING_OVERLAP")

    @staticmethod
    def _url(request: dict[str, JSONValue]) -> str:
        url = request.get("url")
        if not isinstance(url, str) or any(ord(char) <= 32 for char in url):
            raise _bad()
        _bounded(url)
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise _bad()
            _ = parsed.port
        except ValueError:
            raise _bad() from None
        return url

    def _target(
        self, binding: dict[str, JSONValue], views: SourceViews, json_cache: dict[str, JSONValue],
    ) -> JSONValue:
        entity = cast(str, binding["exchange_id"])
        request = _object(_object(views.exchange(entity).raw).get("request"))
        location = _object(binding["location"])
        area = location["area"]
        if area == "request_header":
            pairs = _pairs(request.get("headers"))
            index = self._index(location, len(pairs))
            if pairs[index][0].lower() == "content-type":
                choice = views.selection(entity, "/request/postData/mimeType")
                if choice and (choice["representation"] != "header"
                               or choice.get("header_index") != index):
                    raise _bad("BINDING_SHADOWED_DESTINATION")
            return pairs[index][1]
        if area == "request_path":
            path = urlsplit(self._url(request)).path
            parts = path.split("/")[1:] if path.startswith("/") else path.split("/") if path else []
            return parts[self._index(location, len(parts))]
        if area == "request_query":
            choice = views.selection(entity, "/request/queryString")
            pairs = (
                _pairs(request.get("queryString"))
                if choice and choice["representation"] == "queryString"
                else _form(urlsplit(self._url(request)).query)
            )
            return pairs[self._index(location, len(pairs))][1]
        body = views.body(entity, "request")
        if body.view_status != "available" or body.text is None:
            raise _bad()
        _bounded(body.text)
        media = views.media(entity)
        if area == "request_json" and (media == "application/json" or media.endswith("+json")):
            if entity not in json_cache:
                try:
                    json_cache[entity] = parse_json_text(body.text)
                except Har2LoadError:
                    raise _bad() from None
            try:
                return resolve_pointer(json_cache[entity], cast(str, location["pointer"]))
            except Har2LoadError:
                raise _bad() from None
        if area == "request_form" and media == "application/x-www-form-urlencoded":
            pairs = _form(body.text)
            return pairs[self._index(location, len(pairs))][1]
        if area == "request_text" and media.startswith("text/"):
            start, end = location.get("start"), location.get("end")
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= len(body.text)):
                raise _bad()
            return body.text[start:end]
        raise _bad("BINDING_CAPABILITY_UNSUPPORTED")

    @staticmethod
    def _index(location: dict[str, JSONValue], length: int) -> int:
        index = location.get("index")
        if type(index) is not int or not 0 <= index < length:
            raise _bad()
        return index

    @staticmethod
    def _overlap(left: dict[str, JSONValue], right: dict[str, JSONValue]) -> bool:
        if left["area"] != right["area"]:
            body_areas = {"request_text", "request_json", "request_form"}
            return left["area"] in body_areas and right["area"] in body_areas
        if left["area"] == "request_json":
            left_parts = cast(str, left["pointer"]).split("/")[1:]
            right_parts = cast(str, right["pointer"]).split("/")[1:]
            return (left_parts[:len(right_parts)] == right_parts
                    or right_parts[:len(left_parts)] == left_parts)
        if left["area"] == "request_text":
            a, b = cast(int, left["start"]), cast(int, left["end"])
            c, d = cast(int, right["start"]), cast(int, right["end"])
            return a == c or max(a, c) < min(b, d) or a <= c < b or c <= a < d
        return left["index"] == right["index"]
