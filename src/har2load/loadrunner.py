"""Pure static C scaffold construction; no template, native validation, or execution."""

from collections.abc import Iterable, Iterator
from hashlib import sha256

from har2load import loadrunner_extensions as extensions
from har2load import loadrunner_runtime as runtime
from har2load import loadrunner_sources as sources
from har2load.domain import Diagnostic, JSONValue
from har2load.generation_types import (
    GENERATION_PROFILE,
    MAX_GENERATED_BYTES,
    NativeGroup,
    NativeRequest,
    RequestPlan,
    TargetBuild,
)
from har2load.runtime_sources import GENERATOR_POLICY, INPUT_POLICY
from har2load.runtime_types import (
    EXTENSION_RUNTIME_PROFILE,
    RUNTIME_PROFILE,
    RUNTIME_STATUS_POLICY,
    RuntimePlan,
    RuntimeRequest,
    RuntimeTemplate,
)

_TARGET = "loadrunner"
_ACTION = "loadrunner/Action.c"
_ARRAY_WIDTH = 16
_HEX_BYTE_WIDTH = 6
_PRINTABLE_MIN = 32
_PRINTABLE_MAX = 126
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
_DOC_ROOT = (
    "https://admhelp.microfocus.com/vugen/en/25.1-25.3/help/function_reference/Content/FuncRef/"
)
_DOC_PATHS = (
    "web/lrFr_web_custom_request.htm",
    "web/etc/lrFr_web_Raw_Body_Attribute.htm",
    "web/lrFr_web_add_header.htm",
    "web/lrFr_web_set_option.htm",
    "web/lrFr_web_cleanup_cookies.htm",
    "web/lrFr_web_cache_cleanup.htm",
    "c_vuser/lrFr_lr_continue_on_error.htm",
    "_Direct/lrFuncRef_allp_Error_Cont_Options.htm",
    "c_vuser/lrFr_lr_start_transaction.htm",
    "c_vuser/lrFr_lr_end_transaction.htm",
)
_GLOBALS = """#ifndef H2L_GLOBALS_H
#define H2L_GLOBALS_H
/* Native SDK headers must come from the selected, verified VuGen installation. */
#include "lrun.h"
#include "web_api.h"
#endif
"""
_INIT = """#include "globals.h"

int vuser_init(void)
{
    lr_continue_on_error(LR_ON_ERROR_CONTINUE);
    /* Construction intent only: option return values are unchecked. */
    web_set_option("DownloadNonHtmlResources", "No", LAST);
    web_set_option("MaxRedirectionDepth", "0", LAST);
    return 0;
}
"""
_END = """#include "globals.h"

int vuser_end(void)
{
    return 0;
}
"""
_README = """Har2Load static_literals_v1: intermediate C source scaffold

This is not a complete VuGen project. Generated means intermediate files only.
Do not assume native compilation, compatibility, replay parity, or readiness.
Do not start load from this scaffold. No compilation or execution was performed.
The VuGen version is unknown; a verified Web HTTP/HTML template is pending.
No .usr, default.cfg, run-logic configuration, or native SDK headers are supplied.
Select and verify a real native template and its runtime settings before use.
System C compiler checks are not a substitute for native VuGen validation.

Public 25.1-25.3 documentation supplies API construction facts only; it is not
an agreed target version or evidence of compatibility. No sample code was copied.
Request bytes and headers are private/unredacted. Protect these artifacts.
Runtime variables, extraction, author code, and helpers are outside this profile.

RAW_BODY uses a non-NULL byte buffer even for an explicitly empty body. Its bytes
are not parameterized. EncType sets request Content-Type without encoding bytes;
an absent Content-Type emits EncType= to suppress the implicit POST default.
Headers use web_add_header for the next request only. Duplicate names and braces
in parameterizable attributes are rejected, not merged or silently interpreted.

Initialization requests DownloadNonHtmlResources=No and MaxRedirectionDepth=0.
Options saved at initialization are documented as restored at iteration start.
Option and service-function return values are unchecked: acceptance of zero,
redirect behavior, automatic headers, and actual wire parity remain unverified.
A redirect limit can produce a debug failure; continue-on-error does not retry,
skip an iteration, or explicitly add HTTP calls. Mode=HTTP and Resource=0 are used.
Cookie/cache cleanup runs before requests on every Action iteration; the native
cookie jar can then accept fresh server cookies. Captured authentication and
cookie requirements must have been rejected during common plan validation.
Transactions use LR_AUTO, not proof of business success or native validation.
Native runtime configuration may still affect network behavior; review it first.

Sources (API facts only):
"""


