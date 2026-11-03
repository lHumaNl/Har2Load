"""Execute the shipped C parser/helpers; Python is an independent value oracle."""

import json
import random
from decimal import Decimal
from urllib.parse import quote, quote_plus

import pytest

from har2load.extraction import MAX_MATCHES
from validation.c_runtime.harness import helper_binary, hx, json_case, result, run_cases


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    return helper_binary(tmp_path_factory.mktemp("emitted-helpers"))


def test_root_scalars_exact_numbers_and_strict_acceptance(binary):
    cases = [
        ('"text"', 1, 0, "text"), ('"ERROR_token"', 1, 0, "ERROR_token"),
        ('""', 1, 0, ""), ('true', 3, 0, "true"), ('false', 3, 0, "false"),
        ('null', 4, 0, "null"), ('"123"', 1, 0, "123"),
    ]
    numbers = ["0", "-0", "-0.00e-9", "1.00", "100e-2", "100e-3", "0.0010", "12.3e1",
               "1234567890123456789012345678901234567890", "1.000000000000000000001",
               "1e10000", "1e-10000", "0e-10000", "10000.0e-4", "-12.3E+01"]
    for number in numbers:
        value = Decimal(number)
        cases.append((number, 2, int(value == value.to_integral_value()), number))
    rows = [json_case(body) for body, *_ in cases]
    for case, output in zip(cases, run_cases(binary, rows, "scalars"), strict=True):
        body, kind, integral, text = case
        assert result(output) == (True, True, kind, integral, False, text), body
    rows = [json_case(body, expected_type=kind) for body, *_ in cases for kind in [1, 2, 3, 4, 7]]
    outputs = iter(run_cases(binary, rows, "strict-types"))
    for body, kind, integral, text in cases:
        for expected in [1, 2, 3, 4, 7]:
            accepted = expected == kind or (expected == 7 and kind == 2 and integral == 1)
            observed = result(next(outputs))
            assert observed == (True, bool(accepted), kind if accepted else 0,
                                integral if accepted else 0, not accepted,
                                text if accepted else ""), (body, expected)


def test_full_document_validation_and_utf8(binary):
    invalid = [b"", b" ", b"01", b"-01", b"+1", b"1.", b".1", b"1e", b"1e+", b"--1",
               b"NaN", b"Infinity", b"true false", b"nul", b"TRUE", b"[]tail", b"[1,]",
               b'{"a":1,}', b'{"a" 1}', b'{a:1}', b'"unterminated', b'"\\x00"', b'"a\nb"',
               b'"\\ud800"', b'"\\udfff"', b'"\\ud800\\u0041"', b'"\\u0000"', b'"\\uZZZZ"',
               b'{"a":1,"\\u0061":2}', b'{"selected":1,"unused":{"x":1,"x":2}}',
               b'"\xc0\x80"', b'"\xed\xa0\x80"', b'"\xf4\x90\x80\x80"', b'"\xe2\x82"',
               b'"\x80"', b'"\xff"', b"1e10001", b"1e-10001", b"1e99999999999999999"]
    outputs = run_cases(binary, [json_case(b) for b in invalid], "invalid")
    for body, output in zip(invalid, outputs, strict=True):
        assert result(output) == (False, False, 0, 0, True, ""), body
    valid = ['"\\ud83d\\ude00"', '"\\b\\f\\n\\r\\t\\/\\\\\\\""', '"é中😀"', '"\\u0061"']
    outputs = run_cases(binary, [json_case(b) for b in valid], "unicode")
    for body, output in zip(valid, outputs, strict=True):
        assert result(output) == (True, True, 1, 0, False, json.loads(body)), body


def test_path_semantics_missing_null_empty_and_unselected_containers(binary):
    body = '{"a.b":[{"a\\\"b":{"é":null}},"",true],"number":123,"empty":{}}'
    cases = [(("a.b", 0, 'a"b', "é"), True, True, 4, "null"),
             (("a.b", 1), True, True, 1, ""), (("a.b", 1), False, False, 0, ""),
             (("a.b", 2), True, True, 3, "true"), (("a.b", 3), True, False, 0, ""),
             (("missing",), True, False, 0, ""), (("empty",), True, False, 0, ""),
             (("a.b",), True, False, 0, ""), (("number", 0), True, False, 0, ""),
             ((0,), True, False, 0, ""), (("a.b", 18446744073709551615), True, False, 0, "")]
    rows = [json_case(body, path, empty=empty) for path, empty, *_ in cases]
    for case, output in zip(cases, run_cases(binary, rows, "paths"), strict=True):
        path, empty, success, kind, text = case
        assert result(output) == (True, success, kind, 0, not success, text), (path, empty)
    row = run_cases(binary, [json_case("null", occurrence=2)], "occurrence")[0]
    assert result(row) == (True, False, 0, 0, True, "")


