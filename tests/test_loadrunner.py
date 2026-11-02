"""Synthetic, offline checks of intermediate C source, not native compatibility."""

import ast
import builtins
import re
from dataclasses import replace
from unittest.mock import patch

import pytest

from har2load.domain import Diagnostic
from har2load.generation_types import NativeGroup, NativeRequest, RequestPlan
from har2load.loadrunner import LoadRunnerGenerator

ACTION = "loadrunner/Action.c"
FILES = {ACTION, "loadrunner/vuser_init.c", "loadrunner/vuser_end.c",
         "loadrunner/globals.h", "loadrunner/README.txt"}


def request(**changes: object) -> NativeRequest:
    return replace(NativeRequest("exchange-1", "group-1", "POST", "https://example.invalid/"),
                   **changes)  # type: ignore[arg-type]


def plan(*requests: NativeRequest) -> RequestPlan:
    items = requests or (request(),)
    return RequestPlan(items, (NativeGroup("group-1", "Synthetic", tuple(r.id for r in items)),))


def action(value: RequestPlan) -> str:
    return LoadRunnerGenerator().generate(value).files[ACTION].decode("ascii")


@pytest.mark.parametrize("body", [b"", b"{name}\\n\\x41%\"\n\x00", "caf\u00e9 \u2603".encode(),
                                 bytes(range(256))])
def test_raw_body_round_trips_without_native_parameterization(body: bytes) -> None:
    source = action(plan(request(body=body, body_kind="text")))
    match = re.search(r"static unsigned char (\w+)\[\] = \{(.*?)\};", source, re.S)
    assert match is not None
    decoded = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-f]{2})", match[2]))
    assert decoded == (body or b"\x00")
    assert f"RAW_BODY_START, (char*){match[1]}, (int){len(body)}, RAW_BODY_END" in source
    assert "Body=" not in source and "BodyBinary=" not in source
    assert max(map(len, match[2].splitlines())) <= 104


def test_missing_body_omits_raw_segment() -> None:
    source = action(plan())
    assert "RAW_BODY_START" not in source
    assert "static unsigned char" not in source


def test_headers_are_request_scoped_and_content_type_uses_enctype() -> None:
    first = request(headers=(("cOnTeNt-TyPe", "application/json"), ("X-First", "one")))
    second = request(id="exchange-2", headers=(("X-Second", "two"),))
    source = action(plan(first, second))
    assert '"EncType=application/json"' in source and '"EncType="' in source
    assert "RecContentType" not in source and "web_add_auto_header" not in source
    assert "cOnTeNt-TyPe" not in source
    assert source.index('web_add_header("X-First"') < source.index('"REQ_0001"')
    assert source.index('"REQ_0001"') < source.index('web_add_header("X-Second"')
    assert source.index('web_add_header("X-Second"') < source.index('"REQ_0002"')


@pytest.mark.parametrize("headers", [(("X-A", "one"), ("x-a", "two")),
                                     (("Content-Type", "a"), ("content-type", "a"))])
def test_duplicate_headers_block_entire_target(headers: tuple[tuple[str, str], ...]) -> None:
    result = LoadRunnerGenerator().generate(plan(request(headers=headers)))
    assert result.status == "blocked" and result.files == {} and result.source_map == ()
    assert any(d.code == "LOADRUNNER_DUPLICATE_HEADER_UNSUPPORTED" for d in result.diagnostics)


@pytest.mark.parametrize("changes", [{"url": "https://example.invalid/{name}"},
                                    {"url": "https://example.invalid/}"},
                                    {"headers": (("X-{Name}", "v"),)},
                                    {"headers": (("X-Name", "{v}"),)},
                                    {"headers": (("Content-Type", "{mime}"),)}])
def test_target_metasyntax_is_rejected(changes: dict[str, object]) -> None:
    result = LoadRunnerGenerator().generate(plan(request(**changes)))
    assert result.status == "blocked" and not result.files
    assert any(d.code == "LOADRUNNER_METASYNTAX_UNSUPPORTED" for d in result.diagnostics)
    assert all("{name}" not in d.message for d in result.diagnostics)


