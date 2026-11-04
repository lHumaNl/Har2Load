"""Pure source capabilities must never read inputs or compute live values."""

from dataclasses import replace

import pytest

from har2load.runtime_sources import initial_values, source_diagnostics
from har2load.runtime_types import RuntimePlan


def variable(identifier, source, value_type="string"):
    return {"id": identifier, "name": identifier, "value_type": value_type, "source": source}


def plan(*variables):
    return RuntimePlan(tuple(variables), (), ())


def external(key="INPUT", **changes):
    return {"kind": "external", "key": key, "allow_empty": False, **changes}


def test_initial_values_are_explicit_and_missing_is_not_empty():
    model = plan(variable("missing", external("MISSING")),
                 variable("empty", external("EMPTY", initial_value="")),
                 variable("known", external(initial_value="sensitive initial")),
                 variable("alias", external()))
    assert initial_values(model) == {"EMPTY": "", "INPUT": "sensitive initial"}
    assert not source_diagnostics(model, "jmeter")
    assert "initial_value" not in model.variables[0]["source"]


def test_conflicting_initial_values_are_not_silently_overwritten():
    model = plan(variable("one", external(initial_value="first")),
                 variable("two", external(initial_value="second")))
    for target in ("jmeter", "loadrunner"):
        assert {d.code for d in source_diagnostics(model, target)} == {
            "RUNTIME_INPUT_INITIAL_CONFLICT"}
        assert all(d.target == target for d in source_diagnostics(model, target))


@pytest.mark.parametrize("minimum,maximum", [(-2**63, 2**63 - 1), (0, 0), (-1, 1)])
def test_integer_generation_supports_inclusive_signed_64_bit_bounds(minimum, maximum):
    model = plan(variable("number", {"kind": "generated", "generator": "integer",
                                    "minimum": minimum, "maximum": maximum}, "integer"))
    assert not source_diagnostics(model, "jmeter")
    assert not source_diagnostics(model, "loadrunner")
    wide = replace(model, variables=({**model.variables[0], "source": {
        **model.variables[0]["source"], "minimum": -2**63 - 1}},))
    assert {d.code for d in source_diagnostics(wide, "jmeter")} == {
        "RUNTIME_GENERATOR_RANGE_UNSUPPORTED"}


def test_source_planning_never_reads_environment_or_generates_values(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preparation must not resolve or generate runtime inputs")

    monkeypatch.setattr("os.getenv", forbidden)
    monkeypatch.setattr("uuid.uuid4", forbidden)
    monkeypatch.setattr("random.randint", forbidden)
    model = plan(variable("input", external()),
                 variable("uuid", {"kind": "generated", "generator": "uuid"}))
    assert initial_values(model) == {}
    assert source_diagnostics(model, "jmeter") == []