def test_exact_bounded_resources(binary):
    cases = [
        ("[" * 64 + "0" + "]" * 64, True, (0,) * 64, True),
        ("[" * 65 + "0" + "]" * 65, False, (), False),
        ("[" + ",".join("0" for _ in range(4095)) + "]", True, (4094,), True),
        ("[" + ",".join("0" for _ in range(4096)) + "]", False, (0,), False),
        (json.dumps({str(i): i for i in range(1024)}), True, ("1023",), True),
        (json.dumps({str(i): i for i in range(1025)}), False, ("0",), False),
        ('"' + "x" * 65536 + '"', True, (), True),
        ('"' + "x" * 65537 + '"', True, (), False),
        (" " * (1048576 - 1) + "0", True, (), True),
        (" " * 1048576 + "0", False, (), False),
    ]
    outputs = run_cases(binary, [json_case(b, p) for b, _, p, _ in cases], "limits")
    for i, (case, output) in enumerate(zip(cases, outputs, strict=True)):
        _, parsed, _, selected = case
        actual = result(output)
        assert (actual[0], actual[1], actual[4]) == (parsed, selected, not selected), i


def test_boundary_first_left_nearest_right_occurrence_empty_and_overflow(binary):
    cases = [("{a{b{c}", "{", "}", 1, 1, 0, "a{b{c"),
             ("{a}{b}", "{", "}", 1, 1, 0, None),
             ("{a}{b}", "{", "}", 2, 0, 0, "b"),
             ("{}", "{", "}", 1, 1, 0, None),
             ("{}", "{", "}", 1, 1, 1, ""),
             ("{a", "{", "}", 1, 1, 1, None),
             ("{a}", "{", "}", 2, 0, 1, None),
             ("{a}" * MAX_MATCHES, "{", "}", MAX_MATCHES, 0, 0, "a"),
             ("{a}" * (MAX_MATCHES + 1), "{", "}", 1, 0, 0, None),
             ("{" + "x" * 65537 + "}", "{", "}", 1, 0, 0, None)]
    rows = [("B", hx(b), hx(left), hx(right), o, ex, e)
            for b, left, right, o, ex, e, _ in cases]
    outputs = run_cases(binary, rows, "boundaries")
    for i, (case, output) in enumerate(zip(cases, outputs, strict=True)):
        expected = case[-1]
        actual = result(output)
        assert actual[1:] == (expected is not None, 1 if expected is not None else 0, 0,
                              expected is None, expected or ""), i


def test_encodings_and_failed_typed_json_markers(binary):
    values = ["a b/+%?&=~", '??/"\\\r\n\té中😀', "ERROR_token", "simple"]
    rows = [("S", hx(v), 1, 0, mode) for v in values for mode in range(5)]
    outputs = iter(run_cases(binary, rows, "encodings"))
    for value in values:
        safe = all(32 <= ord(c) <= 126 and c not in "{}\\%" for c in value)
        for mode in range(5):
            expected = [value, value if safe else "ERROR_token", quote(value, safe="~"),
                        quote_plus(value, safe="~"), json.dumps(value, ensure_ascii=False)][mode]
            row = next(outputs)
            observed = bytes.fromhex(row[2]).decode()
            # JSON escape spellings may differ, but values and UTF-8 must not.
            matches = json.loads(observed) == value if mode == 4 else observed == expected
            assert matches, (value, mode)
            assert row[1] == str(int(mode == 1 and not safe))
            assert bytes.fromhex(row[3]).decode() == value, "Header corrupted shared state"
    rows = [("S", "", 0, 0, 4), ("S", hx("123456789012345678901"), 2, 1, 4),
            ("S", hx("false"), 3, 0, 4), ("S", hx("null"), 4, 0, 4)]
    observed = run_cases(binary, rows, "typed-bindings")
    assert [bytes.fromhex(r[2]).decode() for r in observed] == [
        '"ERROR_token"', "123456789012345678901", "false", "null"]
    assert [r[1] for r in observed] == ["1", "0", "0", "0"]


def test_capture_native_length_invalid_bytes_and_release(binary):
    cases = [(b"valid", 1, 0, True), (b"", 1, 0, True), (b"a\x00b", 1, 0, False),
             (b"\xff", 1, 0, False), (b"valid", 0, 0, False), (b"valid", 1, 1, False),
             (b"x" * 1048577, 1, 0, False)]
    rows = [("C", hx(body), ok, rc) for body, ok, rc, _ in cases]
    for case, output in zip(cases, run_cases(binary, rows, "capture"), strict=True):
        body, ok, _, expected = case
        assert int(output[1]) == (11 if ok else 0), "Native capture must be freed even on failure"
        assert bool(int(output[2])) == expected
        assert bytes.fromhex(output[6]) == (body if expected else b"")


