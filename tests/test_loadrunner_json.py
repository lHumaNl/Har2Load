"""Parser source contracts plus independent Python oracles, not C execution proof."""

import re
from decimal import Decimal

import pytest

from har2load.domain import Har2LoadError
from har2load.loadrunner_json import (
    MAX_JSON_NODES,
    MAX_NUMBER_EXPONENT,
    MAX_OBJECT_KEYS,
    parser_lines,
)
from har2load.values import matches_type, parse_json_text


def function(name: str) -> str:
    source = "\n".join(parser_lines())
    match = re.search(r"static [^\n]*\b" + name + r"\([^;]*?\n\{(.*?)\n\}", source, re.S)
    assert match is not None, name
    return match[1]


def test_document_validation_precedes_path_selection_and_checks_every_subtree() -> None:
    parse = function("h2l_json_parse")
    assert "h2l_value_parse(j, 0)" in parse and "j->p != j->end" in parse
    assert "H2L_FIELD_LIMIT" in parse and "h2l_utf8" in parse
    select = function("h2l_json_select")
    assert select.index("j->failed") < select.index("h2l_path_child")
    assert "occurrence != 1" in select and "type > 4" in select
    assert "h2l_unique(j, parent, key)" in function("h2l_child")
    assert "strcmp(j->nodes[child].key, key)" in function("h2l_unique")


def test_resource_limits_cover_allocation_depth_keys_numbers_and_cleanup() -> None:
    source = "\n".join(parser_lines())
    for name, value in [("NODE", MAX_JSON_NODES), ("KEY", MAX_OBJECT_KEYS),
                        ("EXPONENT", MAX_NUMBER_EXPONENT), ("DEPTH", 64)]:
        assert f"#define H2L_{name}_LIMIT {value}" in source
    assert "j->used == H2L_NODE_LIMIT" in function("h2l_value_parse")
    assert "depth >= H2L_DEPTH_LIMIT" in function("h2l_value_parse")
    assert "value > H2L_EXPONENT_LIMIT" in function("h2l_exponent")
    assert "free(j->nodes[i].key)" in function("h2l_json_free")
    assert "free(j->nodes[i].text)" in function("h2l_json_free")
    assert "free(j->nodes)" in function("h2l_json_free")


@pytest.mark.parametrize("text", ['{"selected":1,"unused":{"x":1,"x":2}}',
                                  '{"a":1,"\\u0061":2}', '[1,]', '{"a":1,}',
                                  '01', '+1', '1.', '1e', 'NaN', 'Infinity',
                                  '{"a":1} trailing', '"\\ud800"', '"\\udfff"'])
def test_independent_invalid_document_oracle(text: str) -> None:
    with pytest.raises(Har2LoadError):
        parse_json_text(text)


@pytest.mark.parametrize("lexeme,integral", [("1.00", True), ("100e-2", True),
                                             ("100e-3", False), ("-0.00e-9", True),
                                             ("0.0010", False), ("12.3e1", True),
                                             ("123456789012345678901", True),
                                             ("1.000000000000000000001", False)])
def test_lossless_number_oracle_and_emitted_integrality_rule(lexeme: str, integral: bool) -> None:
    parsed = parse_json_text(lexeme)
    assert parsed == Decimal(lexeme)
    assert matches_type(parsed, "integer") is integral
    body = function("h2l_number")
    assert "!nonzero || exp >= fraction - zeros" in body
    assert "h2l_copy(start, (size_t)(j->p - start))" in body
    assert not any(token in body for token in ("double", "float", "atoi", "strtod"))


def test_json_unicode_unescaping_and_key_matching_are_explicit() -> None:
    assert parse_json_text('"\\ud83d\\ude00"') == "\U0001f600"
    assert "low < 56320 || low > 57343" in function("h2l_codepoint")
    assert "else if (cp >= 56320 && cp <= 57343)" in function("h2l_codepoint")
    assert "if (!cp) j->failed = 1" in function("h2l_codepoint")
    assert "h2l_unicode(out, n, h2l_codepoint(j))" in function("h2l_escape")
    assert "strcmp(part->key, j->nodes[child].key)" in function("h2l_path_child")


def test_containers_and_null_are_not_conflated_with_missing_or_empty() -> None:
    body = function("h2l_json_select")
    assert "node < 0" in body and "type > 4" in body
    assert "type == 1 && !*j->nodes[node].text" in body
    assert 'h2l_keyword(j, node, "null", 4)' in function("h2l_value_parse")
    assert parse_json_text('null') is None
    assert not matches_type("null", "null")
