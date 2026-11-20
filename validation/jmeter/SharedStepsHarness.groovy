// Trusted synthetic JMX, JMeter 5.5. Never start an engine or invoke an HTTP sampler.
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import org.apache.jmeter.JMeter
import org.apache.jmeter.control.ModuleController
import org.apache.jmeter.control.ReplaceableController
import org.apache.jmeter.control.TestFragmentController
import org.apache.jmeter.control.TransactionController
import org.apache.jmeter.engine.PreCompiler
import org.apache.jmeter.engine.event.LoopIterationEvent
import org.apache.jmeter.gui.tree.JMeterTreeModel
import org.apache.jmeter.modifiers.JSR223PreProcessor
import org.apache.jmeter.protocol.http.control.CookieManager
import org.apache.jmeter.protocol.http.sampler.HTTPSamplerProxy
import org.apache.jmeter.protocol.java.sampler.JSR223Sampler
import org.apache.jmeter.sampler.DebugSampler
import org.apache.jmeter.samplers.SampleResult
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.testbeans.TestBeanHelper
import org.apache.jmeter.threads.JMeterContextService
import org.apache.jmeter.threads.JMeterVariables
import org.apache.jmeter.threads.TestCompiler
import org.apache.jmeter.threads.ThreadGroup
import org.apache.jmeter.util.JMeterUtils
import org.apache.jorphan.collections.ListedHashTree
import org.apache.jorphan.collections.SearchByClass
import org.apache.logging.log4j.Level
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.core.LogEvent
import org.apache.logging.log4j.core.appender.AbstractAppender

class ProcessorErrors extends AbstractAppender {
    final List<String> messages = []
    final List<String> markers = []
    ProcessorErrors() { super('SharedStepErrors', null, null, true) }
    @Override void append(LogEvent event) {
        if (event.loggerName.contains('JSR223') && event.level.isMoreSpecificThan(Level.ERROR)) {
            messages.add(event.message.formattedMessage)
        }
        if (event.level == Level.WARN && event.message.formattedMessage.startsWith('ERROR_')) {
            markers.add(event.message.formattedMessage)
        }
    }
}

def find = { tree, type ->
    def search = new SearchByClass(type)
    tree.traverse(search)
    search.searchResults as List
}
def one = { nodes -> assert nodes.size() == 1; nodes.first() }
def ctx = JMeterContextService.getContext()
def labels = ['TF_Browse', 'TF_Compare']
def shared = 'TF_Catalog'
def errors = new ProcessorErrors()
def id = { it.getPropertyAsString('TestPlan.comments') }
def resolved = []
def prepare = { file ->
    def tree = SaveService.loadTree(file)
    // Same lookup and substitution sequence as JMeter 5.5 runNonGui, including modules
    // beneath root fragments. Do not construct a replacement tree in the fixture.
    def model = new JMeterTreeModel(new Object())
    model.addSubTree(tree, model.root)
    def modules = find(tree, ReplaceableController)
    assert modules.size() == 4
    modules.each { it.resolveReplacementSubTree(model.root) }
    def top = tree.getTree(one(tree.list()))
    def fragments = top.list().findAll { it instanceof TestFragmentController }
    assert fragments*.name.sort() == (labels + shared).sort()
    def fragment = { name -> one(fragments.findAll { it.name == name }) }
    def pointsTo = { module, name ->
        assert module instanceof ModuleController
        assert module.nodePath.last().toString() == name
        assert module.selectedNode.testElement.is(fragment(name))
    }
    resolved = modules.collect { it.selectedNode.testElement.name }.sort()
    def sharedTree = top.getTree(fragment(shared))
    assert find(sharedTree, TransactionController).collect(id) == ['g-catalog', 'g-subcatalog']
    assert find(sharedTree, HTTPSamplerProxy).collect(id) == ['producer', 'subcatalog']
    assert find(tree, CookieManager).size() == 2
    assert find(tree, JSR223Sampler).size() == 2
    def byGroup = [:]
    for (group in top.list().findAll { it instanceof ThreadGroup }) {
        def call = one(find(top.getTree(group), ModuleController))
        def label = call.selectedNode.testElement.name
        assert label in labels
        pointsTo(call, label)
        def uc = top.getTree(fragment(label))
        assert find(uc, HTTPSamplerProxy).collect(id) == ['enter', 'follow']
        pointsTo(one(find(uc, ModuleController)), shared)
        byGroup[group.name] = label
    }
    assert byGroup.values().sort() == labels

    def converted = JMeter.convertSubTree(tree, true)
    converted.traverse(new PreCompiler())
    def initial = [:]
    ctx.variables.entrySet().each { initial[it.key] = it.value }
    assert !initial.values().intersect(['user-0', 'user-1', 'pass-0', 'pass-1'])
    ctx.clear()
    def children = converted.getTree(one(converted.list()))
    def workers = []
    TestCompiler.initialize()
    children.list().findAll { it instanceof ThreadGroup }.each { group ->
        def local = new ListedHashTree()
        def subtree = children.getTree(group)
        local.set(group, subtree)
        def compiler = new TestCompiler(local)
        local.traverse(compiler)
        def jar = one(find(subtree, CookieManager))
        assert jar.clearEachIteration && !jar.controlledByThread
        def vars = new JMeterVariables()
        vars.putAll(initial)
        def label = byGroup[group.name]
        workers.add([label: label, i: labels.indexOf(label), group: group, vars: vars,
                     jar: jar, compiler: compiler])
    }
    assert workers.size() == 2 && !workers[0].jar.is(workers[1].jar)
    workers.sort { it.i }
}

