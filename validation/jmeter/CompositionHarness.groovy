// Trusted generated synthetic JMX only. No engine, controller loop, or HTTP sampling.
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import org.apache.jmeter.JMeter
import org.apache.jmeter.control.ModuleController
import org.apache.jmeter.control.TestFragmentController
import org.apache.jmeter.engine.PreCompiler
import org.apache.jmeter.engine.event.LoopIterationEvent
import org.apache.jmeter.gui.tree.JMeterTreeNode
import org.apache.jmeter.modifiers.JSR223PreProcessor
import org.apache.jmeter.protocol.http.control.CookieManager
import org.apache.jmeter.protocol.http.sampler.HTTPSamplerProxy
import org.apache.jmeter.protocol.java.sampler.JSR223Sampler
import org.apache.jmeter.sampler.DebugSampler
import org.apache.jmeter.samplers.SampleResult
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.testbeans.TestBeanHelper
import org.apache.jmeter.testelement.TestPlan
import org.apache.jmeter.threads.JMeterContextService
import org.apache.jmeter.threads.JMeterVariables
import org.apache.jmeter.threads.TestCompiler
import org.apache.jmeter.threads.ThreadGroup
import org.apache.jmeter.util.JMeterUtils
import org.apache.jorphan.collections.ListedHashTree
import org.apache.logging.log4j.Level
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.core.LogEvent
import org.apache.logging.log4j.core.appender.AbstractAppender

class CompositionErrors extends AbstractAppender {
    final List<String> messages = []
    CompositionErrors() { super('CompositionErrors', null, null, true) }
    @Override void append(LogEvent event) {
        if (event.level.isMoreSpecificThan(Level.ERROR) && event.loggerName.contains('JSR223')) {
            messages.add(event.message.formattedMessage)
        }
    }
}

