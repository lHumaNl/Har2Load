"""Deterministic grouping, without filtering, timers or business-step inference.

Annotations are opaque labels. Timing boundaries require trustworthy ordering and
a completely known prior interval prefix, including earlier annotated groups.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from har2load.domain import Capture, Diagnostic, Exchange, Grouping, Har2LoadError, StepGroup
from har2load.precision import exact_sum, timing_number


def _threshold(value: float) -> Decimal:
    number = timing_number(value)
    if number is None or number < 0:
        raise Har2LoadError("invalid_step_gap", "Step gap must be a finite nonnegative number.")
    return number


@dataclass(frozen=True)
class _TimedExchange:
    exchange: Exchange
    start: Decimal | None
    end: Decimal | None


def _timed(exchange: Exchange) -> _TimedExchange:
    start = timing_number(exchange.start_time)
    end = timing_number(exchange.end_time)
    if start is None or end is None or end < start:
        end = None
    return _TimedExchange(exchange, start, end)


def _annotation(exchange: Exchange) -> str | None:
    comment = exchange.raw.get("comment") if isinstance(exchange.raw, dict) else None
    return comment if isinstance(comment, str) and comment.strip() else None


def _diagnostic(entry: _TimedExchange, code: str, message: str) -> Diagnostic:
    return Diagnostic(
        code, "warning", message,
        entity_id=entry.exchange.id, location=f"log.entries[{entry.exchange.index}]",
    )


def _timing_diagnostics(entries: list[_TimedExchange]) -> list[Diagnostic]:
    diagnostics = []
    for entry in entries:
        if entry.start is None:
            diagnostics.append(_diagnostic(
                entry, "grouping_order_fallback",
                "Start time is invalid or unavailable; using source order without timing gaps.",
            ))
        if entry.end is None:
            diagnostics.append(_diagnostic(
                entry, "grouping_timing_unavailable",
                "Interval is unavailable or invalid; later idle gaps cannot be proven.",
            ))
    return diagnostics


def _overlap_diagnostics(entries: list[_TimedExchange]) -> list[Diagnostic]:
    # Sweep valid intervals separately so fallback source order cannot fabricate overlaps.
    intervals = sorted(
        ((entry.start, entry.exchange.index, entry) for entry in entries
         if entry.start is not None and entry.end is not None),
        key=lambda item: (item[0], item[1]),
    )
    diagnostics = []
    max_end: Decimal | None = None
    for start, _, entry in intervals:
        assert entry.end is not None
        if max_end is not None and start < max_end:
            diagnostics.append(_diagnostic(
                entry, "grouping_overlap",
                "Request intervals overlap; sequential grouping does not reconstruct concurrency.",
            ))
        max_end = entry.end if max_end is None else max(max_end, entry.end)
    return diagnostics


@dataclass
class _TimingPrefix:
    trustworthy_order: bool
    max_end: Decimal | None = None
    unknown_end: bool = False

    def has_gap(self, entry: _TimedExchange, threshold: Decimal) -> bool:
        if not self.trustworthy_order or self.unknown_end:
            return False
        if entry.start is None or self.max_end is None:
            return False
        return entry.start >= exact_sum(self.max_end, threshold)

    def observe(self, entry: _TimedExchange) -> None:
        # Unknown intervals poison the entire prefix, not only the current group.
        if entry.end is None:
            self.unknown_end = True
        else:
            self.max_end = entry.end if self.max_end is None else max(self.max_end, entry.end)


@dataclass
class _PendingGroup:
    annotation: str | None
    boundary_reason: str
    exchange_ids: list[str] = field(default_factory=list)

    def freeze(self, ordinal: int) -> StepGroup:
        return StepGroup(
            id=f"grp_{self.exchange_ids[0]}",
            exchange_ids=tuple(self.exchange_ids),
            name=self.annotation if self.annotation is not None else f"Step_{ordinal:03d}",
            origin="annotation" if self.annotation is not None else "timing_heuristic",
            boundary_reason=self.boundary_reason,
        )


def _boundary(groups: list[_PendingGroup], annotation: str | None, gap: bool) -> str | None:
    if not groups:
        return "capture_start"
    previous = groups[-1].annotation
    if annotation != previous:
        if annotation is None:
            return "annotation_end"
        return "annotation_start" if previous is None else "annotation_change"
    return "idle_gap" if annotation is None and gap else None


def _build_groups(
    entries: list[_TimedExchange], threshold: Decimal, trustworthy_order: bool,
) -> tuple[StepGroup, ...]:
    groups: list[_PendingGroup] = []
    prefix = _TimingPrefix(trustworthy_order)
    for entry in entries:
        annotation = _annotation(entry.exchange)
        reason = _boundary(groups, annotation, prefix.has_gap(entry, threshold))
        if reason is not None:
            groups.append(_PendingGroup(annotation, reason))
        groups[-1].exchange_ids.append(entry.exchange.id)
        prefix.observe(entry)
    return tuple(group.freeze(ordinal) for ordinal, group in enumerate(groups, start=1))


class StepGrouper:
    """Group capture exchanges without filtering or inferring business semantics."""

    def group(self, capture: Capture, step_gap_seconds: float = 5.0) -> Grouping:
        threshold = _threshold(step_gap_seconds)
        entries = [_timed(exchange) for exchange in capture.exchanges]
        trustworthy_order = all(entry.start is not None for entry in entries)
        entries.sort(key=lambda entry: (
            entry.start if trustworthy_order and entry.start is not None else 0,
            entry.exchange.index,
        ))
        diagnostics = _timing_diagnostics(entries) + _overlap_diagnostics(entries)
        return Grouping(
            groups=_build_groups(entries, threshold, trustworthy_order),
            ordered_exchange_ids=tuple(entry.exchange.id for entry in entries),
            step_gap_seconds=float(threshold),
            diagnostics=tuple(diagnostics),
        )
