"""Synthetic structural tests; no native execution or input manifests."""

import base64
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from typing import Any

import pytest

from har2load import jmeter
from har2load.domain import Har2LoadError
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.jmeter_composition import compose_jmx
from har2load.runtime_types import RuntimePlan, RuntimeSlot, RuntimeTemplate
from har2load.shared_steps import SharedBlock, StepUse
from tests.test_jmeter_composition import FIRST, SECOND, generated
from tests.test_jmeter_runtime import config, correlation, plan, prop, variable
from tests.test_jmeter_sources import external

NAME = "TF_Shared_Catalog"
PIN = "a" * 64


def block(left: tuple[str, ...], right: tuple[str, ...], name: str = NAME) -> SharedBlock:
    return SharedBlock(name, (StepUse(FIRST, PIN, left), StepUse(SECOND, PIN, right)))


def static(*paths: str, prefix: str = "") -> RequestPlan:
    groups = tuple(NativeGroup(f"{prefix}g{i}", path, (f"{prefix}r{i}",))
                   for i, path in enumerate(paths, 1))
    requests = tuple(NativeRequest(g.exchange_ids[0], g.id, "GET", "https://example.invalid/" + p)
                     for g, p in zip(groups, paths, strict=True))
    return RequestPlan(requests, groups)


def compose(one: RequestPlan, two: RequestPlan, *blocks: SharedBlock) -> ET.Element:
    return ET.fromstring(compose_jmx([(FIRST, generated(one)), (SECOND, generated(two))],
                                     shared_steps=blocks))


def branch(tree: ET.Element, name: str) -> ET.Element:
    top = tree[0][1]
    return next(children for node, children in zip(top[::2], top[1::2], strict=True)
                if node.get("testname") == name)


def contents(tree: ET.Element) -> list[str]:
    return [node[0][-1].text if node.tag == "ModuleController" else prop(node, "TestPlan.comments")
            for node in tree[::2]]


def test_middle_and_end_ranges_keep_complete_steps_and_independent_debug_groups() -> None:
    one = static("a", "b", "c", "d")
    two = static("x", "y", "b", "c", prefix="z")
    tree = compose(one, two, block(("g2", "g3"), ("zg3", "zg4")))
    assert contents(branch(tree, FIRST)) == ["g1", NAME, "g4"]
    assert contents(branch(tree, SECOND)) == ["zg1", "zg2", NAME]
    assert contents(branch(tree, NAME)) == ["g2", "g3"]
    assert len(list(tree.iter("TransactionController"))) == 6
    for index, label in enumerate((FIRST, SECOND), 1):
        call = branch(tree, label)[index * 2]
        assert [n.text for n in call[0]] == ["Test Plan", "Har2Load", NAME]
        assert branch(tree, "DebugTG_" + label).find("CookieManager") is not None
    assert not list(branch(tree, NAME).iter("UserParameters"))


def blocked(one: RequestPlan, two: RequestPlan, *blocks: SharedBlock) -> Har2LoadError:
    with pytest.raises(Har2LoadError) as exc:
        compose(one, two, *blocks)
    assert exc.value.code.startswith("COMPOSE_SHARED_") and exc.value.exit_code == 3
    assert "PRIVATE" not in str(exc.value) and "#" in str(exc.value)
    return exc.value


@pytest.mark.parametrize("left,right", [
    (("g1", "g3"), ("g1", "g2")), (("g2", "g1"), ("g1", "g2")),
    (("g1", "g1"), ("g1", "g2")), (("PRIVATE",), ("g1",)),
    (("g1",), ("g1", "g2")), ((), ()),
])
def test_selection_is_complete_ordered_and_contiguous(left: tuple, right: tuple) -> None:
    one = static("a", "a", "a")
    blocked(one, one, block(left, right))


def test_overlap_and_case_insensitive_fragment_name_collisions() -> None:
    one = static("a", "a", "a")
    first = block(("g1", "g2"), ("g1", "g2"))
    blocked(one, one, first, block(("g2",), ("g3",), "TF_Shared_Other"))
    collision = block(("g1",), ("g1",), "TF_uc1.1")
    assert blocked(one, one, collision).code == "COMPOSE_SHARED_NAME"


def test_two_disjoint_blocks_declared_in_reverse_order() -> None:
    one = static("a", "b", "c")
    tree = compose(one, one, block(("g3",), ("g3",)),
                   block(("g1",), ("g1",), "TF_Shared_Other"))
    assert contents(branch(tree, FIRST)) == ["TF_Shared_Other", "g2", NAME]
    assert contents(branch(tree, SECOND)) == ["TF_Shared_Other", "g2", NAME]


