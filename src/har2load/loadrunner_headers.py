"""Bounded native-exposed header text construction, not a wire/SDK parser claim."""

from collections.abc import Iterator

from har2load.response_headers import MAX_RESPONSE_HEADER_FIELDS

_HELPERS = r'''
typedef struct { const char *name, *value; size_t name_size, size; } h2l_header;
typedef struct { h2l_header fields[H2L_HEADER_FIELDS]; size_t count; int valid; } h2l_headers;
static char *h2l_headers_capture(int ok)
{
    char *native = NULL, *copy = NULL; const char *owned; unsigned long n = 0; int rc;
    if (!ok) return NULL;
    rc = lr_eval_string_ext("{H2L_RESPONSE_HEADERS}", 22, &native, &n, 0, 0, -1);
    if (rc == LR_PASS && native && n <= H2L_FIELD_LIMIT &&
        h2l_utf8((const unsigned char*)native, n)) {
        copy = (char*)malloc((size_t)n + 1);
        if (copy) { memcpy(copy, native, n); copy[n] = 0; }
    }
    /* Every actual evaluation owns cleanup, including errors and NULL output. */
    owned = native; lr_eval_string_ext_free(&owned);
    return copy;
}
static int h2l_header_digit(char c) { return c >= '0' && c <= '9'; }
static int h2l_header_token(unsigned char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        h2l_header_digit((char)c) || (c && strchr("!#$%&'*+-.^_`|~", c) != NULL);
}
static int h2l_header_status(const char *s, const char *end)
{
    if (end - s < 5 || memcmp(s, "HTTP/", 5)) return 0;
    s += 5; if (s == end || !h2l_header_digit(*s)) return 0;
    while (s < end && h2l_header_digit(*s)) ++s;
    if (s < end && *s == '.') {
        ++s; if (s == end || !h2l_header_digit(*s)) return 0;
        while (s < end && h2l_header_digit(*s)) ++s;
    }
    if (end - s < 4 || *s++ != ' ') return 0;
    if (*s < '2' || *s > '5' || !h2l_header_digit(s[1]) || !h2l_header_digit(s[2])) return 0;
    s += 3; if (s < end && *s++ != ' ') return 0;
    for (; s < end; ++s) if ((unsigned char)*s < 32 || (unsigned char)*s > 126) return 0;
    return 1;
}
static int h2l_header_field(h2l_headers *out, const char *s, const char *end)
{
    const char *colon = s, *value; h2l_header *field;
    while (colon < end && h2l_header_token((unsigned char)*colon)) ++colon;
    if (colon == s || colon == end || *colon != ':' || out->count == H2L_HEADER_FIELDS) return 0;
    value = colon + 1;
    while (value < end && (*value == ' ' || *value == '\t')) ++value;
    while (end > value && (end[-1] == ' ' || end[-1] == '\t')) --end;
    field = &out->fields[out->count++]; field->name = s; field->name_size = (size_t)(colon - s);
    field->value = value; field->size = (size_t)(end - value);
    return 1;
}
static int h2l_header_line(const char *s, const char *end)
{
    for (; s < end; ++s) if (((unsigned char)*s < 32 && *s != '\t') || *s == 127) return 0;
    return 1;
}
static void h2l_headers_parse(h2l_headers *out, const char *text)
{
    const char *s = text, *end, *next; size_t size; int first = 1, terminated = 0;
    out->count = 0; out->valid = 0;
    if (!text || !(size = strlen(text)) || size > H2L_FIELD_LIMIT ||
        !h2l_utf8((const unsigned char*)text, size)) return;
    while (*s) {
        end = strchr(s, '\n'); next = end ? end + 1 : s + strlen(s);
        if (!end) end = next;
        else if (end > s && end[-1] == '\r') --end;
        if (!h2l_header_line(s, end) || (terminated && end != s)) return;
        if (end == s) { if (first) return; terminated = 1; }
        else if (first && end - s >= 5 && !memcmp(s, "HTTP/", 5)) {
            if (!h2l_header_status(s, end)) return;
        } else if (!h2l_header_field(out, s, end)) return;
        first = 0; s = next;
    }
    out->valid = 1;
}
static unsigned char h2l_header_lower(unsigned char c)
{ return c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c; }
static int h2l_header_named(const h2l_header *field, const char *name)
{
    size_t i;
    if (field->name_size != strlen(name)) return 0;
    for (i = 0; i < field->name_size; ++i)
        if (h2l_header_lower((unsigned char)field->name[i]) !=
            h2l_header_lower((unsigned char)name[i])) return 0;
    return 1;
}
static void h2l_header_select(h2l_value *out, const h2l_headers *headers, const char *name,
                              unsigned long occurrence, int exact, int empty)
{
    const h2l_header *selected = NULL; size_t i, count = 0;
    h2l_reset(out);
    if (headers->valid) for (i = 0; i < headers->count; ++i) {
        if (h2l_header_named(&headers->fields[i], name) && ++count == occurrence)
            selected = &headers->fields[i];
    }
    if (!selected || (exact && count != 1) || (!empty && !selected->size) ||
        selected->size > H2L_VALUE_LIMIT) { h2l_error(out); return; }
    for (i = 0; i < selected->size; ++i)
        if ((unsigned char)selected->value[i] > 127) { h2l_error(out); return; }
    h2l_set(out, selected->value, selected->size, 1, 0);
}
'''


def helpers() -> Iterator[str]:
    yield f"#define H2L_HEADER_FIELDS {MAX_RESPONSE_HEADER_FIELDS}"
    yield from _HELPERS.splitlines()


def channels(areas: set[str]) -> Iterator[tuple[str, str, str, str]]:
    """Keep channel flags and cleanup independent; the SDK size setting is global."""
    if "response_body" in areas:
        yield "h2l_ok", "H2L_RESPONSE", "Body", "h2l_response"
    if "response_headers" in areas:
        yield "h2l_headers_ok", "H2L_RESPONSE_HEADERS", "Headers", "h2l_headers_response"


def registration(areas: set[str], limit: int) -> Iterator[str]:
    for flag, parameter, _, _ in channels(areas):
        yield f'    {flag} = lr_save_string("", "{parameter}") == LR_PASS;'
    if areas:
        yield f'    if (web_set_max_html_param_len("{limit}") != LR_PASS) {{'
        for flag, _, _, _ in channels(areas):
            yield f"        {flag} = 0;"
        yield "    }"
    for flag, parameter, scope, _ in channels(areas):
        filters = ', "IgnoreRedirections=No"' if scope == "Headers" else ""
        yield f'    if (web_reg_save_param_ex("ParamName={parameter}", "LB=", "RB=",'
        yield f'        "NotFound=warning", SEARCH_FILTERS, "Scope={scope}"{filters}, LAST)'
        yield f"        != LR_PASS) {flag} = 0;"


def cleanup(areas: set[str]) -> Iterator[str]:
    for _, parameter, _, buffer in channels(areas):
        yield f"    free({buffer});"
        yield f'    if (lr_save_string("", "{parameter}") != LR_PASS)'
        yield '        h2l_fail("ERROR_CAPTURE_RESET");'
