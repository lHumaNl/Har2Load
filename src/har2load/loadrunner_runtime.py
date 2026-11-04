"""Bounded scalar runtime source construction, never native execution."""

import json
from collections.abc import Iterator
from typing import cast
from urllib.parse import quote, quote_plus

from har2load import loadrunner_sources as sources
from har2load.domain import Diagnostic, JSONValue
from har2load.extension_runtime import capability_diagnostics
from har2load.extension_runtime import declarations as extension_records
from har2load.extraction import MAX_MATCHES, parse_json_path
from har2load.loadrunner_json import parser_lines
from har2load.runtime_types import (
    MAX_RUNTIME_FIELD_BYTES,
    MAX_RUNTIME_VALUE_BYTES,
    RuntimePlan,
    RuntimeSlot,
    RuntimeTemplate,
)
from har2load.serialization import canonical_bytes
from har2load.values import matches_type

DOC_PATHS = (
    "web/lrFr_web_reg_save_param_ex.htm", "web/lrFr_web_set_max_html_param_len.htm",
    "c_vuser/lrFr_lr_eval_string_ext.htm", "c_vuser/lrFr_lr_eval_string_ext_free.htm",
    "c_vuser/lrFr_lr_save_string.htm",
    "c_vuser/lrFr_lr_end_transaction.htm", "c_vuser/lrFr_lr_output_message.htm",
    "c_vuser/lrFr_lr_error_message.htm", "c_vuser/lrFr_lr_whoami.htm",
)
LIMITATIONS = (
    "Literal/extracted scalars, bounded native inputs/generators and scalar_body_v1 bodies.",
    "Native parameter files support a conservative ASCII subset; SDK presence is unverified.",
    "Authored bodies are not sandboxed; no crash, undefined-behavior or loop containment.",
    "Dependencies are unsupported; helper contracts and native effects remain author-owned.",
    "Body boundary_v1 and strict scalar JSON subset only; header/cookie extractors blocked.",
    "UTF-8 without NUL; response transcoding/native buffer semantics unverified.",
    "JSON: depth 64, 4096 nodes, 1024 keys/object, absolute exponent <=10000.",
    "C static state requires native per-Vuser isolation; template verification pending.",
    f"Values <= {MAX_RUNTIME_VALUE_BYTES} bytes; bodies/fields <= {MAX_RUNTIME_FIELD_BYTES} bytes.",
    "Unsafe live headers become markers; failed JSON scalars become quoted ERROR_name strings.",
    "Header rejection is binding-local; correlation errors log markers, transactions use LR_AUTO.",
    "Diagnostics use lr_output_message; lr_error_message can fail open native transactions.",
    "Fields use worst-case encoded slot sizes; small live values may still be blocked.",
    "Numeric lexemes retained without binary floating-point conversion.",
    "No complete VuGen project, agreed version, native compilation or wire verification.",
)
_ENCODINGS = {"text": 0, "header": 1, "path": 2, "form": 3, "json": 4}
_EXPANSION = {"text": 1, "header": 1, "path": 3, "form": 3, "json": 6}
_TYPES = {"string": 1, "number": 2, "boolean": 3, "null": 4, "integer": 7}
_HELPERS = r'''
#include <stdlib.h>
#include <string.h>
typedef struct { char *data; const char *error; int type, integral; } h2l_value;
typedef struct { char *data; size_t size, capacity; int failed; } h2l_buffer;
/* Historical diagnostic flag only; never used to override native transaction status. */
static int h2l_transaction_failed = 0;
static void h2l_fail(const char *message)
{ h2l_transaction_failed = 1; lr_output_message("%s", message); }
static void h2l_reset(h2l_value *v)
{ free(v->data); v->data = NULL; v->type = 0; v->integral = 0; }
static void h2l_error(h2l_value *v) { h2l_reset(v); h2l_fail(v->error); }
static int h2l_utf8(const unsigned char *s, size_t n)
{
    size_t i = 0; unsigned long cp, minimum; unsigned int more, c;
    while (i < n) {
        c = s[i++]; if (!c) return 0; if (c < 128) continue;
        if (c >= 194 && c <= 223) { more = 1; cp = c & 31; minimum = 128; }
        else if (c >= 224 && c <= 239) { more = 2; cp = c & 15; minimum = 2048; }
        else if (c >= 240 && c <= 244) { more = 3; cp = c & 7; minimum = 65536; }
        else return 0;
        if (more > n - i) return 0;
        while (more--) { c = s[i++]; if ((c & 192) != 128) return 0;
            cp = (cp << 6) | (c & 63); }
        if (cp < minimum || cp > 1114111 || (cp >= 55296 && cp <= 57343)) return 0;
    }
    return 1;
}
static void h2l_set(h2l_value *v, const char *s, size_t n, int type, int integral)
{
    h2l_reset(v);
    if (!s || n > H2L_VALUE_LIMIT || !h2l_utf8((const unsigned char*)s, n)) {
        h2l_error(v); return;
    }
    v->data = (char*)malloc(n + 1);
    if (!v->data) { h2l_error(v); return; }
    memcpy(v->data, s, n); v->data[n] = 0; v->type = type; v->integral = integral;
}
static void h2l_accept(h2l_value *v, const h2l_value *source, int expected)
{
    if (!source || !source->data || !(source->type == expected ||
        (expected == 7 && source->type == 2 && source->integral))) {
        h2l_error(v); return;
    }
    h2l_set(v, source->data, strlen(source->data), source->type, source->integral);
}
static void h2l_open(h2l_buffer *b, size_t capacity)
{
    b->size = 0; b->capacity = capacity; b->failed = 0;
    b->data = (char*)malloc(capacity + 1);
    if (!b->data) { b->failed = 1; h2l_fail("ERROR_RUNTIME_ALLOCATION"); }
    else b->data[0] = 0;
}
static void h2l_append(h2l_buffer *b, const char *s, size_t n)
{
    if (b->failed) return;
    if (n > b->capacity - b->size) {
        b->failed = 1; h2l_fail("ERROR_RUNTIME_FIELD_LIMIT"); return;
    }
    memcpy(b->data + b->size, s, n); b->size += n; b->data[b->size] = 0;
}
static char *h2l_text(h2l_buffer *b, const char *fallback)
{ return b->failed ? (char*)fallback : b->data; }
static int h2l_header_safe(const unsigned char *s)
{
    for (; *s; ++s) if (*s < 32 || *s > 126 || strchr("{}\\%", *s)) {
        return 0;
    }
    return 1;
}
static int h2l_unreserved(unsigned char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '.' || c == '_' || c == '~';
}
static void h2l_encoded_byte(h2l_buffer *b, unsigned char c, int mode)
{
    const char *hex = "0123456789ABCDEF"; char out[6]; size_t n = 1;
    out[0] = (char)c;
    if (mode == 4 && (c == '"' || c == '\\')) { out[0] = '\\'; out[1] = c; n = 2; }
    else if (mode == 4 && c < 32) {
        memcpy(out, "\\u00", 4); out[4] = hex[c >> 4]; out[5] = hex[c & 15]; n = 6;
    } else if (mode == 3 && c == ' ') out[0] = '+';
    else if ((mode == 2 || mode == 3) && !h2l_unreserved(c)) {
        out[0] = '%'; out[1] = hex[c >> 4]; out[2] = hex[c & 15]; n = 3;
    }
    h2l_append(b, out, n);
}
static void h2l_slot(h2l_buffer *b, h2l_value *v, int mode)
{
    int quoted = mode == 4 && (!v->data || v->type == 1);
    const unsigned char *s = (const unsigned char*)(v->data ? v->data : v->error);
    if (!v->data) h2l_fail(v->error);
    if (mode == 1 && !h2l_header_safe(s)) {
        h2l_fail(v->error); s = (const unsigned char*)v->error;
    }
    if (quoted) h2l_append(b, "\"", 1);
    for (; *s; ++s) h2l_encoded_byte(b, *s, mode);
    if (quoted) h2l_append(b, "\"", 1);
}
static char *h2l_capture(int ok)
{
    char *native = NULL, *copy = NULL; const char *owned; unsigned long n = 0; int rc;
    if (!ok) return NULL;
    rc = lr_eval_string_ext("{H2L_RESPONSE}", 14, &native, &n, 0, 0, -1);
    if (rc == LR_PASS && native && n <= H2L_FIELD_LIMIT &&
        h2l_utf8((const unsigned char*)native, n)) {
        copy = (char*)malloc((size_t)n + 1);
        if (copy) { memcpy(copy, native, n); copy[n] = 0; }
    }
    /* The documented eval contract requires cleanup even for a NULL result. */
    owned = native; lr_eval_string_ext_free(&owned);
    return copy;
}
static void h2l_boundary(h2l_value *v, const char *body, const char *left,
                         const char *right, unsigned long occurrence, int exact, int empty)
{
    const char *start, *end, *chosen = NULL; size_t length = 0; unsigned long count = 0;
    h2l_reset(v);
    while (body && (start = strstr(body, left)) != NULL) {
        start += strlen(left); end = strstr(start, right); if (!end) break;
        if (++count > H2L_MATCH_LIMIT) { h2l_error(v); return; }
        if (count == occurrence) { chosen = start; length = (size_t)(end - start); }
        body = end + strlen(right);
    }
    if (!chosen || (exact && count != 1) || (!empty && !length)) { h2l_error(v); return; }
    h2l_set(v, chosen, length, 1, 0);
}
'''


