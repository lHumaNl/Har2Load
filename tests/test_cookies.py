"""Synthetic, offline tests for explicit HAR cookie planning."""

import socket
import time
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest

from har2load.cookies import COOKIE_POLICY, NativeCookie, plan_initial_cookies
from har2load.domain import Capture, Exchange, Har2LoadError, JSONValue

URL = "https://Example.Invalid/a/b?query=ignored"
REQUEST = "ex-1:/request/cookies/0"
RESPONSE = "ex-1:/response/cookies/0"
INVALID = "INITIAL_COOKIE_INVALID"
SELECTION_INVALID = "INITIAL_COOKIE_SELECTION_INVALID"
CONFLICT = "INITIAL_COOKIE_CONFLICT"


def capture(row: object = None, *, url: object = URL, response: object = None) -> Capture:
    raw = {"request": {"url": url, "cookies": [row], "postData": False},
           "response": {"cookies": [response], "content": False}}
    return Capture("capture", "digest", b"original synthetic bytes",
                   (Exchange("ex-1", 0, cast(JSONValue, raw)),))


def cookie(**fields: object) -> dict[str, object]:
    return {"name": "session", "value": "opaque", **fields}


def selected(row: object = None, *, url: object = URL) -> NativeCookie:
    return plan_initial_cookies(capture(cookie() if row is None else row, url=url), (REQUEST,))[0]


def rejected(source: Capture, selections: tuple[str, ...], code: str) -> None:
    with pytest.raises(Har2LoadError) as caught:
        plan_initial_cookies(source, selections)
    assert caught.value.code == code
    assert caught.value.exit_code == 3
    assert str(caught.value) in {
        "Initial cookie selection is invalid.", "Initial cookie is invalid.",
        "Initial cookie selections conflict.",
    }


def test_request_defaults_and_frozen_contract() -> None:
    assert COOKIE_POLICY == "native_cookie_jar_v1"
    result = selected()
    assert result == NativeCookie("session", "opaque", "example.invalid", "/a", True,
                                  False, None, False, False, "ex-1", "request", 0)
    with pytest.raises(FrozenInstanceError):
        result.value = "changed"  # type: ignore[misc]


def test_explicit_response_selection_preserves_order_and_provenance() -> None:
    source = capture(cookie(), response=cookie(name="response", path="/", secure=False,
        httpOnly=True, domain=".EXAMPLE.INVALID", sameSite="LaX"))
    response, request = plan_initial_cookies(source, (RESPONSE, REQUEST))
    assert (response.name, request.name) == ("response", "session")
    assert response.source_area == "response" and response.source_index == 0
    assert response.domain == "example.invalid" and response.domain_specified
    assert response.path_specified and response.path == "/"
    assert response.http_only and not response.secure and response.same_site == "lax"


@pytest.mark.parametrize("raw", [None, False, [], {"request": {"cookies": "bad", "url": None}}])
def test_empty_selection_never_parses_capture(raw: JSONValue) -> None:
    source = replace(capture(), exchanges=(Exchange("ex-1", 0, raw),))
    assert plan_initial_cookies(source, ()) == ()


@pytest.mark.parametrize("selection", ["", "ex-1", "ex-1:/cookies/0", "ex-1:/request/cookies",
    "ex-1:/request/cookies/-1", "ex-1:/request/cookies/+1", "ex-1:/request/cookies/00",
    "ex-1:/request/cookies/01", "ex-1:/request/cookies/1.0", "ex-1:/request/cookies/0/",
    "ex-1:/request/cookies/0\n", "ex-1:/request/cookies/\u0660", "missing:/request/cookies/0",
    "ex-1:/request/cookies/1", "ex-1:/request/cookies/" + "9" * 5000])
def test_invalid_selections(selection: str) -> None:
    rejected(capture(cookie()), (selection,), SELECTION_INVALID)


def test_duplicate_physical_selection_and_selection_limit() -> None:
    rejected(capture(cookie()), (REQUEST, REQUEST), SELECTION_INVALID)
    rejected(capture(cookie()), (REQUEST,) * 257, SELECTION_INVALID)


def test_invalid_input_types_and_ambiguous_exchange_ids() -> None:
    source = capture(cookie())
    rejected(source, cast(tuple[str, ...], [REQUEST]), SELECTION_INVALID)
    rejected(source, cast(tuple[str, ...], (None,)), SELECTION_INVALID)
    duplicate = replace(source, exchanges=source.exchanges * 2)
    rejected(duplicate, (REQUEST,), SELECTION_INVALID)


def test_rejection_preserves_raw_capture() -> None:
    source = capture(cookie(), response=cookie())
    before = deepcopy(source)
    rejected(source, (REQUEST, RESPONSE), CONFLICT)
    assert source == before


