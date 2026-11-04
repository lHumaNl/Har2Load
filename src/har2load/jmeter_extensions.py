"""Data-only authored ABI serialization and emitted native wrappers, never a runner."""

import json
from hashlib import sha256
from typing import cast

from har2load.domain import JSONValue
from har2load.extension_runtime import declarations
from har2load.runtime_types import RuntimePlan

SETUP_EVENT = "extension_setup"
SETUP_GROUP = "Authored setup"
SETUP_NAME = "Authored registry"
REGISTRY_PREFIX = "har2load.scalar_body_v1.registry."


def configuration(plan: RuntimePlan, *, setup: bool = False) -> dict[str, object]:
    blocks, helpers = (declarations(plan, "jmeter", kind) for kind in ("blocks", "helpers"))
    records = {"blocks": blocks, "helpers": helpers}
    namespace = plan.extension_namespace or sha256(json.dumps(
        records, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    if not setup:
        records = {kind: tuple({k: v for k, v in record.items() if k != "source"}
                              for record in items) for kind, items in records.items()}
    return {**records, "registry_key": REGISTRY_PREFIX + namespace}


def enabled(plan: RuntimePlan) -> bool:
    return bool(declarations(plan, "jmeter", "blocks") or declarations(plan, "jmeter", "helpers"))


def _row(kind: str, identifier: str, path: list[str], element: str,
         artifact: str) -> dict[str, JSONValue]:
    return {"kind": kind, "entity_id": identifier, "target": "jmeter", "artifact": artifact,
            "locator": {"component_path": cast(list[JSONValue], path), "element_type": element,
                        "script_action": kind, "declaration_id": identifier}}


def source_maps(plan: RuntimePlan, locations: dict[tuple[str, str], list[str]],
                artifact: str) -> list[dict[str, JSONValue]]:
    rows: list[dict[str, JSONValue]] = []
    for helper in declarations(plan, "jmeter", "helpers"):
        rows.append(_row("helper_definition", cast(str, helper["id"]),
                         locations[SETUP_EVENT, ""], "JSR223Sampler", artifact))
    for block in declarations(plan, "jmeter", "blocks"):
        placement = cast(dict[str, str], block["placement"])
        path = locations[placement["event"], placement["exchange_id"]]
        element = ("JSR223PreProcessor" if placement["event"] == "before_request"
                   else "JSR223PostProcessor")
        identifier = cast(str, block["id"])
        rows.extend(_row(kind, identifier, path, element, artifact)
                    for kind in ("code_block", "reset_outputs"))
        for helper_id in cast(list[str], block["helper_calls"]):
            rows.append({**_row("helper_call", identifier, path, element, artifact),
                         "helper_id": helper_id, "call_evidence": "declared_not_observed"})
    return rows


# Compile only during native setup, using classes with explicit static arguments.
# No Script/Binding instance or closure capturing setup's thread-local objects is cached.
SETUP_SCRIPT = r'''
import groovy.json.JsonSlurper
def cfg = new JsonSlurper().setCheckDates(false).parseText(
    new String(Base64.getDecoder().decode(Parameters), 'UTF-8')).extensions
def registry = [blocks: [:], helpers: [:]]
props.put(cfg.registry_key, registry)
def loader = new GroovyClassLoader(this.class.classLoader)
def compiled = [:]
try {
    ['helpers', 'blocks'].each { kind ->
        cfg[kind].each { declaration ->
            try {
                def signature = kind == 'blocks' ?
                    'Map inputs, Closure emit, Map helpers, Map context' : 'Map inputs, Map helpers'
                def digest = java.security.MessageDigest.getInstance('SHA-256').digest(
                    (kind + '\n' + declaration.source).getBytes('UTF-8')).encodeHex().toString()
                def name = 'Har2Load_' + digest
                def source = 'class ' + name + ' { static Object call(' + signature + ') {\n' +
                    declaration.source + '\n} }\n'
                if (!compiled.containsKey(name)) {
                    compiled[name] = loader.parseClass(source, name + '.groovy')
                }
                registry[kind][declaration.id] = compiled[name]
            } catch (Exception ignored) { log.warn('ERROR_COMPILE_' + declaration.id) }
        }
    }
} finally {
    try { loader.close() } catch (Exception ignored) { log.warn('ERROR_REGISTRY_CLOSE') }
}
return null
'''.lstrip()


# This fragment shares the fixed runtime's checked/save/mark closures. Helpers are
# invocation-local checked callables around shared classes, not shared Script instances.
RUNTIME_SCRIPT = r'''
def authoredScalar = { value, String type ->
    if (value instanceof BigInteger || value instanceof Byte || value instanceof Short ||
        value instanceof Integer || value instanceof Long) value = decimal(value.toString())
    if (value instanceof BigDecimal) value = decimal(value.toString())
    checked(value, type)
}
def extension = cfg.extensions
def registry = extension == null ? null : props.get(extension.registry_key)
def helperDefinitions = (extension?.helpers ?: []).collectEntries { [(it.id): it] }
def helperResult = { value, String type ->
    if (type != 'void') return authoredScalar(value, type)
    if (value != null) fail()
    null
}
def helperCall
def invokeHelper = { definition, arguments ->
    // Groovy passes a sole uncast null as a null varargs array.
    if (arguments == null) arguments = [null] as Object[]
    if (definition == null || arguments.length != definition.parameters.size()) fail()
    def inputs = [:]
    definition.parameters.eachWithIndex { parameter, index ->
        inputs[parameter.name] = authoredScalar(arguments[index], parameter.value_type)
    }
    def dependencies = definition.depends_on.collectEntries { [(it): helperCall(it)] }
    def implementation = registry?.helpers?.get(definition.id)
    if (implementation == null) fail()
    def result = implementation.call(Collections.unmodifiableMap(inputs),
                                      Collections.unmodifiableMap(dependencies))
    helperResult(result, definition.return_type)
}
helperCall = { String identifier ->
    return { Object... arguments -> invokeHelper(helperDefinitions[identifier], arguments) }
}
def blockInputs = { block ->
    block.inputs.collectEntries { identifier ->
        def entry = state.values[identifier]
        if (entry == null || !entry.ok) fail()
        [(identifier): entry.value]
    }
}
def emitter = { outputs ->
    return { String identifier, value ->
        def declaration = outputs[identifier]
        if (declaration == null) fail()
        mark(declaration)
        try { save(declaration, authoredScalar(value, declaration.value_type)) }
        catch (Exception ignored) { log.warn('ERROR_' + declaration.name) }
        null
    }
}
def invokeBlock = { block, outputs ->
    def inputs = blockInputs(block)
    def helpers = block.helper_calls.collectEntries { [(it): helperCall(it)] }
    def implementation = registry?.blocks?.get(block.id)
    if (implementation == null) fail()
    implementation.call(Collections.unmodifiableMap(inputs), emitter(outputs),
        Collections.unmodifiableMap(helpers),
        [vars: vars, ctx: ctx, prev: prev, sampler: sampler, log: log])
}
def extensionRun = {
    if (extension == null) return
    extension.blocks.findAll { it.placement.event == cfg.event &&
        it.placement.exchange_id == cfg.request?.id }.each { block ->
        def outputs = block.outputs.collectEntries { identifier ->
            [(identifier): cfg.variables.find { it.id == identifier }]
        }
        outputs.values().each { mark(it) }
        try { invokeBlock(block, outputs) }
        catch (Exception ignored) { log.warn('ERROR_BLOCK_' + block.id) }
        outputs.values().findAll { !state.values[it.id].ok }.each { log.warn('ERROR_' + it.name) }
    }
}
'''.lstrip()
