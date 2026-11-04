"""Strict declarations and final-batch reference/lifecycle validation."""

from copy import deepcopy
from decimal import Decimal

import pytest

from har2load.domain import Body, Capture, Exchange, Grouping, Har2LoadError, JSONValue
from har2load.parameters import ParameterModel


def capture() -> Capture:
    return Capture("cap", "digest", b"", (
        Exchange("ex_a", 0, {}, response_body=Body(
            "present", text='{"token":"captured"}', view_status="available",
        )), Exchange("ex_b", 1, {}),
    ))


def grouping() -> Grouping:
    return Grouping((), ("ex_a", "ex_b"), 5.0)


def variable(**updates: JSONValue) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "id": "v", "name": "token", "value_type": "string",
        "source": {"kind": "literal", "value": "safe"},
        "initialize": {"event": "iteration_start"}, "scope": "iteration",
        "reset": "iteration_start", "purpose": "Synthetic test", "sensitive": False,
    }
    result.update(updates)
    return result


def extractor(**updates: JSONValue) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "id": "e", "exchange_id": "ex_a", "area": "response_body",
        "selector": {"dialect": "json_path_subset_v1", "expression": "$.token"},
        "cardinality": "exactly_one", "allow_empty": False,
        "on_failure": "mark_error_and_continue",
    }
    result.update(updates)
    return result


def binding(**updates: JSONValue) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "id": "b", "variable_id": "v", "exchange_id": "ex_b",
        "location": {"area": "request_header", "index": 0},
    }
    result.update(updates)
    return result


def model(*records: tuple[str, dict[str, JSONValue]]) -> dict[str, JSONValue]:
    parameters = ParameterModel.empty()
    for kind, record in records:
        ParameterModel.apply_operation(parameters, {"op": "upsert_" + kind, kind: record}, "c.1-0")
    return parameters


def validate(parameters: JSONValue) -> None:
    ParameterModel.validate(parameters, capture(), grouping())


@pytest.mark.parametrize("updates", [
    {"id": "bad-id"}, {"name": "é"}, {"purpose": " "}, {"purpose": "x" * 1025},
    {"sensitive": 1}, {"value_type": "object"}, {"change_id": "spoof"},
    {"extra": True}, {"scope": "global"}, {"reset": "session_start"},
    {"initialize": {"event": "iteration_start", "exchange_id": "ex_a"}},
    {"source": {"kind": "literal", "value": "x" * 65537}},
    {"source": {"kind": "literal", "value": "\ud800"}},
    {"source": {"kind": "expression", "code": "raise Exception()"}},
    {"source": {"kind": "dataset"}}, {"source": {"kind": "agent_code"}},
    {"source": {"kind": "literal", "value": True}, "value_type": "integer"},
    {"source": {"kind": "literal", "value": Decimal("Infinity")}, "value_type": "number"},
    {"source": {"kind": "external", "key": "TOKEN", "allow_empty": 0}},
])
def test_malformed_variable(updates: dict[str, JSONValue]) -> None:
    with pytest.raises(Har2LoadError) as error:
        model(("variable", variable(**updates)))
    assert "raise Exception" not in str(error.value)
    assert "bad-id" not in str(error.value)


