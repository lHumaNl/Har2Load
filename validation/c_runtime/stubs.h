/* Synthetic SDK-shaped behavior only: no sockets, SDK, HAR, or authored code. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "lrun.h"
#include "web_api.h"

static int alloc_attempts, alloc_fail_at = -1, live_allocations;
static void *test_malloc(size_t n) {
    void *p;
    if (alloc_attempts++ == alloc_fail_at) return NULL;
    p = malloc(n); if (p) ++live_allocations; return p;
}
static void *test_calloc(size_t n, size_t size) {
    void *p;
    if (alloc_attempts++ == alloc_fail_at) return NULL;
    p = calloc(n, size); if (p) ++live_allocations; return p;
}
static void test_free(void *p) {
    if (p) { --live_allocations; assert(live_allocations >= 0); }
    free(p);
}

/* Evaluated parameter view. Direct assignment is ONLY a low-level helper-test
 * seam for arbitrary native bytes; lifecycle tests schedule HTTP data below. */
static const unsigned char *response = (const unsigned char *)"";
static size_t response_size;
/* Scheduled bytes are not visible to eval until a registered successful HTTP.
 * This deliberately small contract is NOT an SDK response/capture emulator. */
static const unsigned char *scheduled_response = (const unsigned char *)"";
static size_t scheduled_response_size;
static int eval_rc, eval_calls, eval_frees, error_calls, output_calls, save_calls;
static int eval_null_output, eval_null_frees;
static int save_fail_at = -1, register_rc, max_length_rc, header_rc, http_rc;
static int fail_http_at = -1, request_count, transaction_count;
static int cookie_cleanups, cache_cleanups, option_calls, continue_calls;
/* Observe API calls only: never emulate cookie scope, expiry, or server updates. */
#define SYNTHETIC_COOKIE_CALLS 32
#define SYNTHETIC_COOKIE_ARGUMENT 70656
#define SYNTHETIC_COOKIE_EVENTS 4096
static int cookie_adds, cookie_cleanup_rc, fail_cookie_at = -1;
static char cookie_arguments[SYNTHETIC_COOKIE_CALLS][SYNTHETIC_COOKIE_ARGUMENT];
static char cookie_events[SYNTHETIC_COOKIE_EVENTS];
static size_t cookie_event_count;
static void cookie_event(char event) {
    /* Long unrelated stress tests need not retain an unbounded trace. */
    if (cookie_event_count + 1 >= sizeof(cookie_events)) return;
    cookie_events[cookie_event_count++] = event; cookie_events[cookie_event_count] = 0;
}
static int registration_count, capture_registered;
static char saved_parameter[1048578]; /* bounded synthetic storage, including limit+1 */
static char next_header[4096];
typedef struct {
    char url[4096], method[64], mime[256], body[8192], header[4096];
    int body_present, body_size, status;
} snapshot;
static snapshot requests[32];
static int transactions[32];
/* Borrowed synthetic parameter bytes; deliberately independent of capture storage. */
#define SYNTHETIC_PARAMETER_COUNT 16
typedef struct { const char *key; const unsigned char *data; size_t size; int rc; } native_input;
static native_input native_inputs[SYNTHETIC_PARAMETER_COUNT];
static int native_input_count, native_eval_calls, whoami_calls, synthetic_user_id = 1;
static int time_calls, clock_calls;
static time_t synthetic_seconds = 1709164800;
static clock_t synthetic_ticks = 123;
static time_t synthetic_time(time_t *out) {
    ++time_calls; if (out) *out = synthetic_seconds; return synthetic_seconds;
}
static clock_t synthetic_clock(void) { ++clock_calls; return synthetic_ticks; }
void lr_whoami(int *id, char **group, int *scenario) {
    assert(id && !group && !scenario); *id = synthetic_user_id; ++whoami_calls;
}
static void seed_native(const char *key, const unsigned char *data, size_t size, int rc) {
    int i;
    for (i = 0; i < native_input_count; ++i) if (!strcmp(native_inputs[i].key, key)) break;
    assert(i < SYNTHETIC_PARAMETER_COUNT);
    if (i == native_input_count) ++native_input_count;
    native_inputs[i].key = key; native_inputs[i].data = data;
    native_inputs[i].size = size; native_inputs[i].rc = rc;
}
static int eval_native(const char *name, int length, char **out, unsigned long *n) {
    int i; ++native_eval_calls;
    assert(length >= 2 && name[0] == '{' && name[length - 1] == '}');
    for (i = 0; i < native_input_count; ++i) {
        native_input *input = &native_inputs[i];
        if (strlen(input->key) != (size_t)length - 2 || memcmp(name + 1, input->key, length - 2))
            continue;
        *n = (unsigned long)input->size;
        *out = malloc(input->size ? input->size : 1); assert(*out);
        memcpy(*out, input->data, input->size); return input->rc;
    }
    *out = NULL; *n = 0; return LR_FAIL;
}

