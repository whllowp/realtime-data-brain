"""Round 23: the alert-rate budget and the false-positive budget are different quantities."""
from __future__ import annotations

import pytest

from brian.evolve import (
    EvolveRules,
    Measurement,
    _count_at_or_above,
    free_cut_rates,
    label_free_cut,
)


def measurement(*, alert: float = 0.10, prevalence: float | None = 0.16) -> Measurement:
    empty = Measurement(candidate="c", recall=0.5, fpr=0.1, precision=0.5, reports=1,
                        tokens_per_report=100.0, tokens_per_window=10.0, latency_p95_ms=1.0)
    empty.alert_budget = alert
    empty.prevalence = prevalence
    return empty


def test_the_alert_budget_is_part_of_the_ruleset():
    # Boundary 45: `fpr_budget` caps the label-reading sweep, `alert_budget` sets the label-free bar, and
    # round 23 found one number doing both jobs on a stream where 10% of windows is 13% of healthy windows.
    assert EvolveRules().alert_budget == 0.10
    assert EvolveRules(alert_budget=0.02).to_dict()["alert_budget"] == 0.02
    assert "alert_budget" in EvolveRules().to_dict()


def test_the_cap_is_the_alert_rate_divided_by_the_prevalence():
    # Alerting on the top 10% of windows can find at most 10% of the faults divided by how many windows
    # are faults: one in six on SMD, one in two on RCAEval.
    assert measurement(alert=0.10, prevalence=0.50).label_free_ceiling == pytest.approx(0.20)
    assert measurement(alert=0.10, prevalence=0.16).label_free_ceiling == pytest.approx(0.625)
    assert measurement(alert=0.10, prevalence=0.512).label_free_ceiling == pytest.approx(0.1953125)


def test_a_rare_prevalence_leaves_the_cap_out_of_the_way():
    assert measurement(alert=0.10, prevalence=0.05).label_free_ceiling == pytest.approx(1.0)


def test_the_cap_needs_both_numbers():
    assert measurement(prevalence=0.0).label_free_ceiling is None
    assert measurement(prevalence=None).label_free_ceiling is None
    assert measurement(alert=0.0).label_free_ceiling is None


def test_the_cap_is_recorded_with_the_measurement():
    payload = measurement().to_dict()

    assert payload["alert_budget"] == pytest.approx(0.10)
    assert payload["prevalence"] == pytest.approx(0.16)
    assert payload["label_free_ceiling"] == pytest.approx(0.625)


def test_a_tighter_alert_rate_cannot_find_more():
    high = [_Score(0.95, True), _Score(0.80, True), _Score(0.30, True), _Score(0.20, False),
            _Score(0.10, False), _Score(0.05, False), _Score(0.02, False), _Score(0.01, False)]
    groups = [("only", high)]

    loose = free_cut_rates(groups, alert_budget=0.50)
    tight = free_cut_rates(groups, alert_budget=0.10)

    assert loose["recall"] >= tight["recall"]
    assert tight["recall"] is not None and tight["fpr"] is not None


class _Score:
    def __init__(self, probability: float, anomalous: bool):
        self.predicted_probability = probability
        self.expected_anomaly = anomalous
        self.predicted_anomaly = probability >= 0.5
        self.error = None


def test_the_label_free_cut_of_a_single_window_is_that_window():
    assert label_free_cut([0.4], 0.10) == pytest.approx(0.4)

def test_the_count_at_or_above_a_bar_includes_the_windows_sharing_it():
    # `label_free_cut` walks the distinct scores, so it needs the count at a value, ties included.
    assert _count_at_or_above([], 0.5) == 0
    assert _count_at_or_above([0.1, 0.2, 0.2, 0.9], 0.2) == 3
    assert _count_at_or_above([0.1, 0.2, 0.2, 0.9], 0.95) == 0
    assert _count_at_or_above([0.7, 0.7, 0.7], 0.7) == 3


def test_the_bar_is_raised_past_a_value_too_many_windows_share():
    # Three windows at 0.95 and three at 0.90: a 30% rate on ten windows may page on three, so the bar
    # can sit at 0.95 but not at 0.90 (which would page on six).
    scores = [0.95] * 3 + [0.90] * 3 + [0.2] * 4

    cut = label_free_cut(scores, 0.30)

    assert cut == pytest.approx(0.95)
    assert _count_at_or_above(sorted(scores), cut) <= 3


def test_a_sample_where_every_window_shares_one_score_has_no_bar():
    # Boundary 52: an alert-rate bar controls a count, and with one atom it cannot control anything.
    # Returning None says so; returning the atom would be a bar that lies about the rate.
    assert label_free_cut([0.7] * 10, 0.10) is None
    assert label_free_cut([0.9] * 20 + [0.5] * 10 + [0.1] * 30, 0.10) is None


def test_the_bar_pages_on_no_more_than_the_rate_it_was_given():
    scores = [round(0.02 * index, 2) for index in range(50)] + [0.02 * 3] * 12

    for budget in (0.02, 0.05, 0.10, 0.25):
        cut = label_free_cut(scores, budget)
        assert cut is not None
        paged = _count_at_or_above(sorted(scores), cut)
        assert paged <= max(1, int(round(len(scores) * budget)))


def test_the_alert_rate_actually_spent_is_reported_next_to_the_target():
    measurement_row = measurement()

    measurement_row.alerts_at_free_cut = 43
    measurement_row.scored_at_free_cut = 456

    assert measurement_row.free_cut_alert_rate == pytest.approx(0.0943, abs=1e-4)
    assert measurement_row.to_dict()["free_cut_alert_rate"] == pytest.approx(0.0943, abs=1e-4)
    assert measurement_row.to_dict()["alerts_at_free_cut"] == 43

    measurement_row.alerts_at_free_cut = None
    assert measurement_row.free_cut_alert_rate is None

def test_the_cap_follows_the_rate_the_bar_actually_spent():
    # A fifteen-window sample cannot page on 1.5 windows: the bar pages on two and spends 13.3% at a 10%
    # target. Comparing recall against the nominal budget would then report a violation of a rate the bar
    # was never able to respect (the demo stream does exactly this: recall 0.250 against a nominal cap
    # 0.188, and 2/15 alerts).
    measurement_row = measurement(alert=0.10, prevalence=0.50)
    measurement_row.alerts_at_free_cut = 2
    measurement_row.scored_at_free_cut = 15

    assert measurement_row.free_cut_alert_rate == pytest.approx(2 / 15)
    assert measurement_row.label_free_ceiling == pytest.approx(min(1.0, (2 / 15) / 0.50))

    measurement_row.alerts_at_free_cut = None
    measurement_row.scored_at_free_cut = None
    assert measurement_row.label_free_ceiling == pytest.approx(0.20)