def test_maximum_selection_count_and_indices() -> None:
    rows = [cookie(name=f"n{index}") for index in range(256)]
    raw = {"request": {"url": URL, "cookies": rows}}
    source = replace(capture(), exchanges=(Exchange("ex-1", 0, cast(JSONValue, raw)),))
    selections = tuple(f"ex-1:/request/cookies/{index}" for index in range(256))
    result = plan_initial_cookies(source, selections)
    assert len(result) == 256 and result[-1].source_index == 255


@pytest.mark.parametrize("raw", [None, [], {}, {"request": []}, {"request": {}},
    {"request": {"cookies": {}}}, {"request": {"cookies": []}}])
def test_missing_selected_container(raw: JSONValue) -> None:
    source = replace(capture(), exchanges=(Exchange("ex-1", 0, raw),))
    rejected(source, (REQUEST,), SELECTION_INVALID)


@pytest.mark.parametrize("row", [None, [], False, {}, {"name": "n"}, {"value": "v"}])
def test_invalid_row_or_missing_fields(row: object) -> None:
    rejected(capture(row), (REQUEST,), INVALID)


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "a\x00b", "\ud800", "a" * 65537,
                                   "\u00e9" * 32769])
def test_invalid_values_are_value_free(value: object) -> None:
    rejected(capture(cookie(value=value)), (REQUEST,), INVALID)


@pytest.mark.parametrize("value", ["", "a" * 65536, "\u00e9" * 32768,
    "${__groovy(danger())} {parameter} __import__('os').system('false')\n\t\x01 ;, "])
def test_values_are_preserved_as_opaque_utf8(value: str) -> None:
    assert selected(cookie(value=value)).value == value


@pytest.mark.parametrize("name", ["", "a" * 257, "with space", "colon:", "semi;", "eq=",
                                   "\u00e9", "\ud800", "\x00", None, True, 1])
def test_name_must_be_bounded_ascii_http_token(name: object) -> None:
    rejected(capture(cookie(name=name)), (REQUEST,), INVALID)


def test_token_symbols_and_name_limit() -> None:
    assert selected(cookie(name="!#$%&'*+-.^_`|~09Az")).name == "!#$%&'*+-.^_`|~09Az"
    assert selected(cookie(name="a" * 256)).name == "a" * 256


@pytest.mark.parametrize("url", [None, "", "file:///tmp/x", "https:///x", "//example.invalid/",
    "https://u:p@example.invalid/", "https://@example.invalid/", "https://bad host/",
    "https://bad\nhost/", "https://\u00e9.invalid/", "https://-bad.invalid/",
    "https://bad_.invalid/", "https://a..invalid/", "https://example.invalid:bad/",
    "https://example.invalid:65536/", "https://[bad]/", "https://[::1",
    "https://[::1]suffix/", "https://[::1]:/", "https://example.invalid:/",
    "https://example.invalid/a\x7f", "https://[::1]example.invalid/",
    "https://[fe80::1%25en0]/", "https://" + "a" * 64 + ".invalid/",
    "https://" + ".".join(["a" * 63] * 4) + "/"])
def test_invalid_source_url(url: object) -> None:
    rejected(capture(cookie(), url=url), (REQUEST,), INVALID)


@pytest.mark.parametrize(("url", "domain"), [("http://127.0.0.1/a", "127.0.0.1"),
    ("https://[2001:DB8::1]:443/a", "2001:db8::1"),
    ("https://EXAMPLE.INVALID:443/", "example.invalid")])
def test_source_hostname_normalization(url: str, domain: str) -> None:
    assert selected(url=url).domain == domain


@pytest.mark.parametrize("domain", [None, "", "..example.invalid", "invalid", "other.invalid",
    "sub.example.invalid", "example.invalid.", " example.invalid", "example.invalid;",
    "\u00e9.invalid", "\ud800", False, "a" * 254])
def test_explicit_domain_cannot_broaden_or_change_host(domain: object) -> None:
    rejected(capture(cookie(domain=domain)), (REQUEST,), INVALID)


@pytest.mark.parametrize("domain", ["example.invalid", ".example.invalid", "EXAMPLE.INVALID"])
def test_explicit_equal_domain_retains_domain_specified(domain: str) -> None:
    result = selected(cookie(domain=domain))
    assert result.domain == "example.invalid" and result.domain_specified


@pytest.mark.parametrize(("suffix", "path"), [("", "/"), ("?q=/a/b", "/"), ("/a", "/"),
    ("/a/", "/a"), ("/a/b", "/a"), ("/a%2Fb/c", "/a%2Fb"), ("//a/b", "//a")])
def test_default_directory_is_not_url_decoded(suffix: str, path: str) -> None:
    result = selected(url="http://example.invalid" + suffix)
    assert result.path == path and not result.path_specified and not result.secure