def obj(value: JSONValue) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], value)


def identifier(record: dict[str, JSONValue]) -> str:
    return cast(str, record["id"])


def symbol(kind: str, value: str) -> str:
    from har2load.loadrunner import _marker
    return _marker(kind, value)


def literal(value: str) -> str:
    from har2load.loadrunner import _literal
    return _literal(value)


def _error(code: str, entity: str) -> Diagnostic:
    return Diagnostic("LOADRUNNER_RUNTIME_" + code, "error",
                      "Declaration is outside the bounded LoadRunner runtime profile.",
                      entity_id=entity, target="loadrunner")


def capacity(template: RuntimeTemplate) -> int:
    return sum(len(p.encode("utf-8")) if isinstance(p, str) else
               MAX_RUNTIME_VALUE_BYTES * _EXPANSION.get(p.encoding, 6) + 2
               for p in template.parts)


def scalar(value: JSONValue) -> str:
    return value if isinstance(value, str) else canonical_bytes(value).decode("utf-8")


def _variable_errors(variable: dict[str, JSONValue], authored: bool) -> Iterator[Diagnostic]:
    source = obj(variable["source"])
    if source["kind"] not in ("literal", "extracted", "external", "generated") and not (
            authored and source["kind"] == "agent_code"):
        yield _error("SOURCE_UNSUPPORTED", identifier(variable))
    if source["kind"] == "literal":
        value = scalar(source["value"])
        if "\x00" in value or len(value.encode("utf-8")) > MAX_RUNTIME_VALUE_BYTES:
            yield _error("VALUE_LIMIT", identifier(variable))


