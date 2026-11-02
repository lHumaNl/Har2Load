"""Selected request bytes and exact literal substitutions, never runtime evaluation.

The static profile rejects every runtime source/extractor/extension, recorded
authentication/cookie prerequisite, upload and compressed body. Original HAR and
model remain immutable. Managed-header and constant-folding decisions are explicit.
"""

import re
from typing import cast
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from har2load.binding_locations import BindingLocations, SourceViews
from har2load.domain import Capture, Diagnostic, Exchange, Grouping, Har2LoadError, JSONValue
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.media import mime_signature
from har2load.parameters import ParameterModel
from har2load.presentation import resolve_pointer
from har2load.serialization import canonical_bytes
from har2load.values import MAX_TEXT_BYTES, check_text, parse_json_text

MAX_REQUESTS = 1000
MAX_TOTAL_BODY_BYTES = 4 * MAX_TEXT_BYTES
MAX_TOTAL_REQUEST_BYTES = 8 * MAX_TEXT_BYTES
MAX_HEADERS = 1000
MAX_TOTAL_HEADERS = 10000
MAX_DECISIONS = 20000
MAX_FIELDS = 10000
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_MANAGED = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
            "te", "trailer", "upgrade", "proxy-connection"}


def _bad(code: str) -> Har2LoadError:
    return Har2LoadError(code, "Request cannot be materialized by the static literal profile.", 3)


def _object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise _bad("GENERATION_REQUEST_INVALID")
    return value


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _bad("GENERATION_REQUEST_INVALID")
    return cast(list[dict[str, JSONValue]], value)


def _text(value: JSONValue) -> str:
    if not isinstance(value, str):
        raise _bad("GENERATION_REQUEST_INVALID")
    check_text(value)
    return value


def _scalar(value: JSONValue) -> str:
    return value if isinstance(value, str) else canonical_bytes(value).decode("utf-8")


def _decision(decisions: list[JSONValue], value: JSONValue) -> None:
    if len(decisions) >= MAX_DECISIONS:
        raise _bad("GENERATION_LIMIT")
    decisions.append(value)


def _query_pairs(value: JSONValue) -> str:
    if isinstance(value, list) and len(value) > MAX_FIELDS:
        raise _bad("GENERATION_LIMIT")
    result: list[str] = []
    size = 0
    for item in _objects(value):
        part = (quote_plus(_text(item.get("name")), safe="") + "="
                + quote_plus(_text(item.get("value")), safe=""))
        size += len(part) + bool(result)
        if size > MAX_TEXT_BYTES:
            raise _bad("GENERATION_LIMIT")
        result.append(part)
    return "&".join(result)


def _encoded(text: str, replacements: dict[int, str]) -> str:
    """Rewrite only addressed ordered value occurrences, retaining untouched octets."""
    if not replacements:
        return text
    if text.count("&") >= MAX_FIELDS:
        raise _bad("GENERATION_LIMIT")
    parts, index = text.split("&"), 0
    positions: list[tuple[int, str, str]] = []
    size = len(text.encode("utf-8"))
    for position, fragment in enumerate(parts):
        if not fragment:
            continue
        if index in replacements:
            name = fragment.partition("=")[0]
            size += len(name.encode("utf-8")) + 1 - len(fragment.encode("utf-8"))
            positions.append((position, name, replacements[index]))
        index += 1
    for position, name, replacement in positions:
        encoded = quote_plus(replacement, safe="")
        size += len(encoded)
        if size > MAX_TEXT_BYTES:
            raise _bad("GENERATION_LIMIT")
        parts[position] = name + "=" + encoded
    return "&".join(parts)


def _json_bytes(value: JSONValue) -> bytes:
    """Measure canonical scalar leaves before serializing an amplified JSON tree."""
    size = 0

    def measure(item: JSONValue) -> None:
        nonlocal size
        if isinstance(item, (dict, list)):
            size += 2 + max(0, len(item) - 1)
            if size > MAX_TEXT_BYTES:
                raise _bad("GENERATION_LIMIT")
            if isinstance(item, dict):
                for key, child in item.items():
                    size += len(canonical_bytes(key)) + 1
                    measure(child)
            else:
                for child in item:
                    measure(child)
        else:
            size += len(canonical_bytes(item))
        if size > MAX_TEXT_BYTES:
            raise _bad("GENERATION_LIMIT")

    measure(value)
    return canonical_bytes(value)