@pytest.mark.parametrize("difference", ["method", "query", "extra", "order", "body", "type"])
def test_static_wire_field_differences_block(difference: str) -> None:
    one = static("a?x=1&x=2")
    headers = (("Content-Type", "text/plain"), ("X-Tag", "synthetic"))
    request = replace(one.requests[0], method="POST", headers=headers, body=b"PRIVATE data")
    one = replace(one, requests=(request,))
    overrides = {"method": {"method": "PUT"}, "query": {"url": request.url + "&extra=PRIVATE"},
                 "extra": {"headers": (*headers, ("X-Extra", "PRIVATE"))},
                 "order": {"headers": headers[::-1]}, "body": {"body": b"different"},
                 "type": {"headers": (("Content-Type", "application/json"), headers[1])}}
    other = replace(one, requests=(replace(request, **overrides[difference]),))
    blocked(one, other, block(("g1",), ("g1",)))


def local_runtime(suffix: str) -> RequestPlan:
    original = correlation()
    r = original.runtime
    assert r is not None
    p, c, v, e, b = (key + suffix for key in ("producer", "consumer", "var", "ext", "bind"))
    declaration = {**r.variables[0], "id": v, "source": {"kind": "extracted", "extractor_id": e},
                   "initialize": {"event": "after_response", "exchange_id": p}}
    seed = {**external("initial" + suffix), "id": "seed" + suffix, "name": "seed"}
    unrelated = {**variable({"kind": "literal", "value": suffix}), "id": "unused" + suffix,
                 "name": "unused" + suffix}
    producer = replace(r.requests[0], id=p,
                       body=RuntimeTemplate((RuntimeSlot("first" + suffix, seed["id"], "json"),)))
    consumer = replace(r.requests[1], id=c, body=RuntimeTemplate((RuntimeSlot(b, v, "json"),)))
    runtime = replace(r, requests=(producer, consumer), extractors=({**r.extractors[0], "id": e,
                      "exchange_id": p},), variables=(declaration, seed, unrelated))
    requests = tuple(replace(n, id=i, group_id="g" + suffix)
                     for n, i in zip(original.requests, (p, c), strict=True))
    return replace(original, requests=requests, runtime=runtime,
                   groups=(NativeGroup("g" + suffix, "business step", (p, c)),))


def private_rows(tree: ET.Element, label: str) -> dict[str, str]:
    debug = branch(tree, "DebugTG_" + label)
    node = debug.find(".//UserParameters")
    assert node is not None and debug[2].tag == "JSR223Sampler" and debug[3][0] is node
    return {k.text: v.text for k, v in zip(node[0], node[1][0], strict=True)}


def event_nodes(tree: ET.Element) -> list[ET.Element]:
    return [n for n in tree.iter() if n.tag in {"JSR223PreProcessor", "JSR223PostProcessor"}]


def test_shared_runtime_preserves_every_event_byte_and_all_caller_local_ids() -> None:
    one, two = local_runtime("left"), local_runtime("right")
    plain = compose(one, two)
    tree = compose(one, two, block(("gleft",), ("gright",)))
    shared = event_nodes(branch(tree, NAME))
    assert len(shared) == 4
    assert len(list(tree.iter("UserParameters"))) == 2
    assert not tree.findall(".//TestPlan//elementProp[@elementType='Argument']")
    for label, suffix in ((FIRST, "left"), (SECOND, "right")):
        rows = private_rows(tree, label)
        assert rows[f"har2load.input.{label}.token"] == "initial" + suffix
        for idx, (common, original) in enumerate(zip(shared, event_nodes(branch(plain, label)),
                                                      strict=True)):
            event = ("before_request", "after_response")[idx % 2]
            key = f"har2load.shared.1.1.{idx // 2 + 1}.{event}"
            assert prop(common, "parameters") == "${" + key + "}"
            assert rows[key] == prop(original, "parameters")
            assert prop(common, "script") == prop(original, "script")
            assert json.loads(base64.b64decode(rows[key], validate=True)) == config(original)
            assert prop(common, "filename") == ""


@pytest.mark.parametrize("change", ["literal", "encoding", "order", "section", "header", "name",
                                    "type", "seed-key", "scope", "selector", "effect", "generator"])
def test_runtime_requires_identical_order_types_selectors_and_effects(change: str) -> None:
    one = local_runtime("one")
    other = local_runtime("two")
    r = other.runtime
    assert r is not None
    prod, consume = r.requests
    token, seed, spare = r.variables
    slot = consume.body.parts[0]
    modifications: dict[str, Any] = {
        "literal": replace(consume, url=RuntimeTemplate(("https://example.invalid/PRIVATE",))),
        "encoding": replace(consume, body=RuntimeTemplate((replace(slot, encoding="text"),))),
        "order": replace(consume, body=RuntimeTemplate(
            (slot, RuntimeSlot("b", seed["id"], "json")))),
        "section": replace(consume, headers=(("X-New", consume.body),), body=None),
        "header": replace(consume, headers=(*consume.headers,
                                            ("X-Other", RuntimeTemplate(("x",))))),
    }
    if change in modifications:
        r = replace(r, requests=(prod, modifications[change]))
    else:
        r = change_declaration(r, change, token, seed, spare)
    blocked(one, replace(other, runtime=r), block(("gone",), ("gtwo",)))


