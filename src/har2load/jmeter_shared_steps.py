"""Lower explicit, pinned decisions on *round-trip verified* generator trees only.

Comparison covers ordered generated request fields and all scalar reads/writes in the
range. Exported names and external key names must agree. Request-local IDs are mapped
by position, variables by unique exported name, and extractors by producer and order.
Out-of-range request-specific producers of relevant variables are deliberately refused.
The comparison form is never emitted: native processors receive the original per-UC data.
"""

import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Never, Self, cast

from har2load import jmeter, jmeter_runtime
from har2load.domain import Har2LoadError
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeTemplate
from har2load.shared_steps import TF_LABEL, SharedBlock, StepUse

_COMMENTS = "stringProp[@name='TestPlan.comments']"
_PARAMETERS = "stringProp[@name='parameters']"
_EVENTS = ("before_request", "after_response")
type _Pair = tuple[ET.Element, ET.Element]
type _Record = dict[str, Any]


@dataclass(frozen=True)
class _Position:
    block: int
    use: int = 0
    step: int = 0

    def reject(self, code: str) -> Never:
        where = f"Shared block #{self.block}"
        if self.use:
            where += f", use #{self.use}"
        if self.step:
            where += f", group position #{self.step}"
        raise Har2LoadError("COMPOSE_SHARED_" + code, where + ": composition is blocked.", 3)


def _pairs(branch: ET.Element) -> Iterator[_Pair]:
    return zip(branch[::2], branch[1::2], strict=True)


def add_parameters(branch: ET.Element, rows: list[tuple[str, str]]) -> None:
    """Rows are generator-encoded initial strings or base64 data, never script text."""
    if not rows:
        return
    node = branch.find("UserParameters")
    if node is None:
        node, _ = jmeter._component(branch, "UserParameters", "UserParametersGui", "Initial inputs")
        names = ET.SubElement(node, "collectionProp", name="UserParameters.names")
        users = ET.SubElement(node, "collectionProp", name="UserParameters.thread_values")
        values = ET.SubElement(users, "collectionProp", name="0")
        jmeter._property(node, "UserParameters.per_iteration", False)
    else:
        names, values = node[0], node[1][0]
    for i, (key, data) in enumerate(rows, len(names)):
        jmeter._property(names, str(i), key)
        jmeter._property(values, str(i), data)


@dataclass
class Caller:
    plan: RequestPlan
    wrapped: ET.Element
    debug: ET.Element

    def select(self, ids: tuple[str, ...], pos: _Position) -> list[_Pair]:
        # A previous replacement is a ModuleController and cannot be selected twice.
        available = list(_pairs(self.wrapped))
        by_id = {n.findtext(_COMMENTS): i for i, (n, _) in enumerate(available)
                 if n.tag == "GenericController"}
        start = by_id.get(ids[0]) if ids else None
        if start is None:
            _Position(pos.block, pos.use, 1).reject("RANGE")
        for step, group in enumerate(ids, 1):
            if by_id.get(group) != start + step - 1:
                _Position(pos.block, pos.use, step).reject("RANGE")
        return available[start:start + len(ids)]

    def route(self, steps: list[_Pair], block: int) -> None:
        if self.plan.runtime is None:
            return
        rows = [row for s, (_, tree) in enumerate(steps, 1)
                for row in _route_step(tree, block, s)]
        init_tree = next(tree for node, tree in _pairs(self.debug) if node.tag == "JSR223Sampler")
        add_parameters(init_tree, rows)

    def replace(self, steps: list[_Pair], target: str) -> None:
        index = list(self.wrapped).index(steps[0][0])
        tree = ET.Element("hashTree")
        module, _ = jmeter._component(tree, "ModuleController", "ModuleControllerGui",
                                      "MC_" + target)
        path = ET.SubElement(module, "collectionProp", name="ModuleController.node_path")
        for i, segment in enumerate(("Test Plan", jmeter.PLAN_NAME, target)):
            jmeter._property(path, str(i), segment)
        self.wrapped[index:index + len(steps) * 2] = list(tree)


def _route_step(tree: ET.Element, block: int, step: int) -> Iterator[tuple[str, str]]:
    # Fixed generated layout verified before relocation: transaction -> samplers -> pre/post.
    for r, (_, children) in enumerate(_pairs(tree[1]), 1):
        for event, (node, _) in zip(_EVENTS, _pairs(children), strict=True):
            key = f"har2load.shared.{block}.{step}.{r}.{event}"
            parameter = node.find(_PARAMETERS)
            assert parameter is not None and parameter.text is not None
            encoded = parameter.text
            parameter.text = "${" + key + "}"
            yield key, encoded


