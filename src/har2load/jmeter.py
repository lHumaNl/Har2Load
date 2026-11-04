"""Independent, offline JMX serializer for static and scalar-runtime profiles.

Property contracts were inspected in Apache JMeter rel/v5.5 source; see metadata.
XML structure is not evidence of native loading, wire parity, or runtime safety.
"""

import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import replace
from typing import cast
from urllib.parse import urlsplit

from har2load import jmeter_extensions as extensions
from har2load import jmeter_runtime as runtime
from har2load.domain import Diagnostic, JSONValue
from har2load.generation_types import (
    GENERATION_PROFILE,
    MAX_GENERATED_BYTES,
    NativeGroup,
    NativeRequest,
    RequestPlan,
    TargetBuild,
)
from har2load.runtime_types import (
    EXTENSION_RUNTIME_PROFILE,
    RUNTIME_PROFILE,
    RUNTIME_STATUS_POLICY,
    RuntimePlan,
)

ARTIFACT = "jmeter/scenario.jmx"
TARGET = "jmeter"
PLAN_NAME = "Har2Load"
LABEL_LIMIT = 96
MAX_REQUESTS = 1000
MAX_BODY_BYTES = 1024 * 1024
MAX_TOTAL_BODY_BYTES = 4 * MAX_BODY_BYTES
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
METHODS = BODY_METHODS | {"GET", "HEAD", "OPTIONS", "TRACE"}
XML_INVALID = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
MANAGED_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
    "trailer", "upgrade", "proxy-connection", "cookie", "authorization", "proxy-authorization",
})
SOURCE_BASE = "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/"
SOURCES = (
    "components/src/main/java/org/apache/jmeter/control/ModuleController.java",
    "core/src/main/java/org/apache/jmeter/gui/tree/JMeterTreeModel.java",
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/sampler/HTTPHC4Impl.java",
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/control/CookieManager.java",
    "components/src/main/java/org/apache/jmeter/sampler/DebugSampler.java",
)


class _Rejected(Exception):
    """Only fixed diagnostic codes cross the generation boundary."""


class JMeterGenerator:
    """Build all samplers or return a blocked target with no partial artifact."""

    def generate(self, plan: RequestPlan) -> TargetBuild:
        diagnostics = tuple(replace(item, target=TARGET) for item in plan.diagnostics)
        metadata = _metadata(plan.runtime)
        if any(item.severity == "error" for item in diagnostics):
            return TargetBuild(TARGET, diagnostics=diagnostics, metadata=metadata)
        try:
            _validate(plan)
            configs = runtime.prepare(plan.runtime, MAX_GENERATED_BYTES) if plan.runtime else {}
            root, mappings = _document(plan)
            if plan.runtime is not None:
                _attach_runtime(root, plan.runtime, configs, mappings)
            data = _serialize(root)
        except (_Rejected, runtime.RuntimeRejected) as error:
            diagnostic = Diagnostic(str(error), "error", "JMeter generation is blocked.",
                                    target=TARGET)
            return TargetBuild(TARGET, diagnostics=(*diagnostics, diagnostic), metadata=metadata)
        return TargetBuild(TARGET, {ARTIFACT: data}, tuple(mappings), diagnostics, metadata)


def _metadata(runtime_plan: RuntimePlan | None = None) -> dict[str, JSONValue]:
    authored = bool(runtime_plan and extensions.enabled(runtime_plan))
    return {
        "profile": (EXTENSION_RUNTIME_PROFILE if authored
                    else RUNTIME_PROFILE if runtime_plan else GENERATION_PROFILE),
        "profile_version": "2" if runtime_plan and not authored else "1", "artifact_kind": "jmx",
        **({"output_commit_policy": "per_output_validated_v1"} if authored else {}),
        "native_status_policy": RUNTIME_STATUS_POLICY,
        "compatibility": "unverified", "native_validation": "not_run", "execution": "not_run",
        "intended_baseline": {"jmeter": "5.5", "java": "8"},
        "sources": cast(list[JSONValue], [SOURCE_BASE + path for path in SOURCES] + (
            runtime.SOURCES if runtime_plan else [])),
        "limitations": cast(list[JSONValue], runtime.LIMITATIONS) if runtime_plan else [
            "Structural XML checks do not establish native compatibility or wire parity.",
            "Static literals only; no runtime bindings, extractors, code, or helper execution.",
            "GET, HEAD, OPTIONS and TRACE bodies are unsupported in this profile.",
            "POST/PUT/PATCH/DELETE require explicit Content-Type to avoid native defaults.",
            "Debug Sampler and View Results Tree may expose sensitive live values at runtime.",
            "Cookie state and transport-managed headers are native behavior, not wire literals.",
        ],
    }


