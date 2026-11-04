"""Offline scalar-runtime configuration and fixed inline Groovy, never a runner.

Failed JSON bindings emit markers; successful scalars retain their types. Native parity is
unverified. Jackson Core 2.13.3 is bundled in JMeter 5.5.
"""

import base64
import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from typing import cast
from xml.sax.saxutils import escape

from har2load import jmeter_extensions as extensions
from har2load import jmeter_sources as sources
from har2load.domain import JSONValue
from har2load.extension_runtime import capability_diagnostics
from har2load.extraction import parse_json_path
from har2load.runtime_types import (
    MAX_RUNTIME_FIELD_BYTES,
    MAX_RUNTIME_VALUE_BYTES,
    RuntimePlan,
    RuntimeRequest,
    RuntimeSlot,
    RuntimeTemplate,
)

STATE_KEY = "har2load.scalar_runtime_v1.state"  # Dots cannot occur in model variable names.
FAILED_URL = "https://["  # Invalid IPv6 authority: getUrl fails before any network operation.
MAX_MATCHES = 1000
MAX_DEPTH = 64
SOURCES = [
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/dist/src/dist/expected_release_jars.csv",
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/core/src/main/java/org/apache/jmeter/util/JSR223TestElement.java",
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/core/src/main/java/org/apache/jmeter/testelement/property/StringProperty.java",
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/core/src/main/java/org/apache/jmeter/samplers/SampleResult.java",
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/protocol/java/src/main/java/org/apache/jmeter/protocol/java/sampler/JSR223Sampler.java",
    "https://raw.githubusercontent.com/apache/jmeter/rel/v5.5/src/protocol/http/src/main/java/org/apache/jmeter/protocol/http/sampler/HTTPSamplerBase.java",
    "https://raw.githubusercontent.com/FasterXML/jackson-core/jackson-core-2.13.3/src/main/java/com/fasterxml/jackson/core/JsonParser.java",
    "https://raw.githubusercontent.com/apache/groovy/GROOVY_3_0_11/subprojects/groovy-json/src/main/java/groovy/json/JsonSlurper.java",
]
LIMITATIONS = [
    "Runtime behavior/native loading/wire parity have not been verified.",
    "Native inputs are private literal-encoded UDV strings snapshotted once per thread.",
    "Generators run only at declared initialization; no uniqueness or security-token promise.",
    "Non-opted-in authored extensions are unsupported.",
    "Authored scalar_body_v1 source is unsafe native code, not sandboxed or behavior-verified.",
    "Response header/cookie extraction is unsupported; native header normalization is unverified.",
    "Response bodies require strict UTF-8; absent charset means UTF-8, other charsets fail.",
    "Unsuccessful native HTTP samples fail extraction, including HTTP error responses.",
    "Failed JSON bindings emit ERROR_<name> strings, including numeric/boolean/null declarations.",
    "Correlation/binding/initialization errors log markers without changing native sample status.",
    "Whole-field assembly failure emits an invalid-URL failed sample without sending HTTP.",
    "BigDecimal preserves numeric value, not spelling or the sign of zero; scale is bounded.",
    "Debug Sampler and View Results Tree expose private live values; redaction is off.",
]


class RuntimeRejected(Exception):
    """Fixed, value-free capability code only."""


def templates(request: RuntimeRequest) -> tuple[RuntimeTemplate, ...]:
    return (request.url, *(value for _, value in request.headers), *(
        (request.body,) if request.body is not None else ()))


def validate_runtime(plan: RuntimePlan) -> None:
    diagnostics = capability_diagnostics(plan, "jmeter") + sources.capability_diagnostics(plan)
    if diagnostics:
        raise RuntimeRejected(diagnostics[0].code)
    variables = {cast(str, v["id"]) for v in plan.variables}
    for variable in plan.variables:
        if cast(dict[str, JSONValue], variable["source"])["kind"] not in {
            "literal", "extracted", "agent_code", "external", "generated",
        }:
            raise RuntimeRejected("JMETER_RUNTIME_SOURCE_UNSUPPORTED")
    for extractor in plan.extractors:
        if extractor["area"] != "response_body":
            raise RuntimeRejected("JMETER_RUNTIME_RESPONSE_ITEMS_UNSUPPORTED")
        if cast(dict[str, JSONValue], extractor["selector"])["dialect"] not in {
            "boundary_v1", "json_path_subset_v1",
        }:
            raise RuntimeRejected("JMETER_RUNTIME_SELECTOR_UNSUPPORTED")
    for request in plan.requests:
        _validate_templates(request, variables)