def _set_pointer(document: JSONValue, pointer: str, value: JSONValue) -> JSONValue:
    if not pointer:
        return value
    parent, _, encoded_key = pointer.rpartition("/")
    container = resolve_pointer(document, parent)
    key = encoded_key.replace("~1", "/").replace("~0", "~")
    if isinstance(container, list):
        container[int(key)] = value
    elif isinstance(container, dict):
        container[key] = value
    else:
        raise _bad("GENERATION_BINDING_INVALID")
    return document


class _Request:
    def __init__(
        self, views: SourceViews, exchange: Exchange, bindings: list[dict[str, JSONValue]],
        variables: dict[str, dict[str, JSONValue]], decisions: list[JSONValue],
    ) -> None:
        self.views, self.exchange = views, exchange
        self.request = _object(_object(exchange.raw).get("request"))
        self.bindings, self.variables, self.decisions = bindings, variables, decisions

    def bindings_for(self, area: str) -> list[dict[str, JSONValue]]:
        return [binding for binding in self.bindings
                if _object(binding["location"])["area"] == area]

    def value(self, binding: dict[str, JSONValue]) -> JSONValue:
        variable = self.variables[cast(str, binding["variable_id"])]
        return _object(variable["source"])["value"]

    def indexed(self, area: str) -> dict[int, str]:
        return {cast(int, _object(binding["location"])["index"]): _scalar(self.value(binding))
                for binding in self.bindings_for(area)}

    def url(self) -> str:
        raw = _text(self.request.get("url"))
        selection = self.views.selection(self.exchange.id, "/request/queryString")
        selected_pairs = selection is not None and selection["representation"] == "queryString"
        checked = raw.split("#", 1)[0]
        if selected_pairs:
            checked = checked.split("?", 1)[0]
        if (not checked.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in checked)
                or re.search(r"%(?![0-9a-fA-F]{2})", checked)
                or any(c in checked for c in '<>"{}|\\^`')):
            raise _bad("GENERATION_URL_UNSUPPORTED")
        try:
            parsed = urlsplit(raw)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise _bad("GENERATION_URL_UNSUPPORTED")
            _ = parsed.port
            if parsed.port == 0:
                raise _bad("GENERATION_URL_UNSUPPORTED")
        except ValueError:
            raise _bad("GENERATION_URL_UNSUPPORTED") from None
        path = parsed.path or "/"
        parts = path.split("/")
        path_replacements = self.indexed("request_path")
        path_size = len(path) - sum(len(parts[index + 1]) for index in path_replacements)
        for index, value in path_replacements.items():
            encoded = quote(value, safe="")
            path_size += len(encoded)
            if path_size > MAX_TEXT_BYTES:
                raise _bad("GENERATION_LIMIT")
            parts[index + 1] = encoded
        if selected_pairs:
            query = _query_pairs(self.request.get("queryString"))
        else:
            query = parsed.query
        query = _encoded(query, self.indexed("request_query"))
        if parsed.fragment:
            _decision(self.decisions, {"kind": "fragment_omitted", "exchange_id": self.exchange.id,
                                   "reason": "URL fragments are not sent in HTTP requests"})
        result = urlunsplit((parsed.scheme, parsed.netloc, "/".join(parts), query, ""))
        # Preserve an explicitly empty query when no selected query replaced it.
        if not query and "?" in raw.split("#", 1)[0] and not selected_pairs:
            result += "?"
        check_text(result)
        return result

    def body(self, method: str) -> tuple[bytes | None, str, str | None]:
        if "postData" not in self.request:
            size = self.request.get("bodySize")
            if (size is not None and (type(size) is not int or size < -1)
                    or type(size) is int and size > 0
                    or method in {"POST", "PUT", "PATCH"} and size != 0):
                raise _bad("GENERATION_BODY_UNAVAILABLE")
            return None, "none", None
        params = _object(self.request["postData"]).get("params")
        if isinstance(params, list) and len(params) > MAX_FIELDS:
            raise _bad("GENERATION_LIMIT")
        body = self.views.body(self.exchange.id, "request")
        if body.text is None or body.view_status != "available":
            raise _bad("GENERATION_BODY_UNSUPPORTED")
        text, media = body.text, self.views.media(self.exchange.id)
        check_text(text)
        if media == "application/json" or media.endswith("+json"):
            document = parse_json_text(text)
            replacements = self.bindings_for("request_json")
            for binding in replacements:
                document = _set_pointer(
                    document, cast(str, _object(binding["location"])["pointer"]),
                    self.value(binding),
                )
            encoded = _json_bytes(document) if replacements else text.encode("utf-8")
            kind = "json"
        elif media == "application/x-www-form-urlencoded":
            if re.search(r"%(?![0-9a-fA-F]{2})", text):
                raise _bad("GENERATION_BODY_UNSUPPORTED")
            encoded, kind = _encoded(text, self.indexed("request_form")).encode("utf-8"), "form"
        elif media.startswith("text/"):
            encoded, kind = self.text_body(text), "text"
        else:
            raise _bad("GENERATION_BODY_UNSUPPORTED")
        if len(encoded) > MAX_TEXT_BYTES:
            raise _bad("GENERATION_LIMIT")
        return encoded, kind, body.mime_type or media

    def text_body(self, text: str) -> bytes:
        bindings = sorted(self.bindings_for("request_text"),
                          key=lambda item: cast(int, _object(item["location"])["start"]))
        spans = [(cast(int, _object(item["location"])["start"]),
                  cast(int, _object(item["location"])["end"])) for item in bindings]
        size = len(text.encode("utf-8")) - sum(len(text[a:b].encode("utf-8")) for a, b in spans)
        values: list[str] = []
        for item in bindings:
            value = _scalar(self.value(item))
            size += len(value.encode("utf-8"))
            if size > MAX_TEXT_BYTES:
                raise _bad("GENERATION_LIMIT")
            values.append(value)
        pieces: list[str] = []
        position = 0
        for (start, end), value in zip(spans, values, strict=True):
            pieces.extend((text[position:start], value))
            position = end
        pieces.append(text[position:])
        return "".join(pieces).encode("utf-8")

    def headers(self, mime: str | None) -> tuple[tuple[str, str], ...]:
        headers = _objects(self.request.get("headers", []))
        if len(headers) > MAX_HEADERS:
            raise _bad("GENERATION_LIMIT")
        pairs: list[tuple[str, str]] = []
        size = 0
        for item in headers:
            name, value = _text(item.get("name")), _text(item.get("value"))
            if (not _TOKEN.fullmatch(name) or not value.isascii()
                    or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)):
                raise _bad("GENERATION_HEADER_INVALID")
            size += len(name) + len(value)
            if size > MAX_TEXT_BYTES:
                raise _bad("GENERATION_LIMIT")
            pairs.append((name, value))
        managed = _MANAGED | {token.strip().lower() for name, value in pairs
                               if name.lower() == "connection" for token in value.split(",")}
        replacements = self.indexed("request_header")
        choice = self.views.selection(self.exchange.id, "/request/postData/mimeType")
        result = []
        for index, (name, original) in enumerate(pairs):
            lower = name.lower()
            if lower in {"cookie", "authorization", "proxy-authorization"}:
                raise _bad("GENERATION_SESSION_UNSUPPORTED")
            if lower == "content-encoding" and original.lower() != "identity":
                raise _bad("GENERATION_BODY_UNSUPPORTED")
            if lower in managed:
                if index in replacements:
                    raise _bad("GENERATION_MANAGED_HEADER_BINDING")
                _decision(self.decisions, {"kind": "managed_header_omitted",
                    "exchange_id": self.exchange.id, "index": index, "name": name,
                    "policy": "managed_headers_v1"})
                continue
            if lower == "content-type" and choice and (
                choice["representation"] != "header" or choice.get("header_index") != index
            ):
                continue
            value = replacements.get(index, original)
            if (not value.isascii()
                    or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)):
                raise _bad("GENERATION_HEADER_INVALID")
            if lower == "content-encoding" and value.lower() != "identity":
                raise _bad("GENERATION_BODY_UNSUPPORTED")
            result.append((name, value))
        content_types = [value for name, value in result if name.lower() == "content-type"]
        if len(content_types) > 1:
            raise _bad("GENERATION_CONTENT_TYPE_AMBIGUOUS")
        if content_types:
            signature = mime_signature(content_types[0])
            if signature is None or any(key == "charset" and value not in {"utf-8", "utf8"}
                                        for key, value in signature[1]):
                raise _bad("GENERATION_BODY_UNSUPPORTED")
        if not content_types and mime is not None:
            if not mime.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in mime):
                raise _bad("GENERATION_HEADER_INVALID")
            result.append(("Content-Type", mime))
        if sum(len(name) + len(value) for name, value in result) > MAX_TEXT_BYTES:
            raise _bad("GENERATION_LIMIT")
        return tuple(result)

    def build(self, group_id: str) -> NativeRequest:
        method = self.request.get("method")
        if not isinstance(method, str) or method not in _METHODS:
            raise _bad("GENERATION_METHOD_UNSUPPORTED")
        cookies = self.request.get("cookies", [])
        if not isinstance(cookies, list) or cookies:
            raise _bad("GENERATION_SESSION_UNSUPPORTED")
        url = self.url()
        body, kind, mime = self.body(method)
        headers = self.headers(mime)
        for item in self.bindings:
            _decision(self.decisions, {"kind": "literal_binding_materialized",
                "binding_id": item["id"], "variable_id": item["variable_id"],
                "exchange_id": self.exchange.id, "location": item["location"]})
        return NativeRequest(self.exchange.id, group_id, method, url, headers, body, kind)


