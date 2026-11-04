// Synthetic opt-in execution only: real precompilation and processors, never HTTP sampling.
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import org.apache.jmeter.util.JMeterUtils
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.engine.PreCompiler
import org.apache.jmeter.testbeans.TestBeanHelper
import org.apache.jmeter.testelement.TestPlan
import org.apache.jmeter.testelement.property.StringProperty
import org.apache.jmeter.threads.JMeterContextService
import org.apache.jmeter.threads.JMeterVariables
import org.apache.jmeter.samplers.SampleResult
import org.apache.jmeter.protocol.java.sampler.JSR223Sampler
import org.apache.jmeter.protocol.http.sampler.HTTPSamplerProxy
import org.apache.jmeter.modifiers.JSR223PreProcessor
import org.apache.jmeter.extractor.JSR223PostProcessor
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.Level
import org.apache.logging.log4j.core.appender.AbstractAppender
import org.apache.logging.log4j.core.LogEvent
import java.time.Instant
import java.time.LocalDate
import java.util.concurrent.CountDownLatch

class SourceLogs extends AbstractAppender {
    final List<Map> entries = Collections.synchronizedList(new ArrayList<Map>())
    SourceLogs() { super('SourceLogs', null, null, true) }
    @Override void append(LogEvent event) {
        if (event.level.isMoreSpecificThan(Level.WARN)) {
            entries.add([thread: Thread.currentThread().id, level: event.level,
                         message: event.message.formattedMessage])
        }
    }
    List<Map> current() {
        synchronized (entries) { return entries.findAll { it.thread == Thread.currentThread().id } }
    }
}

class ControlledBytes extends Random {
    final byte fill
    int calls
    ControlledBytes(int fill) { this.fill = (byte) fill }
    @Override void nextBytes(byte[] bytes) { calls++; Arrays.fill(bytes, fill) }
}

class CountingRandom extends Random {
    int draws
    @Override protected int next(int bits) { draws++; return super.next(bits) }
}