static void copy_checked(char *out, size_t capacity, const char *in) {
    assert(strlen(in) < capacity); strcpy(out, in);
}
static void publish_parameter(const unsigned char *bytes, size_t length) {
    assert(length < sizeof(saved_parameter));
    memmove(saved_parameter, bytes, length); saved_parameter[length] = 0;
    response = (const unsigned char *)saved_parameter; response_size = length;
}
static void stub_reset(void) {
    response = (const unsigned char *)""; response_size = 0;
    scheduled_response = (const unsigned char *)""; scheduled_response_size = 0;
    eval_rc = eval_calls = eval_frees = error_calls = output_calls = save_calls = 0;
    eval_null_output = eval_null_frees = 0;
    save_fail_at = fail_http_at = -1;
    register_rc = max_length_rc = header_rc = http_rc = 0;
    request_count = transaction_count = cookie_cleanups = cache_cleanups = 0;
    cookie_adds = cookie_cleanup_rc = 0; fail_cookie_at = -1;
    cookie_event_count = 0; cookie_events[0] = 0;
    memset(cookie_arguments, 0, sizeof(cookie_arguments));
    option_calls = continue_calls = registration_count = capture_registered = 0;
    saved_parameter[0] = next_header[0] = 0;
    memset(requests, 0, sizeof(requests)); memset(transactions, 0, sizeof(transactions));
    alloc_attempts = 0; alloc_fail_at = -1;
    native_input_count = native_eval_calls = whoami_calls = time_calls = clock_calls = 0;
    synthetic_user_id = 1; synthetic_seconds = 1709164800; synthetic_ticks = 123;
}
int lr_error_message(const char *format, ...) {
    /* Native error logging can fail open transactions; tests reject any such call. */
    (void)format; ++error_calls; return 0;
}
int lr_output_message(const char *format, ...) {
    (void)format; ++output_calls; return 0;
}
int lr_eval_string_ext(const char *name, int length, char **out,
                       unsigned long *n, int a, int b, int c) {
    assert(a == 0 && b == 0 && c == -1); ++eval_calls;
    /* Explicit NULL-result seam, independent of return status; not SDK emulation. */
    if (eval_null_output) {
        if (strcmp(name, "{H2L_RESPONSE}")) ++native_eval_calls;
        *out = NULL; *n = 0; return eval_rc;
    }
    if (strcmp(name, "{H2L_RESPONSE}")) return eval_native(name, length, out, n);
    assert(length == 14);
    *n = (unsigned long)response_size;
    /* No NUL terminator: runtime must respect the reported native length. */
    *out = malloc(response_size ? response_size : 1); assert(*out);
    memcpy(*out, response, response_size); return eval_rc;
}
void lr_eval_string_ext_free(const char **p) {
    assert(p); if (!*p) ++eval_null_frees;
    free((void *)*p); *p = NULL; ++eval_frees;
}
int lr_save_string(const char *value, const char *name) {
    assert(!strcmp(name, "H2L_RESPONSE"));
    if (save_calls++ == save_fail_at) return LR_FAIL;
    publish_parameter((const unsigned char *)value, strlen(value)); return LR_PASS;
}
int lr_continue_on_error(int mode) {
    assert(mode == LR_ON_ERROR_CONTINUE); ++continue_calls; return LR_PASS;
}
int lr_start_transaction(const char *name) { (void)name; return LR_PASS; }
int lr_end_transaction(const char *name, int status) {
    (void)name; assert(transaction_count < 32);
    transactions[transaction_count++] = status; return LR_PASS;
}
int web_set_option(const char *name, ...) { (void)name; ++option_calls; return LR_PASS; }
int web_cleanup_cookies(void) {
    cookie_event('C'); ++cookie_cleanups; return cookie_cleanup_rc;
}
int web_cache_cleanup(void) { cookie_event('K'); ++cache_cleanups; return LR_PASS; }
int web_add_cookie(const char *value) {
    int status = cookie_adds == fail_cookie_at ? LR_FAIL : LR_PASS;
    assert(cookie_adds < SYNTHETIC_COOKIE_CALLS); cookie_event('S');
    copy_checked(cookie_arguments[cookie_adds++], SYNTHETIC_COOKIE_ARGUMENT, value);
    return status;
}
int web_set_max_html_param_len(const char *value) {
    assert(!strcmp(value, "1048576")); return max_length_rc;
}
int web_reg_save_param_ex(const char *first, ...) {
    va_list args; const char *part; int count = 0;
    const char *expected[] = {"LB=", "RB=", "NotFound=warning", SEARCH_FILTERS, "Scope=Body"};
    assert(!strcmp(first, "ParamName=H2L_RESPONSE")); va_start(args, first);
    while ((part = va_arg(args, const char *))) {
        assert(count < 5 && !strcmp(part, expected[count++]));
    }
    va_end(args); assert(count == 5); ++registration_count;
    capture_registered = register_rc == LR_PASS; return register_rc;
}
int web_add_header(const char *name, const char *value) {
    assert(!strcmp(name, "X-Token"));
    copy_checked(next_header, sizeof(next_header), value); return header_rc;
}
int web_custom_request(const char *name, ...) {
    va_list args; const char *part; snapshot *s;
    cookie_event('R');
    (void)name; assert(request_count < 32); s = &requests[request_count];
    va_start(args, name);
    while ((part = va_arg(args, const char *))) {
        if (!strcmp(part, RAW_BODY_START)) {
            const char *body = va_arg(args, const char *);
            int length = va_arg(args, int);
            assert(body && length >= 0 && (size_t)length < sizeof(s->body));
            memcpy(s->body, body, (size_t)length); s->body[length] = 0;
            s->body_present = 1; s->body_size = length;
            assert(!strcmp(va_arg(args, const char *), RAW_BODY_END));
        } else if (!strncmp(part, "URL=", 4)) copy_checked(s->url, sizeof(s->url), part + 4);
        else if (!strncmp(part, "Method=", 7)) copy_checked(s->method, sizeof(s->method), part + 7);
        else if (!strncmp(part, "EncType=", 8)) copy_checked(s->mime, sizeof(s->mime), part + 8);
        else assert(!strcmp(part, "Resource=0") || !strcmp(part, "Mode=HTTP"));
    }
    va_end(args);
    copy_checked(s->header, sizeof(s->header), next_header); next_header[0] = 0;
    s->status = request_count == fail_http_at ? LR_FAIL : http_rc;
    if (capture_registered && s->status == LR_PASS)
        publish_parameter(scheduled_response, scheduled_response_size);
    ++request_count; capture_registered = 0;
    return s->status;
}
