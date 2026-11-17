"""The stream file is the boundary where a real verification signal enters the loop.

These tests pin the two properties that make the boundary worth having: the file survives a
round trip without losing a label or an event, and a file this build cannot read is rejected
loudly instead of being measured as an empty dataset. A silently empty stream would report
"no candidate helps", which is indistinguishable from a real negative result.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brian.contracts import Event
from brian.demo import LabeledWindow
from brian.streams import (
    FORMAT,
    Stream,
    StreamError,
    align_window_start,
    dump_stream_file,
    flatten_streams,
    load_stream_file,
    summarise,
)

START = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def _window(minute: int, anomaly: bool, *, scored: bool = True, service: str = "adservice") -> LabeledWindow:
    moment = START.replace(minute=minute)
    return LabeledWindow(
        service=service,
        window_start=moment,
        scenario="cpu" if anomaly else "normal",
        events=[Event(event_time=moment, service=service, kind="metric",
                      payload={"cpu_util": 0.5 if anomaly else 0.1})],
        expected={"is_anomaly": anomaly},
        scored=scored,
    )


def _streams() -> list:
    return [Stream(name="case-a", windows=[_window(0, False), _window(1, True)], meta={"fault": "cpu"}),
            Stream(name="case-b", windows=[_window(2, False, scored=False)], meta={"fault": "mem"})]


def test_a_stream_file_survives_a_round_trip(tmp_path: Path) -> None:
    path = dump_stream_file(tmp_path / "s.json", _streams(), "unit data", 60,
                            overrides={"gate": {"anomaly_probability": 0.5}})
    loaded = load_stream_file(path)

    assert loaded.dataset == "unit data"
    assert loaded.window_s == 60
    assert loaded.overrides == {"gate": {"anomaly_probability": 0.5}}
    assert [stream.name for stream in loaded.streams] == ["case-a", "case-b"]
    assert loaded.streams[0].meta == {"fault": "cpu"}
    assert [window.scored for window in loaded.streams[0].windows] == [True, True]
    assert [window.scored for window in loaded.streams[1].windows] == [False]
    assert [bool((w.expected or {}).get("is_anomaly")) for w in loaded.streams[0].windows] == [False, True]
    assert loaded.streams[0].windows[1].events[0].payload == {"cpu_util": 0.5}


def test_a_round_trip_keeps_the_event_time_not_the_window_start(tmp_path: Path) -> None:
    """Offset events inside a window must not be flattened onto the window start."""
    window = _window(0, True)
    window.events = [Event(event_time=START.replace(minute=0, second=37), service="adservice",
                           kind="metric", payload={"cpu_util": 0.9})]
    path = dump_stream_file(tmp_path / "s.json", [Stream(name="case", windows=[window])], "d", 60)
    loaded = load_stream_file(path)

    assert loaded.streams[0].windows[0].events[0].event_time.second == 37


def test_an_unknown_format_is_rejected_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": "brian.stream/99", "streams": []}), encoding="utf-8")

    with pytest.raises(StreamError, match="brian.stream/99"):
        load_stream_file(path)


def test_a_file_without_streams_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": FORMAT, "streams": []}), encoding="utf-8")

    with pytest.raises(StreamError, match="no streams"):
        load_stream_file(path)


def test_a_stream_without_a_name_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": FORMAT, "streams": [{"windows": []}]}), encoding="utf-8")

    with pytest.raises(StreamError, match="no name"):
        load_stream_file(path)


def test_overrides_must_be_an_object(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": FORMAT, "streams": [{"name": "a", "windows": []}],
                                "overrides": [1, 2]}), encoding="utf-8")

    with pytest.raises(StreamError, match="overrides"):
        load_stream_file(path)


def test_a_window_without_a_readable_start_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": FORMAT, "streams": [
        {"name": "a", "windows": [{"start": "not-a-date", "events": []}]}]}), encoding="utf-8")

    with pytest.raises(StreamError, match="no readable `start`"):
        load_stream_file(path)


def test_a_window_without_a_service_takes_it_from_its_events(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": FORMAT, "streams": [
        {"name": "a", "windows": [{"start": "2026-01-01T00:00:00Z", "events": [
            {"time": "2026-01-01T00:00:00Z", "service": "checkout-api",
             "kind": "metric", "payload": {"cpu_util": 0.1}}]}]}]}), encoding="utf-8")

    assert load_stream_file(path).streams[0].windows[0].service == "checkout-api"


def test_summarise_adds_counts_instead_of_averaging_rates() -> None:
    """One long recording and one short recording must not count as one recording each."""
    totals = summarise(_streams())

    assert totals["streams"] == 2
    assert totals["windows"] == 3
    assert totals["scored"] == 2
    assert totals["anomalous"] == 1
    assert totals["events"] == 3
    assert totals["names"] == ["case-a", "case-b"]


def test_flatten_streams_orders_by_time_across_recordings() -> None:
    streams = [Stream(name="late", windows=[_window(5, False)]),
               Stream(name="early", windows=[_window(1, False)])]

    ordered = flatten_streams(streams)

    assert [event.service for event in ordered] == ["adservice", "adservice"]
    assert ordered[0].event_time < ordered[1].event_time


def test_an_empty_set_summarises_as_empty_not_as_a_zero() -> None:
    assert summarise([]) == {"streams": 0, "windows": 0, "scored": 0, "anomalous": 0,
                             "events": 0, "names": []}


def test_a_label_lands_on_the_window_the_pipeline_will_emit() -> None:
    """The window start is a grid time, not the sample's own timestamp.

    Round 19 found the RCAEval stream carrying sample times (`…588`) against digest times
    (`…560`). Nothing matched, the evaluator scored zero windows, and the table said `recall
    0.000` — which reads like a fact about the detector and was a fact about the label.
    """
    sample = datetime(2023, 5, 27, 9, 56, 28, tzinfo=timezone.utc)

    aligned = align_window_start(sample, 60)

    assert aligned == datetime(2023, 5, 27, 9, 56, 0, tzinfo=timezone.utc)
    assert int(aligned.timestamp()) % 60 == 0
    assert aligned.tzinfo is not None


def test_the_alignment_follows_the_slide_not_just_the_size() -> None:
    """A window opens where the *newest* window that fits would start.

    A 300 s window sliding every 60 s is a 5-minute window that closes on the minute, so an event
    at 09:57:05 belongs to the window that began at 09:53:00 — not to one that began at 09:57:00,
    because that one would have to end at 10:02 and the pipeline has not seen that far yet.
    """
    sample = datetime(2023, 5, 27, 9, 57, 5, tzinfo=timezone.utc)

    assert align_window_start(sample, 300, 60) == datetime(2023, 5, 27, 9, 53, 0,
                                                           tzinfo=timezone.utc)


def test_a_window_size_of_zero_is_rejected_rather_than_division_by_zero() -> None:
    with pytest.raises(StreamError, match="window size must be positive"):
        align_window_start(START, 0)