class RequestMaterializer:
    """Pure supported-profile lowering, not whole-product capability certification."""

    def build(
        self, capture: Capture, grouping: Grouping, scenario: dict[str, JSONValue],
    ) -> RequestPlan:
        diagnostics: list[Diagnostic] = []
        decisions: list[JSONValue] = []
        parameters = _object(scenario.get("parameters", ParameterModel.empty()))
        variables = {cast(str, item["id"]): item for item in _objects(parameters["variables"])}
        if (parameters["extractors"]
                or any(_object(item["source"])["kind"] != "literal"
                       for item in variables.values())):
            diagnostics.append(Diagnostic("GENERATION_RUNTIME_UNSUPPORTED", "error",
                "Runtime variables and extractors are outside this generation profile."))
        groups = tuple(NativeGroup(cast(str, item["id"]), cast(str, item["name"]),
                                   tuple(cast(list[str], item["exchange_ids"])))
                       for item in _objects(scenario["groups"]))
        if not 0 < len(capture.exchanges) <= MAX_REQUESTS:
            diagnostics.append(Diagnostic("GENERATION_LIMIT", "error",
                                          "Generation requires 1 to 1000 requests."))
        if diagnostics:
            return RequestPlan((), groups, tuple(diagnostics))
        selections = cast(list[JSONValue], scenario.get("representation_selections", []))
        try:
            BindingLocations().validate(parameters, capture, selections)
        except Har2LoadError as error:
            return RequestPlan((), groups, (Diagnostic(error.code, "error", str(error)),))
        views = SourceViews(capture, selections)
        group_ids = {entity: group.id for group in groups for entity in group.exchange_ids}
        bindings = _objects(parameters["bindings"])
        requests: list[NativeRequest] = []
        total = 0
        total_request = 0
        total_headers = 0
        for entity in grouping.ordered_exchange_ids:
            try:
                raw_request = _object(_object(views.exchange(entity).raw).get("request"))
                headers = raw_request.get("headers", [])
                if isinstance(headers, list):
                    total_headers += len(headers)
                if total_headers > MAX_TOTAL_HEADERS:
                    raise _bad("GENERATION_LIMIT")
                request = _Request(
                    views, views.exchange(entity),
                    [item for item in bindings if item["exchange_id"] == entity],
                    variables, decisions,
                ).build(group_ids[entity])
                total += len(request.body or b"")
                total_request += (len(request.body or b"") + len(request.url)
                                  + sum(len(name) + len(value) for name, value in request.headers))
                if total > MAX_TOTAL_BODY_BYTES or total_request > MAX_TOTAL_REQUEST_BYTES:
                    raise _bad("GENERATION_LIMIT")
                requests.append(request)
            except Har2LoadError as error:
                diagnostics.append(Diagnostic(error.code, "error", str(error), entity_id=entity))
                if error.code == "GENERATION_LIMIT":
                    break
        try:
            for item in variables.values():
                _decision(decisions, {"kind": "literal_variable_folded", "variable_id": item["id"],
                                     "runtime_state": "not_emitted",
                                     "reason": "Static immutable constant"})
        except Har2LoadError as error:
            diagnostics.append(Diagnostic(error.code, "error", str(error)))
        return RequestPlan(tuple(requests), groups, tuple(diagnostics), tuple(decisions))