def _validate_templates(request: RuntimeRequest, variables: set[str]) -> None:
    for template in templates(request):
        for part in template.parts:
            if isinstance(part, RuntimeSlot):
                if part.variable_id not in variables or part.encoding not in {
                    "text", "header", "path", "form", "json",
                }:
                    raise RuntimeRejected("JMETER_RUNTIME_PLAN_INVALID")
            else:
                _bounded(part, MAX_RUNTIME_FIELD_BYTES)


def _bounded(text: str, limit: int) -> str:
    try:
        if len(text) > limit or len(text.encode("utf-8")) > limit:
            raise RuntimeRejected("JMETER_RUNTIME_VALUE_LIMIT")
    except UnicodeError:
        raise RuntimeRejected("JMETER_RUNTIME_ENCODING_UNSUPPORTED") from None
    return text


def _variables(plan: RuntimePlan) -> list[JSONValue]:
    result: list[JSONValue] = []
    for declaration in plan.variables:
        variable = deepcopy(declaration)
        source = cast(dict[str, JSONValue], variable["source"])
        sources.source_configuration(source)
        value = source.get("value")
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            text = _bounded(str(value), MAX_RUNTIME_VALUE_BYTES)
            scale = Decimal(text).as_tuple().exponent
            if not isinstance(scale, int) or abs(scale) > MAX_RUNTIME_VALUE_BYTES:
                raise RuntimeRejected("JMETER_RUNTIME_NUMERIC_SCALE_UNSUPPORTED")
            source.update(value=text, numeric=True)
        elif isinstance(value, str):
            _bounded(value, MAX_RUNTIME_VALUE_BYTES)
        result.append(variable)
    return result


def _extractors(plan: RuntimePlan) -> list[JSONValue]:
    result: list[JSONValue] = []
    for declaration in plan.extractors:
        extractor = deepcopy(declaration)
        selector = cast(dict[str, JSONValue], extractor["selector"])
        if selector["dialect"] == "json_path_subset_v1":
            tokens = parse_json_path(cast(str, selector["expression"]))
            selector["tokens"] = [{"key": t} if isinstance(t, str) else {"index": str(t)}
                                  for t in tokens]
        extractor["occurrence"] = str(extractor.get("occurrence", 1))
        result.append(extractor)
    return result


def configuration(plan: RuntimePlan, event: str, request: RuntimeRequest | None = None,
                  *, budget: int) -> str:
    """Encode data in a native property, never interpolate it into Groovy source."""
    config: dict[str, object] = {"variables": _variables(plan), "extractors": _extractors(plan),
              "request": asdict(request) if request is not None else None, "event": event,
              "state_key": STATE_KEY, "value_limit": MAX_RUNTIME_VALUE_BYTES,
              "field_limit": MAX_RUNTIME_FIELD_BYTES, "match_limit": MAX_MATCHES,
               "depth_limit": MAX_DEPTH, "failed_url": FAILED_URL,
               "generator_attempts": sources.REJECTION_ATTEMPTS,
              "extensions": extensions.configuration(plan, setup=event == extensions.SETUP_EVENT)}
    return _encode_configuration(config, budget)