def test_allocation_faults_are_clean_and_never_reuse_stale_value(binary):
    body = '{"selected":"fresh","unused":[true,null,123,{"x":"y"}]}'
    rows = [json_case(body, ("selected",), fail_at=i) for i in range(24)]
    outputs = run_cases(binary, rows, "allocation-failures")
    assert any(result(row)[1] for row in outputs), "Fault range must reach beyond final allocation"
    assert any(not result(row)[1] for row in outputs)
    for row in outputs:
        observed = result(row)
        assert observed[-1] == ("fresh" if observed[1] else "")
        assert observed[4] is not observed[1]


def test_seeded_synthetic_parser_differential(binary):
    rng = random.Random(20261104)
    rows, expectations = [], []
    for _ in range(160):
        string = "".join(rng.choice('aé😀\\\"\n\t /') for _ in range(rng.randrange(20)))
        scalar = rng.choice([None, True, False, rng.randrange(-10**25, 10**25), string])
        body = json.dumps({"selected": [scalar], "unused": {"nested": [1, 2, 3]}},
                          ensure_ascii=rng.choice([True, False]))
        rows.append(json_case(body, ("selected", 0)))
        kind = (4 if scalar is None else 3 if isinstance(scalar, bool)
                else 2 if isinstance(scalar, int) else 1)
        text = scalar if kind == 1 else json.dumps(scalar)
        expectations.append((True, True, kind, int(kind == 2), False, text))
        # Every proper prefix of these object documents is invalid, without repair.
        prefix = body[:rng.randrange(len(body))]
        rows.append(json_case(prefix, ("selected", 0)))
        expectations.append((False, False, 0, 0, True, ""))
    outputs = run_cases(binary, rows, "seeded-differential")
    for i, (row, expected) in enumerate(zip(outputs, expectations, strict=True)):
        assert result(row) == expected, (i, rows[i])


def test_seeded_mutation_fuzz_against_independent_strict_json_decoder(binary):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def forbidden_constant(value):
        raise ValueError(value)

    def unicode_valid(value):
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
            if "\0" in value:
                raise ValueError("NUL outside runtime profile")
        elif isinstance(value, list):
            for item in value:
                unicode_valid(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                unicode_valid(key)
                unicode_valid(item)

    rng = random.Random(510450)
    seeds = ['{"selected":1,"other":[true,null,"x"]}', '[0,1,2]', '"a\\n\\u00e9"',
             '1.230e-2', 'true', 'null', '""', '{"a":{"b":1}}']
    alphabet = '{}[],:"\\truefalsnul0123456789.eE+- \t\nxyz'
    bodies = []
    for _ in range(800):
        body = rng.choice(seeds)
        for _ in range(rng.randrange(1, 4)):
            index = rng.randrange(len(body) + 1)
            action = rng.randrange(3)
            if action == 0:
                body = body[:index] + rng.choice(alphabet) + body[index:]
            elif action == 1:
                body = body[:index] + body[index + 1:]
            else:
                body = body[:index] + rng.choice(alphabet) + body[index + 1:]
        bodies.append(body)
    outputs = run_cases(binary, [json_case(body) for body in bodies], "mutation-fuzz")
    valid_count = 0
    for i, (body, output) in enumerate(zip(bodies, outputs, strict=True)):
        observed = result(output)
        try:
            value = json.loads(body, parse_float=Decimal, parse_int=Decimal,
                               object_pairs_hook=pairs, parse_constant=forbidden_constant)
            unicode_valid(value)
        except (ValueError, UnicodeError):
            assert observed == (False, False, 0, 0, True, ""), (i, body)
            continue
        valid_count += 1
        if isinstance(value, (dict, list)):
            assert observed == (True, False, 0, 0, True, ""), (i, body)
        else:
            kind = (1 if isinstance(value, str) else 2 if isinstance(value, Decimal)
                    else 4 if value is None else 3)
            assert observed[:3] == (True, True, kind), (i, body)
            integral = int(value == value.to_integral_value()) if kind == 2 else 0
            assert observed[3] == integral, (i, body)
            assert observed[4] is False
            decoded = (json.loads(observed[5], parse_float=Decimal, parse_int=Decimal)
                       if kind != 1 else observed[5])
            assert decoded == value, (i, body)
    assert 20 < valid_count < 400, "Corpus must exercise both accepted and rejected syntax"