def _captured_strings(plan: RequestPlan) -> Iterator[str]:
    for group in plan.groups:
        yield group.id
        yield group.name
    for request in plan.requests:
        yield request.id
        yield request.group_id
        yield request.method
        yield request.url
        for name, value in request.headers:
            yield name
            yield value


def _validate(plan: RequestPlan) -> None:
    if (any(len(request.headers) > 1000 for request in plan.requests)
            or sum(len(request.headers) for request in plan.requests) > 10000):
        raise _Rejected("JMETER_HEADER_LIMIT")
    size = sum(len(value) for value in _captured_strings(plan))
    size += sum(len(request.body or b"") for request in plan.requests)
    if size > MAX_GENERATED_BYTES:
        raise _Rejected("JMETER_OUTPUT_LIMIT")
    _topology(plan)
    for value in _property_strings(plan):
        _literal(value)
    if sum(len(r.body or b"") for r in plan.requests) > MAX_TOTAL_BODY_BYTES:
        raise _Rejected("JMETER_BODY_LIMIT")
    for request in plan.requests:
        _request_valid(request, runtime_plan=plan.runtime is not None)
    if plan.runtime is not None and [r.id for r in plan.runtime.requests] != [
        r.id for r in plan.requests
    ]:
        raise _Rejected("JMETER_RUNTIME_PLAN_INVALID")


def _property_strings(plan: RequestPlan) -> Iterator[str]:
    if plan.runtime is None:
        yield from _captured_strings(plan)
        return
    for group in plan.groups:
        yield group.id
        yield group.name
    for request in plan.requests:
        yield request.id
        yield request.group_id
        yield request.method


def _topology(plan: RequestPlan) -> None:
    requests = {request.id: request for request in plan.requests}
    ids = [eid for group in plan.groups for eid in group.exchange_ids]
    valid = bool(requests) and bool(plan.groups) and len(requests) <= MAX_REQUESTS
    valid &= len(requests) == len(plan.requests) == len(ids) == len(set(ids))
    valid &= set(ids) == set(requests)
    valid &= len({g.id for g in plan.groups}) == len(plan.groups)
    valid &= all(g.id and g.exchange_ids for g in plan.groups)
    if not valid:
        raise _Rejected("JMETER_PLAN_INVALID")
    for group in plan.groups:
        if any(not eid or requests[eid].group_id != group.id for eid in group.exchange_ids):
            raise _Rejected("JMETER_PLAN_INVALID")


def _literal(value: str) -> None:
    if "${" in value:
        raise _Rejected("JMETER_METASYNTAX_UNSUPPORTED")
    if XML_INVALID.search(value):
        raise _Rejected("JMETER_XML_CHARACTER_UNSUPPORTED")


def _request_valid(request: NativeRequest, *, runtime_plan: bool = False) -> None:
    if request.method not in METHODS:
        raise _Rejected("JMETER_METHOD_UNSUPPORTED")
    _url_valid(request.url)
    _headers_valid(request.headers)
    if request.body is not None:
        if len(request.body) > MAX_BODY_BYTES:
            raise _Rejected("JMETER_BODY_LIMIT")
        try:
            text = request.body.decode("utf-8")
            if not runtime_plan:
                _literal(text)
        except UnicodeDecodeError:
            raise _Rejected("JMETER_BODY_ENCODING_UNSUPPORTED") from None
        if request.method not in BODY_METHODS:
            raise _Rejected("JMETER_BODY_METHOD_UNSUPPORTED")
    if request.method in BODY_METHODS and not any(
        name.lower() == "content-type" for name, _ in request.headers
    ):
        raise _Rejected("JMETER_CONTENT_TYPE_REQUIRED")


def _url_valid(url: str) -> None:
    try:
        parts = urlsplit(url)
        valid = parts.scheme in {"http", "https"} and bool(parts.hostname)
        valid &= parts.port is None or parts.port > 0
        valid &= "@" not in parts.netloc and "#" not in url and url.isascii()
        valid &= not any(ord(char) <= 32 or ord(char) == 127 for char in url)
    except ValueError:
        valid = False
    if not valid:
        raise _Rejected("JMETER_URL_UNSUPPORTED")


