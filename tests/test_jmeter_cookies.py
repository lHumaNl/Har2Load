"""Offline initial cookie contracts; actual native lifecycle tests are opt-in."""

import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace

import pytest

from har2load.cookies import COOKIE_POLICY, NativeCookie
from har2load.generation_types import RequestPlan
from har2load.jmeter import JMeterGenerator
from tests.test_jmeter import ARTIFACT, blocked, plan, prop, tree
from tests.test_jmeter_extensions import authored
from tests.test_jmeter_runtime import plan as runtime_plan

FACTORIES = (plan, runtime_plan, authored)


def seed() -> NativeCookie:
    return NativeCookie(name="session", value="SYNTHETIC_PRIVATE_COOKIE", domain="example.invalid",
        path="/app", secure=True, http_only=True, expires=None, domain_specified=False,
        path_specified=False, source_exchange_id="private_exchange", source_area="response",
        source_index=0)


def same_name_seeds() -> tuple[NativeCookie, ...]:
    return (replace(seed(), path="/", value="SYNTHETIC_ROOT_COOKIE", path_specified=True),
            replace(seed(), value="SYNTHETIC_APP_COOKIE", path_specified=True, source_index=1))


@pytest.mark.parametrize("factory", FACTORIES)
def test_same_name_different_paths_have_distinct_collection_keys(
    factory: Callable[[], RequestPlan],
) -> None:
    cookies = same_name_seeds()
    candidate = replace(factory(), initial_cookies=cookies)
    original = deepcopy(candidate)
    build = JMeterGenerator().generate(candidate)
    nodes = tree(build).findall(".//CookieManager/collectionProp/elementProp")
    assert [node.get("name") for node in nodes] == ["0", "1"]
    assert [node.get("testname") for node in nodes] == ["session", "session"]
    assert [prop(node, "Cookie.path") for node in nodes] == ["/", "/app"]
    assert [prop(node, "Cookie.value") for node in nodes] == [cookie.value for cookie in cookies]
    assert all(node.find("*[@name='Cookie.name']") is None for node in nodes)
    assert build.metadata["initial_cookie_count"] == 2
    assert candidate == original and build == JMeterGenerator().generate(candidate)


@pytest.mark.parametrize("factory", FACTORIES)
def test_default_manager_is_empty_and_native_policy_is_unchanged(
    factory: Callable[[], RequestPlan],
) -> None:
    build = JMeterGenerator().generate(factory())
    root = tree(build)
    assert len(root.findall(".//CookieManager")) == 1
    collection = root.find(".//collectionProp[@name='CookieManager.cookies']")
    assert collection is not None and len(collection) == 0
    assert prop(root, "CookieManager.clearEachIteration") == "true"
    assert prop(root, "CookieManager.controlledByThreadGroup") == "false"
    assert prop(root, "CookieManager.policy") == "standard"
    assert prop(root, "CookieManager.implementation").endswith(".HC4CookieHandler")
    assert build.metadata["cookie_policy"] == COOKIE_POLICY
    assert build.metadata["initial_cookie_count"] == 0


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("specified", [False, True])
def test_selected_cookie_native_fields_and_host_flags(
    factory: Callable[[], RequestPlan], specified: bool,
) -> None:
    cookie = replace(seed(), domain_specified=specified, path_specified=specified)
    root = tree(JMeterGenerator().generate(replace(factory(), initial_cookies=(cookie,))))
    nodes = root.findall(".//CookieManager/collectionProp/elementProp")
    assert len(nodes) == 1 and nodes[0].get("elementType") == "Cookie"
    node = nodes[0]
    assert node.get("testname") == cookie.name
    assert node.find("*[@name='Cookie.name']") is None
    for field in ("value", "domain", "path"):
        assert prop(node, "Cookie." + field) == getattr(cookie, field)
    assert prop(node, "Cookie.secure") == "true"
    assert prop(node, "Cookie.expires") == prop(node, "Cookie.version") == "0"
    for field in ("domain_specified", "path_specified"):
        assert prop(node, "Cookie." + field) == str(specified).lower()
    assert node.find("*[@name='Cookie.httpOnly']") is None
    assert node.find("*[@name='Cookie.sameSite']") is None


@pytest.mark.parametrize("factory", FACTORIES)
def test_cookie_metadata_is_value_free_and_profiles_advance(
    factory: Callable[[], RequestPlan],
) -> None:
    build = JMeterGenerator().generate(replace(factory(), initial_cookies=(seed(),)))
    assert build.metadata["initial_cookie_count"] == 1
    assert build.metadata["cookie_policy"] == COOKIE_POLICY
    versions = {plan: "1", runtime_plan: "4", authored: "3"}
    assert build.metadata["profile_version"] == versions[factory]
    for private in (seed().value, seed().domain, seed().source_exchange_id):
        assert private not in json.dumps(build.metadata)
        assert private not in repr(build.diagnostics)
    assert seed().value.encode() in build.files[ARTIFACT]


@pytest.mark.parametrize("factory", FACTORIES)
def test_cookie_emission_preserves_order_and_plan_without_extra_scripts(
    factory: Callable[[], RequestPlan],
) -> None:
    base = factory()
    cookies = (seed(), replace(seed(), name="second", value="", source_index=1))
    candidate = replace(base, initial_cookies=cookies)
    original = deepcopy(candidate)
    build = JMeterGenerator().generate(candidate)
    root = tree(build)
    assert [node.get("testname") for node in root.findall(
        ".//CookieManager/collectionProp/elementProp")] == [cookie.name for cookie in cookies]
    assert candidate == original and build == JMeterGenerator().generate(candidate)
    assert _scripts(root) == _scripts(tree(JMeterGenerator().generate(base)))
    assert "Cookie" not in [prop(node, "Header.name") for node in root.findall(
        ".//elementProp[@elementType='Header']")]