def _template_errors(template: RuntimeTemplate, entity: str,
                     allowed: tuple[str, ...]) -> Iterator[Diagnostic]:
    if capacity(template) > MAX_RUNTIME_FIELD_BYTES:
        yield _error("FIELD_LIMIT", entity)
    for part in template.parts:
        if isinstance(part, RuntimeSlot):
            if part.encoding not in allowed:
                yield _error("ENCODING_UNSUPPORTED", part.binding_id)
        elif "\x00" in part or ("text" not in allowed and any(c in part for c in "{}\\")):
            yield _error("TEXT_UNSUPPORTED", entity)
        elif allowed == ("header",) and any(not " " <= c <= "~" for c in part):
            yield _error("HEADER_UNSUPPORTED", entity)


def _extractor_errors(ext: dict[str, JSONValue]) -> Iterator[Diagnostic]:
    selector = obj(ext["selector"])
    if (ext["area"] != "response_body" or selector["dialect"] not in
            ("boundary_v1", "json_path_subset_v1")):
        yield _error("EXTRACTOR_UNSUPPORTED", identifier(ext))
    elif selector["dialect"] == "json_path_subset_v1":
        tokens = parse_json_path(cast(str, selector["expression"]))
        if any(isinstance(token, str) and "\x00" in token for token in tokens):
            yield _error("SELECTOR_UNSUPPORTED", identifier(ext))
    elif any("\x00" in str(v) for v in selector.values()):
        yield _error("SELECTOR_UNSUPPORTED", identifier(ext))


def diagnostics(runtime: RuntimePlan) -> tuple[Diagnostic, ...]:
    authored = bool(extension_records(runtime, "loadrunner", "blocks"))
    errors = [e for v in runtime.variables for e in _variable_errors(v, authored)]
    errors.extend(capability_diagnostics(runtime, "loadrunner"))
    errors.extend(sources.diagnostics(runtime))
    for ext in runtime.extractors:
        errors.extend(_extractor_errors(ext))
    for request in runtime.requests:
        errors.extend(_template_errors(request.url, request.id, ("path", "form")))
        for name, template in request.headers:
            errors.extend(_template_errors(template, request.id, ("header",)))
            if name.lower() in ("content-type", "content-encoding") and any(
                    isinstance(p, RuntimeSlot) for p in template.parts):
                errors.append(_error("CONTENT_HEADER_UNSUPPORTED", request.id))
        if request.body is not None:
            errors.extend(_template_errors(request.body, request.id, ("text", "form", "json")))
    return tuple(errors)


