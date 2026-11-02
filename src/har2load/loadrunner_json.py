"""Emitted bounded JSON parser; data-only preparation, never compile or evaluate C."""

from collections.abc import Iterator

from har2load.values import MAX_JSON_DEPTH

MAX_JSON_NODES = 4096
MAX_OBJECT_KEYS = 1024
MAX_NUMBER_EXPONENT = 10000

# Tokens own decoded keys/strings and exact numeric lexemes. All objects, including
# unselected subtrees, are validated before selection. No native JSON type inference.
_PARSER = r'''
typedef struct {
    int type, integral, first, next;
    char *key, *text;
} h2l_node;
typedef struct {
    const char *p, *end;
    h2l_node *nodes;
    int used, failed;
} h2l_json;
typedef struct { const char *key; unsigned long index; } h2l_path;
static int h2l_digit(int c) { return c >= '0' && c <= '9'; }
static void h2l_space(h2l_json *j)
{
    while (j->p < j->end && strchr(" \t\r\n", *j->p)) ++j->p;
}
static int h2l_take(h2l_json *j, int c)
{
    h2l_space(j);
    if (j->p == j->end || *j->p != c) { j->failed = 1; return 0; }
    ++j->p; return 1;
}
static char *h2l_copy(const char *s, size_t n)
{
    char *out = (char*)malloc(n + 1);
    if (out) { memcpy(out, s, n); out[n] = 0; }
    return out;
}
static unsigned long h2l_hex4(h2l_json *j)
{
    unsigned long value = 0; int i, c;
    if (j->end - j->p < 4) { j->failed = 1; return 0; }
    for (i = 0; i < 4; ++i) {
        c = (unsigned char)*j->p++;
        if (c >= '0' && c <= '9') c -= '0';
        else if (c >= 'a' && c <= 'f') c -= 'a' - 10;
        else if (c >= 'A' && c <= 'F') c -= 'A' - 10;
        else { j->failed = 1; return 0; }
        value = value * 16 + c;
    }
    return value;
}
static unsigned long h2l_codepoint(h2l_json *j)
{
    unsigned long cp = h2l_hex4(j), low;
    if (cp >= 55296 && cp <= 56319) {
        if (j->end - j->p < 6 || j->p[0] != '\\' || j->p[1] != 'u') {
            j->failed = 1; return 0;
        }
        j->p += 2; low = h2l_hex4(j);
        if (low < 56320 || low > 57343) { j->failed = 1; return 0; }
        cp = 65536 + (cp - 55296) * 1024 + low - 56320;
    } else if (cp >= 56320 && cp <= 57343) j->failed = 1;
    if (!cp) j->failed = 1; /* Embedded NUL is an explicit profile failure. */
    return cp;
}
static void h2l_unicode(char *out, size_t *n, unsigned long cp)
{
    if (cp < 128) out[(*n)++] = (char)cp;
    else {
        if (cp < 2048) out[(*n)++] = (char)(192 | (cp >> 6));
        else {
            if (cp < 65536) out[(*n)++] = (char)(224 | (cp >> 12));
            else { out[(*n)++] = (char)(240 | (cp >> 18));
                out[(*n)++] = (char)(128 | ((cp >> 12) & 63)); }
            out[(*n)++] = (char)(128 | ((cp >> 6) & 63));
        }
        out[(*n)++] = (char)(128 | (cp & 63));
    }
}
static void h2l_escape(h2l_json *j, char *out, size_t *n)
{
    const char *codes = "\"\\/bfnrt", *found; int c;
    if (j->p == j->end) { j->failed = 1; return; }
    c = *j->p++;
    if (c == 'u') { h2l_unicode(out, n, h2l_codepoint(j)); return; }
    found = strchr(codes, c);
    if (!found) { j->failed = 1; return; }
    out[(*n)++] = "\"\\/\b\f\n\r\t"[found - codes];
}
static char *h2l_string(h2l_json *j)
{
    const char *start; char *out; size_t n = 0; int c;
    if (!h2l_take(j, '"')) return NULL;
    start = j->p; /* Scan once to size allocation; decoding never expands source bytes. */
    while (j->p < j->end && *j->p != '"') {
        if (*j->p++ == '\\' && j->p < j->end) ++j->p;
    }
    if (j->p == j->end) { j->failed = 1; return NULL; }
    out = (char*)malloc((size_t)(j->p - start) + 1); j->p = start;
    if (!out) { j->failed = 1; return NULL; }
    while (!j->failed && j->p < j->end && (c = (unsigned char)*j->p++) != '"') {
        if (c < 32) j->failed = 1;
        else if (c == '\\') h2l_escape(j, out, &n);
        else out[n++] = (char)c;
    }
    out[n] = 0;
    if (j->failed) { free(out); return NULL; }
    return out;
}
static int h2l_exponent(h2l_json *j)
{
    int value = 0, negative = 0;
    if (j->p == j->end || (*j->p != 'e' && *j->p != 'E')) return 0;
    ++j->p;
    if (j->p < j->end && (*j->p == '-' || *j->p == '+')) negative = *j->p++ == '-';
    if (j->p == j->end || !h2l_digit(*j->p)) j->failed = 1;
    while (!j->failed && j->p < j->end && h2l_digit(*j->p)) {
        value = value * 10 + *j->p++ - '0';
        if (value > H2L_EXPONENT_LIMIT) j->failed = 1;
    }
    return negative ? -value : value;
}
static void h2l_number(h2l_json *j, h2l_node *node)
{
    const char *start = j->p, *digits; int fraction = 0, zeros = 0, nonzero = 0, exp;
    if (j->p < j->end && *j->p == '-') ++j->p;
    digits = j->p;
    if (j->p == j->end || !h2l_digit(*j->p)) { j->failed = 1; return; }
    while (j->p < j->end && h2l_digit(*j->p)) ++j->p;
    if (*digits == '0' && j->p - digits != 1) j->failed = 1;
    if (j->p < j->end && *j->p == '.') {
        digits = ++j->p; while (j->p < j->end && h2l_digit(*j->p)) ++j->p;
        fraction = (int)(j->p - digits); if (!fraction) j->failed = 1;
    }
    for (digits = start; digits < j->p; ++digits) if (h2l_digit(*digits)) {
        if (*digits != '0') { nonzero = 1; zeros = 0; } else ++zeros;
    }
    exp = h2l_exponent(j); node->integral = !nonzero || exp >= fraction - zeros;
    node->type = 2; node->text = h2l_copy(start, (size_t)(j->p - start));
    if (!node->text) j->failed = 1;
}
static void h2l_keyword(h2l_json *j, h2l_node *node, const char *word, int type)
{
    size_t n = strlen(word);
    if ((size_t)(j->end - j->p) < n || memcmp(j->p, word, n)) { j->failed = 1; return; }
    node->type = type; node->text = h2l_copy(word, n); j->p += n;
    if (!node->text) j->failed = 1;
}
static int h2l_value_parse(h2l_json *j, int depth);
static int h2l_unique(h2l_json *j, int parent, const char *key)
{
    int child = j->nodes[parent].first, count = 0;
    while (child >= 0) {
        if (++count >= H2L_KEY_LIMIT || !strcmp(j->nodes[child].key, key)) {
            j->failed = 1; return 0;
        }
        child = j->nodes[child].next;
    }
    return 1;
}
static int h2l_child(h2l_json *j, int parent, int depth, int object)
{
    char *key = NULL; int child;
    if (object) {
        key = h2l_string(j);
        if (!key || !h2l_unique(j, parent, key) || !h2l_take(j, ':')) {
            free(key); return -1;
        }
    }
    child = h2l_value_parse(j, depth);
    if (child >= 0) j->nodes[child].key = key; else free(key);
    return child;
}
static void h2l_container(h2l_json *j, int index, int depth, int object)
{
    int child, previous = -1, close = object ? '}' : ']';
    j->nodes[index].type = object ? 5 : 6; ++j->p; h2l_space(j);
    if (j->p < j->end && *j->p == close) { ++j->p; return; }
    while (!j->failed) {
        child = h2l_child(j, index, depth + 1, object); if (child < 0) return;
        if (previous < 0) j->nodes[index].first = child;
        else j->nodes[previous].next = child;
        previous = child; h2l_space(j);
        if (j->p < j->end && *j->p == close) { ++j->p; return; }
        if (!h2l_take(j, ',')) return;
    }
}
static int h2l_value_parse(h2l_json *j, int depth)
{
    int index, c; h2l_node *node; h2l_space(j);
    if (j->failed || j->p == j->end || j->used == H2L_NODE_LIMIT) {
        j->failed = 1; return -1;
    }
    index = j->used++; node = &j->nodes[index]; node->first = node->next = -1; c = *j->p;
    if (c == '{' || c == '[') {
        if (depth >= H2L_DEPTH_LIMIT) j->failed = 1;
        else h2l_container(j, index, depth, c == '{');
    } else if (c == '"') { node->type = 1; node->text = h2l_string(j); }
    else if (c == 't') h2l_keyword(j, node, "true", 3);
    else if (c == 'f') h2l_keyword(j, node, "false", 3);
    else if (c == 'n') h2l_keyword(j, node, "null", 4);
    else h2l_number(j, node);
    return index;
}
static void h2l_json_free(h2l_json *j)
{
    int i;
    for (i = 0; i < j->used; ++i) { free(j->nodes[i].key); free(j->nodes[i].text); }
    free(j->nodes); j->nodes = NULL; j->used = 0; j->failed = 1;
}
static void h2l_json_parse(h2l_json *j, const char *body)
{
    size_t length;
    memset(j, 0, sizeof(*j)); j->failed = 1;
    if (!body) return;
    length = strlen(body);
    if (length > H2L_FIELD_LIMIT || !h2l_utf8((const unsigned char*)body, length)) return;
    j->nodes = (h2l_node*)calloc(H2L_NODE_LIMIT, sizeof(h2l_node));
    if (!j->nodes) return;
    j->failed = 0; j->p = body; j->end = body + length;
    h2l_value_parse(j, 0); h2l_space(j);
    if (j->p != j->end) j->failed = 1;
}
static int h2l_path_child(h2l_json *j, int node, const h2l_path *part)
{
    unsigned long index = 0; int child = j->nodes[node].first;
    if (j->nodes[node].type != (part->key ? 5 : 6)) return -1;
    while (child >= 0) {
        if (part->key ? !strcmp(part->key, j->nodes[child].key) : part->index == index)
            return child;
        ++index; child = j->nodes[child].next;
    }
    return -1;
}
static void h2l_json_select(h2l_value *out, h2l_json *j, const h2l_path *path,
                           int count, unsigned long occurrence, int empty)
{
    int i, node = 0;
    if (j->failed || occurrence != 1) { h2l_error(out); return; }
    for (i = 0; i < count && node >= 0; ++i) node = h2l_path_child(j, node, &path[i]);
    if (node < 0 || j->nodes[node].type > 4 || !j->nodes[node].text ||
        (!empty && j->nodes[node].type == 1 && !*j->nodes[node].text)) {
        h2l_error(out); return;
    }
    h2l_set(out, j->nodes[node].text, strlen(j->nodes[node].text),
            j->nodes[node].type, j->nodes[node].integral);
}
'''


def parser_lines() -> Iterator[str]:
    yield f"#define H2L_NODE_LIMIT {MAX_JSON_NODES}"
    yield f"#define H2L_KEY_LIMIT {MAX_OBJECT_KEYS}"
    yield f"#define H2L_DEPTH_LIMIT {MAX_JSON_DEPTH}"
    yield f"#define H2L_EXPONENT_LIMIT {MAX_NUMBER_EXPONENT}"
    yield from _PARSER.splitlines()
