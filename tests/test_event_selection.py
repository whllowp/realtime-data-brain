"""Event selection: diagnostic value first, recency only to fill the budget.

Round 3 cut the event context from 20 to 4 by recency and saved 36.8% of prompt tokens. Round 4 measured
what that cost: 23% of the log lines kept, 38% of the distinct signatures, and the *first* error of the
window gone entirely. Recency was the wrong rule.

Selecting by diagnostic value instead keeps 100% of the evidence at 8 events, for 31.1% of the tokens.
These tests pin the rule so it cannot silently revert to "last N".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brian.contracts import Event
from brian.llm.prompts import select_events

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(offset: int, kind: str, **payload) -> Event:
    return Event(event_time=BASE + timedelta(seconds=offset), service="svc", kind=kind,
                 payload=payload)


def test_a_budget_larger_than_the_window_changes_nothing() -> None:
    events = [_event(i, "metric", cpu=float(i)) for i in range(5)]
    assert select_events(events, 20) == events
    assert select_events(events, 0) == []


def test_the_first_sighting_of_an_error_survives_a_tight_budget() -> None:
    """The first sighting is what separates "started now" from "has been going on"."""
    events = [_event(0, "log", error="pool exhausted")] + \
             [_event(i, "metric", cpu=1.0) for i in range(1, 10)]
    selected = select_events(events, 2)
    assert any(e.kind == "log" for e in selected)


def test_context_markers_outrank_everything() -> None:
    """A deploy reframes every other measurement in the window, so it cannot be crowded out by metrics."""
    events = [_event(0, "log", deploy="v2.14.0")] + \
             [_event(i, "metric", cpu=1.0) for i in range(1, 20)]
    selected = select_events(events, 1)
    assert len(selected) == 1
    assert selected[0].payload.get("deploy") == "v2.14.0"


def test_distinct_signatures_come_before_repeats() -> None:
    """Two different errors are worth more than the same error twice."""
    events = [
        _event(0, "log", error="timeout acquiring connection"),
        _event(1, "log", error="timeout acquiring connection"),
        _event(2, "log", error="schema mismatch on field id"),
    ] + [_event(10 + i, "metric", cpu=1.0) for i in range(5)]
    selected = select_events(events, 2)
    signatures = {e.payload.get("error") for e in selected if e.kind == "log"}
    assert len(signatures) == 2


def test_output_stays_chronological() -> None:
    """Selection decides which events survive; the rendered list must still read in time order."""
    events = [_event(i, "log", error="e%d" % i) for i in range(6)]
    selected = select_events(events, 3)
    stamps = [e.event_time for e in selected]
    assert stamps == sorted(stamps)