@pytest.mark.parametrize("path", [None, "", "relative", "/\x00", "/\x1f", "/\x7f", "/;",
                                   "/\u00e9", "/\ud800", False, "/" + "a" * 4096])
def test_invalid_explicit_path(path: object) -> None:
    rejected(capture(cookie(path=path)), (REQUEST,), INVALID)


def test_explicit_path_bound_and_default_path_validation() -> None:
    result = selected(cookie(path="/" + "a" * 4095))
    assert len(result.path) == 4096 and result.path_specified
    rejected(capture(cookie(), url="https://example.invalid/a;/b"), (REQUEST,), INVALID)
    assert selected(cookie(path="/"), url="https://example.invalid/a;/b").path == "/"


@pytest.mark.parametrize("field", ["secure", "httpOnly"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_flags_are_strict_booleans(field: str, value: object) -> None:
    rejected(capture(cookie(**{field: value})), (REQUEST,), INVALID)


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("1970-01-01T00:00:01Z", 1),
    ("1970-01-01T01:00:01+01:00", 1), ("1970-01-01T00:00:01.000Z", 1),
    ("1969-12-31T23:00:01-01:00", 1), ("9999-12-31T23:59:59Z", 253402300799)])
def test_expiry_is_timezone_aware_whole_epoch_without_live_filter(value: object,
                                                               expected: int | None) -> None:
    assert selected(cookie(expires=value)).expires == expected


@pytest.mark.parametrize("value", ["", 1, True, "1970-01-01", "1970-01-01T00:00:01",
    "1970-01-01T00:00:00Z", "1969-12-31T23:59:59Z", "1970-01-01T00:00:01.5Z",
    "10000-01-01T00:00:01Z", "9999-12-31T23:59:59-01:00", "2024-02-30T00:00:01Z",
    "2024-01-01T00:00:01+25:00", "2024-01-01T00:00:01Z\n", "\ud800",
    "2024-01-01T00:00:01+00:60", "2024-01-01T00:00:01.0000001Z"])
def test_invalid_expiry(value: object) -> None:
    rejected(capture(cookie(expires=value)), (REQUEST,), INVALID)


@pytest.mark.parametrize(("value", "expected"), [("Strict", "strict"), ("LAX", "lax"),
                                                ("None", "none")])
def test_same_site_is_metadata_only(value: str, expected: str) -> None:
    assert selected(cookie(sameSite=value, secure=False)).same_site == expected


@pytest.mark.parametrize("value", [None, "", "unknown", " Strict", True, 1])
def test_unknown_same_site_is_rejected(value: object) -> None:
    rejected(capture(cookie(sameSite=value)), (REQUEST,), INVALID)


@pytest.mark.parametrize("fields", [{}, {"value": "different"}, {"domain": ".example.invalid"},
                                    {"path": "/a"}, {"secure": False}])
def test_duplicate_identity_is_always_a_conflict(fields: dict[str, object]) -> None:
    rejected(capture(cookie(), response=cookie(**fields)), (REQUEST, RESPONSE), CONFLICT)


def test_distinct_paths_and_hosts_are_not_conflicts() -> None:
    source = capture(cookie(), response=cookie(path="/different"))
    other = replace(capture(cookie(), url="https://other.invalid/a/b").exchanges[0], id="ex-2")
    source = replace(source, exchanges=(*source.exchanges, other))
    assert len(plan_initial_cookies(source, (REQUEST, RESPONSE, "ex-2:/request/cookies/0"))) == 3


def test_unselected_rows_headers_and_unknown_fields_are_never_interpreted() -> None:
    raw = {"request": {"url": URL, "cookies": [cookie(extra={"code": "danger()"}), False],
                       "headers": [{"name": "Cookie", "value": "unselected=secret"}]},
           "response": {"cookies": False, "headers": [{"name": "Set-Cookie", "value": "bad"}]}}
    source = replace(capture(), exchanges=(Exchange("ex-1", 0, cast(JSONValue, raw)),))
    before = deepcopy(source)
    assert len(plan_initial_cookies(source, (REQUEST,))) == 1
    assert source == before


def test_no_clock_environment_network_or_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected external access")
    source = capture(cookie(value="${danger()} __import__('os')", expires="1970-01-01T00:00:01Z"))
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)
    clock = SimpleNamespace(fromisoformat=datetime.fromisoformat, now=forbidden,
                            today=forbidden, utcnow=forbidden)
    monkeypatch.setattr("har2load.cookies.datetime", clock)
    with monkeypatch.context() as context:
        context.setattr("builtins.eval", forbidden)
        context.setattr("builtins.exec", forbidden)
        result = plan_initial_cookies(source, (REQUEST,))
    assert result[0].expires == 1