def _headers_valid(headers: tuple[tuple[str, str], ...]) -> None:
    content_types = 0
    for name, value in headers:
        valid = TOKEN.fullmatch(name) is not None and value.isascii()
        valid &= not any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value)
        valid &= name.lower() not in MANAGED_HEADERS
        if not valid:
            raise _Rejected("JMETER_HEADER_UNSUPPORTED")
        content_types += name.lower() == "content-type"
    if content_types > 1:
        raise _Rejected("JMETER_HEADER_UNSUPPORTED")


def _property(parent: ET.Element, name: str, value: str | bool, tag: str = "stringProp") -> None:
    if isinstance(value, bool):
        tag, value = "boolProp", str(value).lower()
    ET.SubElement(parent, tag, name=name).text = value


def _component(parent: ET.Element, kind: str, gui: str, name: str) -> tuple[ET.Element, ET.Element]:
    node = ET.SubElement(parent, kind, guiclass=gui, testclass=kind, testname=name, enabled="true")
    return node, ET.SubElement(parent, "hashTree")


def _name(prefix: str, ordinal: int, label: str) -> str:
    return f"{prefix}_{ordinal}_{label[:LABEL_LIMIT]}"


def _map(kind: str, entity_id: str, node: ET.Element, path: list[str]) -> dict[str, JSONValue]:
    _property(node, "TestPlan.comments", entity_id)
    return {"kind": kind, "entity_id": entity_id, "artifact": ARTIFACT,
            "locator": {"component_path": [*path, node.attrib["testname"]],
                        "element_type": node.tag}}


def _document(plan: RequestPlan) -> tuple[ET.Element, list[dict[str, JSONValue]]]:
    root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.5")
    top = ET.SubElement(root, "hashTree")
    test_plan, children = _component(top, "TestPlan", "TestPlanGui", PLAN_NAME)
    _property(test_plan, "TestPlan.functional_mode", False)
    _property(test_plan, "TestPlan.serialize_threadgroups", False)
    _arguments(test_plan, "TestPlan.user_defined_variables")
    _cookie_manager(children)
    debug_tree = _thread_group(children)
    mappings: list[dict[str, JSONValue]] = []
    requests = {request.id: request for request in plan.requests}
    for ordinal, group in enumerate(plan.groups, 1):
        _group(children, debug_tree, group, ordinal, requests, mappings, plan.runtime is not None)
    _debug_sampler(debug_tree)
    listener, _ = _component(children, "ResultCollector", "ViewResultsFullVisualizer",
                             "View Results Tree")
    _property(listener, "ResultCollector.error_logging", False)
    _property(listener, "filename", "")
    return root, mappings


def _cookie_manager(parent: ET.Element) -> None:
    manager, _ = _component(parent, "CookieManager", "CookiePanel", "HTTP Cookie Manager")
    ET.SubElement(manager, "collectionProp", name="CookieManager.cookies")
    _property(manager, "CookieManager.clearEachIteration", True)
    _property(manager, "CookieManager.controlledByThreadGroup", False)
    _property(manager, "CookieManager.policy", "standard")
    _property(manager, "CookieManager.implementation",
              "org.apache.jmeter.protocol.http.control.HC4CookieHandler")


def _thread_group(parent: ET.Element, *, setup: bool = False) -> ET.Element:
    group, subtree = _component(parent, "SetupThreadGroup" if setup else "ThreadGroup",
                               "SetupThreadGroupGui" if setup else "ThreadGroupGui",
                               extensions.SETUP_GROUP if setup else "DebugTG")
    _property(group, "ThreadGroup.on_sample_error", "continue")
    _property(group, "ThreadGroup.num_threads", "1")
    _property(group, "ThreadGroup.ramp_time", "0")
    _property(group, "ThreadGroup.scheduler", False)
    loop = ET.SubElement(group, "elementProp", name="ThreadGroup.main_controller",
                         elementType="LoopController", guiclass="LoopControlPanel",
                         testclass="LoopController", testname="LC_Debug", enabled="true")
    _property(loop, "LoopController.continue_forever", False)
    _property(loop, "LoopController.loops", "1", "intProp")
    return subtree


