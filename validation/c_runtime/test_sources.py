"""Opt-in emitted C input/generator execution; synthetic SDK contract, no HTTP."""

import json
import random
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from har2load.loadrunner_runtime import symbol
from har2load.runtime_types import RuntimeSlot, RuntimeTemplate
from tests.test_loadrunner_extensions import authored, block, variable
from tests.test_loadrunner_sources import external, source_plan
from validation.c_runtime.harness import POSTLUDE, PRELUDE, c_literal, compile_source, isolated

MASK = 2**64 - 1
SEED_TIME = 1709164800
SEED_TICKS = 123
VALUE = symbol("VALUE", "token") + "()"
INIT = symbol("INIT_FN", "token") + "(NULL)"


def build_source(directory: Path, model: RequestPlan, checks: str) -> Path:
    build = LoadRunnerGenerator().generate(model)
    assert build.status == "generated", build.diagnostics
    for name, data in build.files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    includes = ('#define time synthetic_time\n#define clock synthetic_clock\n'
                '#include "loadrunner/Action.c"\n#include "loadrunner/vuser_init.c"\n'
                '#include "loadrunner/vuser_end.c"\n#undef time\n#undef clock\n')
    return compile_source(directory, PRELUDE + includes + POSTLUDE + checks)


def execute(directory: Path, model: RequestPlan, checks: str) -> list[str]:
    binary = build_source(directory, model, checks)
    result = isolated([binary], directory)
    (directory / "execution.txt").write_text(result.stdout + result.stderr)
    assert not result.stderr
    return result.stdout.splitlines()


def native_setup(value: str, rc: str = "LR_PASS") -> str:
    return (f'seed_native("token", (const unsigned char *){c_literal(value)}, '
            f'{len(value.encode("utf-8"))}, {rc});')


def main(body: str, setup: str = "") -> str:
    return ("int main(void) { stub_reset(); " + setup + " vuser_init(); " + body +
            " assert(error_calls == 0); vuser_end(); assert(live_allocations == 0); return 0; }")


def model(source: dict[str, JSONValue], kind: str = "string") -> RequestPlan:
    value = source_plan(source, kind)
    assert value.runtime is not None
    first, second = value.runtime.requests
    second = replace(second, url=RuntimeTemplate(("https://synthetic.invalid/",)))
    return replace(value, runtime=replace(value.runtime, requests=(first, second)))


@pytest.mark.parametrize("kind,text,allow_empty,wire", [
    ("string", "native-edited", False, '"native-edited"'),
    ("string", "ERROR_token", False, '"ERROR_token"'),
    ("string", "{token}", False, '"{token}"'),
    ("string", "", True, '""'), ("string", "", False, '"ERROR_token"'),
    ("string", "é", False, '"é"'), ("string", "a\x00b", False, '"ERROR_token"'),
    ("integer", "-9223372036854775808", False, "-9223372036854775808"),
    ("integer", "1.00e2", False, "1.00e2"),
    ("integer", "1.01", False, '"ERROR_token"'),
    ("number", "123456789012345678901.00001", False, "123456789012345678901.00001"),
    ("number", "1 trailing", False, '"ERROR_token"'),
    ("number", "01", False, '"ERROR_token"'),
    ("number", "[1]", False, '"ERROR_token"'),
    ("number", "1e10001", False, '"ERROR_token"'),
    ("boolean", "true", False, "true"), ("boolean", "false", False, "false"),
    ("boolean", "TRUE", False, '"ERROR_token"'),
    ("null", "null", False, "null"), ("null", "", True, '"ERROR_token"'),
])
def test_native_typed_inputs_and_markers(
    tmp_path: Path, kind: str, text: str, allow_empty: bool, wire: str,
) -> None:
    value = model(external(initial_value="seed", allow_empty=allow_empty), kind)
    checks = (f'Action(); assert(!strcmp(requests[1].body, {c_literal(wire)}));'
              'assert(transactions[0] == LR_AUTO && requests[1].status == LR_PASS);'
              'assert(native_eval_calls == 1 && eval_frees == 1 && save_calls == 0);')
    execute(tmp_path, value, main(checks, native_setup(text)))


@pytest.mark.parametrize("declared", [False, True])
def test_missing_native_input_does_not_reuse_seed_or_guess_sentinel(
    tmp_path: Path, declared: bool,
) -> None:
    source = external(initial_value="seed") if declared else external()
    checks = ('Action(); assert(!strcmp(requests[1].body, "\\\"ERROR_token\\\""));'
              f'assert(native_eval_calls == {int(declared)});'
              f'assert(eval_frees == {int(declared)} && eval_null_frees == {int(declared)});'
              'assert(output_calls > 0 && transactions[0] == LR_AUTO);')
    execute(tmp_path, model(source), main(checks))


