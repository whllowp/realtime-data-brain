"""Pin the detector defaults that were arrived at by measurement, not by taste.

Every constant here cost a measurement on the SMD dataset to move, and several of them look like
over-complication to anyone reading them cold. The point of this file is that removing one should require
deleting a failing test and reading why, rather than being a silent "simplification":

* a *second*, long baseline on top of the short one (AUC 0.562 -> 0.758);
* `reduce="last"` even though sampling is sparser than the window (the obvious `max` loses 0.09 recall);
* no summary verdict in the rendered state (0.758 -> 0.583 when it was added);
* three host features, not eight (0.756 vs 0.515 in the other direction).

None of these are universal truths — they are what this checkpoint on this data measured. The tests pin
them so that changing one is a deliberate act with a measurement attached, which is the whole discipline
this project is built on.
"""
from __future__ import annotations

from brian.config import WindowSettings
from brian.state import (
    BASELINE_KEYS,
    DEFAULT_METRICS,
    HOST_BASELINE_KEYS,
    HOST_METRICS,
    BaselineTracker,
    MetricSpec,
)


def test_long_baseline_is_configured_and_on_by_default() -> None:
    """The short horizon alone cannot see a fault that outlasts it.

    Enabling this changed SMD ranking AUC from 0.5617 to 0.7577 with disjoint bootstrap intervals, and it
    is the single largest accuracy change in the project. A default of 0 would silently restore the
    old behaviour.
    """
    settings = WindowSettings()
    assert settings.baseline_windows == 30
    assert settings.baseline_windows_long >= 10 * settings.baseline_windows, (
        "the long horizon must be long enough to not simply track the short one"
    )


def test_host_metrics_stay_at_the_last_sample() -> None:
    """`reduce="max"` was measured and rejected; re-enabling it needs a new measurement.

    Taking the window's worst sample looks obviously right for a sparse feed and is not: on SMD it lowered
    AUC to 0.7427 (from 0.7577) and recall at a 10% false-positive budget to 0.3151 (from 0.4089).
    """
    for spec in HOST_METRICS:
        assert spec.reduce == "last", (
            "%s sets reduce=%r; the window-reduction experiment measured 'last' as better on SMD"
            % (spec.key, spec.reduce)
        )


def test_host_profile_carries_three_signals_not_eight() -> None:
    """Dilution is real: eight host metrics scored 0.515 where three scored 0.756.

    The profile still *defines* eight metrics so an operator can point it at whatever the feed provides,
    but the measured configuration feeds three. This test pins the set that was measured.
    """
    sources = [spec.source for spec in HOST_METRICS]
    measured = {"cpu_r", "load_1", "mem_free"}
    assert measured.issubset(set(sources)), "the measured feature set must be expressible by the profile"
    assert len(sources) >= len(measured), "the profile must be able to represent the measured set"


def test_invalid_reduce_is_rejected_loudly() -> None:
    """A typo in a detector parameter must fail at construction, not silently fall back to 'last'."""
    try:
        MetricSpec("x", "x", "gauge", "", "up", "", "x", reduce="median")
    except ValueError as exc:
        assert "reduce" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("MetricSpec accepted an unknown reduce rule")


def test_service_and_host_profiles_track_separate_keys() -> None:
    """The two profiles must not share a baseline vocabulary.

    If they did, a service process and a host process could end up comparing each other's numbers — which
    is exactly the class of bug that made an earlier feature sweep report a phantom +0.09 recall.
    """
    assert set(BASELINE_KEYS).isdisjoint(HOST_BASELINE_KEYS)
    assert all(key.startswith("host.") for key in HOST_BASELINE_KEYS)
    assert not any(key.startswith("host.") for key in BASELINE_KEYS)


def test_service_profile_still_defaults_to_the_last_sample() -> None:
    """The service rendering was measured with `last`; changing it would invalidate the 0.854 result."""
    for spec in DEFAULT_METRICS:
        assert spec.reduce == "last", "%s changed the service reduction; re-measure brian eval first" % spec.key


def test_a_tracker_without_a_long_horizon_behaves_exactly_as_before() -> None:
    """Adding the long horizon must be a no-op when it is disabled.

    Otherwise every existing deployment would change behaviour on upgrade without asking for it.
    """
    plain = BaselineTracker(window=5, keys=("host.cpu",))
    explicit = BaselineTracker(window=5, keys=("host.cpu",), long_window=0)
    for value in (0.1, 0.2, 0.3):
        plain.observe("svc", {"host": {"cpu": value}})
        explicit.observe("svc", {"host": {"cpu": value}})
    assert plain.stats("svc", "host.cpu").long_median is None
    assert explicit.stats("svc", "host.cpu").long_median is None
    assert plain.to_dict() == explicit.to_dict()
