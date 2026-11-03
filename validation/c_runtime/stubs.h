/* Synthetic SDK-shaped behavior only: no sockets, SDK, HAR, or authored code. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
static int eval_rc, eval_calls, eval_frees, error_calls, save_calls;
static int save_fail_at = -1, register_rc, max_length_rc, header_rc, http_rc;
static int fail_http_at = -1, request_count, transaction_count;
static int cookie_cleanups, cache_cleanups, option_calls, continue_calls;
static int registration_count, capture_registered;
static char saved_parameter[1048578]; /* bounded synthetic storage, including limit+1 */
static char next_header[4096];
typedef struct {
    char url[4096], method[64], mime[256], body[8192], header[4096];
    int body_present, body_size, status;
} snapshot;
static snapshot requests[32];
static int transactions[32];

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
    eval_rc = eval_calls = eval_frees = error_calls = save_calls = 0;
    save_fail_at = fail_http_at = -1;
    register_rc = max_length_rc = header_rc = http_rc = 0;
    request_count = transaction_count = cookie_cleanups = cache_cleanups = 0;
    option_calls = continue_calls = registration_count = capture_registered = 0;
    saved_parameter[0] = next_header[0] = 0;
    memset(requests, 0, sizeof(requests)); memset(transactions, 0, sizeof(transactions));
    alloc_attempts = 0; alloc_fail_at = -1;
}
int lr_error_message(const char *format, ...) {
    /* Logging must NOT implicitly change a transaction's status in these stubs. */
    (void)format; ++error_calls; return 0;
}
int lr_eval_string_ext(const char *name, int length, char **out,
                       unsigned long *n, int a, int b, int c) {
    assert(!strcmp(name, "{H2L_RESPONSE}") && length == 14);
    assert(a == 0 && b == 0 && c == -1); ++eval_calls;
    *n = (unsigned long)response_size;
    /* No NUL terminator: runtime must respect the reported native length. */
    *out = malloc(response_size ? response_size : 1); assert(*out);
    memcpy(*out, response, response_size); return eval_rc;
}
void lr_eval_string_ext_free(const char **p) {
    assert(*p); free((void *)*p); *p = NULL; ++eval_frees;
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
int web_cleanup_cookies(void) { ++cookie_cleanups; return LR_PASS; }
int web_cache_cleanup(void) { ++cache_cleanups; return LR_PASS; }
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