def _scripts(root: ET.Element) -> list[tuple[str, str | None]]:
    return [(node.tag, node.text) for node in root.iter()
            if node.tag.startswith("JSR223") or node.get("name") == "script"]


@pytest.mark.parametrize("field", ["name", "value", "domain", "path"])
@pytest.mark.parametrize("unsafe", ["$", "\\", "\n", "\x00", "\x7f", "é",
                                   "${__groovy(private_marker)}"])
def test_unsafe_cookie_literals_are_blocked_without_echoing(field: str, unsafe: str) -> None:
    cookie = replace(seed(), **{field: getattr(seed(), field) + unsafe})
    build = JMeterGenerator().generate(replace(plan(), initial_cookies=(cookie,)))
    blocked(build, "JMETER_INITIAL_COOKIE_UNSUPPORTED")
    assert "private_marker" not in repr(build.diagnostics) + json.dumps(build.metadata)


@pytest.mark.parametrize("value", ['"quoted"', "space here", "a;b", "a,b", "\t"])
def test_non_cookie_octets_are_not_silently_reencoded(value: str) -> None:
    candidate = replace(plan(), initial_cookies=(replace(seed(), value=value),))
    blocked(JMeterGenerator().generate(candidate), "JMETER_INITIAL_COOKIE_UNSUPPORTED")


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("expires", [1, 2147483647, 253402300799])
def test_all_explicit_expiry_is_blocked_without_clock_filtering(
    factory: Callable[[], RequestPlan], expires: int,
) -> None:
    candidate = replace(factory(), initial_cookies=(replace(seed(), expires=expires),))
    blocked(JMeterGenerator().generate(candidate), "JMETER_INITIAL_COOKIE_EXPIRY_UNSUPPORTED")
    assert candidate.initial_cookies[0].expires == expires


@pytest.mark.parametrize("same_site", ["strict", "lax", "none"])
@pytest.mark.parametrize("factory", FACTORIES)
def test_same_site_is_not_silently_discarded(
    same_site: str, factory: Callable[[], RequestPlan],
) -> None:
    candidate = replace(factory(), initial_cookies=(replace(seed(), same_site=same_site),))
    blocked(JMeterGenerator().generate(candidate), "JMETER_INITIAL_COOKIE_SAMESITE_UNSUPPORTED")


@pytest.mark.parametrize("field,value", [("name", ""), ("name", "not a token"),
    ("domain", ".example.invalid"), ("domain", "invalid..host"), ("domain", "not:ipv6"),
    ("path", "relative"), ("path", "/app;parameter")])
def test_direct_invalid_native_fields_are_blocked(field: str, value: str) -> None:
    cookie = replace(seed(), **{field: value})
    blocked(JMeterGenerator().generate(replace(plan(), initial_cookies=(cookie,))),
            "JMETER_INITIAL_COOKIE_UNSUPPORTED")


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("specified", [False, True])
def test_ipv6_seeds_block_instead_of_silently_missing_native_matching(
    factory: Callable[[], RequestPlan], specified: bool,
) -> None:
    cookie = replace(seed(), domain="2001:db8::1", domain_specified=specified)
    candidate = replace(factory(), initial_cookies=(cookie,))
    build = JMeterGenerator().generate(candidate)
    blocked(build, "JMETER_INITIAL_COOKIE_UNSUPPORTED")
    assert cookie.domain not in repr(build.diagnostics) + json.dumps(build.metadata)
    assert candidate.initial_cookies == (cookie,)


def test_cookie_count_is_bounded_before_constructing_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module
    from har2load.jmeter_cookies import MAX_INITIAL_COOKIES

    cookies = tuple(replace(seed(), name=f"seed{i}") for i in range(MAX_INITIAL_COOKIES))
    build = JMeterGenerator().generate(replace(plan(), initial_cookies=cookies))
    assert build.status == "generated"
    monkeypatch.setattr(module, "_document", lambda _: pytest.fail("Oversized cookie tree built"))
    candidate = replace(plan(), initial_cookies=(*cookies, replace(seed(), name="extra")))
    blocked(JMeterGenerator().generate(candidate), "JMETER_INITIAL_COOKIE_UNSUPPORTED")


def test_cookie_xml_escaping_counts_toward_serialized_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import har2load.jmeter as module

    candidate = replace(plan(), initial_cookies=(replace(seed(), value="<&>" * 100),))
    size = len(JMeterGenerator().generate(candidate).files[ARTIFACT])
    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", size)
    assert len(JMeterGenerator().generate(candidate).files[ARTIFACT]) == size
    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", size - 1)
    blocked(JMeterGenerator().generate(candidate), "JMETER_OUTPUT_LIMIT")


def test_cookie_size_preflight_precedes_tree_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    import har2load.jmeter as module

    monkeypatch.setattr(module, "MAX_GENERATED_BYTES", 100)
    monkeypatch.setattr(module, "_document", lambda _: pytest.fail("Oversized cookie tree built"))
    candidate = replace(plan(), initial_cookies=(replace(seed(), value="x" * 101),))
    blocked(JMeterGenerator().generate(candidate), "JMETER_OUTPUT_LIMIT")


def test_preparation_does_not_execute_cookie_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Cookie generation must remain offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    candidate = replace(runtime_plan(), initial_cookies=(seed(),))
    assert JMeterGenerator().generate(candidate).status == "generated"
