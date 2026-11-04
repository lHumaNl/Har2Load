"""Native UDV literal transport and per-thread scalar source implementation.

Only fixed Groovy is emitted here. Preparation never evaluates inputs or generators.
"""

import xml.etree.ElementTree as ET
from typing import cast

from har2load.domain import Diagnostic, JSONValue
from har2load.runtime_sources import initial_values, source_diagnostics
from har2load.runtime_types import RuntimePlan

UDV_ENCODING = "jmeter_udv_literal_v1"
REJECTION_ATTEMPTS = 128
RESERVED_NATIVE_PREFIXES = ("__jm__", "__jmv_")
SOURCE_BASE = ("https://raw.githubusercontent.com/apache/jmeter/"
               "7153c74dd0e860d0ca3825cc5a23e976e0242b6e/src/core/src/main/java/")
SOURCES = [SOURCE_BASE + path for path in (
    "org/apache/jmeter/engine/PreCompiler.java",
    "org/apache/jmeter/engine/util/FunctionParser.java",
    "org/apache/jmeter/engine/util/ReplaceStringWithFunctions.java",
    "org/apache/jmeter/control/IteratingController.java",
    "org/apache/jmeter/control/LoopController.java",
    "org/apache/jmeter/threads/AbstractThreadGroup.java",
    "org/apache/jmeter/threads/JMeterVariables.java",
    "org/apache/jmeter/threads/JMeterThread.java",
    "org/apache/jmeter/util/JMeterUtils.java",
)]


def capability_diagnostics(plan: RuntimePlan) -> list[Diagnostic]:
    """Reject engine-owned namespaces rather than snapshot engine values as input data."""
    diagnostics = source_diagnostics(plan, "jmeter")
    for variable in plan.variables:
        names = [cast(str, variable["name"])]
        source = cast(dict[str, JSONValue], variable["source"])
        if source["kind"] == "external":
            names.append(cast(str, source["key"]))
        if any(name.startswith(RESERVED_NATIVE_PREFIXES) for name in names):
            diagnostics.append(Diagnostic("JMETER_NATIVE_NAME_RESERVED", "error",
                "Native variable name uses a JMeter-owned namespace.",
                entity_id=cast(str, variable["id"]), target="jmeter"))
    return diagnostics


def source_configuration(source: dict[str, JSONValue]) -> None:
    """Strip initial-only data from a copied declaration, preserving the original plan."""
    source.pop("initial_value", None)
    if source.get("generator") == "integer":
        for key in ("minimum", "maximum"):
            source[key] = str(source[key])  # JsonSlurper must never round signed-64 bounds.


def literal_input(value: str) -> str:
    """Keep UDV StringProperty literal through JMeter 5.5 PreCompiler replacement."""
    return value.replace("\\", "\\\\").replace("${", "\\${")


def attach_inputs(test_plan: ET.Element, plan: RuntimePlan) -> None:
    arguments = test_plan.find(".//collectionProp[@name='Arguments.arguments']")
    assert arguments is not None
    supplied = initial_values(plan)
    keys = {cast(str, source["key"]) for v in plan.variables
            if (source := cast(dict[str, JSONValue], v["source"]))["kind"] == "external"}
    for key, value in supplied.items():
        entry = ET.SubElement(arguments, "elementProp", name=key, elementType="Argument")
        for name, text in (("name", key), ("value", literal_input(value)), ("metadata", "=")):
            ET.SubElement(entry, "stringProp", name="Argument." + name).text = text
    ET.SubElement(test_plan, "stringProp", name="TestPlan.comments").text = (
        "Private initial inputs; redaction is off. " + UDV_ENCODING +
        ": double every backslash, then prefix every dollar-brace opener with a backslash. "
        "Never enter executable functions. Missing native keys (add UDV entries): " +
        ", ".join(sorted(keys - supplied.keys())))


# Escapes are decoded as text exactly once, before any runtime mark/save changes vars.
SNAPSHOT_SCRIPT = r'''
final char inputEscape = '\\' as char
final char inputDollar = '$' as char
final char inputOpenBrace = '{' as char
def decodeInitial = { String text ->
    if (text == null) return null
    def result = new StringBuilder()
    for (int i = 0; i < text.length(); i++) {
        if (text.charAt(i) == inputEscape && i + 1 < text.length()) {
            if (text.charAt(i + 1) == inputEscape ||
                (text.charAt(i + 1) == inputDollar && i + 2 < text.length() &&
                 text.charAt(i + 2) == inputOpenBrace)) i++
        }
        result.append(text.charAt(i))
    }
    result.toString()
}
def initialSnapshot = {
    def inputs = [:]
    cfg.variables.findAll { it.source.kind == 'external' }.each { v ->
        if (!inputs.containsKey(v.source.key)) {
            inputs[v.source.key] = decodeInitial(vars.get(v.source.key))
        }
    }
    Collections.unmodifiableMap(inputs)
}
'''.strip()

RUNTIME_SCRIPT = r'''
def externalValue = { v ->
    def text = state.initialInputs[v.source.key]
    if (text == null || (text == '' && !v.source.allow_empty)) fail()
    utf8(text, cfg.value_limit as int)
    v.value_type == 'string' ? text : parseJson(text)
}
def randomInteger = { BigInteger low, BigInteger high ->
    def width = high.subtract(low).add(BigInteger.ONE)
    if (width.signum() <= 0) fail()
    int bits = width.subtract(BigInteger.ONE).bitLength()
    for (int attempt = 0; attempt < cfg.generator_attempts; attempt++) {
        def offset = new BigInteger(bits, (Random) state.random)
        if (offset.compareTo(width) < 0) return low.add(offset)
    }
    fail()
}
def randomString = { source ->
    def text = new StringBuilder()
    for (int i = 0; i < source.length; i++) {
        text.append(source.alphabet.charAt(state.random.nextInt(source.alphabet.length())))
    }
    text.toString()
}
def randomUuid = {
    // RFC 4122 version 4 and IETF variant bits; deliberately not a security-token API.
    final long versionMask = 0xf000L
    final long versionFour = 0x4000L
    final long variantPayloadMask = 0x3fffffffffffffffL
    long most = (state.random.nextLong() & ~versionMask) | versionFour
    long least = (state.random.nextLong() & variantPayloadMask) | Long.MIN_VALUE
    new UUID(most, least).toString()
}
def randomDate = { source ->
    def low = java.time.LocalDate.parse(source.minimum).toEpochDay()
    def high = java.time.LocalDate.parse(source.maximum).toEpochDay()
    def day = randomInteger(BigInteger.valueOf(low), BigInteger.valueOf(high))
    java.time.LocalDate.ofEpochDay(day.longValueExact()).toString()
}
def generatedValue = { source ->
    switch (source.generator) {
        case 'integer': return new BigDecimal(randomInteger(
            new BigInteger(source.minimum), new BigInteger(source.maximum)))
        case 'string': return randomString(source)
        case 'uuid': return randomUuid()
        case 'utc_datetime': return java.time.Instant.now()
            .plusSeconds(source.offset_seconds as long)
            .truncatedTo(java.time.temporal.ChronoUnit.SECONDS).toString()
        case 'utc_date_range': return randomDate(source)
        default: fail()
    }
}
'''.strip()