def _metadata(plan: RequestPlan | None = None) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "artifact_kind": "c_source_scaffold", "project_status": "incomplete",
        "compatibility": "unverified", "native_validation": "not_run",
        "execution": "not_run", "profile": GENERATION_PROFILE, "profile_version": "1",
        "template_status": "pending", "target_version": None,
        "sources": [_DOC_ROOT + path for path in _DOC_PATHS],
    }
    if plan is not None and plan.runtime is not None:
        result.update(profile=RUNTIME_PROFILE, profile_version="3",
                      limitations=list(runtime.LIMITATIONS),
                      sources=[_DOC_ROOT + path for path in (*_DOC_PATHS, *runtime.DOC_PATHS)],
                      json_failure_wire_policy="quoted_error_marker",
                      runtime_state_isolation="per_vuser_native_verification_pending")
        result["native_status_policy"] = RUNTIME_STATUS_POLICY
        result.update(input_policy=INPUT_POLICY, generator_policy=GENERATOR_POLICY)
        if extensions.enabled(plan.runtime):
            result.update(profile=EXTENSION_RUNTIME_PROFILE, profile_version="2",
                          output_commit_policy="per_output_validated_v1")
    return result


def _error(code: str, message: str, entity_id: str | None = None) -> Diagnostic:
    return Diagnostic(code, "error", message, entity_id=entity_id, target=_TARGET)


def _membership_valid(plan: RequestPlan) -> bool:
    request_ids = [request.id for request in plan.requests]
    group_ids = [group.id for group in plan.groups]
    flattened = [item for group in plan.groups for item in group.exchange_ids]
    if len(set(request_ids)) != len(request_ids) or len(set(group_ids)) != len(group_ids):
        return False
    if flattened != request_ids:
        return False
    owners = {item: group.id for group in plan.groups for item in group.exchange_ids}
    return all(owners[request.id] == request.group_id for request in plan.requests)


def _request_errors(request: NativeRequest) -> Iterator[Diagnostic]:
    names = [name.casefold() for name, _ in request.headers]
    if len(set(names)) != len(names):
        yield _error("LOADRUNNER_DUPLICATE_HEADER_UNSUPPORTED",
                     "Repeated header names cannot be preserved by this profile.", request.id)
    values = [request.url, request.method, *(v for pair in request.headers for v in pair)]
    if any("{" in value or "}" in value for value in values):
        yield _error("LOADRUNNER_METASYNTAX_UNSUPPORTED",
                     "Braces in parameterizable attributes are unsupported.", request.id)
    if any("\x00" in value or any(_SURROGATE_MIN <= ord(c) <= _SURROGATE_MAX for c in value)
           for value in values):
        yield _error("LOADRUNNER_TEXT_UNSUPPORTED",
                     "Native string arguments cannot contain NUL or surrogate code points.",
                     request.id)


def _diagnostics(plan: RequestPlan) -> tuple[Diagnostic, ...]:
    errors = list(plan.diagnostics)
    if not _membership_valid(plan):
        errors.append(_error("LOADRUNNER_PLAN_INVALID",
                             "Group membership must cover requests exactly once in plan order."))
    for request in plan.requests:
        if plan.runtime is None:
            errors.extend(_request_errors(request))
        else:
            errors.extend(_request_errors(NativeRequest(request.id, request.group_id,
                          request.method, "", tuple((name, "") for name, _ in request.headers))))
    if plan.runtime is not None:
        errors.extend(runtime.diagnostics(plan.runtime))
        if [r.id for r in plan.requests] != [r.id for r in plan.runtime.requests]:
            errors.append(_error("LOADRUNNER_RUNTIME_PLAN_INVALID",
                                 "Runtime request order differs."))
    return tuple(errors)