def helpers() -> Iterator[str]:
    yield f"#define H2L_VALUE_LIMIT {MAX_RUNTIME_VALUE_BYTES}"
    yield f"#define H2L_FIELD_LIMIT {MAX_RUNTIME_FIELD_BYTES}"
    yield f"#define H2L_MATCH_LIMIT {MAX_MATCHES}"
    yield from _HELPERS.splitlines()
    yield from parser_lines()


def _comment(kind: str, entity: str) -> str:
    return f"/* {symbol(kind.upper(), entity)} */"


def declarations(runtime: RuntimePlan) -> Iterator[str]:
    yield from sources.declarations(runtime)
    for variable in runtime.variables:
        entity = identifier(variable)
        name = symbol("VALUE", entity)
        yield f"static h2l_value *{name}(void) {{"
        yield "    " + _comment("variable", entity)
        marker = literal("ERROR_" + str(variable["name"]))
        yield f"    static h2l_value value = {{ NULL, {marker}, 0, 0 }};"
        yield "    return &value;\n}"
        yield _comment("reset", entity)
        yield f"static void {symbol('RESET_FN', entity)}(void) {{"
        yield f"    h2l_reset({name}());\n}}"
        yield from _initializer(variable, name)


def _initializer(variable: dict[str, JSONValue], name: str) -> Iterator[str]:
    yield _comment("initialize", identifier(variable))
    yield f"static void {symbol('INIT_FN', identifier(variable))}(const h2l_value *value) {{"
    source = obj(variable["source"])
    if source["kind"] in ("external", "generated"):
        yield from sources.initializer(variable, name, _TYPES[cast(str, variable["value_type"])])
        yield "}"
        return
    if source["kind"] == "literal":
        value = source["value"]
        kind = _TYPES[cast(str, variable["value_type"])]
        kind = 2 if kind == 7 else kind
        integral = int(matches_type(value, "integer"))
        yield f"    h2l_value fixed = {{ {literal(scalar(value))}, NULL, {kind}, {integral} }};"
        yield "    value = &fixed;"
    yield f"    h2l_accept({name}(), value, {_TYPES[cast(str, variable['value_type'])]});"
    yield "}"


def reset_lines(runtime: RuntimePlan, scopes: tuple[str, ...]) -> Iterator[str]:
    for variable in runtime.variables:
        if variable["scope"] in scopes:
            yield f"    {symbol('RESET_FN', identifier(variable))}();"


def event_lines(runtime: RuntimePlan, event: str, exchange: str | None = None) -> Iterator[str]:
    for variable in runtime.variables:
        init = obj(variable["initialize"])
        if (init["event"] == event and init.get("exchange_id") == exchange
                and obj(variable["source"])["kind"] not in ("extracted", "agent_code")):
            yield f"    {symbol('INIT_FN', identifier(variable))}(NULL);"


def session_lines(runtime: RuntimePlan) -> Iterator[str]:
    yield "void h2l_session_start(void) {"
    yield "    h2l_transaction_failed = 0;"
    yield from reset_lines(runtime, ("session", "iteration", "request"))
    yield from sources.session_lines(runtime, start=True)
    yield from event_lines(runtime, "session_start")
    yield "}\nvoid h2l_session_end(void) {"
    yield from reset_lines(runtime, ("session", "iteration", "request"))
    yield from sources.session_lines(runtime, start=False)
    yield "}"


def fallback(template: RuntimeTemplate, runtime: RuntimePlan) -> str:
    names = {identifier(v): "ERROR_" + str(v["name"]) for v in runtime.variables}
    parts = []
    for part in template.parts:
        if isinstance(part, str):
            parts.append(part)
        else:
            value = names[part.variable_id]
            if part.encoding == "json":
                value = json.dumps(value)
            elif part.encoding in ("path", "form"):
                value = quote(value, safe="") if part.encoding == "path" else quote_plus(value)
            parts.append(value)
    return "".join(parts)


def template_lines(template: RuntimeTemplate, name: str, prefix: str = "") -> Iterator[str]:
    yield f"    h2l_open(&{name}, {capacity(template) + len(prefix)});"
    if prefix:
        yield f"    h2l_append(&{name}, {literal(prefix)}, {len(prefix)});"
    for part in template.parts:
        if isinstance(part, str):
            yield f"    h2l_append(&{name}, {literal(part)}, {len(part.encode('utf-8'))});"
        else:
            yield "    " + _comment("binding", part.binding_id)
            variable = symbol("VALUE", part.variable_id)
            yield f"    h2l_slot(&{name}, {variable}(), {_ENCODINGS[part.encoding]});"