@pytest.mark.parametrize(("source", "kind"), [
    ({"kind": "external", "key": "NEVER_READ", "allow_empty": False}, "string"),
    ({"kind": "generated", "generator": "integer", "minimum": -1, "maximum": 2}, "integer"),
    ({"kind": "generated", "generator": "string", "length": 8, "alphabet": "ab !"}, "string"),
    ({"kind": "generated", "generator": "uuid"}, "string"),
    ({"kind": "generated", "generator": "utc_datetime", "offset_seconds": -315360000,
      "format": "iso8601"}, "string"),
    ({"kind": "generated", "generator": "utc_date_range", "minimum": "2024-02-29",
      "maximum": "2025-01-01", "format": "iso8601"}, "string"),
])
def test_declarations_only(source: JSONValue, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Runtime input or execution accessed")
    monkeypatch.setattr("os.getenv", forbidden)
    monkeypatch.setattr("builtins.eval", forbidden)
    parameters = model(("variable", variable(source=source, value_type=kind)))
    validate(parameters)
    assert ParameterModel.checks(parameters, capture()) == []


@pytest.mark.parametrize("source", [
    {"kind": "generated", "generator": "integer", "minimum": True, "maximum": 2},
    {"kind": "generated", "generator": "integer", "minimum": 3, "maximum": 2},
    {"kind": "generated", "generator": "string", "length": 0, "alphabet": "ab"},
    {"kind": "generated", "generator": "string", "length": 4097, "alphabet": "ab"},
    {"kind": "generated", "generator": "string", "length": 1, "alphabet": "aa"},
    {"kind": "generated", "generator": "string", "length": 1, "alphabet": "\n"},
    {"kind": "generated", "generator": "uuid", "seed": 1},
    {"kind": "generated", "generator": "utc_datetime", "offset_seconds": 315360001,
     "format": "iso8601"},
    {"kind": "generated", "generator": "utc_date_range", "minimum": "2023-02-29",
     "maximum": "2025-01-01", "format": "iso8601"},
    {"kind": "generated", "generator": "utc_date_range", "minimum": "2026-01-01",
     "maximum": "2025-01-01", "format": "iso8601"},
])
def test_bad_generators(source: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        model(("variable", variable(source=source)))


def test_unknown_operation_and_exact_shapes() -> None:
    assert ParameterModel.empty() == {"variables": [], "extractors": [], "bindings": []}
    assert not ParameterModel.validate_operation({"op": "rename_group"})
    assert not ParameterModel.apply_operation(ParameterModel.empty(), {"op": "other"}, "c")
    for operation in [{"op": "upsert_variable"}, {"op": "remove_variable", "id": "v", "x": 1}]:
        with pytest.raises(Har2LoadError):
            ParameterModel.validate_operation(operation)


def test_provenance_sorting_and_no_aliasing() -> None:
    authored = variable()
    parameters = model(("variable", variable(id="z", name="z")), ("variable", authored))
    authored["source"]["value"] = "mutated"
    assert [item["id"] for item in parameters["variables"]] == ["v", "z"]
    assert parameters["variables"][0]["change_id"] == "c.1-0"
    assert parameters["variables"][0]["source"]["value"] == "safe"
    validate(parameters)


def test_final_batch_validation_is_order_independent() -> None:
    extracted = variable(source={"kind": "extracted", "extractor_id": "e"},
                         initialize={"event": "after_response", "exchange_id": "ex_a"})
    parameters = model(("binding", binding()), ("variable", extracted), ("extractor", extractor()))
    validate(parameters)
    check = ParameterModel.checks(parameters, capture())[0]
    assert check["status"] == "matched"
    assert check["value"] == "captured"
    assert check["failure_marker"] is None
    assert check["verification"] == "recorded_response_only"


@pytest.mark.parametrize("kind", ["variable", "extractor", "binding"])
def test_remove_missing_rejects(kind: str) -> None:
    with pytest.raises(Har2LoadError):
        ParameterModel.apply_operation(ParameterModel.empty(), {"op": "remove_" + kind, "id": "x"},
                                       "c")


def test_removal_does_not_cascade() -> None:
    parameters = model(("variable", variable()), ("binding", binding()))
    assert ParameterModel.apply_operation(parameters, {"op": "remove_variable", "id": "v"}, "c")
    assert len(parameters["bindings"]) == 1
    with pytest.raises(Har2LoadError):
        validate(parameters)
    ParameterModel.apply_operation(parameters, {"op": "remove_binding", "id": "b"}, "c")
    validate(parameters)


@pytest.mark.parametrize("records", [
    (("variable", variable()), ("variable", variable(id="v2"))),
    (("variable", variable()), ("extractor", extractor(id="v"))),
    (("binding", binding()),),
    (("extractor", extractor(exchange_id="unknown")),),
    (("variable", variable(initialize={"event": "before_request", "exchange_id": "unknown"})),),
])
def test_references_and_uniqueness(records: tuple[tuple[str, dict[str, JSONValue]], ...]) -> None:
    with pytest.raises(Har2LoadError):
        validate(model(*records))


@pytest.mark.parametrize(("scope", "event", "exchange_id", "consumer", "valid"), [
    ("iteration", "session_start", None, "ex_b", False),
    ("session", "session_start", None, "ex_a", True),
    ("session", "after_response", "ex_a", "ex_b", True),
    ("iteration", "after_response", "ex_a", "ex_a", False),
    ("iteration", "before_request", "ex_b", "ex_a", False),
    ("iteration", "before_request", "ex_a", "ex_a", True),
    ("request", "before_request", "ex_a", "ex_a", True),
    ("request", "before_request", "ex_a", "ex_b", False),
    ("request", "iteration_start", None, "ex_b", False),
    ("request", "after_response", "ex_a", "ex_b", False),
])
def test_lifecycle(scope: str, event: str, exchange_id: str | None,
                   consumer: str, valid: bool) -> None:
    initialize: dict[str, JSONValue] = {"event": event}
    if exchange_id is not None:
        initialize["exchange_id"] = exchange_id
    reset = {"session": "session_start", "iteration": "iteration_start",
             "request": "before_request"}
    records = (("variable", variable(scope=scope, reset=reset[scope], initialize=initialize)),
               ("binding", binding(exchange_id=consumer)))
    if valid:
        validate(model(*records))
    else:
        with pytest.raises(Har2LoadError):
            validate(model(*records))


def test_type_mismatch_is_not_coerced() -> None:
    parameters = model(("extractor", extractor()), ("variable", variable(
        source={"kind": "extracted", "extractor_id": "e"}, value_type="integer",
        initialize={"event": "after_response", "exchange_id": "ex_a"},
    )))
    validate(parameters)
    check = ParameterModel.checks(parameters, capture())[0]
    assert (check["status"], check["value"], check["failure_marker"]) == (
        "type_mismatch", None, "ERROR_token",
    )


@pytest.mark.parametrize("location", [
    {"area": "request_json", "pointer": "bad"}, {"area": "request_json", "pointer": "/bad~2"},
    {"area": "request_query", "index": True}, {"area": "request_header", "index": -1},
    {"area": "request_text", "start": 2, "end": 1},
    {"area": "request_path", "index": 0, "extra": True},
])
def test_binding_shapes(location: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        model(("binding", binding(location=location)))


def test_null_binding_and_stored_shapes() -> None:
    null_variable = variable(value_type="null", source={"kind": "literal", "value": None})
    parameters = model(("variable", null_variable),
                       ("binding", binding(location={"area": "request_json", "pointer": "/x~0~1"})))
    validate(parameters)
    changed = deepcopy(parameters)
    changed["bindings"][0]["location"] = {"area": "request_query", "index": 0}
    with pytest.raises(Har2LoadError):
        validate(changed)
    parameters["variables"].append(deepcopy(parameters["variables"][0]))
    with pytest.raises(Har2LoadError):
        validate(parameters)


@pytest.mark.parametrize(("kind", "limit"), [
    ("variable", 256), ("extractor", 256), ("binding", 1024),
])
def test_record_limits(kind: str, limit: int) -> None:
    parameters = model(("variable", variable())) if kind == "binding" else ParameterModel.empty()
    factory = {"variable": variable, "extractor": extractor, "binding": binding}[kind]
    for index in range(limit):
        definition = factory(id=f"record_{index}")
        if kind == "variable":
            definition["name"] = f"name_{index}"
        parameters[kind + "s"].append({**definition, "change_id": "c"})
    validate(parameters)
    parameters[kind + "s"].append({**factory(id="overflow"), "change_id": "c"})
    with pytest.raises(Har2LoadError):
        validate(parameters)


@pytest.mark.parametrize("updates", [
    {"cardinality": "occurrence", "occurrence": True}, {"cardinality": "occurrence"},
    {"occurrence": 1}, {"allow_empty": "yes"}, {"on_failure": "abort"},
    {"selector": {"dialect": "native", "expression": "private"}}, {"extra": None},
])
def test_extractor_declaration_errors(updates: dict[str, JSONValue]) -> None:
    with pytest.raises(Har2LoadError):
        model(("extractor", extractor(**updates)))


@pytest.mark.parametrize("event", ["iteration_start", "before_request", "after_response"])
def test_extracted_initializer_must_match_producer(event: str) -> None:
    initializer: dict[str, JSONValue] = {"event": event}
    if event != "iteration_start":
        initializer["exchange_id"] = "ex_b"
    parameters = model(("extractor", extractor()), ("variable", variable(
        source={"kind": "extracted", "extractor_id": "e"}, initialize=initializer,
    )))
    with pytest.raises(Har2LoadError):
        validate(parameters)


def test_dangling_extractor_and_unavailable_evidence() -> None:
    parameters = model(("extractor", extractor(exchange_id="ex_b")), ("variable", variable(
        source={"kind": "extracted", "extractor_id": "e"},
        initialize={"event": "after_response", "exchange_id": "ex_b"},
    )))
    validate(parameters)
    check = ParameterModel.checks(parameters, capture())[0]
    assert (check["status"], check["value"], check["failure_marker"]) == (
        "unavailable", None, "ERROR_token",
    )
    ParameterModel.apply_operation(parameters, {"op": "remove_extractor", "id": "e"}, "c")
    with pytest.raises(Har2LoadError):
        validate(parameters)


@pytest.mark.parametrize("value", [None, True, [], {}, 1, "x"])
def test_wrong_field_types_fail_safely(value: JSONValue) -> None:
    for factory, kind in [(variable, "variable"), (extractor, "extractor"), (binding, "binding")]:
        for key in factory():
            try:
                model((kind, factory(**{key: value})))
            except Har2LoadError as error:
                assert str(error) == "Invalid parameter declaration."


def test_stored_provenance_and_block_shape() -> None:
    for parameters in [None, [], {}, {**ParameterModel.empty(), "extra": []}]:
        with pytest.raises(Har2LoadError):
            validate(parameters)
    parameters = model(("variable", variable()))
    for provenance in ["bad/secret", None, "x" * 65]:
        parameters["variables"][0]["change_id"] = provenance
        with pytest.raises(Har2LoadError):
            validate(parameters)


def test_grouping_order_not_capture_index_controls_lifecycle() -> None:
    parameters = model(("variable", variable(initialize={
        "event": "after_response", "exchange_id": "ex_a",
    })), ("binding", binding()))
    validate(parameters)
    with pytest.raises(Har2LoadError):
        ParameterModel.validate(parameters, capture(), Grouping((), ("ex_b", "ex_a"), 5.0))


def test_upsert_replaces_and_failed_remove_is_unchanged() -> None:
    parameters = model(("variable", variable()))
    ParameterModel.apply_operation(parameters, {"op": "upsert_variable", "variable": variable(
        purpose="Replacement",
    )}, "c2")
    assert len(parameters["variables"]) == 1
    assert parameters["variables"][0]["change_id"] == "c2"
    before = deepcopy(parameters)
    with pytest.raises(Har2LoadError):
        ParameterModel.apply_operation(parameters, {"op": "remove_variable", "id": "missing"}, "c")
    assert parameters == before


def test_no_io_or_runtime_production(monkeypatch: pytest.MonkeyPatch) -> None:
    declarations = [
        {"kind": "external", "key": "PRIVATE_INPUT", "allow_empty": False},
        {"kind": "generated", "generator": "uuid"},
        {"kind": "generated", "generator": "utc_datetime", "offset_seconds": 0,
         "format": "iso8601"},
    ]
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Preparation must not perform I/O or produce runtime values")
    with monkeypatch.context() as patch:
        for target in ["builtins.open", "os.getenv", "socket.socket", "subprocess.run",
                       "random.randrange", "uuid.uuid4", "builtins.eval"]:
            patch.setattr(target, forbidden)
        for source in declarations:
            validate(model(("variable", variable(source=source))))


def test_batch_can_repair_temporary_duplicate_names() -> None:
    parameters = model(("variable", variable()), ("variable", variable(id="v2")))
    with pytest.raises(Har2LoadError):
        validate(parameters)
    ParameterModel.apply_operation(parameters, {
        "op": "upsert_variable", "variable": variable(id="v2", name="renamed"),
    }, "c2")
    validate(parameters)


@pytest.mark.parametrize("source", [
    {"kind": "literal", "value": "safe", "initial_value": "seed"},
    {"kind": "extracted", "extractor_id": "e", "initial_value": "seed"},
    {"kind": "generated", "generator": "uuid", "initial_value": "seed"},
    {"kind": "agent_code", "behavior_id": "behavior", "initial_value": "seed"},
])
def test_initial_value_is_external_only(source: JSONValue) -> None:
    with pytest.raises(Har2LoadError):
        ParameterModel.validate_operation({
            "op": "upsert_variable", "variable": variable(source=source),
        })