def test_snapshot_is_shared_and_survives_iteration_reset_but_not_restart(tmp_path: Path) -> None:
    value = model(external(initial_value="seed"))
    checks = (f'Action(); assert(!strcmp({VALUE}->data, "first"));'
              + native_setup("changed") + ' Action();'
              f'assert(!strcmp({VALUE}->data, "first") && native_eval_calls == 1);'
              'vuser_init(); Action();'
              f'assert(!strcmp({VALUE}->data, "changed") && native_eval_calls == 2);'
              + native_setup("bad", "LR_FAIL") + ' vuser_init(); Action();'
              f'assert(!{VALUE}->data && native_eval_calls == 3 && eval_frees == 3);'
              'assert(transactions[3] == LR_AUTO);')
    execute(tmp_path, value, main(checks, native_setup("first")))


@pytest.mark.parametrize("size", [65536, 65537])
def test_snapshot_length_limit_and_nonterminated_ownership(tmp_path: Path, size: int) -> None:
    value = model(external(initial_value="seed"))
    checks = (f'{INIT}; assert(({VALUE}->data != NULL) == {int(size == 65536)});'
              'assert(native_eval_calls == 1 && eval_frees == 1); free(bytes);')
    setup = (f'unsigned char *bytes = malloc({size}); memset(bytes, \'x\', {size});'
             f'seed_native("token", bytes, {size}, LR_PASS);')
    execute(tmp_path, value, main(checks, setup))


def test_invalid_utf8_and_eval_error_release_native_bytes(tmp_path: Path) -> None:
    checks = (f'{INIT}; assert(!{VALUE}->data); assert(eval_frees == 1);'
              + native_setup("valid", "LR_FAIL") + f'vuser_init(); {INIT};'
              f'assert(!{VALUE}->data && eval_frees == 2);')
    setup = 'seed_native("token", (const unsigned char *)"\\377", 1, LR_PASS);'
    execute(tmp_path, model(external(initial_value="seed")), main(checks, setup))


def generated(generator: str, **options: JSONValue) -> RequestPlan:
    return model({"kind": "generated", "generator": generator, **options},
                 "integer" if generator == "integer" else "string")


def sequence(count: int, extra: str = "") -> str:
    return main(f"int i; for (i = 0; i < {count}; ++i) {{ "
                "request_count = transaction_count = 0; Action(); "
                f"assert({VALUE}->data); puts({VALUE}->data); {extra} "
                "assert(transactions[0] == LR_AUTO); } assert(whoami_calls == 1);")