def activate = { w ->
    ctx.setVariables(w.vars)
    ctx.setThreadGroup(w.group)
    ctx.setSamplingStarted(true)
}
def base = 'https://example.invalid/app/'
def cookie = { w -> w.jar.getCookieHeaderForURL(new URL(base + 'catalog')) }
def run = { file, failFirst ->
    def workers = prepare(file)
    workers.each { w ->
        activate(w)
        w.group.initialize()
        w.jar.testStarted()
        w.jar.testIterationStart(new LoopIterationEvent(w.group, 1))
        assert cookie(w) == "sid=seed-${w.i}"
    }
    // Interleave at every sampler boundary. Advance the compiled native controllers,
    // including transactions and nested modules, instead of manually dispatching by ID.
    ['init', 'enter', 'producer', 'subcatalog', 'follow', 'debug'].each { stage ->
        workers.reverse().each { w ->
            activate(w)
            def peer = workers[1 - w.i]
            def oldPeer = [cookie(peer), peer.vars.get('item'), peer.vars.get('username'),
                           peer.vars.get('password')]
            def sampler = w.group.next()
            if (stage == 'debug') {
                assert sampler instanceof DebugSampler
                assert w.group.next() == null
                return
            }
            ctx.setCurrentSampler(sampler)
            def pkg = w.compiler.configureSampler(sampler)
            try {
                errors.markers.clear()
                pkg.preProcessors.each { TestBeanHelper.prepare(it); it.process() }
                if (stage == 'init') {
                    assert sampler instanceof JSR223Sampler
                    TestBeanHelper.prepare(sampler)
                    assert sampler.sample(null).successful // Only the generated fixed initializer.
                    assert w.vars.get('username') == "user-${w.i}"
                    assert w.vars.get('password') == "pass-${w.i}"
                    assert w.vars.get('item') == 'ERROR_item'
                } else {
                    assert sampler instanceof HTTPSamplerProxy && id(sampler) == stage
                    assert sampler.cookieManager.is(w.jar)
                    def path = [enter: ['start-browse','start-compare'][w.i], producer: 'catalog',
                        subcatalog: 'subcatalog', follow: ['view-item','compare-item'][w.i]][stage]
                    assert sampler.url.toExternalForm() == base + path
                    def later = stage in ['subcatalog', 'follow']
                    def token = (failFirst && w.i == 0) ? 'ERROR_item' : "item-${w.i}".toString()
                    def expected = [username: "user-${w.i}".toString(), password: "pass-${w.i}".toString()]
                    if (later) expected.item = token
                    assert new JsonSlurper().parseText(sampler.arguments.getArgument(0).value) == expected
                    assert sampler.cookieManager.getCookieHeaderForURL(sampler.url) ==
                        (later ? "sid=live-${w.i}" : "sid=seed-${w.i}")
                    def response = new SampleResult()
                    response.setSuccessful(true)
                    response.setResponseCode('200')
                    def matched = stage == 'producer' && !(failFirst && w.i == 0)
                    response.setResponseData(matched ? JsonOutput.toJson([id: "item-${w.i}"]) : '{}', 'UTF-8')
                    ctx.setPreviousResult(response)
                    pkg.postProcessors.each { TestBeanHelper.prepare(it); it.process() }
                    assert response.successful && response.assertionResults.length == 0
                    if (stage == 'producer') {
                        sampler.cookieManager.addCookieFromHeader("sid=live-${w.i}; Path=/app; Secure", sampler.url)
                    }
                    if (stage != 'enter') assert w.vars.get('item') == token
                }
                if (failFirst && w.i == 0 && stage == 'producer') {
                    assert errors.markers.contains('ERROR_item')
                } else if (failFirst && w.i == 0 && stage in ['subcatalog', 'follow']) {
                    assert errors.markers.any { it.startsWith('ERROR_BINDING_') }
                } else {
                    assert errors.markers.empty
                }
                assert [cookie(peer), peer.vars.get('item'), peer.vars.get('username'),
                        peer.vars.get('password')] == oldPeer
            } finally { w.compiler.done(pkg) }
        }
    }
    def outcome = workers.collectEntries { [(it.label): it.vars.get('item')] }
    workers.each { it.jar.testEnded() }
    ctx.clear()
    outcome
}

assert System.getProperty('java.specification.version') == '1.8'
System.setProperty(JMeter.JMETER_NON_GUI, 'true')
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
errors.start()
def logging = LogManager.getContext(false)
logging.configuration.rootLogger.addAppender(errors, Level.WARN, null)
logging.configuration.rootLogger.setLevel(Level.WARN)
logging.updateLoggers()
def control = new JSR223PreProcessor()
control.setScriptLanguage('groovy')
control.setScript("throw new IllegalStateException('SYNTHETIC_SHARED_CONTROL')")
control.process()
assert errors.messages.size() == 1
errors.messages.clear()
def file = new File(args[1])
def runs = [false, true].collect { run(file, it) }
assert errors.messages.empty
println(JsonOutput.toJson([jmeter: JMeterUtils.getJMeterVersion(), java: System.getProperty('java.runtime.version'),
    sha256: java.security.MessageDigest.getInstance('SHA-256').digest(file.bytes).encodeHex().toString(),
    module_targets: resolved, runs: runs]))
