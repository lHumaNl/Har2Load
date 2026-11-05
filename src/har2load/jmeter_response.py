"""Fixed native response readers; never execute response data during preparation."""

SOURCES = [
    "https://raw.githubusercontent.com/apache/jmeter/7153c74dd0e860d0ca3825cc5a23e976e0242b6e/src/protocol/http/src/main/java/org/apache/jmeter/protocol/http/sampler/HTTPHC4Impl.java",
    "https://raw.githubusercontent.com/apache/httpcomponents-core/rel/v4.4.15/httpcore/src/main/java/org/apache/http/impl/io/AbstractMessageParser.java",
]
LIMITATIONS = [
    "Response headers consume native-exposed text, not raw HAR rows or wire bytes.",
    "HttpCore can unfold wire obs-fold before capture; only visible folding is rejected.",
    "HC4 decompression can normalize/collapse fields and consumes interim responses; wire/HAR "
    "duplicate parity is unverified.",
    "Header blocks are bounded UTF-8 text; selected values require ASCII and stay strings.",
    "Set-Cookie selection returns whole header data, never a parsed cookie or cookie-jar value.",
    "Response cookie extraction remains unsupported.",
]

# Injected after parseJson: body decoding and JSON semantics intentionally stay unchanged.
RUNTIME_SCRIPT = r'''
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
def headerField = { String line ->
    int colon = line.indexOf(':')
    if (colon <= 0 || !(line.substring(0, colon) ==~ /[!#$%&'*+.^_`|~0-9A-Za-z-]+/)) fail()
    if (line.toCharArray().any { ((int) it < 32 && (int) it != 9) || (int) it == 127 }) fail()
    // Anchored alternatives with an unanchored trailing regex can scan internal OWS quadratically.
    int start = colon + 1; int end = line.length()
    while (start < end && (line.charAt(start) == ' ' || line.charAt(start) == '\t')) start++
    while (end > start && (line.charAt(end - 1) == ' ' || line.charAt(end - 1) == '\t')) end--
    def value = line.substring(start, end)
    [name: line.substring(0, colon).toLowerCase(Locale.ROOT), value: value]
}
def parseHeaders = {
    def raw = prev.getResponseHeaders()
    if (!(raw instanceof String)) fail()
    def text = utf8(raw, cfg.field_limit as int).replace('\r\n', '\n')
    if (text.contains('\r') || text.isEmpty() || text.startsWith('\n')) fail()
    def lines = text.split('\n', -1)
    def fields = []; boolean ended = false
    for (int index = 0; index < lines.length; index++) {
        def line = lines[index]
        if (index == 0 && line ==~ /HTTP\/[0-9]+(?:\.[0-9]+)? [2-5][0-9]{2}(?: [\x20-\x7e]*)?/) {
            continue
        }
        if (line.isEmpty()) { ended = true; continue }
        if (ended || fields.size() >= cfg.header_field_limit) fail()
        fields.add(headerField(line))
    }
    fields
}
// Cache success AND failure only within this processor invocation; never across responses.
def headerCache = [attempted: false, ok: false, fields: []]
def headerMatches = { selector ->
    if (!headerCache.attempted) {
        headerCache.attempted = true
        headerCache.fields = parseHeaders()
        headerCache.ok = true
    }
    if (!headerCache.ok) fail()
    def name = selector.name.toLowerCase(Locale.ROOT)
    headerCache.fields.findAll { it.name == name }.collect { it.value }
}
def matches = { e ->
    if (e.area == 'response_headers') return headerMatches(e.selector)
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
'''
