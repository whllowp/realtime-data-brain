"""Who judges a digest: the model, or a deterministic reader of the same text.

These tests pin the three things that make the switch honest rather than clever:

* the reader's score is a pure function of the digest, and monotone, so the gate's thresholds keep
  their meaning and no metric is quietly improved by the mapping;
* the reader never invents the answers it did not ask for (category, severity, blast radius);
* a window the reader cannot read is answered by the model, because silence and no-baseline are
  different findings.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from brian.config import ENV_VARS, load_settings
from brian.contracts import Digest, Lineage, Routing
from brian.laya.client import LayaResult
from brian.scoring import (
    CROSSOVER_DEVIATION,
    SCORING_MODES,
    deviation_probability,
    deviation_result,
    deviation_score,
    judge,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _digest(comparison=None, *, metrics=None) -> Digest:
    return Digest(
        service="svc-a",
        window_start=START,
        window_end=START + timedelta(minutes=1),
        metrics=metrics if metrics is not None else {"comparison": comparison},
        digest_id="d-1",
        lineage=Lineage(),
    )


def _entry(*, delta=None, long_delta=None):
    entry = {"value": 1.0, "n": 10}
    if delta is not None:
        entry["delta_ratio"] = delta
    if long_delta is not None:
        entry["long_delta_ratio"] = long_delta
    return entry


class _Exploding:
    """A client that fails the test if the model is asked anything."""

    def decide(self, digest):  # pragma: no cover - reached only on a bug
        raise AssertionError("the model should not have been asked")


class _Fixed:
    questions_hash = "stub-catalog"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def decide(self, digest):
        self.calls += 1
        return self.result


def test_the_score_is_the_loudest_deviation_the_renderer_would_print():
    digest = _digest({"cpu": _entry(delta=1.10),
                      "memory": _entry(delta=0.60),
                      "latency": _entry(delta=1.25)})
    assert deviation_score(digest) == pytest.approx(0.40)


def test_the_long_horizon_wins_because_the_host_renderer_shows_it():
    digest = _digest({"cpu": _entry(delta=2.0, long_delta=1.10)})
    assert deviation_score(digest) == pytest.approx(0.10)


def test_a_nested_comparison_payload_is_flattened():
    digest = _digest({"http": {"errors": {"rate": _entry(delta=0.5)}}})
    assert deviation_score(digest) == pytest.approx(0.50)


def test_no_comparison_means_no_reading_and_not_a_zero():
    assert deviation_score(_digest(None)) is None
    assert deviation_probability(_digest(None)) is None
    assert deviation_result(_digest(None)) is None
    assert deviation_score(_digest(metrics={})) is None


def test_an_entry_without_a_ratio_is_not_counted_as_a_flat_one():
    assert deviation_score(_digest({"cpu": _entry()})) is None


def test_the_mapping_is_strictly_monotone_and_crosses_at_the_shipped_bar():
    lower = deviation_probability(_digest({"cpu": _entry(delta=1.20)}))
    higher = deviation_probability(_digest({"cpu": _entry(delta=1.40)}))
    assert lower < higher
    crossing = deviation_probability(_digest({"cpu": _entry(delta=1.0 + CROSSOVER_DEVIATION)}))
    assert crossing == pytest.approx(0.3333, abs=1e-4)
    assert 0.30 < crossing < 0.36  # the SMD anomaly_probability bar this was measured against


def test_the_reader_answers_the_detection_question_and_claims_nothing_else():
    result = deviation_result(_digest({"cpu": _entry(delta=1.5)}))
    assert result is not None
    assert set(result.answers) == {"is_anomaly"}
    assert result.answers["is_anomaly"]["type"] == "noul"
    assert result.answers["is_anomaly"]["noul"] == pytest.approx(0.3333, abs=1e-4)
    assert result.routing.model == "deviation-reader"
    assert result.latency_ms == 0.0
    assert result.raw["scoring"] == "deviation"
    assert "category" in result.raw["answers_absent"]


def test_a_deterministic_reading_costs_no_tokens_and_no_gpu():
    result = deviation_result(_digest({"cpu": _entry(delta=1.5)}))
    assert result.input_tokens == 0
    assert result.cache_hit is False


def test_the_reader_decides_when_the_setting_says_so():
    settings = load_settings().with_overrides(
        digest=dataclasses.replace(load_settings().digest, scoring="deviation"))
    digest = _digest({"cpu": _entry(delta=1.5)})
    result = judge(digest, settings, _Exploding())
    assert result.answers["is_anomaly"]["noul"] == pytest.approx(0.3333, abs=1e-4)


def test_a_window_the_reader_cannot_read_is_answered_by_the_model():
    settings = load_settings().with_overrides(
        digest=dataclasses.replace(load_settings().digest, scoring="deviation"))
    sentinel = LayaResult(answers={"is_anomaly": {"type": "noul", "noul": 0.9}},
                          routing=Routing(model="laya"), latency_ms=12.0)
    client = _Fixed(sentinel)
    assert judge(_digest(None), settings, client) is sentinel
    assert client.calls == 1


def test_the_default_still_asks_the_model():
    settings = load_settings()
    sentinel = LayaResult(answers={"is_anomaly": {"type": "noul", "noul": 0.9}},
                          routing=Routing(model="laya"), latency_ms=12.0)
    client = _Fixed(sentinel)
    assert judge(_digest({"cpu": _entry(delta=5.0)}), settings, client) is sentinel
    assert client.calls == 1


def test_a_misspelled_mode_is_a_configuration_error_not_a_silent_fallback():
    settings = load_settings().with_overrides(
        digest=dataclasses.replace(load_settings().digest, scoring="deviaton"))
    with pytest.raises(ValueError) as excinfo:
        judge(_digest({"cpu": _entry(delta=1.5)}), settings, _Exploding())
    assert "unknown digest scoring" in str(excinfo.value)


def test_the_modes_are_the_two_documented_ones():
    assert SCORING_MODES == ("model", "deviation")


def test_the_mode_has_an_environment_variable_and_a_default_of_model():
    assert ENV_VARS[("digest", "scoring")] == "BRIAN_DIGEST_SCORING"
    assert load_settings().digest.scoring == "model"
    assert "scoring" in load_settings().digest.to_dict()


def test_a_whole_evaluation_asks_the_model_only_about_the_windows_it_cannot_read():
    """End to end: the switch has to reach the real pipeline, not just the `judge` function.

    The reader has nothing to say about the first windows of a recording (no baseline yet), so the
    model must still answer those — and only those. A test that asserted zero model calls would be
    asserting a bug.
    """
    from brian.config import load_settings as _load
    from brian.demo import LabeledWindow
    from brian.eval import Evaluator
    from brian.contracts import Event

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = []
    for index in range(16):
        when = start + timedelta(minutes=index)
        value = 0.95 if index % 4 == 0 and index >= 8 else 0.50
        windows.append(LabeledWindow(
            service="svc-a",
            window_start=when,
            scenario="cpu" if value > 0.9 else None,
            events=[Event(event_time=when, service="svc-a", kind="metric",
                          payload={"cpu_util": value, "memory_util": 0.4},
                          event_id="e%d" % index)],
            expected={"is_anomaly": value > 0.9},
            scored=True,
        ))

    base = _load()
    settings = base.with_overrides(
        digest=dataclasses.replace(base.digest, scoring="deviation"),
        window=dataclasses.replace(base.window, size_s=60, slide_s=60, min_events=1,
                                   baseline_windows=3, baseline_windows_long=6),
    )
    client = _Fixed(LayaResult(answers={"is_anomaly": {"type": "noul", "noul": 0.5}},
                               routing=Routing(model="laya"), latency_ms=1.0))
    report = Evaluator(settings, laya=client).run(windows)
    assert report.scores, "the evaluation produced no windows at all"
    assert client.calls <= 3, (
        "the model was asked %d times; only the first windows lack a baseline" % client.calls)

