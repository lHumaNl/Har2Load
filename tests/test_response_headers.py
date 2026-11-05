"""Pure native header capability checks, not wire parsing or live resolution."""

import pytest

from har2load.response_headers import header_diagnostics
from har2load.runtime_types import RuntimePlan


def plan(name, area="response_headers", dialect="name_v1"):
    return RuntimePlan((), ({"id": "e", "area": area,
                            "selector": {"dialect": dialect, "name": name}},), ())


@pytest.mark.parametrize("name", ["X-Token", "location", "Set-Cookie", "A!#$%&'*+-.^_`|~09"])
def test_header_names_have_no_special_case_rules(name):
    for target in ("jmeter", "loadrunner"):
        assert header_diagnostics(plan(name), target) == []


@pytest.mark.parametrize("name", ["", "é", "X Token", "X:Token", "X\nToken", "X\x00"])
def test_native_header_selectors_are_bounded_ascii_tokens(name):
    for target in ("jmeter", "loadrunner"):
        errors = header_diagnostics(plan(name), target)
        assert len(errors) == 1
        assert errors[0].code == "RUNTIME_RESPONSE_HEADER_SELECTOR_UNSUPPORTED"
        assert errors[0].target == target and errors[0].entity_id == "e"


def test_cookie_and_body_profiles_are_not_reinterpreted_by_header_capability():
    assert header_diagnostics(plan("sid", "response_cookies"), "jmeter") == []
    assert header_diagnostics(plan("unused", "response_body"), "jmeter") == []
    assert header_diagnostics(plan("X-Token", dialect="unknown"), "jmeter")


@pytest.mark.parametrize("target", ["jmeter", "loadrunner"])
def test_header_selector_limit_is_exact_and_diagnostics_are_value_free(target):
    assert header_diagnostics(plan("X" * 256), target) == []
    selector = "PRIVATE_SYNTHETIC_HEADER_" + "X" * 257
    errors = header_diagnostics(plan(selector), target)
    assert len(errors) == 1
    assert selector not in repr(errors)
