"""Offline HAR snapshot reader, not a complete HAR 1.2 schema validator.

Original bytes and entry JSON remain authoritative, including unknown extensions.
Views support UTF-8 text and URL-encoded forms only; binary, multipart, arbitrary
charsets and unknown encodings are not replayable. Diagnostics do not sanitize raw
data. No captured strings are executed, logged, fetched, or used as field paths.
Workspace owns input-size limits; JSON recursion failures are rejected safely.
"""

import base64
import binascii
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from har2load.domain import (
    Availability,
    Body,
    Capture,
    Diagnostic,
    Exchange,
    Har2LoadError,
    JSONValue,
)
from har2load.media import mime_signature
from har2load.precision import exact_sum, seconds_from_milliseconds, timing_number

_FORM = "application/x-www-form-urlencoded"
_MESSAGES = {
    "HAR_INVALID_FIELD": "A field is missing or has an unsupported type or value.",
    "HAR_INVALID_TIMING": "Timing is unavailable or is not a timezone-aware finite interval.",
    "HAR_BODY_UNAVAILABLE": "Body text is not recorded; emptiness cannot be established.",
    "HAR_BODY_INVALID": "The recorded body cannot be decoded as declared.",
    "HAR_BODY_NON_TEXT": "Body metadata or bytes do not support a UTF-8 text view.",
    "HAR_BODY_UNSUPPORTED": "This body representation or encoding is unsupported.",
    "HAR_REPRESENTATION_CONFLICT": "Recorded representations are not provably equivalent; "
    "executable generation requires explicit resolution.",
}


def _unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise Har2LoadError("HAR_DUPLICATE_KEY", "Duplicate JSON object keys are unsupported.")
        result[key] = value
    return result


def _reject_constant(value: str) -> JSONValue:
    raise Har2LoadError("HAR_NONFINITE_NUMBER", "Nonfinite JSON numbers are unsupported.")


def _finite_decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        _reject_constant(value)
    return result


def _load_entries(raw_bytes: bytes) -> list[JSONValue]:
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_decimal)
    except UnicodeDecodeError:
        raise Har2LoadError("HAR_INVALID_UTF8", "Input must be UTF-8 JSON.") from None
    except Har2LoadError as error:
        raise Har2LoadError(error.code, str(error)) from None
    except (ValueError, RecursionError, InvalidOperation):
        raise Har2LoadError("HAR_INVALID_JSON", "Input is not supported JSON.") from None
    log = data.get("log") if isinstance(data, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise Har2LoadError("HAR_INVALID_STRUCTURE",
                           "Input must contain a log.entries array.") from None
    return cast(list[JSONValue], entries)


class _Diagnostics:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.items: list[Diagnostic] = []

    def add(self, code: str, location: str) -> None:
        severity: Literal["error", "warning"] = "warning"
        if code in {"HAR_INVALID_FIELD", "HAR_BODY_INVALID", "HAR_REPRESENTATION_CONFLICT"}:
            severity = "error"
        diagnostic = Diagnostic(code, severity, _MESSAGES[code], self.entity_id, location)
        if diagnostic not in self.items:
            self.items.append(diagnostic)


def _string(value: JSONValue) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _number(value: JSONValue) -> bool:
    try:
        return type(value) in (int, float, Decimal) and math.isfinite(cast(float, value))
    except OverflowError:
        return False


def _iso_datetime(value: str) -> datetime:
    # Supported HAR timestamp profile: full date/time, optional decimal seconds,
    # UTC or minute-resolution timezone. Reject other forms rather than rounding
    # offset fractions or accepting Python's arbitrary date/time separators.
    pattern = (
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:[.,][0-9]{1,1000})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    )
    if re.fullmatch(pattern, value) is None:
        raise ValueError
    return datetime.fromisoformat(value)


def _strings(data: dict[str, JSONValue], keys: tuple[str, ...], path: str,
             issues: _Diagnostics) -> None:
    for key in keys:
        if key in data and not _string(data[key]):
            issues.add("HAR_INVALID_FIELD", f"{path}/{key}")


