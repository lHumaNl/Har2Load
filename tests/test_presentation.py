import json

import pytest

from har2load.domain import Diagnostic, Har2LoadError
from har2load.presentation import envelope, resolve_pointer, validate_range


def test_json_pointer_preserves_empty_names_escaping_and_arrays():
    value = {"": {"/": {"~1": ["first", "second"]}}}
    assert resolve_pointer(value, "//~1/~01/1") == "second"
    assert resolve_pointer(value, "") is value


@pytest.mark.parametrize("pointer", ["/01", "/-1", "/+1", "/-", "/999999999999999999"])
def test_array_pointer_requires_canonical_valid_index(pointer):
    with pytest.raises(Har2LoadError):
        resolve_pointer([1, 2], pointer)


def test_diagnostics_are_bounded_with_continuation():
    result = envelope("summary", {}, diagnostics=tuple(
        Diagnostic("TEST", "warning", "Constant message") for _ in range(1000)
    ))
    assert len(result["diagnostics"]) == 10
    assert result["truncated"] is True
    assert result["data"]["diagnostics_continuation"] == {"section": "diagnostics", "offset": 0}
    assert len(json.dumps(result)) < 5000


@pytest.mark.parametrize("offset,limit", [(True, 1), (1, True), (1.5, 1), (0, float("inf"))])
def test_page_ranges_reject_non_integer_values(offset, limit):
    with pytest.raises(Har2LoadError):
        validate_range(offset, limit, 100)
