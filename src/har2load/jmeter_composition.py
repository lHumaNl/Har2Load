"""Compose trusted 0.11 generator artifacts offline; not an importer for arbitrary JMX.

Recover only the generator's data declarations and require a full structural round trip
before moving anything. This checks even configuration that would otherwise be discarded
(notably the listener and the debug module paths). Never execute scripts or resolve paths.
"""

import base64
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from decimal import Decimal, DecimalException
from typing import Any, Never
from xml.parsers import expat

from har2load import jmeter, jmeter_extensions, jmeter_runtime, parameters, values
from har2load.cookies import NativeCookie
from har2load.domain import Har2LoadError, JSONValue
from har2load.generation_types import MAX_GENERATED_BYTES, NativeGroup, NativeRequest, RequestPlan
from har2load.runtime_types import RuntimePlan, RuntimeRequest, RuntimeSlot, RuntimeTemplate

_INVALID = "COMPOSE_JMX_INVALID"
_LIMIT = "COMPOSE_LIMIT"
_AUTHORED = "COMPOSE_AUTHORED_UNSUPPORTED"
_LABEL = re.compile(r"TF_[A-Za-z0-9][A-Za-z0-9_.-]{0,59}")
_MIN_UCS, _MAX_UCS = 2, 16
_DEPTH, _NODES = 48, 128_000  # Per document, before appending the offending element.
_REGISTRY = re.compile(re.escape(jmeter_extensions.REGISTRY_PREFIX) + "[a-f0-9]{64}")
_COMMENTS = "TestPlan.comments"
_ESCAPES = re.compile(r"\\\\|\\(?=\$\{)")
_DATA_ERRORS = (expat.ExpatError, ET.ParseError, KeyError, ValueError, TypeError, AttributeError,
                IndexError, RecursionError, OverflowError, DecimalException)
type _Pair = tuple[ET.Element, ET.Element]


def _reject(code: str = _INVALID) -> Never:
    raise Har2LoadError(code, "JMeter composition is blocked.", 3)


def _require(valid: bool, code: str = _INVALID) -> None:
    if not valid:
        _reject(code)


def _forbidden(*_: object) -> Never:
    _reject()


def _xml_builder(parser: expat.XMLParserType) -> ET.TreeBuilder:
    builder = ET.TreeBuilder()
    depth = count = 0

    def start(tag: str, attrs: dict[str, str]) -> None:
        nonlocal count, depth
        count, depth = count + 1, depth + 1
        _require(depth <= _DEPTH and count <= _NODES, _LIMIT)
        builder.start(tag, attrs)

    def end(tag: str) -> None:
        nonlocal depth
        builder.end(tag)
        depth -= 1

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = builder.data
    return builder


def _parse(data: bytes) -> ET.Element:
    parser = expat.ParserCreate("UTF-8")
    builder = _xml_builder(parser)
    parser.StartDoctypeDeclHandler = parser.CommentHandler = parser.ProcessingInstructionHandler = (
        _forbidden)
    parser.buffer_text = True
    parser.Parse(data, True)
    return builder.close()


def _pairs(branch: ET.Element) -> list[_Pair]:
    _require(branch.tag == "hashTree" and len(branch) % 2 == 0)
    pairs = list(zip(branch[::2], branch[1::2], strict=True))
    _require(all(tree.tag == "hashTree" for _, tree in pairs))
    return pairs


def _prop(node: ET.Element, name: str) -> str:
    item = node.find(f"*[@name='{name}']")
    if item is None:
        _reject()
    return item.text or ""


def _same(actual: ET.Element, expected: ET.Element) -> bool:
    text = actual.text
    if len(actual) and not (text or "").strip():
        text = None  # Allow layout whitespace only between properties, never inside literals.
    return (actual.tag == expected.tag and actual.attrib == expected.attrib
            and text == expected.text and not (actual.tail or "").strip()
            and len(actual) == len(expected) and all(
                _same(a, b) for a, b in zip(actual, expected, strict=True)))


def _cookie(node: ET.Element) -> NativeCookie:
    def value(key: str) -> str:
        return _prop(node, "Cookie." + key)

    # These provenance-only fields and HttpOnly are not serialized by JMeter.
    return NativeCookie(node.attrib["testname"], value("value"), value("domain"), value("path"),
                        value("secure") == "true", False, None, value("domain_specified") == "true",
                        value("path_specified") == "true", "", "", 0)


def _native_request(node: ET.Element, branch: ET.Element, group: str, runtime: bool
                    ) -> NativeRequest:
    method = _prop(node, "HTTPSampler.method")
    identifier = _prop(node, _COMMENTS)
    if runtime:
        # Runtime emits a constant invalid URL and builds the real fields from cfg.request.
        return NativeRequest(identifier, group, method, "https://example.invalid/",
                             (("Content-Type", "text/plain"),))
    port = _prop(node, "HTTPSampler.port")
    address = (_prop(node, "HTTPSampler.protocol") + "://" + _prop(node, "HTTPSampler.domain")
               + (":" + port if port else "") + _prop(node, "HTTPSampler.path"))
    headers = tuple((_prop(h, "Header.name"), _prop(h, "Header.value"))
                    for h in branch.findall(".//elementProp[@elementType='Header']"))
    body = node.find(".//elementProp[@elementType='HTTPArgument']")
    return NativeRequest(identifier, group, method, address, headers,
                         None if body is None else _prop(body, "Argument.value").encode())


