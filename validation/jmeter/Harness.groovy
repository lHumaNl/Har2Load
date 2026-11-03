// Synthetic native-object harness only. No StandardJMeterEngine and no HTTP sampler.sample().
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
import org.apache.jmeter.control.ModuleController
import org.apache.jmeter.control.TestFragmentController
import org.apache.jmeter.gui.tree.JMeterTreeNode
import org.apache.jmeter.testelement.TestPlan
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.Level
import org.apache.logging.log4j.core.appender.AbstractAppender
import org.apache.logging.log4j.core.LogEvent

class ProcessorErrors extends AbstractAppender {
    final List<String> errors = Collections.synchronizedList(new ArrayList<String>())
    ProcessorErrors() { super('Har2LoadHarnessErrors', null, null, true) }
    @Override void append(LogEvent event) {
        if (event.level.isMoreSpecificThan(Level.ERROR) && event.loggerName.contains('JSR223')) {
            errors.add(event.message.formattedMessage)
        }
    }
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def collector = new ProcessorErrors()
collector.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.addAppender(collector, Level.ERROR, null)
logging.updateLoggers()
// Prove that swallowed processor ScriptExceptions cannot produce a false PASS.
def negative = new JSR223PreProcessor()
negative.setName('Synthetic negative control')
negative.setScriptLanguage('groovy')
negative.setParameters('')
negative.setScript("throw new IllegalStateException('SYNTHETIC_NEGATIVE_CONTROL')")
negative.process()
assert collector.errors.size() == 1: 'Processor ERROR detector negative control failed'
collector.errors.clear()
def reader = new JsonSlurper().setCheckDates(false)
def manifest = reader.parse(new File(args[1]), 'UTF-8')
def outcomes = []
manifest.each { testcase ->
    try {
        collector.errors.clear()
        def tree = SaveService.loadTree(new File(testcase.jmx))
        def root = new JMeterTreeNode(new TestPlan(), null)
        def elements = []
        def subtrees = [:]
        def walk
        walk = { branch, parent ->
            branch.list().each { element ->
                elements.add(element)
                subtrees[element] = branch.getTree(element)
                def child = new JMeterTreeNode(element, null)
                parent.add(child)
                walk(branch.getTree(element), child)
            }
        }
        walk(tree, root)
        assert elements.findAll { it instanceof HTTPSamplerProxy }.size() == testcase.requests
        def modules = elements.findAll { it instanceof ModuleController }
        assert modules.size() == testcase.groups.size(): 'module count mismatch'
        assert elements.count { it instanceof TestFragmentController } == testcase.groups.size()
        def replacementIds
        replacementIds = { branch ->
            branch.list().collectMany { node ->
                def ids = node.testElement instanceof HTTPSamplerProxy ?
                    [node.testElement.getPropertyAsString('TestPlan.comments')] : []
                ids + replacementIds(branch.getTree(node))
            }
        }
        def resolvedGroups = []
        modules.each { module ->
            module.resolveReplacementSubTree(root)
            assert module.getSelectedNode() != null
            assert !module.getReplacementSubTree().isEmpty()
            def groupId = module.getSelectedNode().testElement.getPropertyAsString('TestPlan.comments')
            resolvedGroups.add(groupId)
            assert testcase.groups.containsKey(groupId)
            assert replacementIds(module.getReplacementSubTree()) == testcase.groups[groupId]
        }
        assert resolvedGroups == testcase.group_order: 'module group order/coverage mismatch'
        if (testcase.mode == 'runtime') {
            def scripts = elements.findAll { it instanceof org.apache.jmeter.util.JSR223TestElement }
            scripts.each { node -> TestBeanHelper.prepare(node); assert node.compile() }
            assert collector.errors.empty
            def init = elements.find { it instanceof JSR223Sampler }
            assert init != null
            def requests = elements.findAll { it instanceof HTTPSamplerProxy }.collectEntries {
                [(it.getPropertyAsString('TestPlan.comments')): it]
            }
            if (testcase.running_version) requests.values().each { it.setRunningVersion(true) }
            def ctx = JMeterContextService.getContext()
            def variables = new JMeterVariables()
            ctx.setVariables(variables)
            ctx.setPreviousResult(null)
            testcase.steps.eachWithIndex { step, index ->
                if (step.event == 'iteration') {
                    def result = init.sample(null) // Only the generated fixed initializer, never HTTP.
                    assert result.isSuccessful() == (step.success != false): "initializer ${index}"
                } else {
                    def sampler = requests[step.request]
                    assert sampler != null
                    ctx.setCurrentSampler(sampler)
                    def children = subtrees[sampler].list()
                    if (step.event == 'pre') {
                        children.find { it instanceof JSR223PreProcessor }.process()
                        if (!step.invalid_url) {
                            assert step.containsKey('url')
                            assert sampler.getUrl().toExternalForm() == step.url
                        }
                        if (step.invalid_url) {
                            boolean rejected = false
                            try { sampler.getUrl() } catch (java.net.MalformedURLException expected) {
                                rejected = true
                            }
                            assert rejected
                        }
                        if (step.containsKey('body') || step.containsKey('body_len')) {
                            assert sampler.getPostBodyRaw()
                            assert sampler.getArguments().getArgumentCount() == 1
                            def argument = sampler.getArguments().getArgument(0)
                            assert argument.name == ''
                            assert !argument.getPropertyAsBoolean('HTTPArgument.always_encode')
                            assert argument.getEncodedValue('UTF-8') == argument.value
                            if (step.containsKey('body')) assert argument.value == step.body:
                                    "body mismatch case=${testcase.name} step=${index}"
                            if (step.containsKey('body_len')) {
                                assert argument.value.length() == step.body_len
                                assert argument.value.endsWith(step.body_suffix)
                            }
                        }
                        if (step.headers) {
                            def headers = sampler.getHeaderManager()
                            step.headers.each { name, expected ->
                                def matches = (0..<headers.size()).collect { headers.get(it) }
                                    .findAll { it.name == name }.collect { it.value }
                                assert matches == expected
                            }
                        }
                    } else {
                        assert step.event == 'post'
                        def response = new SampleResult()
                        response.setSuccessful(step.transport_success != false)
                        response.setResponseCode(step.transport_success == false ? '500' : '200')
                        response.setDataEncoding(step.charset ?: 'UTF-8')
                        response.setResponseData(step.response_b64 ?
                            Base64.decoder.decode(step.response_b64) : step.response.getBytes('UTF-8'))
                        ctx.setPreviousResult(response)
                        children.find { it instanceof JSR223PostProcessor }.process()
                        assert response.isSuccessful() == (step.success != false):
                            "response status case=${testcase.name} step=${index}"
                        if (step.assertions) assert response.getAssertionResults().length > 0
                        if (testcase.running_version) sampler.recoverRunningVersion()
                    }
                }
                assert collector.errors.empty: "Processor execution ERROR: ${collector.errors}"
                if (step.values) step.values.each { name, expected ->
                    assert variables.get(name) == expected: "variable ${name} case=${testcase.name}"
                }
            }
            scripts.each { it.testEnded() }
            ctx.clear()
        }
        assert !testcase.expected_error: 'Negative control unexpectedly accepted'
        outcomes.add([name: testcase.name, mode: testcase.mode, status: 'passed',
                      jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                          .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
    } catch (Throwable error) {
        if (testcase.expected_error && error.toString().contains(testcase.expected_error)) {
            outcomes.add([name: testcase.name, mode: 'negative_control', status: 'passed',
                          expected_rejection: testcase.expected_error,
                          jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                              .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
        } else outcomes.add([name: testcase.name, status: 'failed', error: error.toString()])
    }
}
println(JsonOutput.toJson([java: System.getProperty('java.runtime.version'),
                          java_vendor: System.getProperty('java.vendor'),
                          architecture: System.getProperty('os.arch'),
                          processor_error_negative_control: 'passed',
                          jmeter: JMeterUtils.getJMeterVersion(), cases: outcomes]))
assert outcomes.every { it.status == 'passed' }
