"""The digest renderer is a model parameter, so its output is pinned.

Why this test exists
--------------------
The renderer decides what Laya reads, and Laya is an encoder over text — so the wording *is* the
model's input. Two failures in this repository came from treating it as formatting:

1. **A silent regression to one line.** `comparison` is stored nested (`comparison.latency_ms.p99`)
   while the first renderer looked it up flat (`.get("latency_ms.p99")`). Every lookup returned `None`,
   every clause was skipped, and the digest became a single "Service X, a 60-second window" sentence.
   Nothing errored. The model scored AUC 0.02.

2. **A benchmark measuring a different input.** `scripts/compare_renderers.py` re-implemented the prose
   renderer, and the production version drifted from it. A threshold swept against the benchmark's text
   was then applied to the production text, giving recall 0.98 at a false-positive rate of 1.00.

So this file asserts the exact rendered text for a fixed window, and asserts that the benchmark's
renderer agrees with production. Both are deliberately brittle: a wording change should fail a test
and be re-measured, because a wording change moves the detector's operating point.
"""
from __future__ import annotations

from datetime import timezone
from pathlib import Path
import sys

import pytest

from brian.state import render_digest

UTC = timezone.utc

#: A window built so that every branch of the renderer fires at least once: an error signal with a
#: baseline, a latency signal, all four resources, an upstream failure, timeouts, throughput, a
#: dominant error template, a deploy, a config change, schema failures and consumer lag.
#:
#: Hand-built rather than generated, because a test that pins the output of a generator pins the
#: generator too: when `synthesize_normal_window` changes, this test would fail for a reason that has
#: nothing to do with the renderer.
BASELINE_METRICS = {
    "comparison": {
        "errors": {"ratio": {"key": "errors.ratio", "unit": "fraction", "value": 0.31,
                             "baseline": 0.005, "delta_ratio": 62.0, "z": 24.1, "n": 12}},
        "latency_ms": {"p99": {"key": "latency_ms.p99", "unit": "ms", "value": 8900.0,
                               "baseline": 179.0, "delta_ratio": 49.72, "z": 31.0, "n": 12}},
        "cpu": {"util": {"key": "cpu.util", "unit": "fraction", "value": 0.98,
                         "baseline": 0.43, "delta_ratio": 2.28, "z": 9.0, "n": 12}},
        "memory": {"util": {"key": "memory.util", "unit": "fraction", "value": 0.72,
                            "baseline": 0.66, "delta_ratio": 1.09, "z": 2.0, "n": 12}},
        "db_pool": {"util": {"key": "db_pool.util", "unit": "fraction", "value": 0.99,
                             "baseline": 0.35, "delta_ratio": 2.83, "z": 18.0, "n": 12}},
        "thread_pool": {"util": {"key": "thread_pool.util", "unit": "fraction", "value": 0.91,
                                 "baseline": 0.38, "delta_ratio": 2.39, "z": 15.0, "n": 12}},
        "upstream": {
            "error_ratio": {"key": "upstream.error_ratio", "unit": "fraction", "value": 0.27,
                            "baseline": 0.004, "delta_ratio": 67.5, "z": 20.0, "n": 12},
            "latency_ms": {"p99": {"key": "upstream.latency_ms.p99", "unit": "ms", "value": 6100.0,
                                   "baseline": 132.0, "delta_ratio": 46.21, "z": 22.0, "n": 12}},
        },
        "timeouts": {"rate": {"key": "timeouts.rate", "unit": "timeout/s", "value": 3.1,
                              "baseline": 0.0, "delta_ratio": None, "z": None, "n": 12}},
        "requests": {"rate": {"key": "requests.rate", "unit": "req/s", "value": 48.0,
                              "baseline": 10.7, "delta_ratio": 4.49, "z": 11.0, "n": 12}},
    },
    "service": "checkout-api",
    "window_seconds": 60.0,
    "event_count": 812,
    "baseline_samples": 12,
    "error_signature": {
        "template": "TimeoutException: connection acquisition from pool orders-db timed out after <num><unit>",
        "count": 412,
        "total": 430,
        "share": 0.9581,
        "distinct": 3,
    },
    "deploys": ["checkout-api v2.14.0"],
    "config_changes": ["new_pricing_engine=enabled"],
    "schema_errors": {"ratio": {"key": "schema_errors.ratio", "unit": "fraction", "value": 0.02,
                                "baseline": 0.0, "delta_ratio": None, "z": None, "n": 12}},
    "consumer": {"lag": {"key": "consumer.lag", "unit": "records", "value": 42000.0,
                         "baseline": 120.0, "delta_ratio": 350.0, "z": 30.0, "n": 12}},
}

