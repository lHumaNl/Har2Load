"""Lower exact destinations to typed runtime segments without evaluating producers.

The static profile is unchanged. Runtime plans preserve declarations and build all
bindings at their original locations in selected views, never by sentinel matching.
"""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import cast
from urllib.parse import urlsplit

from har2load.binding_locations import BindingLocations, SourceViews
from har2load.domain import Capture, Diagnostic, Grouping, Har2LoadError, JSONValue
from har2load.extensions import TargetExtensions
from har2load.generation_types import NativeRequest, RequestPlan
from har2load.materialization import RequestMaterializer
from har2load.parameters import ParameterModel
from har2load.runtime_types import (
    MAX_RUNTIME_FIELD_BYTES,
    RuntimePlan,
    RuntimeRequest,
    RuntimeSlot,
    RuntimeTemplate,
)
from har2load.serialization import canonical_bytes
from har2load.values import parse_json_text

_CONTROL_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
    "trailer", "upgrade", "proxy-connection", "content-type", "content-encoding",
    "cookie", "authorization", "proxy-authorization",
})


def _obj(value: JSONValue) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], value)


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    return cast(list[dict[str, JSONValue]], value)


def _bad(code: str) -> Har2LoadError:
    return Har2LoadError(code, "Request cannot be lowered to the scalar runtime profile.", 3)


def _slot(binding: dict[str, JSONValue], encoding: str) -> RuntimeSlot:
    return RuntimeSlot(cast(str, binding["id"]), cast(str, binding["variable_id"]), encoding)


class _Template:
    def __init__(self) -> None:
        self.parts: list[str | RuntimeSlot] = []
        self.pending: list[str] = []
        self.size = 0

    def add(self, value: str | RuntimeSlot) -> None:
        if isinstance(value, str):
            self.size += len(value.encode("utf-8"))
            if self.size > MAX_RUNTIME_FIELD_BYTES:
                raise _bad("GENERATION_RUNTIME_LIMIT")
            self.pending.append(value)
        else:
            self.flush()
            self.parts.append(value)

    def flush(self) -> None:
        if self.pending:
            text = "".join(self.pending)
            if text:
                self.parts.append(text)
            self.pending.clear()

    def finish(self) -> RuntimeTemplate:
        self.flush()
        return RuntimeTemplate(tuple(self.parts) or ("",))


def _indexed(
    bindings: list[dict[str, JSONValue]], area: str,
) -> dict[int, dict[str, JSONValue]]:
    return {cast(int, _obj(b["location"])["index"]): b for b in bindings
            if _obj(b["location"])["area"] == area}


def _encoded_fields(
    output: _Template, text: str, replacements: dict[int, dict[str, JSONValue]],
) -> None:
    if not replacements:
        output.add(text)
        return
    index = 0
    for position, field in enumerate(text.split("&")):
        if position:
            output.add("&")
        if not field:
            continue
        if index in replacements:
            output.add(field.partition("=")[0] + "=")
            output.add(_slot(replacements[index], "form"))
        else:
            output.add(field)
        index += 1


def _url(request: NativeRequest, bindings: list[dict[str, JSONValue]]) -> RuntimeTemplate:
    output = _Template()
    parsed = urlsplit(request.url)
    output.add(parsed.scheme + "://" + parsed.netloc)
    replacements = _indexed(bindings, "request_path")
    for position, segment in enumerate(parsed.path.split("/")):
        if position:
            output.add("/")
        if position - 1 in replacements:
            output.add(_slot(replacements[position - 1], "path"))
        else:
            output.add(segment)
    if "?" in request.url:
        output.add("?")
        _encoded_fields(output, parsed.query, _indexed(bindings, "request_query"))
    return output.finish()


def _json(
    output: _Template, value: JSONValue, replacements: dict[str, dict[str, JSONValue]],
    pointer: str = "",
) -> None:
    if pointer in replacements:
        output.add(_slot(replacements[pointer], "json"))
    elif isinstance(value, (dict, list)):
        output.add("{" if isinstance(value, dict) else "[")
        keys = sorted(value) if isinstance(value, dict) else range(len(value))
        for index, key in enumerate(keys):
            if index:
                output.add(",")
            if isinstance(value, dict):
                name = cast(str, key)
                output.add(canonical_bytes(name).decode() + ":")
                token, child = name.replace("~", "~0").replace("/", "~1"), value[name]
            else:
                token, child = str(key), value[cast(int, key)]
            _json(output, child, replacements, pointer + "/" + token)
        output.add("}" if isinstance(value, dict) else "]")
    else:
        output.add(canonical_bytes(value).decode())


def _body(request: NativeRequest, bindings: list[dict[str, JSONValue]]) -> RuntimeTemplate | None:
    if request.body is None:
        return None
    output = _Template()
    text = request.body.decode("utf-8")
    json_bindings = {cast(str, _obj(b["location"])["pointer"]): b for b in bindings
                     if _obj(b["location"])["area"] == "request_json"}
    text_bindings = [b for b in bindings if _obj(b["location"])["area"] == "request_text"]
    if json_bindings:
        _json(output, parse_json_text(text), json_bindings)
    elif text_bindings:
        position = 0
        for binding in sorted(text_bindings, key=lambda b: cast(int, _obj(b["location"])["start"])):
            location = _obj(binding["location"])
            output.add(text[position:cast(int, location["start"])])
            output.add(_slot(binding, "text"))
            position = cast(int, location["end"])
        output.add(text[position:])
    else:
        _encoded_fields(output, text, _indexed(bindings, "request_form"))
    return output.finish()