def draws(low: int, high: int, count: int) -> list[int]:
    state, span = SEED_TIME ^ (1 << 32) ^ SEED_TICKS, high - low + 1
    threshold = (2**64 - span) % span
    values = []
    while len(values) < count:
        state = (state + 0x9E3779B97F4A7C15) & MASK
        z = ((state ^ (state >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        z ^= z >> 31
        if z >= threshold:
            values.append(low + z % span)
    return values


@pytest.mark.parametrize("low,high", [
    (-(2**63), 2**63 - 1), (-(2**63), 0), (-(2**63), -(2**63)),
    (2**63 - 1, 2**63 - 1), (-4, 9), (0, 0), (-1, -1),
])
def test_exact_integer_bounds_full_span_and_rejection_oracle(
    tmp_path: Path, low: int, high: int,
) -> None:
    value = generated("integer", minimum=low, maximum=high)
    extra = f'assert({VALUE}->type == 2 && {VALUE}->integral == 1);'
    actual = execute(tmp_path, value, sequence(256, extra))
    assert list(map(int, actual)) == draws(low, high, 256)


@pytest.mark.parametrize("length,alphabet", [
    (1, "x"), (4096, "abc"), (64, " !#$%&'()*+-./:;<=>?@[]^_`|~"),
])
def test_generated_strings_are_bounded_and_reused(
    tmp_path: Path, length: int, alphabet: str,
) -> None:
    value = generated("string", length=length, alphabet=alphabet)
    actual = execute(tmp_path, value, sequence(16))
    assert all(len(text) == length and set(text) <= set(alphabet) for text in actual)


def test_uuid_is_lowercase_v4_rfc_variant_and_changes(tmp_path: Path) -> None:
    actual = execute(tmp_path, generated("uuid"), sequence(128))
    assert len(set(actual)) == len(actual)
    for text in actual:
        parsed = uuid.UUID(text)
        assert str(parsed) == text and parsed.version == 4 and parsed.variant == uuid.RFC_4122


@pytest.mark.parametrize("low,high", [
    ("0001-01-01", "9999-12-31"), ("2000-02-28", "2000-03-01"),
    ("1900-02-28", "1900-03-01"), ("0001-01-01", "0001-01-01"),
    ("9999-12-31", "9999-12-31"), ("2024-02-29", "2024-02-29"),
])
def test_inclusive_date_range_matches_independent_gregorian_oracle(
    tmp_path: Path, low: str, high: str,
) -> None:
    value = generated("utc_date_range", minimum=low, maximum=high, format="iso8601")
    actual = execute(tmp_path, value, sequence(128))
    ordinals = draws(date.fromisoformat(low).toordinal(), date.fromisoformat(high).toordinal(), 128)
    expected = [date.fromordinal(n).isoformat() for n in ordinals]
    assert actual == expected


def test_calendar_differential_dates_across_centuries(tmp_path: Path) -> None:
    rng = random.Random(44)
    ordinals = [1, 3652059, *[rng.randint(1, 3652059) for _ in range(500)]]
    checks = "".join(f'assert(h2l_date_text(text, sizeof(text), {n})); puts(text);'
                     for n in ordinals)
    actual = execute(tmp_path, generated("uuid"), main("char text[11]; " + checks))
    assert actual == [date.fromordinal(n).isoformat() for n in ordinals]


@pytest.mark.parametrize("seconds,offset", [
    (SEED_TIME, 0), (SEED_TIME, 315360000), (SEED_TIME, -315360000),
    (0, -1), (-62135596800, 0), (253402300799, 0), (951782399, 1),
])
def test_utc_datetime_runtime_clock_and_offset(
    tmp_path: Path, seconds: int, offset: int,
) -> None:
    value = generated("utc_datetime", offset_seconds=offset, format="iso8601")
    checks = f'Action(); puts({VALUE}->data); assert(time_calls == 2 && clock_calls == 1);'
    actual = execute(tmp_path, value, main(checks, f"synthetic_seconds = (time_t){seconds}LL;"))
    expected = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds + offset)
    assert actual == [expected.isoformat(timespec="seconds").replace("+00:00", "Z")]


@pytest.mark.parametrize("failure", [
    "synthetic_seconds = (time_t)-1;", "synthetic_ticks = (clock_t)-1;",
])
def test_seed_clock_failure_is_marker_and_does_not_stop(tmp_path: Path, failure: str) -> None:
    checks = ('Action(); assert(!strcmp(requests[1].body, "\\\"ERROR_token\\\""));'
              'assert(request_count == 2 && transactions[0] == LR_AUTO);')
    execute(tmp_path, generated("uuid"), main(checks, failure))


@pytest.mark.parametrize("seconds,offset", [(-62135596800, -1), (253402300799, 1), (-1, 0)])
def test_datetime_out_of_calendar_or_failed_live_clock_is_marker(
    tmp_path: Path, seconds: int, offset: int,
) -> None:
    checks = (f'synthetic_seconds = (time_t){seconds}LL; Action(); assert(!{VALUE}->data);'
              'assert(output_calls > 0 && transactions[0] == LR_AUTO);')
    value = generated("utc_datetime", offset_seconds=offset, format="iso8601")
    execute(tmp_path, value, main(checks))


def test_per_user_seed_and_session_reinitialization(tmp_path: Path) -> None:
    checks = (f'{INIT}; puts({VALUE}->data); synthetic_user_id = 2; vuser_init();'
              f'{INIT}; puts({VALUE}->data); assert(whoami_calls == 2);'
              'assert(time_calls == 2 && clock_calls == 2);')
    actual = execute(tmp_path, generated("uuid"), main(checks))
    assert actual[0] != actual[1]


@pytest.mark.parametrize("scope,event,exchange", [
    ("session", "session_start", None), ("iteration", "iteration_start", None),
    ("request", "before_request", "consumer"), ("session", "after_response", "producer"),
])
def test_initializer_lifecycle_and_multiple_binding_reuse(
    tmp_path: Path, scope: str, event: str, exchange: str | None,
) -> None:
    value = generated("uuid")
    assert value.runtime is not None
    init: dict[str, JSONValue] = {"event": event}
    if exchange:
        init["exchange_id"] = exchange
    record = {**value.runtime.variables[0], "scope": scope, "initialize": init}
    first, second = value.runtime.requests
    body = RuntimeTemplate(("[", RuntimeSlot("a", "token", "json"), ",",
                            RuntimeSlot("b", "token", "json"), "]"))
    value = replace(value, runtime=replace(value.runtime, variables=(record,),
                    requests=(first, replace(second, body=body))))
    checks = ('Action(); puts(requests[1].body); Action(); puts(requests[3].body);'
              'assert(whoami_calls == 1);')
    actual = execute(tmp_path, value, main(checks))
    assert (actual[0] == actual[1]) == (event == "session_start")
    assert all(json.loads(text)[0] == json.loads(text)[1] for text in actual)
    assert all(uuid.UUID(json.loads(text)[0]).version == 4 for text in actual)


@pytest.mark.parametrize("source,kind", [
    (external(initial_value="seed"), "integer"),
    ({"kind": "generated", "generator": "uuid"}, "string"),
])
def test_allocation_fault_sweep_continues_and_cleans_up(
    tmp_path: Path, source: dict[str, JSONValue], kind: str,
) -> None:
    setup = native_setup("42")
    checks = (f'int main(void) {{ int i, count; stub_reset(); {setup} vuser_init(); Action();'
              'count = alloc_attempts; vuser_end(); assert(live_allocations == 0);'
              f'for (i = 0; i < count; ++i) {{ stub_reset(); {setup} alloc_fail_at = i;'
              'vuser_init(); Action(); assert(request_count == 2 && transactions[0] == LR_AUTO);'
              'assert(error_calls == 0); vuser_end(); assert(live_allocations == 0); } return 0; }')
    execute(tmp_path, model(source, kind), checks)


def test_one_snapshot_shared_by_differently_typed_session_initializers(tmp_path: Path) -> None:
    value = model(external(initial_value="seed"))
    assert value.runtime is not None
    first = {**value.runtime.variables[0], "scope": "session",
             "initialize": {"event": "session_start"}}
    second = {**first, "id": "number", "name": "number", "value_type": "integer"}
    value = replace(value, runtime=replace(value.runtime, variables=(first, second)))
    number = symbol("VALUE", "number") + "()"
    checks = (f'assert(!strcmp({VALUE}->data, "42") && {VALUE}->type == 1);'
              f'assert(!strcmp({number}->data, "42") && {number}->type == 2);'
              'assert(native_eval_calls == 1 && eval_frees == 1);')
    execute(tmp_path, value, main(checks, native_setup("42")))


@pytest.mark.parametrize("source", [external(initial_value="seed"),
                                   {"kind": "generated", "generator": "uuid"}])
@pytest.mark.parametrize("event", ["before_request", "after_response"])
def test_baseline_sources_precede_wrappers_and_wrappers_precede_bindings(
    tmp_path: Path, source: dict[str, JSONValue], event: str,
) -> None:
    init: dict[str, JSONValue] = {"event": event, "exchange_id": "producer"}
    records = (variable("token", source=source, initialize=init),
               variable(initialize=init))
    code = 'h2l_emit_text(frame, "value", h2l_input(frame, "token")->data);'
    blocks = (block(code, inputs=["token"], outputs=["value"], placement=init),)
    value = authored(variables=records, blocks=blocks)
    checks = (f'Action(); assert({VALUE}->data);'
              f'assert(!strcmp({symbol("VALUE", "value")}()->data, {VALUE}->data));'
              'assert(transactions[0] == LR_AUTO && transactions[1] == LR_AUTO);')
    execute(tmp_path, value, main(checks, native_setup("live")))


def test_native_errors_never_change_transport_status(tmp_path: Path) -> None:
    checks = ('http_rc = LR_FAIL; Action(); assert(requests[0].status == LR_FAIL);'
              'assert(requests[1].status == LR_FAIL && transactions[0] == LR_AUTO);'
              'http_rc = LR_PASS; Action(); assert(requests[3].status == LR_PASS);'
              'assert(transactions[1] == LR_AUTO && output_calls > 0);')
    execute(tmp_path, model(external()), main(checks))


@pytest.mark.parametrize("null_output", [False, True])
@pytest.mark.parametrize("status", ["LR_PASS", "LR_FAIL"])
def test_native_eval_cleanup_includes_null_and_allocated_results(
    tmp_path: Path, null_output: bool, status: str,
) -> None:
    valid = not null_output and status == "LR_PASS"
    wire = '"live"' if valid else '"ERROR_token"'
    setup = (native_setup("live", status) +
             f"eval_null_output = {int(null_output)}; eval_rc = {status};")
    checks = (f'Action(); assert(!strcmp(requests[1].body, {c_literal(wire)}));'
              'assert(native_eval_calls == 1 && eval_calls == 1 && eval_frees == 1);'
              f'assert(eval_null_frees == {int(null_output)});'
              'assert(transactions[0] == LR_AUTO && requests[1].status == LR_PASS);')
    execute(tmp_path, model(external(initial_value="seed")), main(checks, setup))
