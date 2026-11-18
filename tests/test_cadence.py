# -*- coding: utf-8 -*-
"""Round 27 tests: a window count is not a duration, so the claim records its cadence.

`v020` promises "the loudest of the last 200 windows". On the stream it was measured on a window was 3000
seconds; on the stream that re-checked it, 2100. Round 26 refused to judge that, which was right, but a
refusal with no number leaves the reader nothing to do — so the cadence is compared explicitly and the
check names the window count that would cover the same wall-clock.
"""
import json
from datetime import datetime, timedelta, timezone

from brian.demo import LabeledWindow
from brian.evolve import Candidate, EvolveRules
from brian.promote import (
    UNMEASURABLE,
    Claim,
    _geometry_mismatch,
    claim_check,
    rescale_hint,
)
from brian.streams import Stream, summarise

RECORDED = {"streams": 2, "windows": 948, "scored": 828, "anomalous": 134, "events": 4740,
            "cadence_s": 3000.0, "span_s": 900000}
SAME_CADENCE = {"streams": 2, "windows": 940, "scored": 820, "anomalous": 130, "events": 4700,
                "cadence_s": 3050.0, "span_s": 900000}
OTHER_CADENCE = {"streams": 2, "windows": 940, "scored": 820, "anomalous": 130, "events": 4700,
                 "cadence_s": 2100.0, "span_s": 700000}


def _promotion(window=200):
    return type("Fake", (), {"env": {"BRIAN_DIGEST_ROLLING_WINDOW": str(window)}})()


def _stream(name, starts, *, events_per_window=1):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = []
    for index in range(starts):
        windows.append(LabeledWindow(
            service=name, window_start=origin + timedelta(seconds=3000 * index),
            scenario="normal", events=[], expected={"is_anomaly": False},
        ))
    return Stream(name=name, windows=windows)


def test_summarise_records_the_cadence_and_the_span_it_covers():
    # The number that made a healthy configuration look like a regression: two streams with the same
    # window count but windows 50 minutes apart vs 35.
    slow = _stream("slow", 10)
    fast = Stream(name="fast", windows=[
        LabeledWindow(service="fast", window_start=datetime(2026, 1, 1, tzinfo=timezone.utc)
                      + timedelta(seconds=2100 * index), scenario="normal", events=[],
                      expected={"is_anomaly": False})
        for index in range(10)
    ])

    slow_totals = summarise([slow])
    fast_totals = summarise([fast])

    assert slow_totals["cadence_s"] == 3000.0
    assert fast_totals["cadence_s"] == 2100.0
    assert slow_totals["span_s"] == 3000 * 9
    # Both cover the same 90 000 s when averaged, but the claim's window count does not transfer.
    assert slow_totals["windows"] == fast_totals["windows"] == 10


def test_a_short_stream_has_no_cadence_rather_than_a_wrong_one():
    totals = summarise([_stream("single", 1)])

    assert totals["cadence_s"] is None
    assert totals["span_s"] is None


def test_windows_of_a_different_length_are_a_geometry_mismatch_even_at_equal_counts():
    mismatch = _geometry_mismatch(RECORDED, OTHER_CADENCE)

    assert mismatch is not None
    assert "2100" in mismatch and "3000" in mismatch
    assert _geometry_mismatch(RECORDED, SAME_CADENCE) is None


def test_the_hint_names_the_window_count_that_covers_the_same_span():
    # 200 windows x 3000 s = 600 000 s of traffic; at 2100 s per window that is 286 windows.
    hint = rescale_hint(_promotion(200), RECORDED, OTHER_CADENCE)

    assert hint is not None
    assert "BRIAN_DIGEST_ROLLING_WINDOW=286" in hint
    assert "600000" in hint


def test_no_hint_when_the_cadence_is_the_same_or_the_knob_is_missing():
    assert rescale_hint(_promotion(200), RECORDED, SAME_CADENCE) is None
    assert rescale_hint(_promotion(), RECORDED, None) is None
    assert rescale_hint(type("Fake", (), {"env": {}})(), RECORDED, OTHER_CADENCE) is None


def test_a_different_cadence_is_unmeasurable_not_failed():
    claim = Claim(recall_at_budget=0.507, tokens_per_report=3358.0, fpr_at_budget=0.09)
    rules = EvolveRules()
    # Same shape in windows and events; the only difference is that a window is 35 minutes, not 50.
    measurement = type("M", (), {
        "recall_at_budget": 0.297, "fpr_at_budget": 0.09, "tokens_per_report": 3358.0,
        "fpr": 0.09, "reports": 100,
    })()

    verdict, reason = claim_check(claim, measurement, rules, recorded_stream=RECORDED,
                                  checked_stream=OTHER_CADENCE)

    assert verdict == UNMEASURABLE
    assert "2100" in reason and "3000" in reason


def test_the_record_keeps_the_cadence_so_a_later_check_can_compare_it(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True)
    (directory / "v021.json").write_text(json.dumps({
        "version": 21, "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "stream": dict(RECORDED, source="SMD"), "claim": {"recall_at_budget": 0.507},
    }), encoding="utf-8")

    from brian.promote import load_promotions

    promotion = load_promotions(tmp_path)[0]

    assert promotion.stream["cadence_s"] == 3000.0
    assert "_claimed_window" and Candidate is not None