def _header_controls(raw: dict[str, JSONValue]) -> set[str]:
    return set(_CONTROL_HEADERS) | {
        token.strip().lower() for h in _objects(raw.get("headers", []))
        if cast(str, h["name"]).lower() == "connection"
        for token in cast(str, h["value"]).split(",")
    }


def _headers(
    request: NativeRequest, bindings: list[dict[str, JSONValue]], raw: dict[str, JSONValue],
) -> tuple[tuple[str, RuntimeTemplate], ...]:
    replacements = _indexed(bindings, "request_header")
    # Only non-control headers can be runtime destinations. Their original order
    # survives materialization; repeated names must not collapse to a dictionary.
    by_name: dict[str, list[dict[str, JSONValue] | None]] = {}
    controls = _header_controls(raw)
    for index, header in enumerate(_objects(raw.get("headers", []))):
        name = cast(str, header["name"]).lower()
        if name not in controls:
            by_name.setdefault(name, []).append(replacements.get(index))
    result = []
    positions: dict[str, int] = {}
    for name, value in request.headers:
        lower = name.lower()
        position = positions.get(lower, 0)
        positions[lower] = position + 1
        binding = by_name[lower][position] if lower in by_name else None
        result.append((name, RuntimeTemplate((_slot(binding, "header") if binding else value,))))
    return tuple(result)


class GenerationPlanner:
    """Select static or runtime lowering without generating or reading live inputs."""

    def build(
        self, capture: Capture, grouping: Grouping, scenario: dict[str, JSONValue],
    ) -> RequestPlan:
        parameters = _obj(scenario.get("parameters", ParameterModel.empty()))
        variables, extractors = (_objects(parameters[k]) for k in ("variables", "extractors"))
        extensions = _obj(scenario.get("target_extensions", TargetExtensions.empty()))
        if (not any(extensions.values()) and not extractors
                and all(_obj(v["source"])["kind"] == "literal" for v in variables)):
            return RequestMaterializer().build(capture, grouping, scenario)
        ordering = _obj(TargetExtensions.plan(extensions, grouping)["targets"])
        ordered: dict[str, tuple[dict[str, JSONValue], ...]] = {}
        for kind in ("blocks", "helpers"):
            index = {r["id"]: r for r in _objects(extensions[kind])}
            ordered[kind] = tuple(deepcopy(index[r["id"]]) for target in ordering.values()
                                  for r in _objects(_obj(target)[kind]))
        empty_runtime = RuntimePlan(tuple(deepcopy(variables)), tuple(deepcopy(extractors)), (),
            ordered["blocks"], ordered["helpers"], sha256(canonical_bytes(scenario)).hexdigest())
        # Remove declarations from a clone only to validate immutable request
        # prerequisites; runtime declarations are attached separately, never lost.
        base_model = deepcopy(scenario)
        base_model["parameters"] = ParameterModel.empty()
        base = RequestMaterializer().build(capture, grouping, base_model)
        selections = cast(list[JSONValue], scenario.get("representation_selections", []))
        views = SourceViews(capture, selections)
        bindings = _objects(parameters["bindings"])
        try:
            for binding in bindings:
                location = _obj(binding["location"])
                if location["area"] == "request_header":
                    exchange = views.exchange(cast(str, binding["exchange_id"]))
                    raw = _obj(_obj(exchange.raw)["request"])
                    header = _objects(raw.get("headers", []))[cast(int, location["index"])]
                    if cast(str, header["name"]).lower() in _header_controls(raw):
                        raise _bad("GENERATION_RUNTIME_HEADER_UNSUPPORTED")
            BindingLocations().validate(parameters, capture, selections)
            if any(d.severity == "error" for d in base.diagnostics):
                return replace(base, runtime=empty_runtime)
            requests = []
            for request in base.requests:
                selected = [b for b in bindings if b["exchange_id"] == request.id]
                raw = _obj(_obj(views.exchange(request.id).raw)["request"])
                requests.append(RuntimeRequest(request.id, _url(request, selected),
                    _headers(request, selected, raw), _body(request, selected)))
            runtime = replace(empty_runtime, requests=tuple(requests))
            decisions: tuple[JSONValue, ...] = tuple({
                "kind": "runtime_binding_planned", "binding_id": b["id"],
                "variable_id": b["variable_id"], "exchange_id": b["exchange_id"],
                "location": deepcopy(b["location"]),
            } for b in bindings)
            return replace(base, runtime=runtime, decisions=base.decisions + decisions)
        except Har2LoadError as error:
            return replace(base, diagnostics=base.diagnostics + (
                Diagnostic(error.code, "error", str(error)),),
                runtime=empty_runtime)
