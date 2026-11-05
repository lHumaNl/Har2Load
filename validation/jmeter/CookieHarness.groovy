// Trusted synthetic fixtures only: native loading/precompilation and cookie APIs, never sampling.
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import org.apache.jmeter.engine.PreCompiler
import org.apache.jmeter.engine.event.LoopIterationEvent
import org.apache.jmeter.protocol.http.control.CookieManager
import org.apache.jmeter.protocol.http.gui.CookiePanel
import org.apache.jmeter.save.SaveService
import org.apache.jmeter.threads.JMeterContextService
import org.apache.jmeter.threads.JMeterVariables
import org.apache.jmeter.util.JMeterUtils
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

def flatten = { tree ->
    def elements = []
    def walk
    walk = { branch ->
        branch.list().each { node -> elements.add(node); walk(branch.getTree(node)) }
    }
    walk(tree)
    elements
}
def load = { path ->
    def tree = SaveService.loadTree(new File(path))
    def managers = flatten(tree).findAll { it instanceof CookieManager }
    assert managers.size() == 1
    [tree: tree, manager: managers[0]]
}
def prepared = { path ->
    def loaded = load(path)
    loaded.tree.traverse(new PreCompiler())
    def manager = loaded.manager
    assert manager.clearEachIteration && !manager.controlledByThread
    assert manager.policy == 'standard'
    assert manager.implementation == 'org.apache.jmeter.protocol.http.control.HC4CookieHandler'
    manager.testStarted()
    JMeterContextService.getContext().clear()
    manager
}
def properties = { manager, expected ->
    assert manager.cookieCount == expected.size()
    expected.eachWithIndex { seed, index ->
        def cookie = manager.get(index)
        assert manager.cookies.get(index).name == index.toString()
        assert cookie.name == seed.name && cookie.value == seed.value
        assert cookie.domain == seed.domain && cookie.path == seed.path
        assert cookie.secure == seed.secure && cookie.expires == 0 && cookie.version == 0
        assert cookie.isDomainSpecified() == seed.domain_specified
        assert cookie.isPathSpecified() == seed.path_specified
    }
}
def matching = { manager, testcase ->
    testcase.probes.each { probe ->
        assert manager.getCookieHeaderForURL(new URL(probe.url)) == probe.header
    }
}
def responseUpdate = { manager, testcase, worker ->
    def seed = testcase.cookies ? testcase.cookies.last() : null
    def name = seed?.name ?: 'session'
    def path = seed?.path ?: '/app'
    def url = new URL(testcase.probes[0].url)
    def attributes = '; Path=' + path + (seed?.secure != false ? '; Secure' : '')
    if (seed?.domain_specified) attributes += '; Domain=' + seed.domain
    manager.addCookieFromHeader(name + '=worker' + worker + attributes, url)
    assert manager.cookieCount == Math.max(1, testcase.cookies.size())
    name + '=worker' + worker
}
def responseMatching = { manager, testcase, response ->
    if (testcase.cookies.size() > 1) {
        def root = testcase.cookies.first()
        def rootHeader = root.name + '=' + root.value
        assert manager.getCookieHeaderForURL(new URL('https://' + root.domain + '/')) == rootHeader
        response += '; ' + rootHeader
    }
    // Successive requests keep the update and the other worker must not overwrite it.
    (1..2).each {
        assert manager.getCookieHeaderForURL(new URL(testcase.probes[0].url)) == response
    }
}
def lifecycle = { manager, testcase, worker, changed ->
    JMeterContextService.getContext().setVariables(new JMeterVariables())
    manager.testIterationStart(new LoopIterationEvent(manager, 1))
    properties(manager, testcase.cookies)
    matching(manager, testcase)
    def response = responseUpdate(manager, testcase, worker)
    changed.countDown()
    assert changed.await(30, TimeUnit.SECONDS)
    responseMatching(manager, testcase, response)
    manager.testIterationStart(new LoopIterationEvent(manager, 2))
    properties(manager, testcase.cookies)
    matching(manager, testcase)
    JMeterContextService.getContext().clear()
}
def runWorker = { manager, fixture, worker, changed, failures ->
    try {
        // JsonSlurper maps materialize lazily; never share fixture maps across threads.
        def local = new JsonSlurper().parseText(fixture)
        lifecycle(manager, local, worker, changed)
    } catch (Throwable error) {
        def trace = new StringWriter()
        error.printStackTrace(new PrintWriter(trace))
        failures.add(trace.toString())
        changed.countDown()
    } finally { JMeterContextService.getContext().clear() }
}
def workers = { original, testcase ->
    def failures = Collections.synchronizedList([])
    def copies = (0..1).collect { original.clone() }
    assert !copies[0].cookies.is(copies[1].cookies)
    def changed = new CountDownLatch(2)
    def fixture = JsonOutput.toJson(testcase)
    def threads = (0..1).collect { worker ->
        Thread.start('cookie-worker-' + worker) {
            runWorker(copies[worker], fixture, worker, changed, failures)
        }
    }
    threads.each { it.join() }
    assert failures.empty: failures.toString()
    properties(original, testcase.cookies)
    assert !copies[0].cookies.is(copies[1].cookies)
    testcase.cookies.eachWithIndex { seed, index ->
        assert !copies[0].get(index).is(copies[1].get(index))
    }
}
def functionControl = { path ->
    def loaded = load(path)
    def props = JMeterUtils.getJMeterProperties()
    loaded.manager.get(0).setValue('${__groovy(props["h2l.cookie.executed"]="yes")}')
    loaded.tree.traverse(new PreCompiler())
    loaded.manager.testStarted()
    assert loaded.manager.getCookieHeaderForURL(new URL('https://example.invalid/app')) == 'session=yes'
    assert props.remove('h2l.cookie.executed') == 'yes'
    JMeterContextService.getContext().clear()
}
def expiryControl = { path ->
    def manager = prepared(path)
    manager.get(0).setExpires(1L)
    assert manager.get(0).expires == 1L
    // Known limitation: manual seeds bypass the expired-response rejection path.
    assert manager.getCookieHeaderForURL(new URL('https://example.invalid/app')) != null
}
def guiControl = { path ->
    def manager = prepared(path)
    assert !manager.get(0).isDomainSpecified() && !manager.get(0).isPathSpecified()
    manager.get(0).setExpires(1L)
    def panel = new CookiePanel()
    panel.configure(manager)
    def rebuilt = panel.createTestElement()
    assert rebuilt.get(0).expires == 0L
    assert rebuilt.get(0).isDomainSpecified() && rebuilt.get(0).isPathSpecified()
    rebuilt.testStarted()
    assert rebuilt.getCookieHeaderForURL(new URL('https://sub.example.invalid/app')) != null
}
def ipv6Control = { path ->
    def url = new URL('https://[2001:db8::1]/app')
    assert url.host == '[2001:db8::1]'
    [false, true].each { specified ->
        def manager = prepared(path)
        manager.get(0).setDomain('2001:db8::1')
        manager.get(0).setDomainSpecified(specified)
        assert manager.getCookieHeaderForURL(url) == null
    }
}

