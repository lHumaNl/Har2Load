"""Opt-in emitted C API ordering/arguments only; deliberately no native jar emulator."""

from pathlib import Path

import pytest

from har2load.generation_types import RequestPlan
from tests.test_loadrunner_cookies import cookie, cookie_plan
from validation.c_runtime.harness import c_literal, isolated, lifecycle_binary


def execute(directory: Path, model: RequestPlan, checks: str, setup: str = "") -> None:
    main = ("int main(void) { stub_reset(); " + setup + " vuser_init(); " + checks +
            ' assert(error_calls == 0); vuser_end(); assert(live_allocations == 0);'
            ' puts("COOKIE_API_OK"); return 0; }')
    binary = lifecycle_binary(directory, model, main)
    result = isolated([binary], directory)
    (directory / "execution.txt").write_text(result.stdout + result.stderr)
    assert result.stdout.strip() == "COOKIE_API_OK" and not result.stderr


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("seed_count", [0, 1, 2])
def test_each_action_resets_then_seeds_without_inter_request_reseed(
    tmp_path: Path, live: bool, seed_count: int,
) -> None:
    seeds = tuple(cookie(name=f"SID{index}") for index in range(seed_count))
    checks = ("assert(cookie_adds == 0); Action(); Action();"
              f"assert(cookie_adds == {seed_count * 2});"
              "assert(cookie_cleanups == 2 && cache_cleanups == 2 && request_count == 4);"
              f'assert(!strcmp(cookie_events, "CK{"S" * seed_count}RRCK{"S" * seed_count}RR"));'
              "assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);"
              "assert(output_calls == 0 && eval_calls == 0);")
    for index, seed in enumerate(seeds):
        argument = f"{seed.name}=PRIVATE_SEED; domain=example.invalid; path=/login; secure"
        checks += f"assert(!strcmp(cookie_arguments[{index}], {c_literal(argument)}));"
        checks += f"assert(!strcmp(cookie_arguments[{index + seed_count}], {c_literal(argument)}));"
    execute(tmp_path, cookie_plan(live, *seeds), checks)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("expiry,expected", [
    (None, ""), (1, "; expires=Thu, 01 Jan 1970 00:00:01 GMT"),
    (1709164800, "; expires=Thu, 29 Feb 2024 00:00:00 GMT"),
    (253402300799, "; expires=Fri, 31 Dec 9999 23:59:59 GMT")])
def test_expiry_and_empty_values_reach_native_api_unchanged(
    tmp_path: Path, live: bool, expiry: int | None, expected: str,
) -> None:
    argument = "SID=; domain=example.invalid; path=/login" + expected
    checks = ("Action(); assert(cookie_adds == 1);"
              f"assert(!strcmp(cookie_arguments[0], {c_literal(argument)}));"
              "assert(output_calls == 0 && time_calls == 0 && clock_calls == 0);")
    execute(tmp_path, cookie_plan(live, cookie(value="", secure=False, expires=expiry)), checks)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
@pytest.mark.parametrize("add_failure", [False, True])
@pytest.mark.parametrize("http_failure", [False, True])
def test_api_failures_log_and_continue_without_native_status_override(
    tmp_path: Path, live: bool, cleanup_failure: bool, add_failure: bool, http_failure: bool,
) -> None:
    setup = (f"cookie_cleanup_rc = {int(cleanup_failure)};"
             f"fail_cookie_at = {0 if add_failure else -1};"
             f"http_rc = {int(http_failure)};")
    logs = int(cleanup_failure) + int(add_failure) + 2 * int(live and http_failure)
    checks = ("Action(); assert(cookie_adds == 2 && request_count == 2);"
              'assert(!strcmp(cookie_events, "CKSSRR"));'
              f"assert(output_calls == {logs} && error_calls == 0);"
              f"assert(requests[0].status == {int(http_failure)});"
              f"assert(requests[1].status == {int(http_failure)});"
              "assert(transactions[0] == LR_AUTO);")
    execute(tmp_path, cookie_plan(live, cookie(), cookie(name="OTHER")), checks, setup)