def test_c_string_escaping_preserves_bytes_and_disables_trigraphs() -> None:
    value = 'quote" slash\\ percent% ??/ octal7'
    source = action(plan(request(headers=(("X-Test", value),))))
    match = re.search(r'web_add_header\("X-Test", ("(?:\\.|[^"\\])*")\)', source)
    assert match is not None
    assert ast.literal_eval(match[1]) == value
    assert "??/" not in source


def test_untrusted_labels_and_ids_never_become_c_source() -> None:
    hostile = '*/\n#error INJECTED\n/*'
    item = request(id=hostile, group_id=hostile)
    value = RequestPlan((item,), (NativeGroup(hostile, hostile, (hostile,)),))
    result = LoadRunnerGenerator().generate(value)
    assert b"INJECTED" not in result.files[ACTION]
    assert all(re.fullmatch(r"H2L_(REQUEST|GROUP)_[0-9a-f]{64}", str(m["marker"]))
               for m in result.source_map)
    assert {m["entity_id"] for m in result.source_map} == {hostile}


def test_maps_cover_every_request_and_group_with_unique_stable_markers() -> None:
    first, second = request(), request(id="exchange-2", group_id="group-2")
    groups = (NativeGroup("group-1", "One", (first.id,)),
              NativeGroup("group-2", "Two", (second.id,)))
    result = LoadRunnerGenerator().generate(RequestPlan((first, second), groups))
    expected_ids = {first.id, second.id, *(g.id for g in groups)}
    assert {m["entity_id"] for m in result.source_map} == expected_ids
    assert len(result.source_map) == 4
    for mapping in result.source_map:
        assert mapping["artifact"] == ACTION
        assert result.files[ACTION].count(str(mapping["marker"]).encode()) == 1
    assert result.files[ACTION].count(b"web_custom_request(") == 2
    assert result.files[ACTION].count(b"lr_start_transaction(") == 2
    assert result.files[ACTION].count(b"LR_AUTO") == 2


def test_debug_controls_have_no_extra_requests_or_iteration_abort() -> None:
    result = LoadRunnerGenerator().generate(plan())
    init = result.files["loadrunner/vuser_init.c"].decode()
    source = result.files[ACTION].decode()
    assert 'web_set_option("DownloadNonHtmlResources", "No", LAST)' in init
    assert 'web_set_option("MaxRedirectionDepth", "0", LAST)' in init
    assert "lr_continue_on_error(LR_ON_ERROR_CONTINUE)" in init
    assert source.index("web_cleanup_cookies()") < source.index("web_custom_request(")
    assert source.index("web_cache_cleanup()") < source.index("web_custom_request(")
    assert '"Resource=0"' in source and '"Mode=HTTP"' in source
    assert all(token not in source for token in ("EXTRARES", "Snapshot=", "lr_abort", "lr_exit"))


def test_artifacts_explicitly_are_incomplete_and_unverified() -> None:
    result = LoadRunnerGenerator().generate(plan())
    assert result.status == "generated" and set(result.files) == FILES
    for key, expected in {"artifact_kind": "c_source_scaffold", "project_status": "incomplete",
                          "compatibility": "unverified", "native_validation": "not_run",
                          "execution": "not_run", "profile": "static_literals_v1",
                          "template_status": "pending"}.items():
        assert result.metadata[key] == expected
    readme = result.files["loadrunner/README.txt"].decode()
    assert "not a complete VuGen project" in readme
    assert "Do not assume native compilation" in readme and "Do not start load" in readme
    assert "unchecked" in readme and "25.1-25.3" in readme
    assert '#include "lrun.h"' in result.files["loadrunner/globals.h"].decode()
    assert '#include "web_api.h"' in result.files["loadrunner/globals.h"].decode()


def test_common_error_blocks_all_files_and_preserves_diagnostic() -> None:
    diagnostic = Diagnostic("COMMON_FAILURE", "error", "Synthetic failure")
    result = LoadRunnerGenerator().generate(replace(plan(), diagnostics=(diagnostic,)))
    assert result.status == "blocked" and not result.files and not result.source_map
    assert diagnostic in result.diagnostics


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "mismatch", "reordered"])
def test_inconsistent_membership_never_drops_or_reorders_requests(mutation: str) -> None:
    first, second = request(), request(id="exchange-2")
    ids = {"missing": (first.id,), "duplicate": (first.id, first.id),
           "mismatch": (first.id, "absent"), "reordered": (second.id, first.id)}[mutation]
    result = LoadRunnerGenerator().generate(RequestPlan((first, second),
                                            (NativeGroup("group-1", "One", ids),)))
    assert result.status == "blocked" and not result.files


