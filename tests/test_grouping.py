"""Synthetic contracts for deterministic grouping; no imported traffic fixtures."""

import math
from typing import cast

import pytest

from har2load.domain import Capture, Diagnostic, Exchange, Har2LoadError, JSONValue
from har2load.grouping import StepGrouper


def exchange(
    index: int,
    start: float | None,
    end: float | None,
    comment: JSONValue = None,
) -> Exchange:
    return Exchange(f"ex_{index}", index, {"comment": comment}, start_time=start, end_time=end)


def capture(*exchanges: Exchange, diagnostics: tuple[Diagnostic, ...] = ()) -> Capture:
    return Capture("capture_synthetic", "synthetic", b"{}", exchanges, diagnostics)


def test_empty_capture() -> None:
    result = StepGrouper().group(capture())
    assert result.groups == result.ordered_exchange_ids == result.diagnostics == ()
    assert result.step_gap_seconds == 5.0


@pytest.mark.parametrize("gap,count", [(4.999, 1), (5.0, 2), (5.001, 2)])
def test_idle_gap_threshold_is_inclusive(gap: float, count: int) -> None:
    result = StepGrouper().group(capture(exchange(0, 0, 1), exchange(1, 1 + gap, 8)))
    assert len(result.groups) == count
    assert result.groups[0].boundary_reason == "capture_start"
    if count == 2:
        assert result.groups[1].boundary_reason == "idle_gap"
        assert result.groups[1].name == "Step_002"


def test_same_annotation_ignores_pause_and_preserves_raw_name() -> None:
    result = StepGrouper().group(capture(exchange(0, 0, 1, " A "), exchange(1, 90, 91, " A ")))
    assert len(result.groups) == 1
    assert result.groups[0].name == " A "
    assert result.groups[0].origin == "annotation"
    assert result.groups[0].exchange_ids == ("ex_0", "ex_1")


def test_repeated_annotation_creates_distinct_stable_groups() -> None:
    entries = [exchange(i, i, i + 1, label) for i, label in enumerate(("A", "A", "B", "A"))]
    result = StepGrouper().group(capture(*entries))
    assert [group.name for group in result.groups] == ["A", "B", "A"]
    assert [group.id for group in result.groups] == ["grp_ex_0", "grp_ex_2", "grp_ex_3"]
    assert [group.boundary_reason for group in result.groups[1:]] == ["annotation_change"] * 2


def test_annotations_are_exact_not_normalized() -> None:
    result = StepGrouper().group(capture(exchange(0, 0, 1, "A"), exchange(1, 1, 2, " A ")))
    assert [group.name for group in result.groups] == ["A", " A "]


def test_unannotated_runs_do_not_inherit_labels() -> None:
    entries = [exchange(i, i, i + 1, label) for i, label in enumerate((None, "A", None, "A"))]
    result = StepGrouper().group(capture(*entries))
    assert [group.name for group in result.groups] == ["Step_001", "A", "Step_003", "A"]
    assert [group.origin for group in result.groups] == [
        "timing_heuristic", "annotation", "timing_heuristic", "annotation"
    ]
    assert [group.boundary_reason for group in result.groups[1:]] == [
        "annotation_start", "annotation_end", "annotation_start"
    ]


@pytest.mark.parametrize("comment", [None, "", " \t\n", 3, True, [], {}, ["A"]])
def test_malformed_or_blank_comments_are_unannotated(comment: JSONValue) -> None:
    result = StepGrouper().group(capture(exchange(0, 0, 1, comment), exchange(1, 1, 2)))
    assert len(result.groups) == 1
    assert result.groups[0].name == "Step_001"


@pytest.mark.parametrize("raw", [None, [], "not an entry", 42])
def test_non_object_raw_entry_does_not_crash(raw: JSONValue) -> None:
    result = StepGrouper().group(capture(Exchange("ex_0", 0, raw, start_time=0, end_time=1)))
    assert result.groups[0].name == "Step_001"


def test_ordering_uses_timestamp_then_source_index() -> None:
    result = StepGrouper().group(capture(exchange(2, 0, 1), exchange(0, 2, 3), exchange(1, 0, 1)))
    assert result.ordered_exchange_ids == ("ex_1", "ex_2", "ex_0")
    assert result.groups[0].id == "grp_ex_1"


def test_running_maximum_end_prevents_false_gap() -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, 100), exchange(1, 1, 2), exchange(2, 20, 21), exchange(3, 105, 106))
    )
    assert [group.exchange_ids for group in result.groups] == [("ex_0", "ex_1", "ex_2"), ("ex_3",)]
    overlaps = [item for item in result.diagnostics if item.code == "grouping_overlap"]
    assert [item.entity_id for item in overlaps] == ["ex_1", "ex_2"]
    assert [item.location for item in overlaps] == ["log.entries[1]", "log.entries[2]"]


def test_running_maximum_includes_earlier_annotated_groups() -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, 100, "A"), exchange(1, 1, 2), exchange(2, 20, 21))
    )
    assert [group.exchange_ids for group in result.groups] == [("ex_0",), ("ex_1", "ex_2")]


@pytest.mark.parametrize("end", [None, math.nan, math.inf, -math.inf, -1.0])
def test_unknown_prior_end_permanently_blocks_inferred_gap(end: float | None) -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, end), exchange(1, 100, 101), exchange(2, 200, 201))
    )
    assert len(result.groups) == 1
    assert any(item.code == "grouping_timing_unavailable" for item in result.diagnostics)


def test_unknown_end_remains_unknown_across_annotation_boundary() -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, None, "A"), exchange(1, 100, 101), exchange(2, 200, 201))
    )
    assert [group.exchange_ids for group in result.groups] == [("ex_0",), ("ex_1", "ex_2")]


