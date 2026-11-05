"""Shared synthetic expectations for native_response_headers_v1, not a parser oracle."""


def header_cases():
    """Explicit normalized-field contract; no native I/O or captured values."""
    cases = []

    def add(name, text, value=None, *, ok=True, selector="X-Token", exact=True,
            occurrence=1, empty=False):
        cases.append({"name": name, "text": text, "value": value, "ok": ok,
                      "selector": selector, "exact": exact, "occurrence": occurrence,
                      "allow_empty": empty})

    add("lf_native", "HTTP/1.1 200 OK\nX-Token: abc\n", "abc")
    add("crlf_native", "HTTP/1.1 200 OK\r\nX-Token: abc\r\n\r\n", "abc")
    add("header_only", "X-Token: abc\n", "abc")
    add("unterminated_last_field", "X-Token: abc", "abc")
    add("http2_status", "HTTP/2 200\nX-Token: abc\n", "abc")
    add("case_insensitive", "x-tOkEn: abc\n", "abc", selector="X-TOKEN")
    add("ows_only_at_edges", "X-Token:\t abc\tdef \t\n", "abc\tdef")
    add("comma_not_split", "X-Token: a, b\n", "a, b")
    add("colon_in_value", "X-Token: scheme:value:tail\n", "scheme:value:tail")
    add("quoted_string", 'X-Token: "abc"\n', '"abc"')
    add("marker_is_data", "X-Token: ERROR_token\n", "ERROR_token")
    add("functions_are_data", "X-Token: ${untrusted}\n", "${untrusted}")
    add("literal_null", "X-Token: null\n", "null")
    add("literal_number", "X-Token: 001\n", "001")
    add("literal_boolean", "X-Token: false\n", "false")
    add("empty_allowed", "X-Token:\t \n", "", empty=True)
    add("empty_denied", "X-Token: \n", ok=False)
    add("missing", "X-Other: abc\n", ok=False)
    add("empty_capture", "", ok=False, empty=True)
    add("status_only", "HTTP/1.1 200 OK\n\n", ok=False, empty=True)
    repeated = "X-Token: first\nx-token: second\n"
    add("multiple_exact", repeated, ok=False)
    add("occurrence_first", repeated, "first", exact=False)
    add("occurrence_second", repeated, "second", exact=False, occurrence=2)
    add("occurrence_missing", repeated, ok=False, exact=False, occurrence=3)
    add("set_cookie_is_whole_header", 'Set-Cookie: SID="abc"; Path=/\n',
        'SID="abc"; Path=/', selector="Set-Cookie")
    add("set_cookie_repeat", "Set-Cookie: SID=a\nSet-Cookie: SID=b; Path=/app\n",
        "SID=b; Path=/app", selector="Set-Cookie", exact=False, occurrence=2)
    add("unused_unicode", "X-Other: 雪\nX-Token: abc\n", "abc")
    for kind, whitespace in (("tabs", "\t"), ("spaces", " ")):
        internal = "a" + whitespace * 65534 + "z"
        add("selected_internal_" + kind, "X-Token: \t" + internal + "\t \n", internal)
        add("unselected_internal_" + kind,
            "X-Other: a" + whitespace * 500000 + "z\nX-Token: good\n", "good")
    add("selected_unicode", "X-Token: 雪\n", ok=False)
    for name, text in [
        ("bare_cr", "X-Token: abc\rX-Other: no"),
        ("fold", "X-Token: abc\n continuation\n"),
        ("fold_tab", "X-Token: abc\n\tcontinuation\n"),
        ("bad_field_name", "X Token: abc\nX-Token: good\n"),
        ("space_before_colon", "X-Token : abc\n"),
        ("missing_colon", "X-Token abc\n"),
        ("nul", "X-Token: abc\x00hidden\n"),
        ("control", "X-Other: bad\x07\nX-Token: good\n"),
        ("delete", "X-Token: bad\x7f\n"),
        ("multiple_blocks", "HTTP/1.1 100 Continue\n\nHTTP/1.1 200 OK\nX-Token: a\n"),
        ("second_status", "HTTP/1.1 200 OK\nHTTP/1.1 200 OK\nX-Token: a\n"),
        ("interim_only", "HTTP/1.1 100 Continue\nX-Token: a\n"),
        ("bad_status", "HTTP/1.1 abc Invalid\nX-Token: a\n"),
        ("after_terminator", "X-Token: good\n\nX-Token: later\n"),
        ("leading_blank", "\nX-Token: abc\n"),
        ("unresolved_native_parameter", "{H2L_RESPONSE_HEADERS}"),
    ]:
        add(name, text, ok=False)
    add("value_at_limit", "X-Token: " + "a" * 65536 + "\n", "a" * 65536)
    add("value_over_limit", "X-Token: " + "a" * 65537 + "\n", ok=False)
    add("fields_at_limit", "X-Token: a\n" * 1000, "a", exact=False, occurrence=1000)
    add("fields_over_limit", "X-Token: a\n" * 1001, ok=False, exact=False)
    add("unrelated_fields_over_limit", "X-Other: a\n" * 1000 + "X-Token: a\n", ok=False)
    add("capture_over_limit", "X-Token: a\nX-Other: " + "a" * (1024 * 1024), ok=False)
    return cases
