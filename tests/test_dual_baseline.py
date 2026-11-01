"""Dual-horizon baseline: the short window finds spikes, the long one finds sustained faults.

Why both exist, in numbers
--------------------------
Measured on the SMD server dataset (4,738 windows of 300 s, 15.65% labelled anomalous), changing *only*
the reference horizon of the digest moved ranking AUC:

    reference = last 30 windows  (2.5 h)   AUC 0.5617   CI [0.528, 0.601]
    reference = last 480 windows (40 h)   AUC 0.7577   CI [0.729, 0.783]
    reference = all history              AUC 0.7721   CI [0.748, 0.800]

The mechanism: a rolling median *follows* a fault that outlasts its window, so `0.92x of baseline`
collapses to `1.00x` exactly when the fault is at its worst. Neither horizon is correct on its own — a
sudden spike is only visible against recent history, and a slow burn is only visible against a long one —
so the tracker keeps both and each profile reads the one it was measured with.

These tests pin the mechanics, not the numbers (the numbers need the dataset and the engine).
"""
from __future__ import annotations

from brian.state import BaselineTracker, MetricValue, render_digest


def test_short_and_long_baselines_differ_after_a_sustained_shift() -> None:
    """A step change must look normal to the short horizon and abnormal to the long one.

    This is the failure mode in miniature: 60 windows at 0.10, then 60 windows at 0.90. By the end of
    the shift the short median is 0.90 (ratio 1.0x, "nothing to see") while the long median still
    remembers the 0.10 era.
    """
    tracker = BaselineTracker(window=30, keys=("cpu.util",), long_window=480)
    for _ in range(60):
        tracker.observe("svc", {"cpu": {"util": 0.10}})
    for _ in range(60):
        tracker.observe("svc", {"cpu": {"util": 0.90}})

    stats = tracker.stats("svc", "cpu.util")
    assert stats.median is not None and stats.long_median is not None
    assert abs(stats.median - 0.90) < 1e-9, "the short horizon should have followed the shift"
    # The long horizon holds 60 windows of each regime, so its median sits half way between them — the
    # point is that it is *not* the short horizon's answer. Asserting a fixed number here would only pin
    # the test fixture's arithmetic, so assert the relationship that matters.
    assert 0.10 <= stats.long_median <= 0.50
    assert stats.long_median < stats.median - 0.30, "the long horizon must stay far from the shift"
    assert stats.long_n > stats.n, "the long horizon must hold more history than the short one"


def test_long_horizon_is_optional_and_off_by_default() -> None:
    tracker = BaselineTracker(window=30, keys=("cpu.util",))
    tracker.observe("svc", {"cpu": {"util": 0.5}})
    stats = tracker.stats("svc", "cpu.util")
    assert stats.long_median is None
    assert stats.long_n == 0


def test_long_history_survives_a_checkpoint_round_trip() -> None:
    """A restarted Flink operator must not have to re-learn the slow horizon.

    The long horizon takes 40 h of windows to become meaningful, so losing it in a checkpoint would
    silently revert the system to the short-horizon behaviour that cannot see sustained faults.
    """
    tracker = BaselineTracker(window=4, keys=("cpu.util",), long_window=20)
    for value in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        tracker.observe("svc", {"cpu": {"util": value}})
    restored = BaselineTracker.from_dict(tracker.to_dict(), keys=("cpu.util",))

    assert restored.long_window == 20
    assert len(restored._long["svc"]["cpu.util"]) == 6
    assert restored.stats("svc", "cpu.util").long_median == tracker.stats("svc", "cpu.util").long_median


def test_host_renderer_prefers_the_long_baseline_when_present() -> None:
    """The rendering that was measured uses the long reference; the short one is the fallback."""
    metrics = {
        "service": "machine-1-6",
        "window_seconds": 300.0,
        "event_count": 5,
        "baseline_samples": 30,
        "comparison": {
            "host": {
                "cpu": {
                    "key": "host.cpu", "label": "CPU", "unit": "", "value": 0.92,
                    "baseline": 0.90, "delta_ratio": 1.02, "n": 30,
                    "long_baseline": 0.18, "long_delta_ratio": 5.11, "long_n": 480,
                }
            }
        },
    }
    text, _ = render_digest(metrics, profile="host")
    assert "CPU 0.92 (baseline 0.18, 5.11x)." in text, text
    # The short-horizon reading (1.02x) must not be what the model sees: it is the one that hides a
    # sustained fault, and stating it alongside would ask the model to arbitrate between two references.
    assert "0.90" not in text
    assert "1.02x" not in text


def test_host_renderer_falls_back_when_no_long_horizon_was_tracked() -> None:
    metrics = {
        "service": "machine-1-6",
        "window_seconds": 300.0,
        "event_count": 5,
        "baseline_samples": 30,
        "comparison": {
            "host": {
                "cpu": {"key": "host.cpu", "label": "CPU", "unit": "", "value": 0.92,
                        "baseline": 0.18, "delta_ratio": 5.11, "n": 30},
            }
        },
    }
    text, _ = render_digest(metrics, profile="host")
    assert "CPU 0.92 (baseline 0.18, 5.11x)." in text


def test_metric_value_only_carries_long_fields_when_they_exist() -> None:
    """A tracker without the long horizon must not emit null fields into the contract."""
    short_only = MetricValue(key="host.cpu", label="CPU", unit="", value=1.0, baseline=0.5, n=30)
    assert "long_baseline" not in short_only.to_dict()

    with_long = MetricValue(key="host.cpu", label="CPU", unit="", value=1.0, baseline=0.5, n=30,
                            long_baseline=0.2, long_delta_ratio=5.0, long_n=480)
    payload = with_long.to_dict()
    assert payload["long_baseline"] == 0.2
    assert payload["long_delta_ratio"] == 5.0
    assert payload["long_n"] == 480