def registration(runtime: RuntimePlan, exchange: str) -> Iterator[str]:
    producer_ids = {e["id"] for e in runtime.extractors if e["exchange_id"] == exchange}
    for variable in runtime.variables:
        if obj(variable["source"]).get("extractor_id") in producer_ids:
            yield f"    {symbol('RESET_FN', identifier(variable))}();"
    if producer_ids:
        yield '    h2l_ok = lr_save_string("", "H2L_RESPONSE") == LR_PASS;'
        yield f'    if (web_set_max_html_param_len("{MAX_RUNTIME_FIELD_BYTES}") != LR_PASS)'
        yield '        h2l_ok = 0;'
        yield '    if (web_reg_save_param_ex("ParamName=H2L_RESPONSE", "LB=", "RB=",'
        yield '        "NotFound=warning", SEARCH_FILTERS, "Scope=Body", LAST) != LR_PASS)'
        yield '        h2l_ok = 0;'


def _selector_lines(ext: dict[str, JSONValue]) -> Iterator[str]:
    selector = obj(ext["selector"])
    occurrence = min(cast(int, ext.get("occurrence", 1)), MAX_MATCHES + 1)
    empty = int(bool(ext["allow_empty"]))
    if selector["dialect"] == "boundary_v1":
        args = [literal(cast(str, selector[k])) for k in ("left", "right")]
        args += [str(occurrence), str(int(ext["cardinality"] == "exactly_one")), str(empty)]
        yield f"    h2l_boundary(&result, h2l_response, {', '.join(args)});"
    else:
        path = parse_json_path(cast(str, selector["expression"]))
        yield "    static const h2l_path path[] = {"
        for token in path:
            yield (f"        {{ {literal(token)}, 0 }}," if isinstance(token, str)
                   else f"        {{ NULL, {min(token, MAX_RUNTIME_FIELD_BYTES)}UL }},")
        yield "        { NULL, 0 }\n    };"
        yield f"    h2l_json_select(&result, &document, path, {len(path)}, {occurrence}, {empty});"


def _extractor_lines(runtime: RuntimePlan, ext: dict[str, JSONValue]) -> Iterator[str]:
    yield "    {"
    yield '    h2l_value result = { NULL, "ERROR_EXTRACTOR", 0, 0 };'
    yield "    " + _comment("extractor", identifier(ext))
    yield from _selector_lines(ext)
    for variable in runtime.variables:
        if obj(variable["source"]).get("extractor_id") == ext["id"]:
            yield f"    {symbol('INIT_FN', identifier(variable))}(&result);"
    yield "    h2l_reset(&result);\n    }"


def extraction_lines(runtime: RuntimePlan, exchange: str) -> Iterator[str]:
    extractors = [ext for ext in runtime.extractors if ext["exchange_id"] == exchange]
    if not extractors:
        return
    json_used = any(obj(e["selector"])["dialect"] == "json_path_subset_v1" for e in extractors)
    yield "    h2l_response = h2l_capture(h2l_ok && h2l_rc == LR_PASS);"
    if json_used:
        yield "    h2l_json_parse(&document, h2l_response);"
    for ext in extractors:
        yield from _extractor_lines(runtime, ext)
    if json_used:
        yield "    h2l_json_free(&document);"
    yield "    free(h2l_response);"
    yield '    if (lr_save_string("", "H2L_RESPONSE") != LR_PASS)'
    yield '        h2l_fail("ERROR_CAPTURE_RESET");'


def source_map(runtime: RuntimePlan) -> Iterator[dict[str, JSONValue]]:
    entries = [(kind, identifier(v)) for v in runtime.variables
               for kind in ("variable", "initialize", "reset")]
    entries += [("extractor", identifier(e)) for e in runtime.extractors]
    for request in runtime.requests:
        templates = [request.url, *(t for _, t in request.headers)]
        if request.body is not None:
            templates.append(request.body)
        entries += [("binding", p.binding_id) for t in templates for p in t.parts
                    if isinstance(p, RuntimeSlot)]
    for kind, entity in entries:
        yield _mapping(kind, entity)


def _mapping(kind: str, entity: str) -> dict[str, JSONValue]:
    marker = symbol(kind.upper(), entity)
    function = {"initialize": "INIT_FN", "reset": "RESET_FN"}.get(kind)
    locator: dict[str, JSONValue] = {"marker": marker}
    if kind == "variable":
        locator["function"] = symbol("VALUE", entity)
    else:
        locator["function"] = symbol(function, entity) if function else "Action"
    return {"kind": kind, "entity_kind": "variable" if function else kind,
            "entity_id": entity, "artifact": "loadrunner/Action.c",
            "marker": marker, "locator": locator}