assert System.getProperty('java.specification.version') == '1.8'
JMeterUtils.setJMeterHome(args[0])
JMeterUtils.loadJMeterProperties(args[0] + '/bin/jmeter.properties')
JMeterUtils.setLocale(Locale.ENGLISH)
assert JMeterUtils.getJMeterVersion() == '5.5'
SaveService.loadProperties()
def cases = new JsonSlurper().parse(new File(args[1]), 'UTF-8')
def control = cases.find { it.name == 'static_host_only' }
functionControl(control.jmx)
expiryControl(control.jmx)
guiControl(control.jmx)
ipv6Control(control.jmx)
def outcomes = []
cases.each { testcase ->
    try {
        def manager = prepared(testcase.jmx)
        properties(manager, testcase.cookies)
        workers(manager, testcase)
        outcomes.add([name: testcase.name, status: 'passed', workers: 2,
            jmx_sha256: java.security.MessageDigest.getInstance('SHA-256')
                .digest(new File(testcase.jmx).bytes).encodeHex().toString()])
    } catch (Throwable error) {
        outcomes.add([name: testcase.name, status: 'failed', error: error.toString()])
    }
}
println(JsonOutput.toJson([java: System.getProperty('java.runtime.version'),
    jmeter: JMeterUtils.getJMeterVersion(), function_negative_control: 'passed',
    past_expiry_negative_control: 'passed', gui_roundtrip_flag_loss_control: 'passed',
    ipv6_host_mismatch_control: 'passed',
    scope: 'SaveService/PreCompiler/native-cookie-APIs; no engine, sampling or wire parity',
    cases: outcomes]))
assert outcomes.every { it.status == 'passed' }