def change_declaration(r: RuntimePlan, change: str, token: dict, seed: dict, spare: dict
                       ) -> RuntimePlan:
    if change == "selector":
        e = {**r.extractors[0], "selector": {"dialect": "json_path_subset_v1", "expression": "$.x"}}
        return replace(r, extractors=(e,))
    if change == "effect":
        spare = {**spare, "initialize": {"event": "after_response", "exchange_id": "producertwo"}}
    if change == "scope":
        token = {**token, "scope": "session", "reset": "session_start"}
    if change == "name":
        token = {**token, "name": "PRIVATE"}
    if change == "seed-key":
        seed = {**seed, "source": {**seed["source"], "key": "PRIVATE"}}
    if change == "generator":
        seed = {**seed, "source": {"kind": "generated", "generator": "uuid"}}
    if change == "type":
        token = {**token, "value_type": "number"}
    return replace(r, variables=(token, seed, spare))


def test_mixed_profiles_are_not_unified() -> None:
    blocked(static("a"), plan(), block(("g1",), ("g1",)))


def test_out_of_range_extractor_producing_a_live_slot_is_not_assumed_equivalent() -> None:
    src = correlation()
    groups = (NativeGroup("g1", "Producer", ("r1",)), NativeGroup("g2", "Consumer", ("r2",)))
    src = replace(src, groups=groups,
                  requests=(src.requests[0], replace(src.requests[1], group_id="g2")))
    err = blocked(src, src, block(("g2",), ("g2",)))
    assert err.code == "COMPOSE_SHARED_DEPENDENCY"


def test_unknown_uc_or_single_caller_is_not_a_shared_fragment() -> None:
    one = static("a")
    first = block(("g1",), ("g1",))
    for uses in (first.uses[:1], first.uses[:1] * 2,
                 (first.uses[0], replace(first.uses[1], uc="TF_PRIVATE"))):
        blocked(one, one, replace(first, uses=uses))


def test_nonparticipants_are_unchanged_and_a_missing_seed_needs_no_global_variable() -> None:
    one, other = generated(plan(external())), generated(static("a"))
    inputs = [(FIRST, one), (SECOND, one), ("TF_Other", other)]
    legacy = compose_jmx(inputs)
    assert legacy == compose_jmx(inputs, shared_steps=())
    tree = ET.fromstring(compose_jmx(inputs, shared_steps=(block(("g1",), ("g1",)),)))
    for label in ("TF_Other", "DebugTG_TF_Other"):
        assert ET.tostring(branch(tree, label)) == ET.tostring(branch(ET.fromstring(legacy), label))
    assert set(private_rows(tree, FIRST)) == {
        "har2load.shared.1.1.1.before_request", "har2load.shared.1.1.1.after_response"}


def test_like_requests_cannot_be_reordered_across_step_boundaries() -> None:
    one, other = static("a", "b"), static("b", "a")
    err = blocked(one, other, block(("g1", "g2"), ("g1", "g2")))
    assert str(err) == "Shared block #1, use #2, group position #1: composition is blocked."


def test_output_cap_still_applies_after_adding_shared_fragments(monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    data = generated(static("a", "b", "c", "d"))
    inputs = [(FIRST, data), (SECOND, data)]
    blocks = (block(("g2",), ("g2",)),)
    exact = len(compose_jmx(inputs, shared_steps=blocks))
    assert exact > len(data)
    monkeypatch.setattr(jmeter, "MAX_GENERATED_BYTES", exact - 1)
    with pytest.raises(Har2LoadError) as exc:
        compose_jmx(inputs, shared_steps=blocks)
    assert exc.value.code == "COMPOSE_SHARED_LIMIT"


def test_three_calls_resolve_one_definition() -> None:
    data = generated(static("items"))
    labels = (FIRST, SECOND, "TF_Third")
    decision = SharedBlock(NAME, tuple(StepUse(label, PIN, ("g1",)) for label in labels))
    tree = ET.fromstring(compose_jmx([(label, data) for label in labels], shared_steps=(decision,)))
    assert len(list(tree.iter("HTTPSamplerProxy"))) == 1
    assert len(list(tree.iter("CookieManager"))) == 3
    for label in labels:
        assert contents(branch(tree, label)) == [NAME]


def test_ambiguous_exported_names_rejected_even_when_a_duplicate_has_no_local_binding() -> None:
    candidate = plan()
    r = candidate.runtime
    assert r is not None
    alias = {**r.variables[0], "id": "other"}
    dup = replace(candidate, runtime=replace(r, variables=(*r.variables, alias)))
    assert blocked(candidate, dup, block(("g1",), ("g1",))).code == "COMPOSE_SHARED_AMBIGUOUS"


def test_reusing_the_same_two_slots_in_reverse_order_is_not_equivalent() -> None:
    candidate = local_runtime("one")
    r = candidate.runtime
    assert r is not None
    produce, consume = r.requests
    slots = (RuntimeSlot("b1", "varone", "json"), RuntimeSlot("b2", "seedone", "json"))
    plans = []
    for order in (slots, slots[::-1]):
        req = replace(consume, body=RuntimeTemplate(("[", order[0], ",", order[1], "]")))
        plans.append(replace(candidate, runtime=replace(r, requests=(produce, req))))
    blocked(*plans, block(("gone",), ("gone",)))