def _pairs(value: JSONValue) -> list[tuple[str, str]] | None:
    if not isinstance(value, list):
        return None
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or not _string(item.get("name")):
            return None
        if not _string(item.get("value")):
            return None
        result.append((cast(str, item["name"]), cast(str, item["value"])))
    return result


def _form_pairs(text: str) -> list[tuple[str, str]] | None:
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        return None
    try:
        return parse_qsl(text, keep_blank_values=True, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return None


def _url_query(value: JSONValue) -> str | None:
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        _ = parsed.port  # Validate malformed ports without retaining their value.
        return parsed.query
    except ValueError:
        return None


def _mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _utf8_mime(mime: str) -> bool:
    signature = mime_signature(mime)
    return signature is not None and all(
        key != "charset" or value in {"utf-8", "utf8"} for key, value in signature[1]
    )


def _valid_param(item: JSONValue) -> bool:
    if not isinstance(item, dict) or not _string(item.get("name")):
        return False
    if "value" not in item and "fileName" not in item:
        return False
    return all(key not in item or _string(item[key])
               for key in ("value", "fileName", "contentType", "comment"))


def _textual(mime: str | None) -> bool:
    if mime is None:
        return True
    media = _mime(mime)
    return (media.startswith("text/") or media.endswith(("+json", "+xml"))
            or media in {_FORM, "application/json", "application/xml",
                         "application/javascript", "application/graphql"})


class _BodyReader:
    def __init__(self, entity_id: str, path: str) -> None:
        self.issues = _Diagnostics(entity_id)
        self.path = path
        self.mime: str | None = None
        self.encoding: str | None = None
        self.media_types: list[str] = []

    def _result(
        self, state: Availability, code: str, *,
        representation: Literal["text", "params", "none"] = "none",
    ) -> Body:
        self.issues.add(code, self.path)
        view: Literal["unavailable", "unsupported", "nontext", "invalid"] = "unavailable"
        if state == "invalid":
            view = "invalid"
        elif code == "HAR_BODY_UNSUPPORTED":
            view = "unsupported"
        elif code == "HAR_BODY_NON_TEXT":
            view = "nontext"
        return Body(
            state, mime_type=self.mime, encoding=self.encoding,
            representation=representation,
            diagnostics=tuple(self.issues.items), view_status=view,
        )

    def _text(self, text: str, representation: Literal["text", "params"] = "text") -> Body:
        return Body("present" if text else "empty", text, self.mime, self.encoding,
                    representation, tuple(self.issues.items), view_status="available")

    def read(self, parent: dict[str, JSONValue], key: str) -> Body:
        if key not in parent:
            return self._result("unavailable", "HAR_BODY_UNAVAILABLE")
        data = parent[key]
        if not isinstance(data, dict):
            return self._result("invalid", "HAR_INVALID_FIELD")
        self._metadata(data)
        self._consistency(parent, data)
        if any(d.code == "HAR_INVALID_FIELD" for d in self.issues.items):
            return self._result("invalid", "HAR_BODY_INVALID")
        if self._upload(data) and "text" not in data:
            return self._unsupported(data)
        return self._view(data)

    def _metadata(self, data: dict[str, JSONValue]) -> None:
        mime, encoding = data.get("mimeType"), data.get("encoding")
        self.mime = mime if isinstance(mime, str) else None
        self.encoding = encoding if isinstance(encoding, str) else None
        _strings(data, ("mimeType", "encoding", "text", "comment"), self.path, self.issues)
        for key in ("size", "compression"):
            if key in data and not _number(data[key]):
                self.issues.add("HAR_INVALID_FIELD", f"{self.path}/{key}")
        if "params" in data:
            params = data["params"]
            if not isinstance(params, list) or not all(_valid_param(item) for item in params):
                self.issues.add("HAR_INVALID_FIELD", f"{self.path}/params")

    def _upload(self, data: dict[str, JSONValue]) -> bool:
        params = data.get("params")
        return (self.mime is not None and _mime(self.mime).startswith("multipart/")) or (
            isinstance(params, list) and any(isinstance(item, dict) and "fileName" in item
                                            for item in params))

    def _consistency(self, parent: dict[str, JSONValue], data: dict[str, JSONValue]) -> None:
        headers = _pairs(parent.get("headers")) or []
        self.media_types = [value for name, value in headers if name.lower() == "content-type"]
        if self.mime is not None:
            self.media_types.append(self.mime)
        signatures = [mime_signature(value) for value in self.media_types]
        if len(signatures) > 1 and (None in signatures or len(set(signatures)) > 1):
            self.issues.add("HAR_REPRESENTATION_CONFLICT", self.path + "/mimeType")
        if "text" in data and "params" in data:
            text, params = data["text"], _pairs(data["params"])
            equivalent = (self._form_supported()
                          and isinstance(text, str) and params is not None
                          and not self._upload(data) and _form_pairs(text) == params)
            if not equivalent:
                self.issues.add("HAR_REPRESENTATION_CONFLICT", self.path + "/params")

    def _form_supported(self) -> bool:
        return (self.mime is not None and _mime(self.mime) == _FORM and self.encoding is None
                and all(_utf8_mime(value) for value in self.media_types))

    def _view(self, data: dict[str, JSONValue]) -> Body:
        if self.encoding is not None and self.encoding != "base64":
            return self._unsupported(data)
        if "text" in data:
            return self._decode(cast(str, data["text"]))
        if "params" in data:
            if self._form_supported():
                params = _pairs(data["params"])
                if params is not None:
                    return self._text(urlencode(params), "params")
            return self._unsupported(data)
        return self._result("unavailable", "HAR_BODY_UNAVAILABLE")

    def _unsupported(self, data: dict[str, JSONValue]) -> Body:
        if "text" in data:
            return self._result("present", "HAR_BODY_UNSUPPORTED", representation="text")
        params = data.get("params")
        if isinstance(params, list) and _pairs(params) is not None and not any(
            isinstance(item, dict) and "fileName" in item for item in params
        ):
            return self._result("present", "HAR_BODY_UNSUPPORTED", representation="params")
        return self._result("unavailable", "HAR_BODY_UNSUPPORTED")

    def _decode(self, text: str) -> Body:
        if self.encoding == "base64":
            try:
                binary = base64.b64decode(text, validate=True)
            except (ValueError, binascii.Error):
                return self._result("invalid", "HAR_BODY_INVALID")
            if not binary:
                return self._text("")
            if not all(_utf8_mime(value) for value in self.media_types):
                return self._result("present", "HAR_BODY_NON_TEXT", representation="text")
            try:
                text = binary.decode("utf-8")
            except UnicodeDecodeError:
                return self._result("present", "HAR_BODY_NON_TEXT", representation="text")
        if not text:
            return self._text("")
        if not all(_textual(value) for value in self.media_types) or self._binary_controls(text):
            return self._result("present", "HAR_BODY_NON_TEXT", representation="text")
        return self._text(text)

    def _binary_controls(self, text: str) -> bool:
        return any(ord(char) < 32 and char not in "\t\r\n" for char in text)


class _EntryReader:
    def __init__(self, digest: str, index: int) -> None:
        self.index = index
        self.entity_id = f"ex_{digest}_{index:06d}"
        self.issues = _Diagnostics(self.entity_id)

    def read(self, raw: JSONValue) -> Exchange:
        if not isinstance(raw, dict):
            self.issues.add("HAR_INVALID_FIELD", "/")
            return Exchange(self.entity_id, self.index, raw, diagnostics=tuple(self.issues.items))
        request = self._side(raw, "request")
        response = self._side(raw, "response")
        self._query(request)
        start, end = self._timing(raw)
        _strings(raw, ("comment", "pageref", "serverIPAddress", "connection"), "", self.issues)
        request_body = _BodyReader(self.entity_id, "/request/postData").read(request, "postData")
        response_body = _BodyReader(self.entity_id, "/response/content").read(response, "content")
        return Exchange(self.entity_id, self.index, raw, request_body, response_body,
                        start, end, tuple(self.issues.items))

    def _side(self, raw: dict[str, JSONValue], side: str) -> dict[str, JSONValue]:
        value = raw.get(side)
        if not isinstance(value, dict):
            self.issues.add("HAR_INVALID_FIELD", f"/{side}")
            return {}
        path = f"/{side}"
        fields = ("method", "url") if side == "request" else ("statusText", "redirectURL")
        _strings(value, (*fields, "httpVersion", "comment"), path, self.issues)
        required = ("method", "url") if side == "request" else ("status",)
        for key in required:
            valid = _string(value.get(key)) if key != "status" else type(value.get(key)) is int
            if not valid:
                self.issues.add("HAR_INVALID_FIELD", f"{path}/{key}")
        for key in ("headersSize", "bodySize"):
            if key in value and not _number(value[key]):
                self.issues.add("HAR_INVALID_FIELD", f"{path}/{key}")
        self._collections(value, path)
        return value

    def _collections(self, data: dict[str, JSONValue], path: str) -> None:
        keys = ("headers", "cookies") + (("queryString",) if path == "/request" else ())
        for key in keys:
            if key not in data:
                continue
            values = data[key]
            if _pairs(values) is None:
                self.issues.add("HAR_INVALID_FIELD", f"{path}/{key}")
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        _strings(item, ("comment",), f"{path}/{key}", self.issues)
                        if key == "cookies":
                            self._attributes(item, f"{path}/{key}")

    def _attributes(self, data: dict[str, JSONValue], path: str) -> None:
        _strings(data, ("comment", "path", "domain", "expires", "sameSite"), path, self.issues)
        for key in ("httpOnly", "secure"):
            if key in data and not isinstance(data[key], bool):
                self.issues.add("HAR_INVALID_FIELD", f"{path}/{key}")

    def _query(self, request: dict[str, JSONValue]) -> None:
        query = _url_query(request.get("url"))
        if query is None:
            self.issues.add("HAR_INVALID_FIELD", "/request/url")
            if "queryString" in request:
                self.issues.add("HAR_REPRESENTATION_CONFLICT", "/request/queryString")
            return
        if "queryString" in request:
            pairs = _pairs(request["queryString"])
            if pairs is None or _form_pairs(query) != pairs:
                self.issues.add("HAR_REPRESENTATION_CONFLICT", "/request/queryString")

    def _timing(self, data: dict[str, JSONValue]) -> tuple[Decimal | None, Decimal | None]:
        start = self._start_time(data.get("startedDateTime"))
        if start is None:
            return None, None
        duration = timing_number(data.get("time"))
        if duration is None or duration < 0:
            self.issues.add("HAR_INVALID_TIMING", "/time")
            return start, None
        return start, exact_sum(start, seconds_from_milliseconds(duration))

    def _start_time(self, timestamp: JSONValue) -> Decimal | None:
        try:
            if not isinstance(timestamp, str) or len(timestamp) > 1050:
                raise ValueError
            date = _iso_datetime(timestamp)
            if date.tzinfo is None:
                raise ValueError
            delta = date.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
            whole = Decimal(delta.days * 86400 + delta.seconds)
            fraction = re.search(r"[Tt ]\d{2}:\d{2}:\d{2}[.,](\d+)", timestamp)
            subseconds = (
                Decimal("0." + fraction[1]) if fraction else Decimal(date.microsecond).scaleb(-6)
            )
            return exact_sum(whole, subseconds)
        except (ValueError, OverflowError, OSError):
            self.issues.add("HAR_INVALID_TIMING", "/startedDateTime")
            return None


class HarImporter:
    """Parse exact input bytes into a snapshot and conservative interpreted views."""

    def parse(self, raw_bytes: bytes) -> Capture:
        entries = _load_entries(raw_bytes)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        exchanges = tuple(_EntryReader(digest, index).read(raw)
                          for index, raw in enumerate(entries))
        diagnostics = tuple(d for exchange in exchanges for d in
                            (*exchange.diagnostics, *exchange.request_body.diagnostics,
                             *exchange.response_body.diagnostics))
        return Capture(f"cap_{digest}", digest, raw_bytes, exchanges, diagnostics)
