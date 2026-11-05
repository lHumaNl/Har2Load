"""Pure cookie API construction checks; no SDK, cookie jar, or network execution."""

import ast
import re
from dataclasses import replace
from unittest.mock import patch

import pytest

from har2load.cookies import COOKIE_POLICY, NativeCookie
from har2load.generation_types import RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from tests.test_loadrunner import ACTION, plan, request
from tests.test_loadrunner_extensions import authored
from tests.test_loadrunner_sources import source_plan


def cookie(**changes: object) -> NativeCookie:
    value = NativeCookie(name="SID", value="PRIVATE_SEED", domain="example.invalid",
                         path="/login", secure=True, http_only=True, expires=None,
                         domain_specified=True, path_specified=True,
                         source_exchange_id="exchange-1", source_area="response", source_index=0)
    return replace(value, **changes)  # type: ignore[arg-type]


def cookie_plan(live: bool, *seeds: NativeCookie) -> RequestPlan:
    value = (source_plan({"kind": "literal", "value": "fresh"}) if live else
             plan(request(), request(id="exchange-2")))
    return replace(value, initial_cookies=seeds)


def arguments(source: str) -> list[str]:
    return [ast.literal_eval(value) for value in
            re.findall(r'web_add_cookie\(("(?:\\.|[^"\\])*")\)', source)]


@pytest.mark.parametrize("live", [False, True])
def test_default_jar_has_no_initial_cookie_api_calls(live: bool) -> None:
    build = LoadRunnerGenerator().generate(cookie_plan(live))
    source = build.files[ACTION].decode()
    assert "web_add_cookie(" not in source
    assert source.count("web_cleanup_cookies()") == 1
    assert source.count("web_cache_cleanup()") == 1
    assert build.metadata["cookie_policy"] == COOKIE_POLICY
    assert build.metadata["initial_cookie_count"] == 0
    assert build.metadata["profile_version"] == ("4" if live else "1")


@pytest.mark.parametrize("live", [False, True])
def test_seeds_follow_cleanup_and_precede_all_requests_once_per_action(live: bool) -> None:
    build = LoadRunnerGenerator().generate(cookie_plan(live, cookie()))
    source = build.files[ACTION].decode()
    positions = [source.index(token) for token in ("int Action(void)", "web_cleanup_cookies()",
                 "web_cache_cleanup()", "web_add_cookie(", '"REQ_0001"', '"REQ_0002"')]
    assert positions == sorted(positions)
    assert arguments(source) == ["SID=PRIVATE_SEED; domain=example.invalid; path=/login; secure"]
    assert source.count("web_add_cookie(") == source.count("web_cleanup_cookies()") == 1
    assert b"web_add_cookie(" not in build.files["loadrunner/vuser_init.c"]
    assert 'web_add_header("Cookie"' not in source
    assert not any(path.endswith((".prm", ".dat", ".json")) for path in build.files)
    assert build.metadata["initial_cookie_count"] == 1
    assert all(value not in str(build.metadata) for value in ("PRIVATE_SEED", "example.invalid",
                                                            "/login", "SID"))


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("expiry,expected", [(1, "Thu, 01 Jan 1970 00:00:01 GMT"),
                                           (1709164800, "Thu, 29 Feb 2024 00:00:00 GMT"),
                                           (253402300799, "Fri, 31 Dec 9999 23:59:59 GMT")])