@pytest.mark.parametrize("start", [None, math.nan, math.inf, -math.inf])
def test_invalid_start_falls_back_to_source_index_without_gaps(start: float | None) -> None:
    result = StepGrouper().group(
        capture(exchange(2, start, 201), exchange(1, 100, 101), exchange(0, 0, 1))
    )
    assert result.ordered_exchange_ids == ("ex_0", "ex_1", "ex_2")
    assert len(result.groups) == 1
    fallback = [item for item in result.diagnostics if item.code == "grouping_order_fallback"]
    assert [(item.entity_id, item.location) for item in fallback] == [("ex_2", "log.entries[2]")]


def test_fallback_with_backwards_timestamps_never_infers_gap() -> None:
    result = StepGrouper().group(
        capture(
            exchange(0, 100, 101), exchange(1, 0, 1),
            exchange(2, 200, 201), exchange(3, None, None),
        )
    )
    assert len(result.groups) == 1
    assert not any(item.code == "grouping_overlap" for item in result.diagnostics)


def test_explicit_annotations_still_apply_with_unknown_timing() -> None:
    result = StepGrouper().group(
        capture(exchange(0, None, None, "A"), exchange(1, None, None, "B"), exchange(2, None, None))
    )
    assert [group.name for group in result.groups] == ["A", "B", "Step_003"]


def test_current_unknown_end_does_not_hide_proven_prior_gap() -> None:
    result = StepGrouper().group(capture(exchange(0, 0, 1), exchange(1, 6, None)))
    assert len(result.groups) == 2


def test_zero_threshold_splits_touching_but_not_overlapping_intervals() -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, 2), exchange(1, 1, 2), exchange(2, 2, 3)), step_gap_seconds=0
    )
    assert [group.exchange_ids for group in result.groups] == [("ex_0", "ex_1"), ("ex_2",)]


@pytest.mark.parametrize(
    "threshold", [-1, math.nan, math.inf, -math.inf, True, False, None, "secret", 10**1000]
)
def test_invalid_threshold_raises_safe_domain_error(threshold: object) -> None:
    with pytest.raises(Har2LoadError) as caught:
        StepGrouper().group(capture(), cast(float, threshold))
    assert caught.value.code == "invalid_step_gap"
    assert str(caught.value) == "Step gap must be a finite nonnegative number."


def test_capture_diagnostics_not_duplicated_and_raw_labels_not_in_diagnostics() -> None:
    diagnostic = Diagnostic("capture_test", "warning", "Existing diagnostic")
    source = capture(exchange(0, None, None, "private annotation"), diagnostics=(diagnostic,))
    result = StepGrouper().group(source)
    assert diagnostic not in result.diagnostics
    assert all("private annotation" not in item.message for item in result.diagnostics)
    assert result.diagnostics
    assert all(
        item.entity_id == "ex_0" and item.location == "log.entries[0]"
        for item in result.diagnostics
    )


def test_grouping_is_deterministic_and_does_not_mutate_source() -> None:
    source = capture(exchange(1, 6, 7), exchange(0, 0, 1))
    grouper = StepGrouper()
    first = grouper.group(source)
    assert first == grouper.group(source)
    assert source.exchanges[0].id == "ex_1"
    flattened = tuple(item for group in first.groups for item in group.exchange_ids)
    assert flattened == first.ordered_exchange_ids


def test_ids_are_independent_of_threshold_and_sequential_display_name() -> None:
    source = capture(exchange(0, 0, 1), exchange(1, 6, 7), exchange(2, 8, 9, "A"))
    narrow = StepGrouper().group(source, 5)
    wide = StepGrouper().group(source, 10)
    assert narrow.groups[-1].id == wide.groups[-1].id == "grp_ex_2"
    assert len(narrow.groups) == 3
    assert len(wide.groups) == 2


def test_missing_comment_key_is_unannotated() -> None:
    entry = Exchange("ex_0", 0, {}, start_time=0, end_time=1)
    result = StepGrouper().group(capture(entry))
    assert result.groups[0].name == "Step_001"


def test_missing_end_does_not_prevent_timestamp_sorting() -> None:
    result = StepGrouper().group(capture(exchange(0, 10, None), exchange(1, 0, 1)))
    assert result.ordered_exchange_ids == ("ex_1", "ex_0")
    assert not any(item.code == "grouping_order_fallback" for item in result.diagnostics)


def test_overlaps_are_reported_even_in_fallback_order() -> None:
    result = StepGrouper().group(
        capture(exchange(0, 10, 20), exchange(1, 0, 100), exchange(2, None, None))
    )
    overlaps = [item for item in result.diagnostics if item.code == "grouping_overlap"]
    assert [item.entity_id for item in overlaps] == ["ex_0"]
    assert result.ordered_exchange_ids == ("ex_0", "ex_1", "ex_2")


@pytest.mark.parametrize("value", [True, False, "private timing", [], 10**1000])
def test_runtime_invalid_start_values_are_conservative(value: object) -> None:
    result = StepGrouper().group(
        capture(exchange(0, cast(float, value), 1), exchange(1, 100, 101))
    )
    assert len(result.groups) == 1
    assert any(item.code == "grouping_order_fallback" for item in result.diagnostics)


@pytest.mark.parametrize("value", [True, False, "private timing", [], 10**1000])
def test_runtime_invalid_end_values_are_conservative(value: object) -> None:
    result = StepGrouper().group(
        capture(exchange(0, 0, cast(float, value)), exchange(1, 100, 101))
    )
    assert len(result.groups) == 1
    assert any(item.code == "grouping_timing_unavailable" for item in result.diagnostics)