def walk
walk = { tree, visit ->
    tree.list().each { node ->
        visit(node)
        walk(tree.getTree(node), visit)
    }
}
def guiTree
guiTree = { tree, parent ->
    tree.list().each { node ->
        def child = new JMeterTreeNode(node, null)
        parent.add(child)
        guiTree(tree.getTree(node), child)
    }
}
def one = { items -> assert items.size() == 1; items.first() }
def requestId = { node -> node.getPropertyAsString('TestPlan.comments') }
def ctx = JMeterContextService.getContext()
def prepare = { testcase ->
    def tree = SaveService.loadTree(new File(testcase.path))
    def root = new JMeterTreeNode(new TestPlan(), null)
    guiTree(tree, root)
    def plan = one(tree.list())
    def top = tree.getTree(plan)
    def groups = top.list().findAll { it instanceof ThreadGroup }
    def fragments = top.list().findAll { it instanceof TestFragmentController }
    assert fragments*.name == testcase.groups*.label
    assert groups.size() == fragments.size()
    def managers = []
    walk(tree, { if (it instanceof CookieManager) managers.add(it) })
    assert managers.size() == groups.size()
    def byName = [:]
    groups.each { group ->
        def children = top.getTree(group).list() as List
        def module = one(children.findAll { it instanceof ModuleController })
        def manager = one(children.findAll { it instanceof CookieManager })
        def debug = one(children.findAll { it instanceof DebugSampler })
        assert children.indexOf(module) < children.indexOf(debug)
        module.resolveReplacementSubTree(root)
        def label = module.selectedNode?.testElement?.name
        def expectation = one(testcase.groups.findAll { it.label == label })
        assert module.nodePath.last().toString() == label
        assert module.selectedNode.testElement.is(one(fragments.findAll { it.name == label }))
        def ids = []
        walk(module.replacementSubTree, { if (it.testElement instanceof HTTPSamplerProxy)
            ids.add(requestId(it.testElement)) })
        assert ids == expectation.ids
        def init = children.findAll { it instanceof JSR223Sampler }
        assert init.size() == (expectation.scalar ? 1 : 0)
        if (init) assert children.indexOf(init.first()) < children.indexOf(module)
        assert manager.clearEachIteration && !manager.controlledByThread
        assert !byName.containsKey(group.name)
        byName[group.name] = expectation
    }
    assert (byName.values()*.label as Set) == (testcase.groups*.label as Set)
    // Resolve first, then use JMeter's actual replacement conversion and compiler. Work with
    // the resulting samplers/packages, not with a name-indexed approximation of scope.
    def converted = JMeter.convertSubTree(tree, true)
    converted.traverse(new PreCompiler())
    def initial = [:]
    ctx.variables.entrySet().each { initial[it.key] = it.value }
    assert !initial.containsKey('token')
    assert !initial.values().any { v -> v != null && testcase.groups.any { it.input == v } }
    ctx.clear()
    def planChildren = converted.getTree(one(converted.list()))
    def workers = []
    TestCompiler.initialize()
    planChildren.list().findAll { it instanceof ThreadGroup }.each { group ->
        def local = new ListedHashTree()
        def subtree = planChildren.getTree(group)
        local.set(group, subtree)
        def compiler = new TestCompiler(local)
        local.traverse(compiler)
        def manager = one(subtree.list().findAll { it instanceof CookieManager })
        manager.testStarted()
        def requests = []
        walk(subtree, { if (it instanceof HTTPSamplerProxy) requests.add(it) })
        def spec = byName[group.name]
        assert requests.collect(requestId) == spec.ids
        def vars = new JMeterVariables()
        vars.putAll(initial)
        workers.add([spec: spec, compiler: compiler, vars: vars, group: group,
            manager: manager, init: subtree.list().find { it instanceof JSR223Sampler },
            requests: requests, url: new URL(spec.prefix + '/producer')])
    }
    workers
}
def runSampler = { worker, sampler, inspect, response ->
    ctx.setVariables(worker.vars)
    ctx.setCurrentSampler(sampler)
    ctx.setSamplingStarted(true)
    def pkg = worker.compiler.configureSampler(sampler)
    try {
        pkg.preProcessors.each { TestBeanHelper.prepare(it); it.process() }
        if (sampler instanceof JSR223Sampler) {
            TestBeanHelper.prepare(sampler)
            assert sampler.sample(null).isSuccessful() // Only the fixed runtime initializer.
        } else {
            assert sampler instanceof HTTPSamplerProxy
            assert sampler.cookieManager.is(worker.manager)
            inspect(sampler)
            def result = new SampleResult()
            result.setSuccessful(true)
            result.setResponseCode('200')
            result.setResponseData(response, 'UTF-8')
            ctx.setPreviousResult(result)
            pkg.postProcessors.each { TestBeanHelper.prepare(it); it.process() }
            assert result.successful && result.assertionResults.length == 0
        }
    } finally { worker.compiler.done(pkg) }
}
def check = { testcase ->
    def workers = prepare(testcase)
    assert workers.size() == 2
    def props = JMeterUtils.getJMeterProperties()
    def header = { w -> w.manager.getCookieHeaderForURL(w.url) }
    (1..2).each { iteration ->
        def order = iteration == 1 ? workers : workers.reverse()
        order.each { w ->
            ctx.setVariables(w.vars)
            w.manager.testIterationStart(new LoopIterationEvent(w.group, iteration))
            assert header(w) == 'sid=' + w.spec.seed
            if (w.spec.scalar) {
                runSampler(w, w.init, null, null)
                assert w.vars.get('token') == (w.spec.input == null ? 'ERROR_token' : w.spec.input)
                assert w.vars.get('response_token') == 'ERROR_response_token'
            }
            def peer = one(workers.findAll { !it.is(w) })
            if (peer.spec.input) { // Only a distinctive nonempty value identifies peer leakage.
                assert !w.vars.entrySet().any { it.value == peer.spec.input }
            }
        }
        order.each { w ->
            def other = one(workers.findAll { !it.is(w) })
            def oldOther = header(other)
            def oldOtherResponse = other.vars.get('response_token')
            def marker = w.spec.label + '-' + iteration
            runSampler(w, w.requests[0], { sampler ->
                assert sampler.url.toExternalForm() == w.spec.prefix + '/producer'
                assert header(w) == 'sid=' + w.spec.seed
            }, JsonOutput.toJson([id: marker]))
            w.manager.addCookieFromHeader("sid=${marker}; Path=/app; Secure", w.url)
            assert header(other) == oldOther
            assert other.vars.get('response_token') == oldOtherResponse
            if (w.spec.scalar) assert w.vars.get('response_token') == marker
        }
        order.each { w ->
            def marker = w.spec.label + '-' + iteration
            assert header(w) == 'sid=' + marker
            if (w.spec.scalar) {
                runSampler(w, w.requests[1], { sampler ->
                    assert sampler.url.toExternalForm() == w.spec.prefix + '/consumer'
                    assert new JsonSlurper().parseText(sampler.arguments.getArgument(0).value) ==
                        [input: (w.spec.input == null ? 'ERROR_token' : w.spec.input), response: marker]
                }, '{}')
                assert w.vars.get('response_token') == marker
            }
        }
    }
    assert props.get('h2l.compose.executed') == null
    workers.each { it.manager.testEnded() }
    ctx.clear()
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def errors = new CompositionErrors()
errors.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.addAppender(errors, Level.ERROR, null)
logging.updateLoggers()
// JSR223 processors may swallow exceptions. Prove this harness sees them before trusting it.
def negative = new JSR223PreProcessor()
negative.setScriptLanguage('groovy')
negative.setScript("throw new IllegalStateException('SYNTHETIC_COMPOSITION_CONTROL')")
negative.process()
assert errors.messages.size() == 1
errors.messages.clear()
def cases = new JsonSlurper().parse(new File(args[1]), 'UTF-8')
def outcomes = cases.collect { testcase ->
    def sha = java.security.MessageDigest.getInstance('SHA-256')
        .digest(new File(testcase.path).bytes).encodeHex().toString()
    try {
        errors.messages.clear()
        check(testcase)
        assert errors.messages.empty
        [name: testcase.name, status: 'passed', sha256: sha]
    } catch (Throwable error) {
        def trace = new StringWriter()
        error.printStackTrace(new PrintWriter(trace))
        [name: testcase.name, status: 'failed', sha256: sha, error: trace.toString()]
    } finally { ctx.clear() }
}
println(JsonOutput.toJson([jmeter: JMeterUtils.getJMeterVersion(),
    java: System.getProperty('java.runtime.version'), processor_error_control: 'passed',
    cases: outcomes]))
