// Synthetic native-exposed text only: SaveService/PreCompiler and real processors, no HTTP.
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import org.apache.jmeter.util.JMeterUtils
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.engine.PreCompiler
import org.apache.jmeter.testbeans.TestBeanHelper
import org.apache.jmeter.testelement.TestPlan
import org.apache.jmeter.threads.JMeterContextService
import org.apache.jmeter.threads.JMeterVariables
import org.apache.jmeter.samplers.SampleResult
import org.apache.jmeter.protocol.http.sampler.HTTPSamplerProxy
import org.apache.jmeter.protocol.http.control.CookieManager
import org.apache.jmeter.protocol.http.control.Cookie
import org.apache.jmeter.modifiers.JSR223PreProcessor
import org.apache.jmeter.extractor.JSR223PostProcessor
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.Level
import org.apache.logging.log4j.core.appender.AbstractAppender
import org.apache.logging.log4j.core.LogEvent
import java.util.concurrent.CountDownLatch

class HeaderLogs extends AbstractAppender {
    final List<Map> entries = Collections.synchronizedList(new ArrayList<Map>())
    HeaderLogs() { super('HeaderLogs', null, null, true) }
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

class HeaderResult extends SampleResult {
    int headerReads
    @Override String getResponseHeaders() { headerReads++; return super.getResponseHeaders() }
}

def reader = new JsonSlurper().setCheckDates(false)
def configuration = { element ->
    new JsonSlurper().setCheckDates(false).parseText(
        new String(Base64.decoder.decode(element.parameters), 'UTF-8'))
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
def requestsFrom = { elements ->
    def requests = [:]
    elements.findAll { it instanceof HTTPSamplerProxy }.each { sampler ->
        requests[sampler.getPropertyAsString('TestPlan.comments')] = [sampler: sampler]
    }
    elements.findAll { it instanceof JSR223PreProcessor || it instanceof JSR223PostProcessor }.each {
        requests[configuration(it).request.id][it instanceof JSR223PreProcessor ? 'pre' : 'post'] = it
    }
    requests
}
def load = { path ->
    def tree = SaveService.loadTree(new File(path))
    tree.traverse(new PreCompiler())
    def elements = flatten(tree)
    elements.findAll { it instanceof org.apache.jmeter.util.JSR223TestElement }.each {
        TestBeanHelper.prepare(it)
        assert it.compile()
    }
    JMeterContextService.getContext().clear()
    def manager = elements.find { it instanceof CookieManager }
    assert manager.clearEachIteration && !manager.controlledByThread
    assert manager.policy == 'standard' && manager.cookieCount == 0
    [elements: elements, requests: requestsFrom(elements), cookies: manager,
     init: elements.find { it.name == 'Runtime iteration' }]
}
def response = { step ->
    def result = new HeaderResult()
    result.setSuccessful(step.containsKey('transport') ? step.transport : true)
    result.setResponseCode(result.isSuccessful() ? '200' : '500')
    result.setResponseHeaders(step.headers)
    result.setResponseData(step.containsKey('response_b64') ?
        Base64.decoder.decode(step.response_b64) : (step.response ?: '{}').getBytes('UTF-8'))
    result.setDataEncoding(step.charset ?: 'UTF-8')
    result
}
def checkValues = { step, vars, state ->
    if (step.values) step.values.each { key, value -> assert vars.get(key) == value }
    if (step.states) step.states.each { key, value -> assert state.values[key].ok == value }
    if (step.states) step.states.findAll { it.value }.each { key, value ->
        assert state.values[key].value instanceof String
    }
}
def checkLogs = { logs, step, count ->
    if (step.warning) assert logs.current().size() > count
    assert !logs.current().any { it.level.isMoreSpecificThan(Level.ERROR) }
    assert logs.current().every { it.message ==~ /ERROR_[A-Za-z0-9_]+/ }
}
def runStep = { step, local, ctx ->
    def request = local.requests[step.request ?: 'r1']
    ctx.setCurrentSampler(request.sampler)
    if (step.event == 'iteration') assert local.init.sample(null).isSuccessful()
    if (step.event == 'pre') request.pre.process()
    if (step.event == 'post') {
        def result = response(step)
        boolean nativeSuccess = result.isSuccessful()
        ctx.setPreviousResult(step.missing_result ? null : result)
        request.post.process()
        assert result.isSuccessful() == nativeSuccess && result.assertionResults.length == 0
        assert result.headerReads == step.header_reads
    }
    if (step.body != null) {
        assert request.sampler.getUrl().host == 'example.invalid'
        assert reader.parseText(request.sampler.arguments.getArgument(0).value) == step.body
    }
}
def runWorker = { local, steps, worker, logs, ready, go ->
    def ctx = JMeterContextService.getContext()
    def vars = new JMeterVariables()
    ctx.setVariables(vars)
    def cfg = configuration(local.init)
    local.cookies.add(new Cookie('SID', 'worker-' + worker, 'example.invalid', '/', false, 0))
    ready.countDown(); go.await()
    steps.each { step ->
        int count = logs.current().size()
        runStep(step, local, ctx)
        checkValues(step, vars, vars.getObject(cfg.state_key))
        checkLogs(logs, step, count)
        assert local.cookies.cookieCount == 1
        assert local.cookies.get(0).value == 'worker-' + worker
    }
    def state = vars.getObject(cfg.state_key)
    ctx.clear()
    state
}
def setupRegistry = { local ->
    def setup = local.elements.find { it.name == 'Authored registry' }
    if (setup == null) return null
    JMeterContextService.getContext().setVariables(new JMeterVariables())
    assert setup.sample(null).isSuccessful()
    JMeterContextService.getContext().clear()
    configuration(setup).extensions.registry_key
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def logs = new HeaderLogs()
logs.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.setLevel(Level.WARN)
logging.configuration.rootLogger.addAppender(logs, Level.WARN, null)
logging.updateLoggers()
def props = JMeterUtils.getJMeterProperties()
def cases = reader.parse(new File(args[1]), 'UTF-8')
// Prove functions could execute at the native precompiler boundary, but header data does not.
def control = SaveService.loadTree(new File(cases[0].jmx))
flatten(control).find { it instanceof TestPlan }.arguments.addArgument(
    'control', '${__groovy(props["h2l.header.executed"]="yes")}')
control.traverse(new PreCompiler())
assert props.remove('h2l.header.executed') == 'yes'
JMeterContextService.getContext().clear()
def negative = new JSR223PreProcessor()
negative.setScriptLanguage('groovy')
negative.setScript("throw new IllegalStateException('SYNTHETIC_HEADER_CONTROL')")
negative.process()
assert logs.current().any { it.level == Level.ERROR }
logs.entries.clear()
def outcomes = []
cases.each { testcase ->
    try {
        logs.entries.clear()
        def copies = (1..2).collect { load(testcase.jmx) }
        def registry = setupRegistry(copies[0])
        def failures = Collections.synchronizedList([])
        def states = Collections.synchronizedList([])
        def ready = new CountDownLatch(2)
        def go = new CountDownLatch(1)
        def stepJson = JsonOutput.toJson(testcase.steps)
        def workers = (0..1).collect { worker ->
            Thread.start('header-worker-' + worker) {
                try {
                    def steps = new JsonSlurper().setCheckDates(false).parseText(
                        stepJson.replace('WORKER', worker.toString()))
                    states.add(runWorker(copies[worker], steps, worker, logs, ready, go))
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
        assert !states[0].values.is(states[1].values)
        assert props.get('h2l.header.executed') == null
        if (registry) props.remove(registry)
        outcomes.add([name: testcase.name, status: 'passed', workers: 2,
            jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
    } catch (Throwable error) {
        outcomes.add([name: testcase.name, status: 'failed', error: error.toString()])
    }
}
println(JsonOutput.toJson([java: System.getProperty('java.runtime.version'),
    jmeter: JMeterUtils.getJMeterVersion(), function_control: 'passed',
    processor_error_control: 'passed', cases: outcomes]))
assert outcomes.every { it.status == 'passed' }