def test_expiry_is_deterministic_utc_without_live_clock(
    live: bool, expiry: int, expected: str,
) -> None:
    model = cookie_plan(live, cookie(expires=expiry, secure=False))
    with patch("time.time", side_effect=AssertionError("live clock")), patch("os.environ", {}):
        build = LoadRunnerGenerator().generate(model)
    assert arguments(build.files[ACTION].decode()) == [
        f"SID=PRIVATE_SEED; domain=example.invalid; path=/login; expires={expected}"]
    assert build == LoadRunnerGenerator().generate(model)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("changes,code", [
    ({"domain_specified": False}, "HOST_ONLY_UNSUPPORTED"),
    *[({"same_site": mode}, "SAME_SITE_UNSUPPORTED") for mode in ("strict", "lax", "none")],
    *[({field: "{PRIVATE_SEED}"}, "METASYNTAX_UNSUPPORTED")
      for field in ("name", "value", "domain", "path")],
    *[({"value": value}, "REPRESENTATION_UNSUPPORTED")
      for value in ('a;b', 'a"b', "a\\b", "a,b", "a b", "a\nb", "a\x00b", "é", "\x7f")],
    ({"path": "/x;secure"}, "REPRESENTATION_UNSUPPORTED"),
    ({"domain": "example.invalid;secure"}, "REPRESENTATION_UNSUPPORTED"),
    ({"expires": 253402300800}, "EXPIRY_UNSUPPORTED"),
])
def test_target_rejections_are_atomic_and_value_free(
    live: bool, changes: dict[str, object], code: str,
) -> None:
    build = LoadRunnerGenerator().generate(cookie_plan(live, cookie(**changes)))
    assert build.status == "blocked" and not build.files and not build.source_map
    assert any(d.code == "LOADRUNNER_INITIAL_COOKIE_" + code for d in build.diagnostics)
    assert all("PRIVATE_SEED" not in d.message for d in build.diagnostics)


@pytest.mark.parametrize("value", ["", "abc+/==!#$%&'()*-.:<>?@[]^_`|~"])
def test_cookie_octets_are_not_encoded_and_empty_value_is_preserved(value: str) -> None:
    build = LoadRunnerGenerator().generate(cookie_plan(False, cookie(value=value)))
    assert arguments(build.files[ACTION].decode())[0].startswith("SID=" + value + "; domain=")


def test_cleanup_and_seed_failures_only_log_value_free_diagnostics() -> None:
    source = LoadRunnerGenerator().generate(cookie_plan(False, cookie())).files[ACTION].decode()
    assert 'if (web_cleanup_cookies() != LR_PASS)' in source
    messages = re.findall(r'lr_output_message\("([^"]*)"\)', source)
    assert messages == ["LOADRUNNER_INITIAL_COOKIE_CLEANUP_FAILED",
                        "LOADRUNNER_INITIAL_COOKIE_ADD_FAILED"]
    forbidden = ("lr_error_message", "LR_FAIL", "lr_abort", "lr_exit")
    assert all(token not in source for token in forbidden)
    assert 'lr_end_transaction("TC_0001", LR_AUTO)' in source


@pytest.mark.parametrize("live", [False, True])
def test_cookie_source_bytes_are_included_in_atomic_output_budget(live: bool) -> None:
    model = cookie_plan(live, cookie(value="x" * 65536))
    build = LoadRunnerGenerator().generate(model)
    total = sum(map(len, build.files.values()))
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total):
        assert LoadRunnerGenerator().generate(model) == build
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total - 1):
        blocked = LoadRunnerGenerator().generate(model)
    assert blocked.status == "blocked" and not blocked.files and not blocked.source_map
    assert any(d.code == "LOADRUNNER_OUTPUT_LIMIT" for d in blocked.diagnostics)


def test_authored_wrapper_retains_source_and_maps_with_initial_seeds() -> None:
    model = authored("/* opaque source, never compiled here */ not valid C;")
    baseline = LoadRunnerGenerator().generate(model)
    build = LoadRunnerGenerator().generate(replace(model, initial_cookies=(cookie(),)))
    assert build.metadata["profile_version"] == "3"
    assert build.metadata["profile"] == "authored_scalar_runtime_v1"
    assert build.source_map == baseline.source_map
    assert b"not valid C;" in build.files[ACTION]
    assert len(arguments(build.files[ACTION].decode())) == 1


def test_http_only_and_default_path_flags_do_not_invent_native_attributes() -> None:
    model = cookie_plan(False, cookie(http_only=True, path_specified=False))
    build = LoadRunnerGenerator().generate(model)
    other = cookie_plan(False, cookie(http_only=False, path_specified=True))
    assert build.files[ACTION] == LoadRunnerGenerator().generate(other).files[ACTION]
    assert "HttpOnly" not in arguments(build.files[ACTION].decode())[0]
    assert b"native HttpOnly enforcement is unverified" in build.files["loadrunner/README.txt"]
