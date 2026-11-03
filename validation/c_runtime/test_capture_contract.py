"""Synthetic stub contract controls, not claims about a real LoadRunner SDK."""

from subprocess import CalledProcessError

import pytest

from validation.c_runtime.harness import HERE, compile_source
from validation.support import run_isolated


def contract(tmp_path, checks):
    source = '#include "stubs.h"\n' + r'''
static void expect_parameter(const char *expected, size_t length) {
    char *native = NULL; const char *owned; unsigned long size = 0;
    int rc = lr_eval_string_ext("{H2L_RESPONSE}", 14, &native, &size, 0, 0, -1);
    assert(rc == eval_rc && native && size == length);
    assert(!memcmp(native, expected, length));
    owned = native; lr_eval_string_ext_free(&owned); assert(owned == NULL);
    assert(eval_calls == eval_frees);
}
static int register_capture(void) {
    return web_reg_save_param_ex("ParamName=H2L_RESPONSE", "LB=", "RB=",
                                "NotFound=warning", SEARCH_FILTERS, "Scope=Body", LAST);
}
static int perform_http(void) {
    return web_custom_request("synthetic", "URL=https://synthetic.invalid/", LAST);
}
static void schedule(const char *bytes, size_t length) {
    scheduled_response = (const unsigned char *)bytes; scheduled_response_size = length;
}
''' + checks
    (tmp_path / "stub-contract-snapshot.h").write_bytes((HERE / "stubs.h").read_bytes())
    binary = compile_source(tmp_path, source)
    try:
        result = run_isolated([str(binary)], tmp_path)
    except CalledProcessError as failure:
        (tmp_path / "contract-failure.txt").write_text(failure.stdout + failure.stderr)
        raise
    (tmp_path / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "CAPTURE_CONTRACT_OK"
    assert not result.stderr


@pytest.mark.parametrize("operation", ["save", "clear", "failed-save"])
def test_save_updates_the_evaluated_parameter_storage(tmp_path, operation):
    checks = {
        "save": 'assert(lr_save_string("SAVED", "H2L_RESPONSE") == LR_PASS);'
                'expect_parameter("SAVED", 5);',
        "clear": 'response = (const unsigned char *)"STALE"; response_size = 5;'
                 'assert(lr_save_string("", "H2L_RESPONSE") == LR_PASS);'
                 'expect_parameter("", 0);',
        "failed-save": 'assert(lr_save_string("SAVED", "H2L_RESPONSE") == LR_PASS);'
                       'save_fail_at = save_calls;'
                       'assert(lr_save_string("", "H2L_RESPONSE") == LR_FAIL);'
                       'expect_parameter("SAVED", 5);',
    }
    contract(tmp_path, 'int main(void) { stub_reset(); ' + checks[operation] +
             ' puts("CAPTURE_CONTRACT_OK"); return 0; }')


@pytest.mark.parametrize("scenario", [
    "no-registration-empty", "no-registration-stale", "registration-after-http",
    "one-shot-publication", "failed-registration", "failed-http-consumes-registration",
    "clear-after-capture", "binary-copy-and-eval-failure",
])
def test_capture_publication_requires_registration_at_corresponding_http(tmp_path, scenario):
    checks = {
        "no-registration-empty": '''
            schedule("FRESH", 5); assert(perform_http() == LR_PASS);
            expect_parameter("", 0);
        ''',
        "no-registration-stale": '''
            lr_save_string("STALE", "H2L_RESPONSE"); schedule("FRESH", 5);
            assert(perform_http() == LR_PASS); expect_parameter("STALE", 5);
            lr_save_string("", "H2L_RESPONSE"); expect_parameter("", 0);
            assert(perform_http() == LR_PASS); expect_parameter("", 0);
        ''',
        "registration-after-http": '''
            schedule("PAST", 4); assert(perform_http() == LR_PASS);
            assert(register_capture() == LR_PASS); expect_parameter("", 0);
            schedule("NEXT", 4); assert(perform_http() == LR_PASS);
            expect_parameter("NEXT", 4);
        ''',
        "one-shot-publication": '''
            schedule("FIRST", 5); assert(register_capture() == LR_PASS);
            expect_parameter("", 0); assert(perform_http() == LR_PASS);
            expect_parameter("FIRST", 5); assert(capture_registered == 0);
            schedule("SECOND", 6); assert(perform_http() == LR_PASS);
            expect_parameter("FIRST", 5);
        ''',
        "failed-registration": '''
            lr_save_string("STALE", "H2L_RESPONSE"); schedule("FRESH", 5);
            register_rc = LR_FAIL; assert(register_capture() == LR_FAIL);
            assert(perform_http() == LR_PASS); expect_parameter("STALE", 5);
        ''',
        "failed-http-consumes-registration": '''
            lr_save_string("STALE", "H2L_RESPONSE"); schedule("FAILED", 6);
            assert(register_capture() == LR_PASS); http_rc = LR_FAIL;
            assert(perform_http() == LR_FAIL); expect_parameter("STALE", 5);
            assert(capture_registered == 0); http_rc = LR_PASS;
            schedule("NEXT", 4); assert(perform_http() == LR_PASS);
            expect_parameter("STALE", 5);
        ''',
        "clear-after-capture": '''
            schedule("FRESH", 5); assert(register_capture() == LR_PASS);
            assert(perform_http() == LR_PASS); expect_parameter("FRESH", 5);
            lr_save_string("", "H2L_RESPONSE"); expect_parameter("", 0);
            assert(perform_http() == LR_PASS); expect_parameter("", 0);
            assert(register_capture() == LR_PASS); assert(perform_http() == LR_PASS);
            expect_parameter("FRESH", 5); /* saving did not change scheduled HTTP data */
        ''',
        "binary-copy-and-eval-failure": r'''
            char bytes[] = {'a', 0, (char)255, 'z'};
            char expected[] = {'a', 0, (char)255, 'z'};
            schedule(bytes, sizeof(bytes)); assert(register_capture() == LR_PASS);
            assert(perform_http() == LR_PASS); memset(bytes, 'X', sizeof(bytes));
            expect_parameter(expected, sizeof(expected)); /* capture owns a snapshot */
            eval_rc = LR_FAIL; expect_parameter(expected, sizeof(expected));
            lr_save_string("", "H2L_RESPONSE"); expect_parameter("", 0);
        ''',
    }
    contract(tmp_path, 'int main(void) { stub_reset(); ' + checks[scenario] +
             ' puts("CAPTURE_CONTRACT_OK"); return 0; }')