def flatten = { tree ->
    def elements = []
    def walk
    walk = { branch ->
        branch.list().each { node -> elements.add(node); walk(branch.getTree(node)) }
    }
    walk(tree)
    elements
}
def requestElements = { elements ->
    def requests = [:]
    elements.findAll { it instanceof HTTPSamplerProxy }.each { sampler ->
        requests[sampler.getPropertyAsString('TestPlan.comments')] = [sampler: sampler]
    }
    elements.findAll { it instanceof JSR223PreProcessor || it instanceof JSR223PostProcessor }.each {
        def config = new JsonSlurper().parseText(new String(Base64.decoder.decode(it.parameters), 'UTF-8'))
        requests[config.request.id][it instanceof JSR223PreProcessor ? 'pre' : 'post'] = it
    }
    requests
}
def precompile = { path ->
    def tree = SaveService.loadTree(new File(path))
    tree.traverse(new PreCompiler())
    def elements = flatten(tree)
    def plan = elements.find { it instanceof TestPlan }
    plan.arguments.arguments.each { property ->
        assert property.objectValue.getProperty('Argument.value') instanceof StringProperty
    }
    def initial = [:]
    JMeterContextService.getContext().variables.entrySet().each { initial[it.key] = it.value }
    elements.findAll { it instanceof org.apache.jmeter.util.JSR223TestElement }.each {
        TestBeanHelper.prepare(it)
        assert it.compile()
    }
    JMeterContextService.getContext().clear()
    [elements: elements, initial: initial, requests: requestElements(elements)]
}
def controllerCollision = { path, boolean supplied ->
    def tree = SaveService.loadTree(new File(path))
    def elements = flatten(tree)
    def plan = elements.find { it instanceof TestPlan }
    def key = '__jm__DebugTG__idx'
    plan.arguments.getArgument(0).setName(key)
    plan.arguments.getArgument(0).setValue('configured')
    if (!supplied) plan.arguments.removeAllArguments()
    tree.traverse(new PreCompiler())
    def vars = JMeterContextService.getContext().variables
    assert vars.get(key) == (supplied ? 'configured' : null)
    def group = elements.find { it.class == org.apache.jmeter.threads.ThreadGroup }
    def sentinel = new JSR223Sampler() // Controller return only: never sample this object.
    group.addTestElement(sentinel)
    group.initialize()
    assert group.samplerController.name == 'DebugTG'
    assert group.next().is(sentinel)
    assert vars.getObject(key) instanceof Integer && vars.get(key) == '0'
    JMeterContextService.getContext().clear()
}
def checkSpec = { value, spec, long before, long after ->
    if (spec.generator == 'integer') {
        assert value instanceof BigDecimal && value.stripTrailingZeros().scale() <= 0
        assert value >= new BigDecimal(spec.minimum.toString())
        assert value <= new BigDecimal(spec.maximum.toString())
    } else if (spec.generator == 'string') {
        assert value.length() == spec.length
        assert value.toCharArray().every { spec.alphabet.indexOf((int) it) >= 0 }
    } else if (spec.generator == 'uuid') {
        assert value ==~ /[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/
        assert UUID.fromString(value).version() == 4 && UUID.fromString(value).variant() == 2
    } else if (spec.generator == 'utc_datetime') {
        assert value ==~ /\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/
        def seconds = Instant.parse(value).epochSecond - spec.offset_seconds
        assert before <= seconds && seconds <= after
    } else if (spec.generator == 'utc_date_range') {
        assert value ==~ /\d{4}-\d\d-\d\d/
        assert !LocalDate.parse(value).isBefore(LocalDate.parse(spec.minimum))
        assert !LocalDate.parse(value).isAfter(LocalDate.parse(spec.maximum))
    } else assert false
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def logs = new SourceLogs()
logs.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.setLevel(Level.WARN)
logging.configuration.rootLogger.addAppender(logs, Level.WARN, null)
logging.updateLoggers()
def props = JMeterUtils.getJMeterProperties()
def reader = new JsonSlurper().setCheckDates(false)
def cases = reader.parse(new File(args[1]), 'UTF-8')
controllerCollision(cases[0].jmx, true)
controllerCollision(cases[0].jmx, false)
// JMeterThread.initRun writes a Boolean, not a string; exported names must not corrupt it.
def namespaceVars = new JMeterVariables()
namespaceVars.putObject('__jmv_SAME_USER', true)
assert namespaceVars.isSameUserOnNextIteration()
namespaceVars.put('__jmv_SAME_USER', 'true')
assert !namespaceVars.isSameUserOnNextIteration()
// Prove that the same native traversal executes an UNESCAPED function, not a mock decoder.
def controlTree = SaveService.loadTree(new File(cases.find { it.name == 'literal_udv_5' }.jmx))
def controlPlan = flatten(controlTree).find { it instanceof TestPlan }
controlPlan.arguments.getArgument(0).setValue('${__groovy(props["h2l.synthetic.executed"]="yes")}')
controlTree.traverse(new PreCompiler())
assert props.remove('h2l.synthetic.executed') == 'yes'
JMeterContextService.getContext().clear()
// Native processor errors must be observable, not silently swallowed by the harness.
def negative = new JSR223PreProcessor()
negative.setScriptLanguage('groovy')
negative.setScript("throw new IllegalStateException('SYNTHETIC_SOURCE_CONTROL')")
negative.process()
assert logs.current().any { it.level == Level.ERROR }
logs.entries.clear()
def outcomes = []
cases.each { testcase ->
    try {
        logs.entries.clear()
        def copies = (1..2).collect { precompile(testcase.jmx) }
        assert props.get('h2l.synthetic.executed') == null
        def setup = copies[0].elements.find { it.name == 'Authored registry' }
        def registryKey
        if (setup != null) {
            def cfg = reader.parseText(new String(Base64.decoder.decode(setup.parameters), 'UTF-8'))
            registryKey = cfg.extensions.registry_key
            JMeterContextService.getContext().setVariables(new JMeterVariables())
            assert setup.sample(null).isSuccessful()
            JMeterContextService.getContext().clear()
        }
        def failures = Collections.synchronizedList([])
        def states = Collections.synchronizedList([])
        def ready = new CountDownLatch(2)
        def go = new CountDownLatch(1)
        def stepJson = JsonOutput.toJson(testcase.steps)
        def workers = (0..1).collect { worker ->
            Thread.start('source-worker-' + worker) {
                try {
                    def local = copies[worker]
                    def sequence = new JsonSlurper().setCheckDates(false).parseText(
                        stepJson.replace('WORKER', worker.toString()))
                    def ctx = JMeterContextService.getContext()
                    def vars = new JMeterVariables()
                    vars.putAll(local.initial)
                    ctx.setVariables(vars)
                    def sampler = local.requests.values().first().sampler
                    def init = local.elements.find { it.name == 'Runtime iteration' }
                    def cfg = new JsonSlurper().parseText(
                        new String(Base64.decoder.decode(init.parameters), 'UTF-8'))
                    ctx.setCurrentSampler(sampler)
                    def previous = new SampleResult()
                    previous.setSuccessful(false)
                    ctx.setPreviousResult(previous)
                    def remembered
                    def random
                    ready.countDown(); go.await()
                    sequence.each { step ->
                        def request = local.requests[step.request ?: local.requests.keySet().first()]
                        sampler = request.sampler
                        ctx.setCurrentSampler(sampler)
                        int count = logs.current().size()
                        if (step.overwrite) step.overwrite.each { key, value -> vars.put(key, value) }
                        if (step.containsKey('random_fill')) {
                            random = new ControlledBytes(step.random_fill as int)
                            vars.getObject(cfg.state_key).random = random
                        }
                        if (step.tracking_random) {
                            random = new CountingRandom()
                            vars.getObject(cfg.state_key).random = random
                        }
                        long before = Instant.now().epochSecond
                        if (step.event == 'iteration') assert init.sample(null).isSuccessful()
                        if (step.event == 'pre') request.pre.process()
                        if (step.event == 'post') {
                            previous = new SampleResult()
                            previous.setSuccessful(step.transport)
                            previous.setResponseCode(step.transport ? '200' : '500')
                            previous.setDataEncoding('UTF-8')
                            previous.setResponseData(step.response ?: '{}', 'UTF-8')
                            ctx.setPreviousResult(previous)
                            request.post.process()
                        }
                        assert previous.isSuccessful() == (step.event == 'post' ? step.transport :
                            previous.responseCode == '200')
                        assert previous.assertionResults.length == 0
                        def state = vars.getObject(cfg.state_key)
                        if (random != null) assert state.random.is(random)
                        random = state?.random
                        if (step.containsKey('draws')) assert random.draws == step.draws
                        if (step.containsKey('random_calls')) assert random.calls == step.random_calls
                        def entry = state?.values?.v1
                        if (step.uninitialized) step.uninitialized.each { identifier ->
                            assert state?.values?.get(identifier)?.ok != true
                        }
                        if (step.spec) { assert entry.ok; checkSpec(entry.value, step.spec,
                            before, Instant.now().epochSecond) }
                        if (step.values) step.values.each { key, value ->
                            assert vars.get(key) == (value == null ? 'null' : value.toString())
                        }
                        if (step.states) step.states.each { key, value ->
                            assert state.values[key].ok == value
                        }
                        if (step.remember) remembered = entry.value
                        if (step.reuse || step.same) assert entry.value == remembered
                        if (step.changed) assert entry.value != remembered
                        if (step.wrapped) {
                            assert state.values.out.ok && state.values.out.value == entry.value.toString()
                        }
                        if (step.event == 'pre') {
                            assert sampler.getUrl().host == 'example.invalid'
                            def body = sampler.arguments.getArgument(0).value
                            if (step.body) assert reader.parseText(body) == step.body
                            if (step.raw_body) assert body == step.raw_body
                            if (step.reuse) assert reader.parseText(body).x == remembered
                            if (step.wrapped) {
                                def rendered = reader.parseText(body)
                                assert rendered.x == entry.value && rendered.wrapped == state.values.out.value
                            }
                        }
                        if (step.warning) assert logs.current().size() > count
                        assert !logs.current().any { it.level.isMoreSpecificThan(Level.ERROR) }
                        assert logs.current().every { it.message ==~ /ERROR_[A-Za-z0-9_]+/ }
                    }
                    states.add(vars.getObject(cfg.state_key))
                    try {
                        vars.getObject(cfg.state_key).initialInputs.put('mutate', 'forbidden')
                        assert false: 'snapshot is mutable'
                    } catch (UnsupportedOperationException expected) { }
                    ctx.clear()
                } catch (Throwable error) {
                    def trace = new StringWriter()
                    error.printStackTrace(new PrintWriter(trace))
                    failures.add(trace.toString()); ready.countDown()
                }
            }
        }
        ready.await(); go.countDown(); workers.each { it.join() }
        assert failures.empty: failures.toString()
        assert states.size() == 2 && !states[0].is(states[1])
        assert !states[0].initialInputs.is(states[1].initialInputs)
        assert !states[0].random.is(states[1].random)
        assert props.get('h2l.synthetic.executed') == null
        if (registryKey) props.remove(registryKey)
        outcomes.add([name: testcase.name, status: 'passed', workers: 2,
            jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
    } catch (Throwable error) {
        outcomes.add([name: testcase.name, status: 'failed', error: error.toString()])
    }
}
println(JsonOutput.toJson([java: System.getProperty('java.runtime.version'),
    jmeter: JMeterUtils.getJMeterVersion(), udv_function_negative_control: 'passed',
    controller_collision_control: 'passed', same_user_namespace_control: 'passed', cases: outcomes]))
assert outcomes.every { it.status == 'passed' }