def test_budget_is_global_and_exact_boundary_is_inclusive() -> None:
    value = plan(request(body=b"synthetic", body_kind="text"))
    result = LoadRunnerGenerator().generate(value)
    total = sum(map(len, result.files.values()))
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total):
        assert LoadRunnerGenerator().generate(value).files == result.files
    with patch("har2load.loadrunner.MAX_GENERATED_BYTES", total - 1):
        blocked = LoadRunnerGenerator().generate(value)
    assert blocked.status == "blocked" and not blocked.files and not blocked.source_map
    assert any(d.code == "LOADRUNNER_OUTPUT_LIMIT" for d in blocked.diagnostics)


def test_body_budget_is_checked_before_array_expansion() -> None:
    with (patch("har2load.loadrunner.MAX_GENERATED_BYTES", 64),
          patch("har2load.loadrunner._body_array", side_effect=AssertionError("Expanded body"))):
        result = LoadRunnerGenerator().generate(plan(request(body=b"x" * 64, body_kind="text")))
    assert result.status == "blocked" and result.files == {}


def test_text_budget_is_checked_before_literal_expansion() -> None:
    with (patch("har2load.loadrunner.MAX_GENERATED_BYTES", 64),
          patch("har2load.loadrunner._literal", side_effect=AssertionError("Expanded text"))):
        result = LoadRunnerGenerator().generate(plan(request(url="https://example.invalid/" * 64)))
    assert result.status == "blocked" and result.files == {}


@pytest.mark.parametrize("value", ["a\x00b", "\ud800", "\udfff"])
def test_nul_and_surrogates_in_native_strings_are_rejected(value: str) -> None:
    result = LoadRunnerGenerator().generate(plan(request(headers=(("X-Test", value),))))
    assert result.status == "blocked" and not result.files
    assert any(d.code == "LOADRUNNER_TEXT_UNSUPPORTED" for d in result.diagnostics)


def test_utf8_c_escaping_uses_unambiguous_byte_octal() -> None:
    value = "caf\u00e97\t\u2603"
    source = action(plan(request(headers=(("X-Test", value),))))
    match = re.search(r'web_add_header\("X-Test", ("(?:\\.|[^"\\])*")\)', source)
    assert match is not None
    assert ast.literal_eval(match[1]).encode("latin-1") == value.encode("utf-8")
    assert "\\303\\2517" in match[1]


def test_target_error_in_later_request_blocks_all_requests() -> None:
    result = LoadRunnerGenerator().generate(plan(request(), request(id="exchange-2", url="{url}")))
    assert result.status == "blocked" and not result.files and not result.source_map


def test_group_ownership_mismatch_is_rejected() -> None:
    result = LoadRunnerGenerator().generate(plan(request(group_id="other-group")))
    assert result.status == "blocked" and not result.files


def test_common_warning_is_preserved_without_blocking() -> None:
    warning = Diagnostic("SYNTHETIC_WARNING", "warning", "Synthetic warning")
    result = LoadRunnerGenerator().generate(replace(plan(), diagnostics=(warning,)))
    assert result.status == "generated" and warning in result.diagnostics


def test_generation_is_deterministic_and_has_no_io_or_environment_reads() -> None:
    value = plan(request(body=b"{literal}\x00", body_kind="text"))
    expected = LoadRunnerGenerator().generate(value)
    denied = AssertionError("Unexpected side effect")
    with (patch.object(builtins, "open", side_effect=denied),
          patch("os.getenv", side_effect=denied), patch("os.environ", {}),
          patch("socket.socket", side_effect=denied),
          patch("subprocess.Popen", side_effect=denied)):
        assert LoadRunnerGenerator().generate(value) == expected
    assert value.requests[0].body == b"{literal}\x00"