def _steps(branch: ET.Element, runtime: bool) -> Iterator[tuple[NativeGroup, list[NativeRequest]]]:
    for i, (step, subtree) in enumerate(_pairs(branch)[2:-1], 1):
        _require(step.tag == "TestFragmentController")
        gid, label = _prop(step, _COMMENTS), step.attrib["testname"].removeprefix(f"TF_{i}_")
        [(_, samplers)] = _pairs(subtree)
        requests = [_native_request(node, children, gid, runtime)
                    for node, children in _pairs(samplers)]
        yield NativeGroup(gid, label, tuple(r.id for r in requests)), requests


def _initials(root: ET.Element) -> dict[str, str]:
    entries = root.findall("./hashTree/TestPlan/elementProp/collectionProp/elementProp")
    return {_prop(e, "Argument.name"): _ESCAPES.sub(lambda m: m[0][1:],
            _prop(e, "Argument.value")) for e in entries}


def _config(node: ET.Element) -> dict[str, Any]:
    _require(_prop(node, "script") == jmeter_runtime.SCRIPT)
    text = base64.b64decode(_prop(node, "parameters"), validate=True).decode("ascii")
    values._depth_check(text)
    config: dict[str, Any] = json.loads(text)
    ext = config["extensions"]
    _require(ext["blocks"] == [] and ext["helpers"] == [], _AUTHORED)
    _require(_REGISTRY.fullmatch(ext["registry_key"]) is not None)
    return config


def _variable(record: dict[str, Any], initials: dict[str, str]) -> dict[str, JSONValue]:
    source = record["source"]
    _require(source["kind"] != "agent_code", _AUTHORED)
    if source["kind"] == "external" and source["key"] in initials:
        source["initial_value"] = initials[source["key"]]
    if source.get("generator") == "integer":
        for key in ("minimum", "maximum"):
            source[key] = int(source[key])
    if source.pop("numeric", False):
        text = source["value"]
        number = Decimal(text)
        source["value"] = number if str(number) == text else float(text)
    return parameters._record("variable", record, stored="change_id" in record)


def _extractor(record: dict[str, Any]) -> dict[str, JSONValue]:
    occurrence = record.pop("occurrence")
    if record["cardinality"] == "occurrence":
        record["occurrence"] = int(occurrence)
    record["selector"].pop("tokens", None)
    return parameters._record("extractor", record, stored="change_id" in record)


def _template(data: dict[str, Any]) -> RuntimeTemplate:
    parts = data["parts"]
    _require(isinstance(parts, list) and
             all(isinstance(p, str) or isinstance(p, dict) and
                 all(isinstance(v, str) for v in p.values()) for p in parts))
    return RuntimeTemplate(tuple(part if isinstance(part, str) else RuntimeSlot(**part)
                                 for part in parts))


def _request(data: dict[str, Any]) -> RuntimeRequest:
    _require(isinstance(data["id"], str) and isinstance(data["headers"], list))
    _require(all(isinstance(name, str) for name, _ in data["headers"]))
    return RuntimeRequest(data["id"], _template(data["url"]),
                          tuple((key, _template(value)) for key, value in data["headers"]),
                          None if data["body"] is None else _template(data["body"]))


def _runtime(root: ET.Element) -> RuntimePlan | None:
    _require(root.find(".//SetupThreadGroup") is None, _AUTHORED)
    init = root.find(".//JSR223Sampler")
    if init is None:
        return None
    cfg, initials = _config(init), _initials(root)
    _require(len(cfg["variables"]) <= parameters.MAX_VARIABLES and
             len(cfg["extractors"]) <= parameters.MAX_EXTRACTORS, _LIMIT)
    variables = tuple(_variable(v, initials) for v in cfg["variables"])
    extractors = tuple(_extractor(e) for e in cfg["extractors"])
    requests = tuple(_request(_config(n)["request"]) for n in root.iter("JSR223PreProcessor"))
    for n in root.iter("JSR223PostProcessor"):
        _config(n)  # Refuse authored declarations even if present only in a later event.
    key = cfg["extensions"]["registry_key"]
    return RuntimePlan(variables, extractors, requests,
                       extension_namespace=key.removeprefix(jmeter_extensions.REGISTRY_PREFIX))