EXPECTED = """\
Service checkout-api, a 60-second window with 812 events.
Error fraction 31.00% against a baseline of 0.50% (62.00x).
Latency 8900 ms against a baseline of 179 ms (49.72x).
Resources: CPU 98.00%, memory 72.00%, DB pool 99.00%, thread pool 91.00%.
Upstream calls failed at 27.00% against a baseline of 0.40% (67.50x).
Connection timeouts at 3.100 per second against a baseline of 0.
Throughput 48.0 requests per second against a baseline of 10.7 req/s (4.49x), with errors rising at the same time.
The most frequent error is: TimeoutException: connection acquisition from pool orders-db timed out after <num><unit>
A deployment was recorded: checkout-api v2.14.0.
A configuration change was recorded: new_pricing_engine=enabled."""


def test_rendered_digest_is_pinned() -> None:
    """The exact text the model reads. A change here is a change to model behaviour.

    If this fails, the renderer was edited. Do not update the expected string and move on: re-run
    `scripts/compare_renderers.py` (to see the effect on ranking) and `brian eval` (to see it on the
    operating point) first, because a wording change moved AUC from 0.02 to 0.89 in this project.
    """
    text, dropped = render_digest(BASELINE_METRICS)
    assert dropped == []
    assert text == EXPECTED


def test_every_signal_is_present() -> None:
    """Each independently-checked clause appears, so a lookup regression cannot hide.

    This is the check that would have caught the nested-vs-flat `comparison` bug: that renderer did not
    crash, it silently produced one line.
    """
    text, _ = render_digest(BASELINE_METRICS)
    for fragment in (
        "Error fraction",
        "Latency",
        "Resources:",
        "Upstream calls failed",
        "Connection timeouts",
        "Throughput",
        "The most frequent error is:",
        "A deployment was recorded",
        "A configuration change was recorded",
    ):
        assert fragment in text, "the renderer dropped the %r clause" % fragment


# --------------------------------------------------------------------------------------
# host profile: a separate measured rendering, not a variant of the service one
# --------------------------------------------------------------------------------------

HOST_METRICS_FIXTURE = {
    "service": "machine-1-6",
    "window_seconds": 300.0,
    "event_count": 5,
    "baseline_samples": 30,
    "comparison": {
        "host": {
            "cpu": {"key": "host.cpu", "label": "CPU", "unit": "", "value": 0.72,
                    "baseline": 0.18, "delta_ratio": 4.0, "n": 30},
            "cpu_system": {"key": "host.cpu_system", "label": "CPU system", "unit": "",
                           "value": 0.12, "baseline": 0.08, "delta_ratio": 1.53, "n": 30},
            "load_1m": {"key": "host.load_1m", "label": "load 1m", "unit": "",
                        "value": 0.70, "baseline": 0.69, "delta_ratio": 1.01, "n": 30},
            "mem_free": {"key": "host.mem_free", "label": "free memory", "unit": "",
                         "value": 0.25, "baseline": 0.25, "delta_ratio": 1.0, "n": 30},
        }
    },
}

HOST_EXPECTED = """\
Host machine-1-6, a 300-second window with 5 samples.
CPU 0.72 (baseline 0.18, 4.00x).
CPU system 0.12 (baseline 0.08, 1.53x).
load 1m 0.70 (baseline 0.69, 1.01x).
free memory 0.25 (baseline 0.25, 1.00x).
2 of 4 measurements are outside +/-25% of their baseline."""


def test_host_profile_digest_is_pinned() -> None:
    """The host rendering is its own measured artefact and is pinned separately.

    The two profiles exist because they have incompatible requirements, and that is not a guess:

    * adding per-resource baselines to the **service** profile raised the false-positive rate on the
      labelled HTTP suite from 0.000 to 1.000 — every healthy window began firing;
    * removing them from the **host** profile leaves the real SMD server dataset with a two-line
      digest (`Resources: CPU 19.75%, memory 0.00%.`), on which the model suppressed every window as
      confidently normal, fault-injected ones included.

    So the profile split is the fix, and this test exists so a future "simplification" that merges them
    fails here rather than silently in production.
    """
    text, dropped = render_digest(HOST_METRICS_FIXTURE, profile="host")
    assert dropped == []
    assert text == HOST_EXPECTED


