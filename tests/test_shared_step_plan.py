"""Only a pinned, explicitly decided plan can request shared JMeter steps."""

import hashlib
import json

import pytest

from har2load.domain import Har2LoadError
from har2load.shared_steps import read_shared_plan

PINS = {"TF_A": "1" * 64, "TF_B": "2" * 64}


def document():
    return {"schema_version": "1.0", "blocks": [{
        "name": "TF_Browse", "decision": "high_confidence", "reason": "Same catalog traversal.",
        "uses": [{"uc": uc, "manifest_sha256": pin, "groups": ["first", "second"]}
                 for uc, pin in PINS.items()]}]}


def test_explicit_plan_preserves_original_identity_and_order(tmp_path):
    path = tmp_path / "sharing.json"
    doc = document()
    path.write_text(json.dumps(doc, indent=2))
    parsed = read_shared_plan(path, PINS)
    assert parsed.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert parsed.blocks[0].uses[1].groups == ("first", "second")
    assert parsed.document == doc


@pytest.mark.parametrize("change", ["unknown", "ambiguous", "blank", "surrogate", "stale",
                                     "one_uc", "unknown_uc", "case_collision", "overlap",
                                     "repeat_uc", "repeat_group", "large_range", "nested",
                                     "empty_plan", "block_limit", "bad_version"])
def test_rejects_undecided_unpinned_or_invalid_ranges(tmp_path, change):
    doc = document()
    b = doc["blocks"][0]
    u = b["uses"][0]
    if change == "unknown":
        b["auto"] = True
    elif change == "ambiguous":
        b["decision"] = "needs_clarification"
    elif change == "blank":
        b["reason"] = "  "
    elif change == "surrogate":
        b["reason"] = "\ud800"
    elif change == "stale":
        u["manifest_sha256"] = "3" * 64
    elif change == "one_uc":
        b["uses"].pop()
    elif change == "unknown_uc":
        u["uc"] = "TF_C"
    elif change == "case_collision":
        b["name"] = "TF_a"
    elif change == "overlap":
        doc["blocks"].append({**b, "name": "TF_Overlap"})
    elif change == "repeat_uc":
        b["uses"][1] = {**u, "groups": ["unrelated_group"]}
    elif change == "repeat_group":
        u["groups"] *= 2
    elif change == "large_range":
        u["groups"] = [f"g{i}" for i in range(129)]
    elif change == "nested":
        u["groups"] = [["PRIVATE"]]
    elif change == "empty_plan":
        doc["blocks"] = []
    elif change == "block_limit":
        doc["blocks"] *= 33
    else:
        doc["schema_version"] = 1
    path = tmp_path / "PRIVATE"
    path.write_text(json.dumps(doc))
    with pytest.raises(Har2LoadError) as exc:
        read_shared_plan(path, PINS)
    assert exc.value.code.startswith("COMPOSE_SHARED_") and "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("content", [
    json.dumps(document()).replace('"schema_version":', '"schema_version":"1.0","schema_version":',
                                   1).encode(), b"x" * 262145, b"\xff"])
def test_rejects_invalid_file(tmp_path, content):
    path = tmp_path / "PRIVATE"
    path.write_bytes(content)
    with pytest.raises(Har2LoadError) as exc:
        read_shared_plan(path, PINS)
    assert exc.value.code == "COMPOSE_SHARED_PLAN_INVALID" and "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("control", ["\x7f", "\x85", "\u2028", "\u2029"])
def test_reason_must_be_a_single_line_without_controls(tmp_path, control):
    doc = document()
    doc["blocks"][0]["reason"] = "valid" + control + "text"
    path = tmp_path / "plan"
    path.write_text(json.dumps(doc))
    with pytest.raises(Har2LoadError):
        read_shared_plan(path, PINS)