def _verify(root: ET.Element) -> None:
    runtime = _runtime(root)
    groups, lists = zip(*_steps(root[0][1], runtime is not None), strict=True)
    requests = tuple(r for step in lists for r in step)
    cookies = tuple(_cookie(c) for c in root.findall(".//elementProp[@elementType='Cookie']"))
    plan = RequestPlan(requests, groups, runtime=runtime, initial_cookies=cookies)
    result = jmeter.JMeterGenerator().generate(plan)
    _require(result.status == "generated" and
             _same(root, ET.fromstring(result.files[jmeter.ARTIFACT])))


def _input_name(label: str, key: str) -> str:
    # The checked original model keys/names have no dots. Every external read is remapped.
    return f"har2load.input.{label}.{key}"


def _relocate_keys(root: ET.Element, label: str) -> None:
    for node in root.iter():
        if not node.tag.startswith("JSR223"):
            continue
        cfg = _config(node)
        sources = [v["source"] for v in cfg["variables"] if v["source"]["kind"] == "external"]
        if sources:
            for source in sources:
                source["key"] = _input_name(label, source["key"])
            encoded = jmeter_runtime._encode_configuration(cfg, MAX_GENERATED_BYTES)
            for child in node:
                if child.get("name") == "parameters":
                    child.text = encoded


def _user_parameters(branch: ET.Element, plan: ET.Element, label: str) -> None:
    entries = plan.findall("elementProp/collectionProp/elementProp")
    if not entries:
        return
    node, _ = jmeter._component(branch, "UserParameters", "UserParametersGui", "Initial inputs")
    names = ET.SubElement(node, "collectionProp", name="UserParameters.names")
    users = ET.SubElement(node, "collectionProp", name="UserParameters.thread_values")
    values = ET.SubElement(users, "collectionProp", name="0")
    for i, entry in enumerate(entries):
        jmeter._property(names, str(i), _input_name(label, _prop(entry, "Argument.name")))
        jmeter._property(values, str(i), _prop(entry, "Argument.value"))
    jmeter._property(node, "UserParameters.per_iteration", False)


def _debug(debug: ET.Element, cookie: _Pair, plan: ET.Element, label: str) -> None:
    modules = _pairs(debug)
    debug[:] = list(cookie)
    if modules[0][0].tag == "JSR223Sampler":
        init = modules.pop(0)
        debug.extend(init)
        _user_parameters(init[1], plan, label)
    module, children = modules[0]
    module.set("testname", "MC_" + label)
    module[0][2].text = label
    debug.extend((module, children, *modules[-1]))


def _uc(root: ET.Element, label: str, output: ET.Element) -> None:
    _relocate_keys(root, label)
    cookie, (group, debug), *steps, _ = _pairs(root[0][1])
    group.set("testname", "DebugTG_" + label)
    output.extend((group, debug))
    _debug(debug, cookie, root[0][0], label)
    _, wrapped = jmeter._component(output, "TestFragmentController", "TestFragmentControllerGui",
                                   label)
    for fragment, contents in steps:
        fragment.tag = "GenericController"
        fragment.attrib.update(testclass=fragment.tag, guiclass="LogicControllerGui")
        wrapped.extend((fragment, contents))


def _append_input(ordinal: int, label: str, data: bytes, output: ET.Element) -> ET.Element:
    try:
        root = _parse(data)
        _verify(root)
        _uc(root, label, output)
        return root
    except Har2LoadError as error:
        code = error.code if error.code.startswith("COMPOSE_") else _INVALID
    except (jmeter._Rejected, jmeter_runtime.RuntimeRejected):
        code = _LIMIT
    except _DATA_ERRORS:
        code = _INVALID
    raise Har2LoadError(code, f"JMeter composition is blocked for UC input #{ordinal}.", 3)


def _compose(inputs: list[tuple[str, bytes]]) -> bytes:
    _require(_MIN_UCS <= len(inputs) <= _MAX_UCS)
    _require(all(isinstance(data, bytes) for _, data in inputs))
    _require(sum(len(data) for _, data in inputs) <= MAX_GENERATED_BYTES, _LIMIT)
    _require(all(_LABEL.fullmatch(s) for s, _ in inputs) and
             len({s.lower() for s, _ in inputs}) == len(inputs))
    output = ET.Element("hashTree")
    for ordinal, (label, data) in enumerate(inputs, 1):
        root = _append_input(ordinal, label, data, output)
    output.extend(_pairs(root[0][1])[-1])
    plan = root[0][0]
    del plan[2][0][:]  # Confirmed 0.11 TestPlan.user_defined_variables/Arguments.arguments.
    del plan[3:]  # 0.11 private UDV instructions refer to the old, global plan.
    root[0][1] = output
    return jmeter._serialize(root)


def compose_jmx(inputs: list[tuple[str, bytes]]) -> bytes:
    """Return a complete private JMX or a value-free exit-3 error; performs no I/O."""
    try:
        return _compose(inputs)
    except Har2LoadError as error:
        if error.code.startswith("COMPOSE_"):
            raise
    except (jmeter._Rejected, jmeter_runtime.RuntimeRejected):
        _reject(_LIMIT)
    except _DATA_ERRORS:
        pass
    _reject()