def test_host_profile_states_every_metric_with_a_baseline() -> None:
    """No value may appear without its reference point in the host profile.

    A bare number is not evidence: it is a fact with no scale. This is the exact failure the profile was
    introduced to fix.
    """
    text, _ = render_digest(HOST_METRICS_FIXTURE, profile="host")
    for line in text.splitlines()[1:5]:
        assert "baseline" in line, "host line without a baseline: %r" % line


def test_the_two_profiles_render_different_text_for_the_same_metrics() -> None:
    """A profile is a model input, not a formatting flag: identical metrics, different state."""
    service_text, _ = render_digest(HOST_METRICS_FIXTURE, profile="service")
    host_text, _ = render_digest(HOST_METRICS_FIXTURE, profile="host")
    assert service_text != host_text
    # The service profile has no clause for host.* metrics, so it states them in one aggregate line.
    assert "Other signals:" in service_text
    assert "Host " in host_text


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        render_digest(BASELINE_METRICS, profile="nonsense")


def test_service_profile_still_says_nothing_about_unpopulated_metrics() -> None:
    """The `Other signals` line must not fire for the shipped HTTP catalog.

    If it did, every synthetic digest would gain a line restating metrics it already covers, which is
    precisely the "prose plus restated numbers" form that measured worst (AUC 0.43).
    """
    text, _ = render_digest(BASELINE_METRICS)
    assert "Other signals:" not in text


def test_comparisons_are_stated_in_a_comparable_unit() -> None:
    """Both halves of a comparison carry the same unit, and fractions are rendered as percentages.

    `0.05% against a baseline of 0.0003` asks the model to compare a percentage with a raw fraction;
    the measured answers were worse for it.
    """
    text, _ = render_digest(BASELINE_METRICS)
    assert "against a baseline of 0.50%" in text          # a fraction's baseline is a percentage
    assert "against a baseline of 179 ms" in text         # a duration's baseline carries its unit
    assert "0.0003" not in text
    assert "0.005" not in text
    # A duration baseline must not be rendered as a bare number: that asks the model to compare a
    # duration against something unitless.
    assert "baseline of 179 (" not in text


def test_extra_statistics_are_not_rendered() -> None:
    """z-scores and sample counts stay in `metrics`, not in the text.

    Measured: including them compressed the answers into a 0.04-wide band (AUC 0.78) while the lean
    form spread them over 0.13 (AUC 0.89). They remain available to the reasoner and to an auditor.
    """
    text, _ = render_digest(BASELINE_METRICS)
    assert "z=" not in text.rstrip(".")
    assert "n=12" not in text
    assert "standing out" not in text
    # ...but the statistics are still in the record.
    assert BASELINE_METRICS["comparison"]["errors"]["ratio"]["z"] == 24.1
    assert BASELINE_METRICS["comparison"]["errors"]["ratio"]["n"] == 12


def test_a_missing_baseline_is_stated_rather_than_implied() -> None:
    """With no baseline the text says so. A bare list of numbers reads like a healthy window."""
    text, _ = render_digest({"service": "svc", "window_seconds": 60, "event_count": 10,
                             "comparison": {}, "baseline_samples": 0})
    assert "No baseline is available yet" in text


def test_truncation_is_reported_not_silent() -> None:
    text, dropped = render_digest(BASELINE_METRICS, max_words=12)
    assert dropped, "a tight budget must report what it dropped"
    assert "omitted for length" in text


def test_the_benchmark_renderer_agrees_with_production() -> None:
    """`scripts/compare_renderers.py` must render exactly what production renders.

    When these drifted, a threshold swept against the benchmark was applied to production text and the
    detector produced a false positive on every healthy window. A benchmark that renders its own input
    is measuring a different system.
    """
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if not (scripts / "compare_renderers.py").is_file():
        pytest.skip("comparison script not present")
    sys.path.insert(0, str(scripts))
    try:
        import compare_renderers  # type: ignore
    finally:
        sys.path.pop(0)
    for name in ("prose",):
        assert compare_renderers.RENDERERS[name](BASELINE_METRICS) == render_digest(BASELINE_METRICS)[0]
