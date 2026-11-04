// Trusted synthetic fixtures only; no HTTP sampler.sample(), engine or network.
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import org.apache.jmeter.util.JMeterUtils
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.testbeans.TestBeanHelper
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
import java.util.concurrent.CountDownLatch

class WrapperLogs extends AbstractAppender {
    final List<Map> entries = Collections.synchronizedList(new ArrayList<Map>())
    WrapperLogs() { super('WrapperLogs', null, null, true) }
    @Override void append(LogEvent event) {
        if (event.loggerName.contains('JSR223') && event.level.isMoreSpecificThan(Level.WARN)) {
            entries.add([thread: Thread.currentThread().id, level: event.level,
                         message: event.message.formattedMessage])
        }
    }
    List<Map> current() {
        synchronized (entries) { return entries.findAll { it.thread == Thread.currentThread().id } }
    }
}

def load = { path ->
    def tree = SaveService.loadTree(new File(path))
    def elements = []
    def walk
    walk = { branch ->
        branch.list().each { node -> elements.add(node); walk(branch.getTree(node)) }
    }
    walk(tree)
    elements.findAll { it instanceof org.apache.jmeter.util.JSR223TestElement }.each {
        TestBeanHelper.prepare(it)
        assert it.compile()
    }
    elements
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def logs = new WrapperLogs()
logs.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.setLevel(Level.WARN)
logging.configuration.rootLogger.addAppender(logs, Level.WARN, null)
logging.updateLoggers()
def negative = new JSR223PreProcessor()
negative.setScriptLanguage('groovy')
negative.setScript("throw new IllegalStateException('SYNTHETIC_ERROR_CONTROL')")
negative.process()
assert logs.current().count { it.level == Level.ERROR } == 1
logs.entries.clear()
def reader = new JsonSlurper().setCheckDates(false)
def manifest = reader.parse(new File(args[1]), 'UTF-8')
def outcomes = []
manifest.each { testcase ->
    try {
        logs.entries.clear()
        def elements = load(testcase.jmx)
        def setup = elements.find { it.name == 'Authored registry' }
        assert setup instanceof JSR223Sampler
        def cfg = reader.parseText(new String(Base64.decoder.decode(setup.parameters), 'UTF-8'))
        def key = cfg.extensions.registry_key
        def props = JMeterUtils.getJMeterProperties()
        def setupContext = JMeterContextService.getContext()
        setupContext.setVariables(new JMeterVariables())
        setupContext.variables.put('worker', 'SETUP_MUST_NOT_LEAK')
        def original = new SampleResult()
        original.setSuccessful(false)
        setupContext.setPreviousResult(original)
        assert setup.sample(null).isSuccessful()
        assert !original.isSuccessful()
        def previousRegistry = props.get(key)
        previousRegistry.stale = 'must disappear'
        assert setup.sample(null).isSuccessful()
        def registry = props.get(key)
        assert !registry.is(previousRegistry) && !registry.containsKey('stale')
        assert !original.isSuccessful() && original.assertionResults.length == 0
        def classes = new LinkedHashMap(registry.blocks)
        def failures = Collections.synchronizedList([])
        def ready = new CountDownLatch(2)
        def go = new CountDownLatch(1)
        // JsonSlurper LazyMap and native TestBean initialization are not shared across workers.
        def workerElements = (1..2).collect { load(testcase.jmx) }
        def stepJson = JsonOutput.toJson(testcase.steps)
        def workers = (1..2).collect { worker ->
            Thread.start('synthetic-worker-' + worker) {
                try {
                    def local = workerElements[worker - 1]
                    def sequence = new JsonSlurper().setCheckDates(false).parseText(stepJson)
                    def ctx = JMeterContextService.getContext()
                    def vars = new JMeterVariables()
                    ctx.setVariables(vars)
                    vars.put('worker', 'worker-' + worker)
                    def sampler = local.find { it instanceof HTTPSamplerProxy }
                    ctx.setCurrentSampler(sampler)
                    def pre = local.find { it instanceof JSR223PreProcessor }
                    def post = local.find { it instanceof JSR223PostProcessor }
                    def init = local.find { it.name == 'Runtime iteration' }
                    ready.countDown()
                    go.await()
                    sequence.each { step ->
                        int start = logs.current().size()
                        def previous = ctx.previousResult
                        def previousSuccess = previous?.isSuccessful()
                        if (step.event == 'iteration') assert init.sample(null).isSuccessful()
                        if (step.event == 'pre') {
                            pre.process()
                            assert sampler.getUrl().host == 'example.invalid'
                            assert sampler.arguments.getArgument(0).value ==
                                step.body.replace('WORKER', 'worker-' + worker)
                        }
                        if (step.event == 'post') {
                            def response = new SampleResult()
                            response.setSuccessful(step.transport)
                            response.setResponseCode(step.transport ? '200' : '500')
                            response.setDataEncoding('UTF-8')
                            response.setResponseData((step.response ?: '{}').getBytes('UTF-8'))
                            ctx.setPreviousResult(response)
                            post.process()
                            assert response.isSuccessful() == step.transport
                            assert response.responseCode == (step.transport ? '200' : '500')
                            assert response.assertionResults.length == 0
                        } else if (previous != null) {
                            assert previous.isSuccessful() == previousSuccess
                            assert previous.assertionResults.length == 0
                        }
                        if (step.values) step.values.each { name, value ->
                            assert vars.get(name) == value: 'variable ' + name
                        }
                        if (step.states) step.states.each { identifier, valid ->
                            assert vars.getObject(cfg.state_key).values[identifier].ok == valid
                        }
                        if (step.warning) assert logs.current().size() > start: 'missing warning'
                        assert !logs.current().any { it.level.isMoreSpecificThan(Level.ERROR) }:
                            logs.current().toString()
                    }
                    assert vars.get('worker') == 'worker-' + worker
                    assert registry.blocks == classes // No per-invocation compilation/replacement.
                    ctx.clear()
                } catch (Throwable error) {
                    def trace = new StringWriter()
                    error.printStackTrace(new PrintWriter(trace))
                    failures.add(trace.toString()); ready.countDown()
                }
            }
        }
        ready.await()
        go.countDown()
        workers.each { it.join() }
        assert failures.empty: failures.toString()
        assert !logs.entries.any { it.level.isMoreSpecificThan(Level.ERROR) }: logs.entries.toString()
        outcomes.add([name: testcase.name, status: 'passed', workers: 2, registry_replaced: true,
            jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
        setupContext.clear()
        props.remove(key)
    } catch (Throwable error) {
        outcomes.add([name: testcase.name, status: 'failed', error: error.toString()])
    }
}
println(JsonOutput.toJson([java: System.getProperty('java.runtime.version'),
    jmeter: JMeterUtils.getJMeterVersion(), cases: outcomes]))
assert outcomes.every { it.status == 'passed' }
