"""Private, deterministic scalar evidence; equality never establishes causality.

Locations use binding-shaped request addresses, raw response RFC6901 pointers,
or decoded_body_pointer/text spans. Decoded path addresses identify an occurrence,
not a proposed substitution. Only whole text bodies are compared. Coverage counts
enumerated slots (including invalid/empty slots), links and cumulative scanned
UTF-8 bytes; payload accounting conservatively includes unmatched source records.
Truncated counts limit events, not omitted values: the remaining extent is unknown.
Consumer alternative sets are published atomically. IDs hash locators, not values.
No input is modified and no model operations are produced.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import cast
from urllib.parse import SplitResult, unquote, urlsplit

from har2load.binding_locations import SourceViews
from har2load.domain import Capture, Exchange, Grouping, Har2LoadError, JSONValue
from har2load.media import mime_signature
from har2load.precision import timing_number
from har2load.serialization import canonical_bytes
from har2load.values import MAX_JSON_DEPTH, MAX_TEXT_BYTES, check_text, parse_json_text

_MAX_LIMIT = 64 * 1024 * 1024
_SHORT = 3
_COMMON_OCCURRENCES = 10
_FORM = "application/x-www-form-urlencoded"
_LIMITATIONS = ["Equality does not prove causality.",
                 "Only supported scalar locations are covered; no replay or native parity.",
                 "Text matching uses whole bodies, without token or substring mining.",
                 "URL decoding is context-specific and performed once.",
                 "Non-string scalars and strings of at most three characters are low priority.",
                 f"At least {_COMMON_OCCURRENCES} retained source/consumer occurrences are "
                 "low priority, not suppressed."]
_MESSAGES = {"invalid": "A supported location is malformed or invalid.",
             "unsupported": "A body or scalar representation is unsupported.",
             "unavailable": "A body view is unavailable.",
             "conflicts": "An unresolved representation conflict was skipped.",
             "truncated": "An analysis resource limit was reached; remainder is unknown."}


@dataclass(frozen=True)
class AnalysisLimits:
    max_values: int = 10_000
    max_candidates: int = 1_000
    max_links: int = 10_000
    max_text_bytes: int = MAX_TEXT_BYTES
    max_body_nodes: int = 10_000
    max_indexed_text_bytes: int = 4 * MAX_TEXT_BYTES
    max_payload_bytes: int = 8 * MAX_TEXT_BYTES

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            ceiling = MAX_TEXT_BYTES if item.name == "max_text_bytes" else _MAX_LIMIT
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("Analysis limits must be bounded positive integers.")


class _Stopped(Exception):
    """Internal budget exhaustion, never containing recorded values."""


def _identifier(prefix: str, value: JSONValue) -> str:
    return prefix + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _key(value: JSONValue) -> tuple[str, object]:
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, str):
        return "string", value
    return "number", Decimal(str(value))


def _temporal(source: Exchange, consumer: Exchange) -> str:
    start, end = timing_number(source.start_time), timing_number(source.end_time)
    target = timing_number(consumer.start_time)
    if start is None or end is None or target is None or end < start:
        return "unknown"
    return "available_before_request" if end <= target else "overlap"


@dataclass
class _Bucket:
    value: JSONValue
    sources: list[tuple[dict[str, JSONValue], Exchange]] = field(default_factory=list)
    record: dict[str, JSONValue] | None = None


class _Analysis:
    def __init__(self, limits: AnalysisLimits) -> None:
        self.limits = limits
        self.counts = dict.fromkeys(("values", "body_nodes", "indexed_text_bytes", "payload_bytes",
                                     "links", "sources", "consumers", "candidates", *_MESSAGES), 0)
        self.index: dict[tuple[str, object], _Bucket] = {}
        self.results: list[JSONValue] = []

    def spend(self, counter: str, maximum: int, amount: int = 1) -> None:
        if self.counts[counter] + amount > maximum:
            raise _Stopped
        self.counts[counter] += amount

    def slot(self) -> None:
        self.spend("values", self.limits.max_values)

    def text(self, text: str) -> None:
        size = check_text(text, self.limits.max_text_bytes)
        self.spend("indexed_text_bytes", self.limits.max_indexed_text_bytes, size)

    def payload(self, value: JSONValue) -> None:
        self.spend("payload_bytes", self.limits.max_payload_bytes, len(canonical_bytes(value)) + 2)

    def error(self, error: Har2LoadError) -> None:
        if "LIMIT" in error.code:
            raise _Stopped from None
        category = "conflicts" if "CONFLICT" in error.code else "invalid"
        self.counts[category] += 1

    def result(self) -> dict[str, JSONValue]:
        coverage: dict[str, JSONValue] = dict(self.counts)
        coverage.update(status="partial" if any(self.counts[k] for k in _MESSAGES) else "complete",
                        unknown_remainder=bool(self.counts["truncated"]),
                        limitations=list(_LIMITATIONS))
        diagnostics: list[JSONValue] = [
            {"code": "CANDIDATE_LIMIT" if key == "truncated" else "CANDIDATE_" + key.upper(),
             "message": message, "count": self.counts[key]}
            for key, message in _MESSAGES.items() if self.counts[key]]
        limits: dict[str, JSONValue] = {
            item.name: getattr(self.limits, item.name) for item in fields(self.limits)}
        return {"profile": "scalar_candidates_v1", "candidates": self.results,
                "diagnostics": diagnostics, "coverage": coverage,
                "limits": limits}

    def source(self, value: JSONValue, exchange: Exchange, area: str,
               location: dict[str, JSONValue], view: str) -> None:
        record: dict[str, JSONValue] = {"exchange_id": exchange.id, "area": area,
                                       "location": location, "source_view": view}
        record["id"] = _identifier("src_", record)
        self.payload(record)
        self.payload(value)
        bucket = self.index.setdefault(_key(value), _Bucket(value))
        bucket.sources.append((record, exchange))
        self.counts["sources"] += 1

    def new_candidate(self, bucket: _Bucket) -> dict[str, JSONValue]:
        if self.counts["candidates"] >= self.limits.max_candidates:
            raise _Stopped
        kind = _key(bucket.value)[0]
        if kind == "number" and bucket.value == Decimal(str(bucket.value)).to_integral_value():
            kind = "integer"
        low = kind != "string" or len(cast(str, bucket.value)) <= _SHORT
        record: dict[str, JSONValue] = {
            "id": _identifier("cand_", bucket.sources[0][0]["id"]),
            "knowledge_status": "heuristic_candidate", "value": bucket.value, "value_type": kind,
            "sources": [], "consumers": [], "priority": "low" if low else "normal",
            "ambiguity": False, "limitations": list(_LIMITATIONS)}
        self.payload(record)
        return record

    def consumer(self, value: JSONValue, exchange: Exchange, area: str,
                 location: dict[str, JSONValue], method: str) -> None:
        self.counts["consumers"] += 1
        bucket = self.index.get(_key(value))
        if bucket is None:
            return
        if self.counts["links"] + len(bucket.sources) > self.limits.max_links:
            raise _Stopped
        record = bucket.record if bucket.record is not None else self.new_candidate(bucket)
        alternatives: list[JSONValue] = [
            {"source_id": source["id"], "temporal_status": _temporal(producer, exchange)}
            for source, producer in bucket.sources]
        consumer: dict[str, JSONValue] = {"exchange_id": exchange.id, "area": area,
                                         "location": {"area": area, **location},
                                         "match_method": method, "alternatives": alternatives}
        self.payload(consumer)
        self.counts["links"] += len(bucket.sources)
        self.publish(bucket, record, consumer)

    def publish(self, bucket: _Bucket, record: dict[str, JSONValue],
                consumer: dict[str, JSONValue]) -> None:
        # Publish a complete alternative set or none, even when a budget is exhausted.
        if bucket.record is None:
            bucket.record = record
            self.results.append(record)
            self.counts["candidates"] += 1
        sources = cast(list[JSONValue], record["sources"])
        sources.extend(source for source, _ in bucket.sources[len(sources):])
        cast(list[JSONValue], record["consumers"]).append(consumer)
        record["ambiguity"] = len(sources) > 1
        if len(sources) + len(cast(list[JSONValue], record["consumers"])) >= _COMMON_OCCURRENCES:
            record["priority"] = "low"


class _Scanner:
    def __init__(self, state: _Analysis, views: SourceViews) -> None:
        self.state, self.views = state, views
        self.parsed_urls: dict[str, SplitResult] = {}

    def emit(self, value: JSONValue, exchange: Exchange, area: str,
             location: dict[str, JSONValue], method: str = "exact", view: str = "captured") -> None:
        if isinstance(value, str):
            self.state.text(value)
        if area.startswith("response_"):
            self.state.source(value, exchange, area, location, view)
        else:
            self.state.consumer(value, exchange, area, location, method)

    def pairs(self, value: JSONValue, exchange: Exchange, area: str, pointer: str) -> None:
        if not isinstance(value, list):
            self.state.counts["invalid"] += 1
            return
        for index, item in enumerate(value):
            self.state.slot()
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("value"), str)):
                self.state.counts["invalid"] += 1
                continue
            self.state.text(cast(str, item["name"]))
            if area == "request_header" and self.shadowed(item, exchange.id, index):
                continue
            location: dict[str, JSONValue] = ({"index": index} if area.startswith("request_") else
                                              {"pointer": f"{pointer}/{index}/value"})
            self.emit(item["value"], exchange, area, location)

    def shadowed(self, item: dict[str, JSONValue], entity: str, index: int) -> bool:
        if cast(str, item["name"]).lower() != "content-type":
            return False
        try:
            choice = self.views.selection(entity, "/request/postData/mimeType")
        except Har2LoadError as error:
            self.state.error(error)
            return True
        return bool(choice and (choice["representation"] != "header"
                                or choice.get("header_index") != index))

    def decode(self, value: str, *, plus: bool) -> str:
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise Har2LoadError("CANDIDATE_INVALID", "Malformed URL encoding.")
        try:
            return unquote(value.replace("+", " ") if plus else value, errors="strict")
        except UnicodeError:
            raise Har2LoadError("CANDIDATE_INVALID", "Malformed URL encoding.") from None

    def encoded(self, text: str, exchange: Exchange, area: str) -> None:
        self.state.text(text)
        index = 0
        for match in re.finditer(r"[^&]*", text):
            self.state.slot()
            fragment = match.group()
            if not fragment:
                continue
            name, _, value = fragment.partition("=")
            self.decode(name, plus=True)
            self.emit(self.decode(value, plus=True), exchange, area,
                      {"index": index}, "url_decoded")
            index += 1

    def parsed_url(self, exchange: Exchange, request: dict[str, JSONValue]) -> SplitResult:
        if exchange.id in self.parsed_urls:
            return self.parsed_urls[exchange.id]
        value = request.get("url")
        if not isinstance(value, str):
            raise Har2LoadError("CANDIDATE_INVALID", "Malformed URL.")
        self.state.text(value)
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or any(ord(char) <= 32 for char in value)):
            raise Har2LoadError("CANDIDATE_INVALID", "Malformed URL.")
        _ = parsed.port
        self.parsed_urls[exchange.id] = parsed
        return parsed

    def url(self, exchange: Exchange, request: dict[str, JSONValue]) -> None:
        parsed = self.parsed_url(exchange, request)
        parts = parsed.path.split("/")[1:] if parsed.path.startswith("/") else []
        for index, part in enumerate(parts):
            self.state.slot()
            self.emit(self.decode(part, plus=False), exchange, "request_path",
                      {"index": index}, "url_decoded")

    def query(self, exchange: Exchange, request: dict[str, JSONValue]) -> None:
        choice = self.views.selection(exchange.id, "/request/queryString")
        if choice and choice["representation"] == "queryString":
            self.pairs(request.get("queryString"), exchange, "request_query", "")
        else:
            self.encoded(self.parsed_url(exchange, request).query, exchange, "request_query")

    def guard_body(self, section: dict[str, JSONValue], field_name: str) -> None:
        # Bound SourceViews' projection before it inspects known metadata; ignore vendor trees.
        data = section.get(field_name)
        if not isinstance(data, dict):
            return
        for key in ("mimeType", "encoding", "text", "comment"):
            self.state.slot()
            if isinstance(data.get(key), str):
                self.state.text(cast(str, data[key]))
        for name, items in (("params", data.get("params", [])),
                            ("headers", section.get("headers", []))):
            if isinstance(items, list):
                for item in items:
                    self.state.slot()
                    if isinstance(item, dict):
                        self.guard_pair(item, name)

    def guard_pair(self, item: dict[str, JSONValue], name: str) -> None:
        keys = ("name", "value", "fileName", "contentType", "comment") if name == "params" else (
            "name", "value")
        for key in keys:
            value = item.get(key)
            if isinstance(value, str):
                self.state.text(value)

    def body(self, exchange: Exchange, side: str, section: dict[str, JSONValue]) -> None:
        parent = f"/{side}/{'postData' if side == 'request' else 'content'}"
        self.guard_body(section, parent.rsplit("/", 1)[1])
        selected = any(self.views.selection(exchange.id, parent + suffix) is not None
                       for suffix in ("/params", "/mimeType"))
        body = self.views.body(exchange.id, side)
        if body.view_status != "available" or body.text is None:
            self.body_unavailable(body.view_status)
            return
        self.state.text(body.text)
        signature = mime_signature(body.mime_type or "")
        if signature is None or any(k == "charset" and v not in {"utf-8", "utf8"}
                                    for k, v in signature[1]):
            self.state.counts["unsupported"] += 1
            return
        view = "scenario_selected" if selected else "captured"
        self.body_values(body.text, signature[0], exchange, side, view)

    def body_unavailable(self, status: str) -> None:
        if status == "limit_exceeded":
            raise _Stopped
        category = ("invalid" if status == "invalid" else "unsupported"
                    if status in {"unsupported", "nontext"} else "unavailable")
        self.state.counts[category] += 1

    def body_values(self, text: str, media: str, exchange: Exchange, side: str, view: str) -> None:
        if media == "application/json" or media.endswith("+json"):
            self.walk(parse_json_text(text), "", exchange, side, view)
        elif side == "request" and media == _FORM:
            self.encoded(text, exchange, "request_form")
        elif media.startswith("text/"):
            self.state.slot()
            self.emit(text, exchange, side + "_text", {"start": 0, "end": len(text)}, view=view)
        else:
            self.state.counts["unsupported"] += 1

    def walk(self, value: JSONValue, pointer: str, exchange: Exchange, side: str,
             view: str, depth: int = 0) -> None:
        self.state.slot()
        self.state.spend("body_nodes", self.state.limits.max_body_nodes)
        if depth > MAX_JSON_DEPTH:
            raise _Stopped
        if isinstance(value, dict):
            for key, child in value.items():
                escaped = key.replace("~", "~0").replace("/", "~1")
                self.walk(child, pointer + "/" + escaped, exchange, side, view, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.walk(child, pointer + f"/{index}", exchange, side, view, depth + 1)
        else:
            location = {"pointer" if side == "request" else "decoded_body_pointer": pointer}
            self.emit(value, exchange, side + "_json", cast(dict[str, JSONValue], location),
                      view=view)

    def scan(self, exchange: Exchange, side: str) -> None:
        self.state.slot()
        section = exchange.raw.get(side) if isinstance(exchange.raw, dict) else None
        if not isinstance(section, dict):
            self.state.counts["invalid"] += 1
            return
        for operation in self.operations(exchange, side, section):
            try:
                operation()
            except Har2LoadError as error:
                self.state.error(error)
            except ValueError:
                self.state.counts["invalid"] += 1

    def operations(self, exchange: Exchange, side: str,
                   section: dict[str, JSONValue]) -> list[Callable[[], None]]:
        operations: list[Callable[[], None]] = [
            lambda: self.pairs(section.get("headers", []), exchange,
                               side + "_header", f"/{side}/headers")]
        if side == "response":
            operations.append(lambda: self.pairs(section.get("cookies", []), exchange,
                                                "response_cookie", "/response/cookies"))
        else:
            operations.append(lambda: self.url(exchange, section))
            operations.append(lambda: self.query(exchange, section))
        operations.append(lambda: self.body(exchange, side, section))
        return operations


class CandidateAnalyzer:
    """Analyze trusted Capture/Grouping and validated scenario selections, without I/O."""

    def __init__(self, *, limits: AnalysisLimits | None = None) -> None:
        self.limits = limits if limits is not None else AnalysisLimits()

    def analyze(self, capture: Capture, grouping: Grouping,
                scenario: dict[str, JSONValue]) -> dict[str, JSONValue]:
        state = _Analysis(self.limits)
        try:
            self.run(state, capture, grouping, scenario)
        except _Stopped:
            state.counts["truncated"] += 1
        except Har2LoadError as error:
            state.counts["truncated" if "LIMIT" in error.code else "invalid"] += 1
        return state.result()

    def run(self, state: _Analysis, capture: Capture, grouping: Grouping,
            scenario: dict[str, JSONValue]) -> None:
        selections = scenario.get("representation_selections", [])
        if not isinstance(selections, list):
            raise Har2LoadError("CANDIDATE_INVALID", "Invalid scenario selections.")
        state.spend("values", state.limits.max_values, len(capture.exchanges) + len(selections)
                    + len(capture.diagnostics) + len(grouping.ordered_exchange_ids))
        views = SourceViews(capture, selections)
        scanner = _Scanner(state, views)
        seen: set[str] = set()
        for entity in grouping.ordered_exchange_ids:
            if entity in seen:
                state.counts["invalid"] += 1
                continue
            seen.add(entity)
            exchange = views.exchange(entity)
            scanner.scan(exchange, "request")
            scanner.scan(exchange, "response")