def _debug_sampler(parent: ET.Element) -> None:
    sampler, _ = _component(parent, "DebugSampler", "TestBeanGUI", "Debug Sampler")
    _property(sampler, "displayJMeterVariables", True)
    _property(sampler, "displayJMeterProperties", False)
    _property(sampler, "displaySystemProperties", False)


def _group(parent: ET.Element, debug: ET.Element, group: NativeGroup, ordinal: int,
           requests: dict[str, NativeRequest], maps: list[dict[str, JSONValue]],
           runtime_plan: bool = False) -> None:
    fragment_name = _name("TF", ordinal, group.name)
    fragment, children = _component(parent, "TestFragmentController", "TestFragmentControllerGui",
                                    fragment_name)
    maps.append(_map("group", group.id, fragment, [PLAN_NAME]))
    transaction, samplers = _component(
        children, "TransactionController", "TransactionControllerGui",
        _name("TC", ordinal, group.name))
    _property(transaction, "TransactionController.parent", False)
    _property(transaction, "TransactionController.includeTimers", False)
    maps.append(_map("transaction", group.id, transaction, [PLAN_NAME, fragment_name]))
    _module(debug, group, ordinal, fragment_name)
    path = [PLAN_NAME, fragment_name, transaction.attrib["testname"]]
    for index, request_id in enumerate(group.exchange_ids, 1):
        node = _sampler(samplers, requests[request_id], index, runtime_plan)
        maps.append(_map("request", request_id, node, path))


def _module(parent: ET.Element, group: NativeGroup, ordinal: int, fragment_name: str) -> None:
    module, _ = _component(parent, "ModuleController", "ModuleControllerGui",
                          _name("MC", ordinal, group.name))
    path = ET.SubElement(module, "collectionProp", name="ModuleController.node_path")
    # rel/v5.5 traversal skips segment zero (the GUI tree's synthetic Test Plan root).
    for index, name in enumerate(("Test Plan", PLAN_NAME, fragment_name)):
        _property(path, str(index), name)


def _arguments(parent: ET.Element, name: str) -> ET.Element:
    arguments = ET.SubElement(parent, "elementProp", name=name, elementType="Arguments",
                              guiclass="ArgumentsPanel", testclass="Arguments",
                              testname="Arguments", enabled="true")
    return ET.SubElement(arguments, "collectionProp", name="Arguments.arguments")


def _body(sampler: ET.Element, body: bytes | None) -> None:
    arguments = _arguments(sampler, "HTTPsampler.Arguments")
    _property(sampler, "HTTPSampler.postBodyRaw", body is not None)
    if body is None:
        return
    argument = ET.SubElement(arguments, "elementProp", name="", elementType="HTTPArgument")
    _property(argument, "HTTPArgument.always_encode", False)
    _property(argument, "HTTPArgument.use_equals", False)
    _property(argument, "Argument.name", "")
    _property(argument, "Argument.value", body.decode("utf-8"))
    _property(argument, "Argument.metadata", "=")


def _sampler(parent: ET.Element, request: NativeRequest, ordinal: int,
             runtime_plan: bool = False) -> ET.Element:
    sampler, subtree = _component(parent, "HTTPSamplerProxy", "HttpTestSampleGui",
                                 _name("HTTP", ordinal, request.id))
    _body(sampler, None if runtime_plan else request.body)
    if runtime_plan:
        _property(sampler, "HTTPSampler.path", runtime.FAILED_URL)
        _property(sampler, "HTTPSampler.method", request.method)
    else:
        _address(sampler, request)
    _property(sampler, "HTTPSampler.implementation", "HttpClient4")
    _property(sampler, "HTTPSampler.contentEncoding", "UTF-8")
    for name in ("follow_redirects", "auto_redirects", "image_parser", "DO_MULTIPART_POST"):
        _property(sampler, "HTTPSampler." + name, False)
    _property(sampler, "HTTPSampler.use_keepalive", True)
    if request.headers and not runtime_plan:
        _headers(subtree, request.headers)
    return sampler


def _jsr223(parent: ET.Element, kind: str, name: str, config: str,
            script: str = runtime.SCRIPT) -> ET.Element:
    node, _ = _component(parent, kind, "TestBeanGUI", name)
    for key, value in (("scriptLanguage", "groovy"), ("filename", ""),
                       ("cacheKey", RUNTIME_PROFILE), ("parameters", config),
                       ("script", script)):
        _property(node, key, value)
    return node