def _literal(value: str) -> str:
    # Three-digit octal escapes cannot absorb following digits. Escape question
    # marks too, so old C preprocessors cannot interpret captured trigraphs.
    parts = []
    for byte in value.encode("utf-8"):
        character = chr(byte)
        if character in ('"', "\\"):
            parts.append("\\" + character)
        elif _PRINTABLE_MIN <= byte <= _PRINTABLE_MAX and character != "?":
            parts.append(character)
        else:
            parts.append(f"\\{byte:03o}")
    return '"' + "".join(parts) + '"'


def _marker(kind: str, entity_id: str) -> str:
    digest = sha256(entity_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"H2L_{kind}_{digest}"


def _mapping(kind: str, entity_id: str, native_name: str) -> dict[str, JSONValue]:
    marker = _marker(kind, entity_id)
    return {"kind": kind.lower(), "entity_kind": kind.lower(), "entity_id": entity_id,
            "artifact": _ACTION, "marker": marker, "native_name": native_name,
            "locator": {"function": "Action", "marker": marker}}


def _source_map(plan: RequestPlan) -> tuple[dict[str, JSONValue], ...]:
    groups = [_mapping("GROUP", group.id, f"TC_{index:04d}")
              for index, group in enumerate(plan.groups, start=1)]
    requests = [_mapping("REQUEST", request.id, f"REQ_{index:04d}")
                for index, request in enumerate(plan.requests, start=1)]
    for entry, request in zip(requests, plan.requests, strict=True):
        entry.update(exchange_id=request.id, group_id=request.group_id)
    for entry, group in zip(groups, plan.groups, strict=True):
        entry["group_id"] = group.id
    return tuple(groups + requests) + (tuple(runtime.source_map(plan.runtime)) +
                                      tuple(extensions.source_map(plan.runtime))
                                      if plan.runtime is not None else ())


def _body_array(body: bytes, index: int) -> Iterator[str]:
    yield f"static unsigned char H2L_BODY_{index:04d}[] = {{"
    # A sentinel preserves the non-NULL pointer requirement without sending it.
    data = body or b"\x00"
    for offset in range(0, len(data), _ARRAY_WIDTH):
        chunk = data[offset:offset + _ARRAY_WIDTH]
        yield "    " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ","
    yield "};"


def _request_lines(request: NativeRequest, index: int) -> Iterator[str]:
    yield f"    /* {_marker('REQUEST', request.id)} */"
    mime = ""
    for name, value in request.headers:
        if name.casefold() == "content-type":
            mime = value
        else:
            yield f"    web_add_header({_literal(name)}, {_literal(value)});"
    yield f'    web_custom_request("REQ_{index:04d}",'
    for attribute in ("URL=" + request.url, "Method=" + request.method,
                      "Resource=0", "Mode=HTTP", "EncType=" + mime):
        yield f"        {_literal(attribute)},"
    if request.body is not None:
        yield (f"        RAW_BODY_START, (char*)H2L_BODY_{index:04d}, "
               f"(int){len(request.body)}, RAW_BODY_END,")
    yield "        LAST);"


def _group_lines(group: NativeGroup, index: int,
                 requests: dict[str, tuple[int, NativeRequest]]) -> Iterator[str]:
    yield f"    /* {_marker('GROUP', group.id)} */"
    yield f'    lr_start_transaction("TC_{index:04d}");'
    for exchange_id in group.exchange_ids:
        request_index, request = requests[exchange_id]
        yield from _request_lines(request, request_index)
    yield f'    lr_end_transaction("TC_{index:04d}", LR_AUTO);'


def _action_lines(plan: RequestPlan) -> Iterator[str]:
    if plan.runtime is not None:
        yield from _runtime_action(plan, plan.runtime)
        return
    yield '#include "globals.h"'
    for index, request in enumerate(plan.requests, start=1):
        if request.body is not None:
            yield from _body_array(request.body, index)
    yield "\nint Action(void)\n{"
    yield "    lr_continue_on_error(LR_ON_ERROR_CONTINUE);"
    yield "    web_cleanup_cookies();"
    yield "    web_cache_cleanup();"
    requests = {request.id: (index, request)
                for index, request in enumerate(plan.requests, start=1)}
    for index, group in enumerate(plan.groups, start=1):
        yield from _group_lines(group, index, requests)
    yield "    return 0;\n}"


def _runtime_fields(request: RuntimeRequest) -> list[tuple[str, RuntimeTemplate, str]]:
    fields = [("url", request.url, "URL=")]
    mime = RuntimeTemplate(("",))
    for index, (name, template) in enumerate(request.headers):
        if name.lower() == "content-type":
            mime = template
        else:
            fields.append((f"header_{index}", template, ""))
    fields.append(("mime", mime, "EncType="))
    if request.body is not None:
        fields.append(("body", request.body, ""))
    return fields


def _runtime_buffers(request: RuntimeRequest, model: RuntimePlan) -> Iterator[str]:
    fields = _runtime_fields(request)
    for name, template, prefix in fields:
        fallback = _literal(prefix + runtime.fallback(template, model))
        yield f"    const char *fallback_{name} = {fallback};"
        yield f"    h2l_buffer buffer_{name}; char *{name};"
    yield "    int h2l_ok = 1, h2l_rc; char *h2l_response; h2l_json document;"


def _runtime_materialize(request: RuntimeRequest) -> Iterator[str]:
    for name, template, prefix in _runtime_fields(request):
        yield from runtime.template_lines(template, "buffer_" + name, prefix)
        yield f"    {name} = h2l_text(&buffer_{name}, fallback_{name});"


def _runtime_http(request: RuntimeRequest, native: NativeRequest, index: int) -> Iterator[str]:
    for position, (name, _) in enumerate(request.headers):
        if name.lower() != "content-type":
            yield f"    if (web_add_header({_literal(name)}, header_{position}) != LR_PASS)"
            yield '        h2l_fail("ERROR_REQUEST_HEADER");'
    yield f'    h2l_rc = web_custom_request("REQ_{index:04d}", url,'
    yield f'        {_literal("Method=" + native.method)}, "Resource=0", "Mode=HTTP", mime,'
    if request.body is not None:
        yield "        RAW_BODY_START, body, (int)strlen(body), RAW_BODY_END,"
    yield "        LAST);"
    yield '    if (h2l_rc != LR_PASS) h2l_fail("ERROR_REQUEST_HTTP");'


def _runtime_request(request: RuntimeRequest, native: NativeRequest,
                     index: int, model: RuntimePlan) -> Iterator[str]:
    yield f"    /* {_marker('REQUEST', request.id)} */\n    {{"
    yield from _runtime_buffers(request, model)
    yield from runtime.reset_lines(model, ("request",))
    yield from runtime.event_lines(model, "before_request", request.id)
    yield from extensions.event_lines(model, "before_request", request.id)
    yield from runtime.registration(model, request.id)
    yield from _runtime_materialize(request)
    yield from _runtime_http(request, native, index)
    yield from runtime.extraction_lines(model, request.id)
    yield from runtime.event_lines(model, "after_response", request.id)
    yield from extensions.event_lines(model, "after_response", request.id)
    yield from runtime.reset_lines(model, ("request",))
    for name, _, _ in _runtime_fields(request):
        yield f"    free(buffer_{name}.data);"
    yield "    }"


def _runtime_groups(plan: RequestPlan, model: RuntimePlan) -> Iterator[str]:
    requests = {r.id: (i, r, live) for i, (r, live) in
                enumerate(zip(plan.requests, model.requests, strict=True), start=1)}
    for index, group in enumerate(plan.groups, start=1):
        yield f"    /* {_marker('GROUP', group.id)} */"
        yield f'    lr_start_transaction("TC_{index:04d}");'
        for entity in group.exchange_ids:
            number, native, live = requests[entity]
            yield from _runtime_request(live, native, number, model)
        yield f'    lr_end_transaction("TC_{index:04d}", LR_AUTO);'
        yield '    h2l_transaction_failed = 0;'


def _runtime_action(plan: RequestPlan, model: RuntimePlan) -> Iterator[str]:
    yield '#include "globals.h"'
    yield from runtime.helpers()
    yield from runtime.declarations(model)
    yield from extensions.definitions(model)
    yield from runtime.session_lines(model)
    yield "int Action(void) {"
    yield "    lr_continue_on_error(LR_ON_ERROR_CONTINUE);"
    yield "    web_cleanup_cookies();\n    web_cache_cleanup();"
    yield from runtime.reset_lines(model, ("iteration", "request"))
    yield from runtime.event_lines(model, "iteration_start")
    yield from _runtime_groups(plan, model)
    yield "    return 0;\n}"


class _OutputLimit(Exception):
    """Internal control flow: never publish partial artifacts."""


class _Output:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.used = 0

    def add(self, path: str, lines: Iterable[str]) -> None:
        data = bytearray()
        for line in lines:
            encoded = (line + "\n").encode("utf-8")
            if self.used + len(encoded) > MAX_GENERATED_BYTES:
                raise _OutputLimit
            self.used += len(encoded)
            data.extend(encoded)
        self.files[path] = bytes(data)


def _minimum_size(plan: RequestPlan) -> int:
    if plan.runtime is not None:
        return sum(len(part) for request in plan.runtime.requests
                   for _, template, _ in _runtime_fields(request)
                   for part in template.parts if isinstance(part, str))
    body_bytes = sum(len(r.body or b"") * _HEX_BYTE_WIDTH for r in plan.requests)
    attributes = sum(len(r.url) + len(r.method) for r in plan.requests)
    headers = sum(len(name) + len(value) for r in plan.requests for name, value in r.headers)
    return body_bytes + attributes + headers


def _files(plan: RequestPlan) -> dict[str, bytes]:
    # Reject certain oversized inputs before expanding C tokens and escapes.
    if _minimum_size(plan) > MAX_GENERATED_BYTES:
        raise _OutputLimit
    output = _Output()
    globals_text, init, end, readme = _scaffold_text(plan.runtime is not None)
    if plan.runtime is not None and extensions.enabled(plan.runtime):
        readme = readme.replace(RUNTIME_PROFILE, EXTENSION_RUNTIME_PROFILE)
    output.add("loadrunner/globals.h", globals_text.splitlines())
    output.add("loadrunner/vuser_init.c", init.splitlines())
    output.add("loadrunner/vuser_end.c", end.splitlines())
    output.add("loadrunner/README.txt", readme.splitlines())
    output.add(_ACTION, _action_lines(plan))
    if plan.runtime is not None:
        for path, lines in sources.artifacts(plan.runtime):
            output.add(path, lines)
    return output.files


def _scaffold_text(live: bool) -> tuple[str, str, str, str]:
    globals_text, init, end, readme = _GLOBALS, _INIT, _END, _README
    paths: tuple[str, ...] = _DOC_PATHS
    if live:
        globals_text = globals_text.replace("#endif", "void h2l_session_start(void);\n"
                                            "void h2l_session_end(void);\n#endif")
        init = init.replace("    return 0;", "    h2l_session_start();\n    return 0;")
        end = end.replace("    return 0;", "    h2l_session_end();\n    return 0;")
        readme = readme.replace(GENERATION_PROFILE, RUNTIME_PROFILE).replace(
            "Runtime variables, extraction, author code, and helpers are outside this profile.",
            "Runtime subset and limitations:\n" + "\n".join(runtime.LIMITATIONS))
        readme = readme.replace("Option and service-function return values are unchecked:",
                                "Initialization/cookie/cache returns remain unchecked;")
        paths += runtime.DOC_PATHS
        readme += sources.INSTRUCTIONS
    return globals_text, init, end, readme + "\n".join(_DOC_ROOT + path for path in paths) + "\n"


class LoadRunnerGenerator:
    """Render a validated literal plan without reading files or native resources."""

    def generate(self, plan: RequestPlan) -> TargetBuild:
        diagnostics = _diagnostics(plan)
        if any(d.severity == "error" for d in diagnostics):
            return TargetBuild(_TARGET, diagnostics=diagnostics, metadata=_metadata(plan))
        try:
            files = _files(plan)
        except _OutputLimit:
            diagnostic = _error("LOADRUNNER_OUTPUT_LIMIT",
                                "Generated files exceed the byte budget.")
            return TargetBuild(_TARGET, diagnostics=(*diagnostics, diagnostic),
                               metadata=_metadata(plan))
        return TargetBuild(_TARGET, files, _source_map(plan), diagnostics, _metadata(plan))