@dataclass
class _Runtime:
    variables: list[_Record]
    extractors: list[_Record]
    names: dict[str, str]
    exchanges: dict[str, int]
    producers: dict[str, tuple[int, int]]
    requests: dict[str, RuntimeRequest]

    @classmethod
    def for_range(cls, plan: RuntimePlan, groups: list[NativeGroup], pos: _Position) -> Self:
        variables = cast(list[_Record], jmeter_runtime._variables(plan))
        extractors = cast(list[_Record], jmeter_runtime._extractors(plan))
        names = {v["id"]: v["name"] for v in variables}
        if len(variables) != len(names) or len(set(names.values())) != len(names):
            pos.reject("AMBIGUOUS")
        if len({e["id"] for e in extractors}) != len(extractors):
            pos.reject("AMBIGUOUS")
        ids = (key for group in groups for key in group.exchange_ids)
        exchanges = {key: i for i, key in enumerate(ids, 1)}
        producers = _producer_positions(extractors, exchanges)
        requests = {r.id: r for r in plan.requests}
        return cls(variables, extractors, names, exchanges, producers, requests)

    def template(self, template: RuntimeTemplate | None) -> tuple[object, ...] | None:
        if template is None:
            return None
        return tuple(p if isinstance(p, str) else (self.names[p.variable_id], p.encoding)
                     for p in template.parts)

    def declaration(self, v: _Record, pos: _Position) -> tuple[object, ...]:
        init, source = v["initialize"], v["source"].copy()
        producer = None
        if "exchange_id" in init:
            producer = self.exchanges.get(init["exchange_id"])
            if producer is None:
                pos.reject("DEPENDENCY")
        if source["kind"] == "extracted":
            key = self.producers.get(source["extractor_id"])
            if key is None or key[0] != producer or init["event"] != "after_response":
                pos.reject("DEPENDENCY")
            source["extractor_id"] = key
        return (v["name"], v["value_type"], v["scope"], v["reset"], init["event"], producer, source)

    def signature(self, req: NativeRequest, pos: _Position) -> tuple[object, ...]:
        r = self.requests[req.id]
        used = {p.variable_id for t in jmeter_runtime.templates(r)
                for p in t.parts if not isinstance(p, str)}
        # Every local init, producer reset, request-scoped reset, and slot read matters,
        # including unbound outputs consumed by private tails. Filter in declaration order.
        declarations = tuple(self.declaration(v, pos) for v in self.variables
                             if v["id"] in used or v["reset"] == "before_request"
                             or v["initialize"].get("exchange_id") == req.id)
        extractors = tuple({k: val for k, val in e.items()
                            if k not in {"id", "change_id", "exchange_id"}}
                           for e in self.extractors if e["exchange_id"] == req.id)
        return ("scalar", req.method, self.template(r.url),
                tuple((k, self.template(t)) for k, t in r.headers),
                self.template(r.body), declarations, extractors)


def _producer_positions(extractors: list[_Record], exchanges: dict[str, int]
                        ) -> dict[str, tuple[int, int]]:
    producers: dict[str, tuple[int, int]] = {}
    counts: Counter[int] = Counter()
    for e in extractors:
        if (ordinal := exchanges.get(e["exchange_id"])) is not None:
            counts[ordinal] += 1
            producers[e["id"]] = ordinal, counts[ordinal]
    return producers


@dataclass
class _Use:
    caller: Caller
    steps: list[_Pair]
    signatures: list[tuple[object, ...]] = field(default_factory=list)

    @classmethod
    def prepare(cls, caller: Caller, use: StepUse, pos: _Position) -> Self:
        item = cls(caller, caller.select(use.groups, pos))
        by_id = {g.id: g for g in caller.plan.groups}
        groups = [by_id[key] for key in use.groups]
        runtime = (_Runtime.for_range(caller.plan.runtime, groups, pos)
                   if caller.plan.runtime else None)
        requests = {r.id: r for r in caller.plan.requests}
        for i, group in enumerate(groups, 1):
            where = _Position(pos.block, pos.use, i)
            item.signatures.append(tuple(_signature(requests[key], runtime, where)
                                         for key in group.exchange_ids))
        return item


def _signature(req: NativeRequest, runtime: _Runtime | None, pos: _Position) -> tuple[object, ...]:
    if runtime is not None:
        return runtime.signature(req, pos)
    return "static", req.method, req.url, req.headers, req.body


def _equivalent(reference: _Use, candidate: _Use, pos: _Position) -> None:
    if len(reference.steps) != len(candidate.steps):
        missing = min(len(reference.steps), len(candidate.steps)) + 1
        _Position(pos.block, pos.use, missing).reject("RANGE")
    for i, (a, b) in enumerate(zip(reference.signatures, candidate.signatures, strict=True), 1):
        if a != b:
            _Position(pos.block, pos.use, i).reject("MISMATCH")


def _lower_block(output: ET.Element, callers: dict[str, Caller], block: SharedBlock,
                 ordinal: int) -> None:
    names = {node.get("testname", "").lower() for node, _ in _pairs(output)}
    if not TF_LABEL.fullmatch(block.name) or block.name.lower() in names:
        _Position(ordinal).reject("NAME")
    if len(block.uses) < 2 or len({u.uc for u in block.uses}) != len(block.uses):
        _Position(ordinal).reject("RANGE")
    uses: list[_Use] = []
    for i, use in enumerate(block.uses, 1):
        pos = _Position(ordinal, i)
        if use.uc not in callers:
            pos.reject("RANGE")
        item = _Use.prepare(callers[use.uc], use, pos)
        if uses:
            _equivalent(uses[0], item, pos)
        uses.append(item)
    _publish_block(output, block.name, uses, ordinal)


def _publish_block(output: ET.Element, name: str, uses: list[_Use], ordinal: int) -> None:
    _, shared = jmeter._component(output, "TestFragmentController", "TestFragmentControllerGui",
                                  name)
    for use in uses:
        use.caller.route(use.steps, ordinal)
        use.caller.replace(use.steps, name)
    for pair in uses[0].steps:
        shared.extend(pair)


def lower_shared_steps(output: ET.Element, callers: dict[str, Caller],
                       blocks: tuple[SharedBlock, ...]) -> None:
    """Transform a private unpublished tree; an error discards the entire composition."""
    for ordinal, block in enumerate(blocks, 1):
        _lower_block(output, callers, block, ordinal)