def _attach_runtime(root: ET.Element, plan: RuntimePlan, configs: dict[tuple[str, str], str],
                    maps: list[dict[str, JSONValue]]) -> None:
    branches = {node: tree for branch in root.iter("hashTree")
                for node, tree in zip(list(branch)[::2], list(branch)[1::2], strict=True)}
    debug = branches[next(root.iter("ThreadGroup"))]
    temporary = ET.Element("hashTree")
    init = _jsr223(temporary, "JSR223Sampler", "Runtime iteration", configs["iteration_start", ""])
    debug[0:0] = list(temporary)
    locations = {("iteration_start", ""): [PLAN_NAME, "DebugTG", init.attrib["testname"]]}
    _extension_setup(root, plan, configs, locations)
    request_maps = {row["entity_id"]: row for row in maps if row["kind"] == "request"}
    for sampler in root.iter("HTTPSamplerProxy"):
        identifier = sampler.find("stringProp[@name='TestPlan.comments']")
        assert identifier is not None and identifier.text is not None
        row = request_maps[identifier.text]
        path = cast(list[str], cast(dict[str, JSONValue], row["locator"])["component_path"])
        _runtime_processors(branches[sampler], identifier.text, path, configs, locations)
    maps.extend(runtime.source_maps(plan, locations, ARTIFACT))
    maps.extend(extensions.source_maps(plan, locations, ARTIFACT))


def _extension_setup(root: ET.Element, plan: RuntimePlan, configs: dict[tuple[str, str], str],
                     locations: dict[tuple[str, str], list[str]]) -> None:
    if not extensions.enabled(plan):
        return
    temporary = ET.Element("hashTree")
    branch = _thread_group(temporary, setup=True)
    _jsr223(branch, "JSR223Sampler", extensions.SETUP_NAME, configs[extensions.SETUP_EVENT, ""],
            extensions.SETUP_SCRIPT)
    parent = root.find("./hashTree/hashTree")
    assert parent is not None
    parent[0:0] = list(temporary)
    locations[extensions.SETUP_EVENT, ""] = [
        PLAN_NAME, extensions.SETUP_GROUP, extensions.SETUP_NAME]


def _runtime_processors(branch: ET.Element, request_id: str, path: list[str],
                        configs: dict[tuple[str, str], str],
                        locations: dict[tuple[str, str], list[str]]) -> None:
    for event, kind in (("before_request", "JSR223PreProcessor"),
                        ("after_response", "JSR223PostProcessor")):
        node = _jsr223(branch, kind, "Runtime " + event, configs[event, request_id])
        locations[event, request_id] = [*path, node.attrib["testname"]]


def _address(sampler: ET.Element, request: NativeRequest) -> None:
    parts = urlsplit(request.url)
    # Keep escaped query bytes in the path; named arguments would re-encode them.
    path = parts.path or "/"
    if "?" in request.url:
        path += "?" + parts.query
    host = (parts.netloc.split("]", 1)[0] + "]" if parts.netloc.startswith("[")
            else parts.netloc.split(":")[0])
    for name, value in (("method", request.method), ("protocol", parts.scheme), ("domain", host),
                        ("port", str(parts.port) if parts.port is not None else ""),
                        ("path", path)):
        _property(sampler, "HTTPSampler." + name, value)


def _headers(parent: ET.Element, headers: tuple[tuple[str, str], ...]) -> None:
    manager, _ = _component(parent, "HeaderManager", "HeaderPanel", "HTTP Header Manager")
    collection = ET.SubElement(manager, "collectionProp", name="HeaderManager.headers")
    # HTTPHC4Impl.setConnectionHeaders iterates the collection and calls addHeader.
    for name, value in headers:
        header = ET.SubElement(collection, "elementProp", name="", elementType="Header")
        _property(header, "Header.name", name)
        _property(header, "Header.value", value)


class _XMLSink:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, data: bytes) -> int:
        # XML normalizes literal CR on read; references preserve CR and CRLF bodies.
        encoded = data.replace(b"\r", b"&#13;")
        if self.buffer.tell() + len(encoded) > MAX_GENERATED_BYTES:
            raise _Rejected("JMETER_OUTPUT_LIMIT")
        self.buffer.write(encoded)
        return len(data)


def _serialize(root: ET.Element) -> bytes:
    sink = _XMLSink()
    ET.ElementTree(root).write(sink, encoding="utf-8", xml_declaration=True)
    return sink.buffer.getvalue()
