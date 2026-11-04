"""Offline native project assets and emitted source contracts; not SDK validation."""

import configparser
import csv
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch

import pytest

from har2load.domain import JSONValue
from har2load.generation_types import RequestPlan
from har2load.loadrunner import LoadRunnerGenerator
from tests.test_loadrunner_runtime import ACTION, plan, variable


def source_plan(source: dict[str, JSONValue], kind: str = "string") -> RequestPlan:
    value = plan(variable(source=source, value_type=kind,
                          initialize={"event": "iteration_start"}))
    assert value.runtime is not None
    return replace(value, runtime=replace(value.runtime, extractors=()))


def external(**changes: JSONValue) -> dict[str, JSONValue]:
    return {"kind": "external", "key": "token", "allow_empty": False, **changes}


def test_native_assets_are_project_local_and_initial_not_embedded_in_code() -> None:
    build = LoadRunnerGenerator().generate(source_plan(external(initial_value="PRIVATE_SEED")))
    assert build.status == "generated"
    path = "data/input_" + sha256(b"token").hexdigest() + ".dat"
    assert build.files["loadrunner/" + path] == b"Value\nPRIVATE_SEED\n"
    fragment = build.files["loadrunner/native_inputs.prm"].decode()
    assert '[parameter:token]' in fragment and 'ParamName="token"' in fragment
    assert f'Table="{path}"' in fragment and 'GenerateNewVal="Once"' in fragment
    assert 'Type="Table"' in fragment and 'OutOfRangePolicy="ContinueWithLast"' in fragment
    assert b"PRIVATE_SEED" not in build.files[ACTION]
    assert "PRIVATE_SEED" not in str(build.metadata)
    assert b"ParameterFile=native_inputs.prm" in build.files["loadrunner/README.txt"]


@pytest.mark.parametrize("value", ["", "é", 'a"b', "a,b", "a\nb", "a\tb", " x", "x "])
def test_unverified_native_file_representations_block(value: str) -> None:
    build = LoadRunnerGenerator().generate(source_plan(external(initial_value=value)))
    assert build.status == "blocked" and not build.files
    assert any(d.code == "LOADRUNNER_NATIVE_INPUT_REPRESENTATION_UNSUPPORTED"
               for d in build.diagnostics)


def test_missing_native_key_has_no_active_definition_or_eval_call() -> None:
    build = LoadRunnerGenerator().generate(source_plan(external()))
    assert build.status == "generated"
    assert not any(p.endswith((".prm", ".dat")) for p in build.files)
    assert b'"{token}"' not in build.files[ACTION]
    assert b"h2l_external" in build.files[ACTION]


GENERATORS: list[tuple[str, dict[str, JSONValue]]] = [
    ("integer", {"minimum": -(2**63), "maximum": 2**63 - 1}),
    ("string", {"length": 4096, "alphabet": "ab"}), ("uuid", {}),
    ("utc_datetime", {"offset_seconds": -315360000, "format": "iso8601"}),
    ("utc_date_range", {"minimum": "0001-01-01", "maximum": "9999-12-31", "format": "iso8601"}),
]


@pytest.mark.parametrize("generator,options", GENERATORS)
def test_generators_are_emitted_not_evaluated(
    generator: str, options: dict[str, JSONValue],
) -> None:
    source = {"kind": "generated", "generator": generator, **options}
    value = source_plan(source, "integer" if generator == "integer" else "string")
    with patch("builtins.open", side_effect=AssertionError("file read")), \
            patch("os.getenv", side_effect=AssertionError("environment read")), \
            patch("time.time", side_effect=AssertionError("live clock")):
        build = LoadRunnerGenerator().generate(value)
    assert build.status == "generated"
    assert build == LoadRunnerGenerator().generate(value)
    assert b"lr_whoami(&id, NULL, NULL)" in build.files[ACTION]
    assert b"rand(" not in build.files[ACTION]
    assert b"lr_error_message(" not in build.files[ACTION]


def test_shared_key_has_one_definition_and_snapshot() -> None:
    value = source_plan(external(initial_value="seed"))
    assert value.runtime is not None
    other = variable(id="other", name="other", source=external(),
                     initialize={"event": "session_start"}, scope="session")
    variables = (*value.runtime.variables, other)
    value = replace(value, runtime=replace(value.runtime, variables=variables))
    build = LoadRunnerGenerator().generate(value)
    assert build.status == "generated"
    assert build.files["loadrunner/native_inputs.prm"].count(b"[parameter:") == 1
    assert build.files[ACTION].count(b'"{token}"') == 1


def test_native_capture_reserved_key_is_blocked() -> None:
    value = source_plan(external(key="H2L_RESPONSE", initial_value="x"))
    build = LoadRunnerGenerator().generate(value)
    assert build.status == "blocked"


def test_parameter_fragment_and_table_parse_structurally_only() -> None:
    build = LoadRunnerGenerator().generate(source_plan(external(initial_value="ASCII seed")))
    fragment = configparser.ConfigParser(interpolation=None)
    fragment.read_string(build.files["loadrunner/native_inputs.prm"].decode("ascii"))
    table = fragment["parameter:token"]["Table"].strip('"')
    rows = list(csv.reader(build.files["loadrunner/" + table].decode("ascii").splitlines()))
    assert rows == [["Value"], ["ASCII seed"]]
    assert build.metadata["native_validation"] == "not_run"


@pytest.mark.parametrize("key", ["bad-key", "bad.name", 'bad"key', "a/b", "x\nType=Random"])
def test_unverified_or_injected_parameter_keys_block(key: str) -> None:
    build = LoadRunnerGenerator().generate(source_plan(external(key=key, initial_value="x")))
    assert build.status == "blocked" and not build.files


def test_budget_accounts_for_native_assets_atomically() -> None:
    value = source_plan(external(initial_value="private"))
    expected = LoadRunnerGenerator().generate(value)
    size = sum(map(len, expected.files.values()))
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", size):
        assert LoadRunnerGenerator().generate(value) == expected
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", size - 1):
        blocked = LoadRunnerGenerator().generate(value)
    assert blocked.status == "blocked" and not blocked.files and not blocked.source_map


def test_initialize_maps_remain_real_c_functions_and_public_metadata_is_value_free() -> None:
    value = source_plan(external(key="PRIVATE_KEY", initial_value="PRIVATE_SEED"))
    build = LoadRunnerGenerator().generate(value)
    for row in build.source_map:
        assert row["artifact"] == ACTION
        assert build.files[ACTION].count(str(row["marker"]).encode()) == 1
    assert "PRIVATE_KEY" not in str(build.metadata) and "PRIVATE_SEED" not in str(build.metadata)
    assert build.metadata["profile_version"] == "3"


def test_rejection_budget_is_explicit_and_no_native_status_override() -> None:
    value = source_plan({"kind": "generated", "generator": "uuid"})
    source = LoadRunnerGenerator().generate(value).files[ACTION]
    assert b"#define H2L_RANDOM_ATTEMPTS 128" in source
    assert b"attempt < H2L_RANDOM_ATTEMPTS" in source
    for forbidden in (b"lr_exit(", b"lr_error_message(", b"LR_FAIL", b"lr_save_string("):
        assert forbidden not in source