def _encode_configuration(config: dict[str, object], budget: int) -> str:
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    chunks: list[str] = []
    size = 0
    for chunk in encoder.iterencode(config):
        size += len(chunk)  # ensure_ascii guarantees byte and character counts coincide.
        if ((size + 2) // 3) * 4 > budget:
            raise RuntimeRejected("JMETER_OUTPUT_LIMIT")
        chunks.append(chunk)
    return base64.b64encode("".join(chunks).encode("ascii")).decode("ascii")


def prepare(plan: RuntimePlan, budget: int) -> dict[tuple[str, str], str]:
    """Bound repeated script/config amplification before constructing the XML tree."""
    validate_runtime(plan)
    events = [("iteration_start", None), *((event, request) for request in plan.requests
              for event in ("before_request", "after_response"))]
    if extensions.enabled(plan):
        events.insert(0, (extensions.SETUP_EVENT, None))
    script_size = len(escape(SCRIPT).encode("utf-8"))
    if len(events) * script_size > budget:
        raise RuntimeRejected("JMETER_OUTPUT_LIMIT")
    result: dict[tuple[str, str], str] = {}
    size = 0
    for event, request in events:
        encoded = configuration(plan, event, request, budget=budget - size - script_size)
        size += script_size + len(encoded)
        if size > budget:
            raise RuntimeRejected("JMETER_OUTPUT_LIMIT")
        result[event, request.id if request else ""] = encoded
    return result


def _variable_actions(plan: RuntimePlan) -> Iterator[tuple[str, str, str, str]]:
    for variable in plan.variables:
        initializer = cast(dict[str, JSONValue], variable["initialize"])
        identifier = cast(str, variable["id"])
        exchange = cast(str, initializer.get("exchange_id", ""))
        event = cast(str, initializer["event"])
        for kind in ("variable", "initialize"):
            yield kind, identifier, event, exchange
        yield "reset", identifier, cast(str, variable["reset"]), exchange


def _actions(plan: RuntimePlan) -> Iterator[tuple[str, str, str, str]]:
    yield from _variable_actions(plan)
    for extractor in plan.extractors:
        yield ("extractor", cast(str, extractor["id"]), "after_response",
               cast(str, extractor["exchange_id"]))
    for request in plan.requests:
        for template in templates(request):
            for part in template.parts:
                if isinstance(part, RuntimeSlot):
                    yield "binding", part.binding_id, "before_request", request.id


def source_maps(plan: RuntimePlan, locations: dict[tuple[str, str], list[str]],
                artifact: str) -> list[dict[str, JSONValue]]:
    rows: list[dict[str, JSONValue]] = []
    for kind, identifier, event, exchange in _actions(plan):
        key = ("iteration_start", "") if event in {"session_start", "iteration_start"} else (
            event, exchange)
        if key not in locations:
            raise RuntimeRejected("JMETER_RUNTIME_PLAN_INVALID")
        rows.append({"kind": kind, "entity_id": identifier, "target": "jmeter",
                      "artifact": artifact, "locator": {
                          "component_path": cast(list[JSONValue], locations[key]),
                          "element_type": ("JSR223Sampler" if key[0] == "iteration_start" else
                                           "JSR223PreProcessor" if key[0] == "before_request"
                                           else "JSR223PostProcessor"),
                         "script_action": kind, "event": event, "declaration_id": identifier,
                         "additional_component_paths": _reset_paths(plan, kind, identifier,
                                                                    locations)}})
    return rows


def _reset_paths(plan: RuntimePlan, kind: str, identifier: str,
                 locations: dict[tuple[str, str], list[str]]) -> list[JSONValue]:
    if kind != "reset":
        return []
    variable = next(v for v in plan.variables if v["id"] == identifier)
    initializer = cast(dict[str, JSONValue], variable["initialize"])
    source = cast(dict[str, JSONValue], variable["source"])
    if variable["scope"] == "request":
        return [cast(list[JSONValue], path) for (event, _), path in locations.items()
                if event == "before_request"]
    if source["kind"] == "extracted":
        key = ("before_request", cast(str, initializer["exchange_id"]))
        return [cast(list[JSONValue], locations[key])]
    return []


# Programmatic StringProperty assignments bypass pre-run function compilation.
# Runtime declarations use base64 JSON; initial input strings use literal-encoded UDV.
SCRIPT = r'''import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.JsonParser
import com.fasterxml.jackson.core.JsonToken
import java.nio.ByteBuffer
import java.nio.CharBuffer
import java.nio.charset.StandardCharsets
import java.nio.charset.CodingErrorAction
import org.apache.jmeter.config.Arguments
import org.apache.jmeter.protocol.http.control.Header
import org.apache.jmeter.protocol.http.control.HeaderManager

def reader = new JsonSlurper().setCheckDates(false)
def cfg = reader.parseText(new String(Base64.getDecoder().decode(Parameters), 'UTF-8'))
// Correlation consumes the native result but never overrides its success status.
def transportSucceeded = prev != null && prev.isSuccessful()
def fail = { throw new IllegalArgumentException('HAR2LOAD_RUNTIME_INVALID') }
def utf8 = { String text, int maximum ->
    if (text.length() > maximum) fail()
    def encoder = StandardCharsets.UTF_8.newEncoder().onMalformedInput(CodingErrorAction.REPORT)
    def bytes = encoder.encode(CharBuffer.wrap(text))
    if (bytes.remaining() > maximum) fail()
    text
}
def decimal = { String text ->
    def number = new BigDecimal(utf8(text, cfg.value_limit as int))
    if (Math.abs((long) number.scale()) > cfg.value_limit) fail()
    number
}
def scalarText = { value -> value == null ? 'null' : value.toString() }
def jsonScalar = { value ->
    value instanceof String ? JsonOutput.toJson((Object) value) : scalarText(value)
}
def checked = { value, String type ->
    def numeric = value instanceof BigDecimal
    def valid = [string: value instanceof String, boolean: value instanceof Boolean,
                 number: numeric, integer: numeric && value.stripTrailingZeros().scale() <= 0,
                 'null': value == null]
    if (!valid[type]) fail()
    utf8(scalarText(value), cfg.value_limit as int)
    value
}
__HAR2LOAD_INPUT_SNAPSHOT__
def state = vars.getObject(cfg.state_key)
if (state == null) {
    state = [values: [:], errors: [:], started: false,
             initialInputs: initialSnapshot(), random: new Random()]
    vars.putObject(cfg.state_key, state)
}
def mark = { v ->
    def marker = 'ERROR_' + v.name
    state.values[v.id] = [ok: false, value: marker]
    vars.put(v.name, marker)
}
def save = { v, value ->
    def valid = checked(value, v.value_type)
    state.values[v.id] = [ok: true, value: valid]
    vars.put(v.name, scalarText(valid))
}
def readValue
def readContainer = { parser, int depth, boolean object ->
    if (depth >= cfg.depth_limit) fail()
    def result = object ? [:] : []
    def end = object ? JsonToken.END_OBJECT : JsonToken.END_ARRAY
    while (parser.nextToken() != end) {
        if (parser.currentToken() == null) fail()
        if (object) {
            if (parser.currentToken() != JsonToken.FIELD_NAME) fail()
            def key = utf8(parser.currentName, cfg.field_limit as int)
            parser.nextToken()
            result[key] = readValue(parser, depth + 1)
        } else result.add(readValue(parser, depth + 1))
    }
    result
}
readValue = { parser, int depth ->
    def token = parser.currentToken()
    if (token == JsonToken.START_OBJECT) return readContainer(parser, depth, true)
    if (token == JsonToken.START_ARRAY) return readContainer(parser, depth, false)
    if (token == JsonToken.VALUE_STRING) return utf8(parser.text, cfg.field_limit as int)
    if (token == JsonToken.VALUE_NULL) return null
    if (token == JsonToken.VALUE_TRUE) return true
    if (token == JsonToken.VALUE_FALSE) return false
    if (token in [JsonToken.VALUE_NUMBER_INT, JsonToken.VALUE_NUMBER_FLOAT]) {
        return decimal(parser.text)
    }
    fail()
}
def parseJson = { String text ->
    def factory = new JsonFactory().enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
    def parser = factory.createParser(utf8(text, cfg.field_limit as int))
    try {
        parser.nextToken()
        def result = readValue(parser, 0)
        if (parser.nextToken() != null) fail()
        return result
    } finally { parser.close() }
}
def responseText = {
    def bytes = prev.getResponseData()
    if (bytes.length > cfg.field_limit) fail()
    // Do not use JMeter's ISO-8859-1 default or replacement-character decoding.
    def charset = (prev.getDataEncodingNoDefault() ?: 'UTF-8').toUpperCase(Locale.ROOT)
    if (!(charset in ['UTF-8', 'UTF8'])) fail()
    def decoder = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
    decoder.decode(ByteBuffer.wrap(bytes)).toString()
}
def boundaries = { String text, selector ->
    def values = []; int position = 0
    while (values.size() <= cfg.match_limit) {
        int start = text.indexOf(selector.left, position)
        if (start < 0) break
        start += selector.left.length()
        int end = text.indexOf(selector.right, start)
        if (end < 0) break
        values.add(text.substring(start, end))
        position = end + selector.right.length()
    }
    values
}
def matches = { e ->
    def text = responseText()
    if (e.selector.dialect == 'boundary_v1') return boundaries(text, e.selector)
    def value = parseJson(text)
    for (token in e.selector.tokens) {
        if (token.containsKey('key') && value instanceof Map && value.containsKey(token.key)) {
            value = value[token.key]
        } else if (token.containsKey('index') && value instanceof List) {
            def index = new BigInteger(token.index)
            if (index >= value.size()) return []
            value = value[index.intValue()]
        } else return []
    }
    [value]
}
def responseAvailable = { transportSucceeded }
def choose = { e ->
    if (!responseAvailable()) fail()
    def values = matches(e)
    def occurrence = new BigInteger(e.occurrence)
    if (values.size() > cfg.match_limit || occurrence > values.size()) fail()
    if (e.cardinality == 'exactly_one' && values.size() != 1) fail()
    def value = values[occurrence.intValue() - 1]
    if (value == '' && !e.allow_empty) fail()
    if (value instanceof Map || value instanceof List) fail()
    utf8(scalarText(value), cfg.value_limit as int)
    value
}
def results = [:]
def extract = {
    cfg.extractors.findAll { it.exchange_id == cfg.request.id }.each { e ->
        try {
            results[e.id] = [ok: true, value: choose(e)]
            state.errors.remove(e.id)
        } catch (Exception ignored) {
            state.errors[e.id] = 'ERROR_EXTRACTOR_' + e.id
            log.warn('ERROR_EXTRACTOR_' + e.id)
        }
    }
}
__HAR2LOAD_SOURCES__
def initialize = { v ->
    if (v.source.kind == 'agent_code') return
    mark(v)
    try {
        def source = v.source
        if (source.kind == 'literal') {
            save(v, source.numeric ? decimal(source.value) : source.value)
        } else if (source.kind == 'external' || source.kind == 'generated') {
            save(v, source.kind == 'external' ? externalValue(v) : generatedValue(source))
        } else {
            def result = results[source.extractor_id]
            if (result == null || !result.ok) fail()
            save(v, result.value)
        }
    } catch (Exception ignored) { log.warn('ERROR_' + v.name) }
}
def event = { String name ->
    cfg.variables.findAll { it.reset == name }.each { mark(it) }
    cfg.variables.findAll { it.initialize.event == name &&
        (it.initialize.exchange_id == null || it.initialize.exchange_id == cfg.request?.id)
    }.each { initialize(it) }
}
def encodeSlot = { slot ->
    def entry = state.values[slot.variable_id]
    def declaration = cfg.variables.find { it.id == slot.variable_id }
    if (entry == null) { mark(declaration); entry = state.values[slot.variable_id] }
    if (!entry.ok) log.warn('ERROR_BINDING_' + slot.binding_id)
    def value = entry.ok ? entry.value : 'ERROR_' + declaration.name
    if (slot.encoding == 'json') return jsonScalar(value)
    def text = utf8(scalarText(value), cfg.value_limit as int)
    if (slot.encoding == 'header' && text.toCharArray().any { (int) it < 32 || (int) it >= 127 }) {
        log.warn('ERROR_BINDING_' + slot.binding_id); text = 'ERROR_' + declaration.name
    }
    if (slot.encoding in ['path', 'form']) {
        text = java.net.URLEncoder.encode(text, 'UTF-8').replace('*', '%2A').replace('%7E', '~')
        if (slot.encoding == 'path') text = text.replace('+', '%20')
    }
    text
}
def render = { template ->
    def output = new StringBuilder(); int size = 0
    template.parts.each { part ->
        def text = part instanceof String ? part : encodeSlot(part)
        size += utf8(text, cfg.field_limit as int).getBytes('UTF-8').length
        if (size > cfg.field_limit) fail()
        output.append(text)
    }
    output.toString()
}
def assemble = {
    // Install only after every field renders; never send partial or stale requests.
    def url = render(cfg.request.url)
    def manager = new HeaderManager()
    cfg.request.headers.each { pair -> manager.add(new Header(pair[0], render(pair[1]))) }
    def body = cfg.request.body == null ? null : render(cfg.request.body)
    sampler.setArguments(new Arguments())
    sampler.removeProperty('HTTPSampler.header_manager')
    sampler.setHeaderManager(manager)
    sampler.setPostBodyRaw(body != null)
    if (body != null) sampler.addNonEncodedArgument('', body, '')
    // Full URL avoids GET/DELETE query parsing. Assigned strings are not expressions.
    sampler.setPath(url)
}
__HAR2LOAD_EXTENSIONS__
if (cfg.event == 'iteration_start') {
    if (!state.started) { event('session_start'); state.started = true }
    event('iteration_start')
} else if (cfg.event == 'before_request') {
    sampler.setPath(cfg.failed_url)
    cfg.variables.findAll { it.source.kind == 'extracted' &&
        it.initialize.exchange_id == cfg.request.id }.each { mark(it) }
    event('before_request')
    extensionRun()
    try { assemble() } catch (Exception ignored) {
        sampler.setPath(cfg.failed_url)
        log.warn('ERROR_REQUEST_RUNTIME')
    }
} else {
    extract()
    event('after_response')
    extensionRun()
}
return null
'''.lstrip().replace('__HAR2LOAD_EXTENSIONS__', extensions.RUNTIME_SCRIPT).replace(
    '__HAR2LOAD_INPUT_SNAPSHOT__', sources.SNAPSHOT_SCRIPT).replace(
    '__HAR2LOAD_SOURCES__', sources.RUNTIME_SCRIPT)
