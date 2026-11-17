"""The improvement mechanism (`Imp`) as a unit, without a Laya service or an LLM.

What is worth testing here is not that a measurement runs — that is `brian eval` and `brian cost` — but
that the loop refuses to accept changes it should refuse, that it remembers those refusals, and that a
change it accepts can be reproduced and reversed from the file it wrote.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brian.config import ENV_VARS, load_settings
from brian.contracts import Event
from brian.demo import LabeledWindow
from brian.evolve import (
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    alerts_per_1000,
    apply_overrides,
    audit,
    catalogue,
    decide,
    deployable,
    format_audit,
    format_rejudge,
    format_round,
    free_cut_rates,
    grade,
    history,
    label_free_cut,
    operating_point,
    operating_point_by_recording,
    own_cut_shortfall,
    persist,
    previously_rejected,
    rejudge,
    rules_fingerprint,
    state_ceiling,
    tokens_per_incident,
)
from brian.llm.prompts import evidence_coverage, evidence_floor
from brian.scoring import text_deviation
from brian.streams import Stream


PERFECT = {"log_retention": 1.0, "signature_retention": 1.0,
           "first_error_kept": 1.0, "context_retention": 1.0}


def measure(name: str = "candidate", *, recall: float = 0.875, fpr: float = 0.0,
            tokens: float | None = 100.0, coverage=None, auc_by_recording=None) -> Measurement:
    return Measurement(
        candidate=name,
        recall=recall,
        fpr=fpr,
        precision=1.0,
        reports=8 if tokens is not None else 0,
        tokens_per_report=tokens,
        tokens_per_window=None if tokens is None else tokens / 4.0,
        latency_p95_ms=1.0,
        coverage=dict(coverage if coverage is not None else PERFECT),
        trial_s=0.1,
        reasoner="offline",
        auc_by_recording=dict(auc_by_recording or {}),
    )


# --------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------
def test_accepts_a_cheaper_configuration_that_keeps_everything():
    verdict = decide(measure("baseline"), measure("cheaper", tokens=60.0), EvolveRules())
    assert verdict.accepted
    assert verdict.rule == "cost"


def test_rejects_a_candidate_that_loses_recall_before_looking_at_cost():
    verdict = decide(measure("baseline"), measure("cheaper", recall=0.75, tokens=10.0), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "recall"
    assert "0.875 -> 0.750" in verdict.reason


def test_rejects_a_candidate_that_raises_the_false_positive_rate():
    verdict = decide(measure("baseline"), measure("cheaper", fpr=0.2, tokens=10.0), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "fpr"


def test_rejects_a_candidate_that_starves_the_prompt():
    """The label signal cannot see this; the deterministic coverage reading exists to."""
    starved = dict(PERFECT, first_error_kept=0.0)
    verdict = decide(measure("baseline"), measure("cheaper", tokens=10.0, coverage=starved),
                     EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "evidence"
    assert "first_error_kept 0%" in verdict.reason


def test_rejects_a_gain_below_the_bar():
    verdict = decide(measure("baseline"), measure("slightly-cheaper", tokens=99.0), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "gain"


def test_the_bar_is_the_rules_not_a_constant():
    rules = EvolveRules(min_gain=0.0)
    verdict = decide(measure("baseline"), measure("slightly-cheaper", tokens=99.0), rules)
    assert verdict.accepted


def test_a_veto_overrides_a_perfect_measurement():
    veto = "drops the reasoning pass the ladder exists to request"
    verdict = decide(measure("baseline"), measure("cheap", tokens=1.0), EvolveRules(), veto=veto)
    assert not verdict.accepted
    assert verdict.rule == "veto"
    assert verdict.reason == veto


# --------------------------------------------------------------------------------------
# the threshold-invariant label signal
# --------------------------------------------------------------------------------------
def at_budget(name: str = "candidate", *, recall: float = 0.875, fpr: float = 0.10,
              raw_recall: float | None = 0.0, tokens: float | None = 100.0) -> Measurement:
    m = measure(name, recall=raw_recall if raw_recall is not None else 0.0, tokens=tokens)
    m.recall_at_budget = recall
    m.fpr_at_budget = fpr
    m.threshold_at_budget = 0.33
    m.budget = 0.10
    return m


def test_a_nominal_shortfall_against_the_free_cut_does_not_veto_a_large_gain():
    """Round 14: the rolling rank found 0.216 where the sweep found 0.224, and was rejected.

    The baseline found 0.060. Rejecting a 3.7x recall gain because the recorded cut lands 3.6% short of
    a sample-fitted optimum is a proportionality failure, not a Pareto argument.
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.072, raw_recall=1.000)
    trial = at_budget("host-rolling-rank", recall=0.224, fpr=0.091, raw_recall=0.216)
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted, verdict.reason
    assert verdict.rule == "quality"


def test_a_material_shortfall_against_the_free_cut_is_still_rejected():
    baseline = at_budget("baseline", recall=0.060, fpr=0.072, raw_recall=1.000)
    trial = at_budget("silencing-threshold", recall=0.224, fpr=0.090, raw_recall=0.050)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "operational"
    assert "78% fewer" in verdict.reason
    assert "10% shortfall tolerated" in verdict.reason


def test_the_shortfall_tolerance_is_a_rule_and_its_default_decides_round_14():
    baseline = at_budget("baseline", recall=0.060, fpr=0.072, raw_recall=1.000)
    trial = at_budget("host-rolling-rank", recall=0.224, fpr=0.091, raw_recall=0.216)
    strict = decide(baseline, trial, EvolveRules(operational_tolerance=0.0))
    relaxed = decide(baseline, trial, EvolveRules(operational_tolerance=0.10))
    assert not strict.accepted and strict.rule == "operational"
    assert "4% fewer" in strict.reason
    assert relaxed.accepted and relaxed.rule == "quality"


def test_the_shortfall_is_zero_when_nothing_can_be_compared():
    assert own_cut_shortfall(measure("a", recall=0.5)) == 0.0
    undefined = measure("a", recall=0.5)
    undefined.recall_at_budget = 0.0
    assert own_cut_shortfall(undefined) == 0.0
    better = at_budget("a", recall=0.20, raw_recall=0.30)
    assert own_cut_shortfall(better) == 0.0


def test_the_label_check_uses_the_at_budget_pair_when_both_sides_have_one():
    """A cut calibrated on another domain must not decide a comparison on this one.

    On SMD every answer lands between 0.30 and 0.47, so the shipped 0.5 reports recall 0 for a
    detector whose ranking is 0.75 AUC. Judging at a common budget is what makes the label column
    say something about the detector instead of about the constant.
    """
    baseline = at_budget("baseline", recall=0.030, raw_recall=0.0)
    trial = at_budget("host-features-3", recall=0.386, raw_recall=0.0, tokens=60.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted, verdict.reason
    assert verdict.rule == "cost"


def test_a_candidate_that_finds_fewer_incidents_at_the_budget_is_rejected():
    baseline = at_budget("baseline", recall=0.386)
    trial = at_budget("narrower", recall=0.030, tokens=10.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "recall"
    assert "recall at 10% false positives 0.386 -> 0.030" in verdict.reason


def test_a_candidate_that_exceeds_the_false_positive_budget_is_rejected():
    baseline = at_budget("baseline", recall=0.386, fpr=0.10)
    trial = at_budget("looser", recall=0.50, fpr=0.40, tokens=10.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "fpr"
    assert "exceeds" in verdict.reason


def test_spending_more_of_the_budget_for_more_incidents_is_accepted():
    """The SMD round: 0.046 -> 0.089 of a 10% budget for recall 0.060 -> 0.187.

    Inside a fixed budget the false-positive rate is permission, not a score. Rejecting any increase
    would reject a detector that finds three times as many faults for four more points of a budget it
    was allowed to spend; the pages cost is real, so the rate stays reported in every table.
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.046, tokens=None)
    trial = at_budget("host-features-3", recall=0.187, fpr=0.089, tokens=None)
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted, verdict.reason
    assert verdict.rule == "quality"


def test_spending_more_of_the_budget_without_finding_more_is_rejected():
    baseline = at_budget("baseline", recall=0.386, fpr=0.046)
    trial = at_budget("noisier", recall=0.386, fpr=0.089, tokens=10.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "fpr"
    assert "without finding more incidents" in verdict.reason


def test_without_an_at_budget_pair_the_raw_rates_still_decide():
    """A measurement that cannot sweep (no probabilities) must not silently pass the label gate."""
    baseline = measure("baseline", recall=0.5, fpr=0.0)
    trial = measure("cheaper", recall=0.4, fpr=0.0, tokens=10.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "recall"


class _Score:
    def __init__(self, probability, label, error=None, predicted=None):
        self.predicted_probability = probability
        self.expected_anomaly = label
        self.predicted_anomaly = bool(label) if predicted is None else predicted
        self.error = error


def test_the_operating_point_sweeps_the_observed_probabilities():
    """The reachable confusion pairs are finite, so the sweep is exact rather than interpolated."""
    scores = [_Score(0.9, True), _Score(0.8, True), _Score(0.7, False),
              _Score(0.6, False), _Score(0.5, False), _Score(0.4, True)]
    tight = operating_point(scores, budget=0.0)
    assert tight["fpr"] == 0.0
    assert tight["threshold"] == 0.8
    assert tight["recall"] == pytest.approx(2 / 3)  # cut at 0.8 keeps both positives, no negatives
    loose = operating_point(scores, budget=1.0)
    assert loose["recall"] == 1.0
    assert loose["threshold"] == 0.4


def test_the_operating_point_reports_nothing_it_cannot_divide():
    """A budget is a ratio to the healthy population; with no healthy windows there is no rate."""
    assert operating_point([], budget=0.1)["recall"] is None
    assert operating_point([_Score(0.9, True)], budget=0.1)["recall"] is None
    assert operating_point([_Score(0.1, False)], budget=0.1)["recall"] is None


def test_scores_that_errored_are_not_counted_as_healthy():
    """A failed window has no probability; treating it as a negative would fake a low FPR."""
    scores = [_Score(0.9, True), _Score(None, False)]
    assert operating_point(scores, budget=1.0)["recall"] is None


def test_each_recording_gets_its_own_cut_and_the_counts_are_added():
    """Two hosts do not share a probability scale, so one cut across both is not a fair sweep.

    On SMD machine-1-6 ranks at AUC 0.674 and machine-2-2 at 0.347 with identical columns; a pooled
    cut mixes a threshold that suits the first with negatives that belong to the second.
    """
    high = [_Score(0.90, True), _Score(0.10, True), _Score(0.50, False)]
    low = [_Score(0.30, True), _Score(0.20, True), _Score(0.05, False)]
    pooled = operating_point(high + low, budget=0.0)
    per_recording = operating_point_by_recording([("a", high), ("b", low)], budget=0.0)
    assert per_recording["recall"] == 0.75
    assert per_recording["fpr"] == 0.0
    assert per_recording["recordings"] == 2
    assert per_recording["pooled"] is False
    assert per_recording["thresholds"] == {"a": 0.9, "b": 0.2}
    assert pooled["recall"] == 0.25


def test_one_recording_is_just_the_plain_sweep():
    scores = [_Score(0.9, True), _Score(0.8, False)]
    only = operating_point_by_recording([("solo", scores)], budget=0.0)
    assert only["recall"] == 1.0
    assert only["recordings"] == 1
    assert only["pooled"] is True
    assert only["thresholds"] == {"solo": 0.9}


def test_an_unsweepable_recording_falls_back_to_the_pooled_sweep_and_says_so():
    """Dropping the recording would hide its false positives; pretending is worse than pooling."""
    healthy_only = [_Score(0.5, False), _Score(0.4, False)]
    with_positives = [_Score(0.9, True), _Score(0.2, False)]
    result = operating_point_by_recording([("a", with_positives), ("b", healthy_only)],
                                          budget=1.0)
    assert result["pooled"] is True
    assert result["recordings"] == 2
    assert result["recall"] == 1.0


def test_a_zero_baseline_bill_does_not_divide_by_zero_and_is_not_a_gain():
    verdict = decide(measure("baseline", tokens=0.0), measure("zero", tokens=0.0), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "gain"


def test_equal_cost_is_not_a_win():
    verdict = decide(measure("baseline"), measure("same", tokens=100.0), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "gain"


def test_a_detection_gain_at_equal_cost_is_a_win():
    """The loop's objective is the bill, but a stalled detector is not "no change" worth keeping.

    On SMD the eight-column catalog scores AUC 0.511 and three columns score 0.674 through the same
    production renderer at the same cost; a rule that only accepts cheaper configurations could never
    record that, which is how a cost loop quietly freezes a diluted detector in place.
    """
    baseline = at_budget("baseline", recall=0.045, tokens=100.0)
    trial = at_budget("host-features-3", recall=0.282, tokens=100.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted, verdict.reason
    assert verdict.rule == "quality"
    assert "0.045 -> 0.282" in verdict.reason


def test_a_detection_gain_and_no_bill_on_either_side_is_still_decidable():
    """Nothing alerts on the domain, so the ledger is empty; the label column must still decide."""
    baseline = at_budget("baseline", recall=0.030, tokens=None)
    trial = at_budget("host-features-3", recall=0.282, tokens=None)
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted, verdict.reason
    assert verdict.rule == "quality"
    assert "cost is undefined" in verdict.reason


def test_a_detection_gain_that_raises_the_bill_past_tolerance_is_rejected():
    baseline = at_budget("baseline", recall=0.045, tokens=100.0)
    trial = at_budget("host-features-3", recall=0.282, tokens=150.0)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "cost"


def test_a_detection_gain_inside_the_cost_tolerance_is_accepted():
    baseline = at_budget("baseline", recall=0.045, tokens=100.0)
    trial = at_budget("host-features-3", recall=0.282, tokens=104.0)
    assert decide(baseline, trial, EvolveRules()).accepted


def test_an_empty_ledger_and_an_unchanged_detector_is_not_a_win():
    baseline = at_budget("baseline", recall=0.030, tokens=None)
    trial = at_budget("host-features-3", recall=0.030, tokens=None)
    verdict = decide(baseline, trial, EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "no-reports"


# --------------------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------------------
def test_applying_a_candidate_leaves_the_original_settings_untouched():
    settings = load_settings()
    before = settings.llm.max_context_events
    candidate = Candidate(name="x", hypothesis="y", section="llm", field="max_context_events", value=3)

    applied = candidate.apply(settings)

    assert applied.llm.max_context_events == 3
    assert settings.llm.max_context_events == before
    assert applied is not settings


def test_every_catalogue_entry_names_a_real_field_and_an_environment_variable():
    settings = load_settings()
    for candidate in catalogue():
        for section_name, field, _value in candidate.changes():
            section = getattr(settings, section_name)
            assert field in {item.name for item in dataclasses.fields(section)}, candidate.name
            assert (section_name, field) in ENV_VARS, candidate.name
        assert candidate.apply(settings) is not settings
        assert candidate.hypothesis, candidate.name


def test_a_companion_change_is_applied_together_and_recorded_together():
    """A fix can need two settings at once; applying half of it ships what nobody measured."""
    settings = load_settings()
    candidate = Candidate(name="joint", hypothesis="two fields", section="llm",
                          field="max_context_events", value=6,
                          also=(("gate", "anomaly_probability", 0.9),))

    applied = candidate.apply(settings)
    assert applied.llm.max_context_events == 6
    assert applied.gate.anomaly_probability == 0.9
    assert settings.gate.anomaly_probability != 0.9

    assert candidate.describe() == "llm.max_context_events = 6, gate.anomaly_probability = 0.9"
    assert [list(change) for change in candidate.changes()] == [
        ["llm", "max_context_events", 6], ["gate", "anomaly_probability", 0.9]]
    assert [change[0] for change in candidate.to_dict()["changes"]] == ["llm", "gate"]


def test_a_companion_change_is_validated_like_the_first_one():
    settings = load_settings()
    typo = Candidate(name="joint", hypothesis="two fields", section="llm",
                     field="max_context_events", value=6,
                     also=(("gate", "anomly_probability", 0.9),))
    with pytest.raises(ValueError):
        typo.apply(settings)


def test_a_joint_change_exports_every_variable_and_can_be_reversed(tmp_path):
    candidate = Candidate(name="joint", hypothesis="two fields", section="digest",
                          field="scoring", value="deviation",
                          also=(("gate", "anomaly_probability", 0.87),))
    settings = load_settings()

    baseline = measure("baseline", recall=0.060, tokens=3349.0)
    trial = measure("joint", recall=0.142, tokens=800.0)
    result = RoundResult(
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
        stream={"source": "test"},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, Verdict(candidate=candidate.name, accepted=True,
                                           rule="quality", reason="recall rose"))],
    )
    written = persist(tmp_path, result)
    payload = json.loads((tmp_path / written["versions"][0]).read_text(encoding="utf-8"))

    assert "BRIAN_DIGEST_SCORING=deviation" in payload["env"]
    assert "BRIAN_GATE_ANOMALY_P=0.87" in payload["env"]
    assert "unset BRIAN_DIGEST_SCORING" in payload["rollback"]
    assert "unset BRIAN_GATE_ANOMALY_P" in payload["rollback"]
    assert len(payload["candidate"]["changes"]) == 2
    assert settings.digest.scoring == "model"


def test_catalogue_names_are_unique_and_that_is_what_makes_the_ledger_joinable():
    names = [candidate.name for candidate in catalogue()]
    assert len(names) == len(set(names))
    assert names


def test_the_routing_candidate_carries_a_veto():
    """It measures best and is the one change a numeric loop would wrongly take."""
    routing = [c for c in catalogue() if c.field == "analyze_actions"]
    assert routing, "the routing candidate was removed without replacing the limit it documents"
    assert all(candidate.veto for candidate in routing)


# --------------------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------------------
def test_grading_walks_the_ladder_one_rung_at_a_time():
    assert grade(has_catalogue=False, has_history=False)["level"] == "L1"
    assert grade(has_catalogue=True, has_history=False)["level"] == "L2"
    # Persisting a version file is not applying it: the ladder's L3 needs the change to take effect
    # without a human, so history alone must not buy a rung.
    assert grade(has_catalogue=True, has_history=True)["level"] == "L2"
    assert grade(has_catalogue=True, has_history=True, applies_changes=True)["level"] == "L3"
    assert grade(has_catalogue=True, has_history=True, applies_changes=True,
                 mutates_mechanism=True)["level"] == "L4"


def test_a_human_in_the_apply_step_is_named_as_the_missing_capability():
    assert "without a human" in " ".join(grade(True, True)["missing_for_next"])


def test_grading_names_what_is_missing():
    assert grade(False, False)["capabilities"] == []
    for level in (grade(False, False), grade(True, False), grade(True, True),
                  grade(True, True, applies_changes=True),
                  grade(True, True, applies_changes=True, mutates_mechanism=True)):
        assert level["missing_for_next"], level["level"]
    for level in (grade(True, False), grade(True, True),
                  grade(True, True, applies_changes=True, mutates_mechanism=True)):
        assert level["capabilities"], level["level"]


def test_the_grader_does_not_claim_general_rsi():
    """L5 requires transfer across domains; nothing here measures that."""
    assert grade(True, True, applies_changes=True, mutates_mechanism=True)["level"] != "L5"


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------
def _round(tmp_path, *, accepted: bool) -> RoundResult:
    candidate = Candidate(name="events-8", hypothesis="the event block dominates the prompt",
                          section="llm", field="max_context_events", value=8)
    trial = measure("events-8", tokens=60.0 if accepted else 100.0)
    verdict = Verdict("events-8", accepted, "cost" if accepted else "gain",
                      "100 -> 60 tokens/report (-40.0%)" if accepted else "no gain")
    return RoundResult(
        started_at="2026-11-18T00:00:00+00:00",
        finished_at="2026-11-18T00:01:00+00:00",
        stream={"service": "checkout-api", "windows": 35, "scored": 30, "anomalous": 8, "events": 900},
        rules=EvolveRules(),
        baseline=measure("baseline"),
        trials=[(candidate, trial, verdict)],
    )


def test_an_accepted_change_writes_a_version_file_that_can_be_reproduced_and_reversed(tmp_path):
    result = _round(tmp_path, accepted=True)
    written = persist(tmp_path, result)

    assert len(written["versions"]) == 1
    payload = json.loads((tmp_path / written["versions"][0]).read_text(encoding="utf-8"))
    assert payload["change"] == "llm.max_context_events = 8"
    assert payload["env"] == "BRIAN_LLM_MAX_CONTEXT_EVENTS=8"
    assert payload["rollback"].startswith("unset BRIAN_LLM_MAX_CONTEXT_EVENTS")
    assert payload["measurement"]["tokens_per_report"] == 60.0


def test_a_rejected_change_writes_no_version_file(tmp_path):
    written = persist(tmp_path, _round(tmp_path, accepted=False))
    assert written["versions"] == []


def test_the_reject_ledger_re_decides_stored_rounds_under_today_rules(tmp_path):
    """A rule change must be able to clear a stored rejection without anyone remembering to."""
    candidate = Candidate(name="events-8", hypothesis="the event block dominates the prompt",
                          section="llm", field="max_context_events", value=8)
    baseline = measure("baseline")
    baseline.fpr = 1.0
    trial = measure("events-8", tokens=60.0)
    trial.fpr = 0.94
    verdict = decide(baseline, trial, EvolveRules())          # rejected by the deploy floor
    assert not verdict.accepted and verdict.rule == "deploy"
    result = RoundResult(
        started_at="2026-11-18T00:00:00+00:00",
        finished_at="2026-11-18T00:01:00+00:00",
        stream={"service": "checkout-api"},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, verdict)],
    )
    persist(tmp_path, result)

    assert "re-judged: " in previously_rejected(tmp_path, EvolveRules())["events-8"]
    assert "events-8" not in previously_rejected(tmp_path, EvolveRules(deploy_fpr_ceiling=1.0))


def test_a_rejudge_reports_a_rule_that_moved_without_moving_the_verdict(tmp_path):
    """Round 7 relabels round 6's two accepts: the verdict stands, the reason is more precise."""
    candidate = Candidate(name="host-scoring-deviation", hypothesis="a deterministic reader ranks "
                          "the digest better than the engine ranks it",
                          section="digest", field="scoring", value="deviation",
                          also=(("gate", "anomaly_probability", 0.87),))
    baseline = measure("baseline")
    baseline.fpr, baseline.recall = 1.0, 1.0
    trial = measure("host-scoring-deviation", tokens=100.0)
    trial.fpr, trial.recall = 0.098, 0.142
    trial.recall_at_budget, trial.fpr_at_budget = 0.142, 0.094
    baseline.recall_at_budget, baseline.fpr_at_budget = 0.060, 0.046
    stored = Verdict("host-scoring-deviation", True, "quality", "recall went up for the same bill")
    result = RoundResult(
        started_at="2026-11-18T00:00:00+00:00",
        finished_at="2026-11-18T00:01:00+00:00",
        stream={"source": "SMD"},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, stored)],
    )
    written = persist(tmp_path, result)

    replay = rejudge(Path(written["round"]), EvolveRules())

    assert replay.changed == []
    assert replay.relabelled == [("host-scoring-deviation", "quality", "operational")]
    table = format_rejudge(replay)
    assert "same verdict, new rule" in table
    assert "still accepted, but the rule that decides it moved quality -> operational" in table


def test_history_is_append_only_and_survives_a_second_round(tmp_path):
    persist(tmp_path, _round(tmp_path, accepted=True))
    persist(tmp_path, _round(tmp_path, accepted=False))

    rows = history(tmp_path)
    assert len(rows) == 2
    assert rows[0]["accepted"] == ["events-8"]
    assert rows[1]["accepted"] == []
    assert rows[1]["rejected"] == ["events-8"]


def test_history_returns_the_measurements_not_only_the_index(tmp_path):
    """`--history` prints the tokens; an index line alone cannot say what was measured."""
    persist(tmp_path, _round(tmp_path, accepted=True))

    row = history(tmp_path)[0]
    assert row["finished_at"] == "2026-11-18T00:01:00+00:00"
    assert row["baseline"]["tokens_per_report"] == 100.0
    assert row["trials"][0]["measurement"]["tokens_per_report"] == 60.0


def test_a_truncated_round_file_does_not_hide_the_round_from_the_index(tmp_path):
    written = persist(tmp_path, _round(tmp_path, accepted=True))
    (tmp_path / written["round"]).write_text("{not json", encoding="utf-8")

    rows = history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["accepted"] == ["events-8"]
    assert rows[0].get("trials") is None


def test_a_rejection_is_remembered_so_the_next_round_does_not_rediscover_it(tmp_path):
    persist(tmp_path, _round(tmp_path, accepted=False))

    rejected = previously_rejected(tmp_path)
    assert rejected["events-8"] == "on checkout-api: no gain"


def test_a_later_acceptance_on_another_stream_clears_the_rejection(tmp_path):
    """A verdict is about a dataset; the latest one wins, or the ledger remembers the wrong answer."""
    persist(tmp_path, _round(tmp_path, accepted=False))
    persist(tmp_path, _round(tmp_path, accepted=True))

    assert "events-8" not in previously_rejected(tmp_path)


def test_versions_are_numbered_across_rounds(tmp_path):
    first = persist(tmp_path, _round(tmp_path, accepted=True))
    second = persist(tmp_path, _round(tmp_path, accepted=True))

    assert first["versions"][0].endswith("v001.json")
    assert second["versions"][0].endswith("v002.json")


def test_an_empty_directory_reports_no_history_rather_than_failing(tmp_path):
    assert history(tmp_path) == []
    assert previously_rejected(tmp_path) == {}


# --------------------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------------------
def test_the_table_shows_the_baseline_the_trial_and_the_reason(tmp_path):
    text = format_round(_round(tmp_path, accepted=True))
    assert "baseline" in text
    assert "events-8" in text
    assert "+" in text
    assert "Interpretation" in text
    assert "not a claim" in text


def test_a_round_with_no_candidates_says_so(tmp_path):
    result = _round(tmp_path, accepted=True)
    result.trials = []
    assert "no candidates were tried" in format_round(result)


def test_a_verdict_reason_wraps_under_its_column_instead_of_one_character_per_line(tmp_path):
    """The reason is a paragraph, so the table has to hang it — not print it vertically.

    `_wrap` returned one joined string until round 18 and a caller iterated it, which printed
    "w\\nh\\ny" in the proposal block. A wrap helper is the kind of thing whose bug looks like a
    formatting preference, so the assertion is on the shape of the output, not on a helper's type.
    """
    result = _round(tmp_path, accepted=True)
    candidate, measurement, verdict = result.trials[0]
    verdict.reason = " ".join(["this reason is long enough to need more than one line"] * 4)
    text = format_round(result)
    assert "\n".join(verdict.reason.split()) not in text  # the paragraph was wrapped
    rows = [line for line in text.splitlines() if "this reason" in line or "more than" in line]
    assert rows, "the reason did not reach the table"
    assert all(len(row) > 20 for row in rows), rows


def test_the_table_records_which_proposal_rule_asked_the_question(tmp_path):
    result = _round(tmp_path, accepted=True)
    result.proposal = {"mode": "expand", "reason": "the last round accepted events-8",
                       "space": "10 field(s), 13 bounded, at most 8 proposals",
                       "space_fingerprint": "dccb2a7b0b81", "source": "SMD",
                       "candidates": ["events-8-4"]}
    text = format_round(result)
    assert "proposal" in text
    assert "expand" in text
    assert "dccb2a7b0b81" in text
    assert "the last round accepted events-8" in text


# --------------------------------------------------------------------------------------
# evidence guardrail
# --------------------------------------------------------------------------------------
def test_coverage_of_an_empty_history_is_complete_not_undefined():
    assert evidence_floor(evidence_coverage([], 8)) == 1.0


def test_a_cap_larger_than_the_history_removes_nothing(sample_events):
    coverage = evidence_coverage(sample_events, max_events=len(sample_events))
    assert evidence_floor(coverage) == 1.0


@pytest.fixture
def sample_events():
    from brian.demo import DEFAULT_SEQUENCE, flatten, generate

    windows = generate(sequence=DEFAULT_SEQUENCE[:1], warmup_minutes=2, seed=11)
    return flatten(windows)


# --------------------------------------------------------------------------------------
# a dataset on which nothing alerts (RCAEval, and SMD through the host profile)
# --------------------------------------------------------------------------------------
def test_a_run_without_reports_is_judged_on_its_reports_not_crashed():
    """`tokens_per_report` is None when nothing alerted. That must become a verdict.

    On RCAEval the detector stayed silent at the shipped threshold, so both sides of the
    comparison had no reports at all. The loop crashing there would have hidden the one
    finding that mattered — that the bottleneck was detection, not the cost policy.
    """
    verdict = decide(measure("baseline", tokens=None), measure("candidate", tokens=None),
                     EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "no-reports"
    assert "no report" in verdict.reason


def test_a_missing_report_on_one_side_alone_blocks_the_comparison():
    verdict = decide(measure("baseline"), measure("candidate", tokens=None), EvolveRules())
    assert not verdict.accepted
    assert verdict.rule == "no-reports"
    assert "candidate" in verdict.reason


def test_the_table_states_an_undefined_cost_as_n_a(tmp_path):
    result = _round(tmp_path, accepted=False)
    result.baseline = measure("baseline", tokens=None)
    result.trials = [(result.trials[0][0], measure("events-8", tokens=None), result.trials[0][2])]

    text = format_round(result)
    assert "n/a" in text
    assert "None" not in text


# --------------------------------------------------------------------------------------
# the base configuration a stream file carries
# --------------------------------------------------------------------------------------
def test_a_stream_file_can_set_the_base_configuration_before_any_candidate():
    settings = load_settings()
    tuned = apply_overrides(settings, {"gate": {"anomaly_probability": 0.5},
                                       "digest": {"profile": "host"}})

    assert tuned.gate.anomaly_probability == 0.5
    assert tuned.digest.profile == "host"
    assert settings.gate.anomaly_probability != 0.5      # the caller's settings are untouched


def test_a_round_is_recorded_even_when_no_change_is_applied(tmp_path):
    """A record that only exists for accepted changes lets the next round re-propose a dead end."""
    written = persist(tmp_path, _round(tmp_path, accepted=True), write_versions=False)

    assert written["versions"] == []
    assert (tmp_path / written["round"]).exists()
    assert len(history(tmp_path)) == 1
    assert not list((tmp_path / "var" / "evolve" / "versions").glob("v*.json"))


def test_the_ledger_separates_accepted_from_applied(tmp_path):
    """'Accepted' and 'integrated' are different facts and the ladder grades them separately."""
    persist(tmp_path, _round(tmp_path, accepted=True), write_versions=False)
    row = history(tmp_path)[0]

    assert row["accepted"] == ["events-8"]
    assert row["applied"] == []


def test_an_override_naming_an_unknown_section_or_field_is_rejected():
    """A typo must not silently leave the domain on the shipped thresholds."""
    settings = load_settings()
    with pytest.raises(ValueError, match="unknown settings section"):
        apply_overrides(settings, {"gateway": {"anomaly_probability": 0.5}})
    with pytest.raises(ValueError, match="unknown field"):
        apply_overrides(settings, {"gate": {"anomaly_probabilty": 0.5}})


# --------------------------------------------------------------------------------------
# the state ceiling: what a trivial reader of the same digest recovers
# --------------------------------------------------------------------------------------
def _cpu_stream(*, spikes: bool, count: int = 40, name: str = "s", scale: float = 0.50,
                ) -> Stream:
    """One metric event per minute: healthy windows flat, anomalous windows either a
    single-window spike (`spikes=True`) or a sustained step (`spikes=False`).

    A sustained step is deliberately available: a rolling median follows it, and the ceiling here
    inherits that blindness — which is the reason the tracker keeps a second, longer horizon.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = []
    for index in range(count):
        when = start + timedelta(minutes=index)
        anomalous = (index >= 12 and index % 4 == 0) if spikes else index >= count - 8
        event = Event(
            event_time=when,
            service="svc-a",
            kind="metric",
            payload={"cpu_util": 0.95 * scale if anomalous else 0.50 * scale,
                     "memory_util": 0.4 * scale},
            event_id="%s-e%d" % (name, index),
        )
        windows.append(LabeledWindow(
            service="svc-a",
            window_start=when,
            scenario="cpu" if anomalous else "normal",
            events=[event],
            expected={"is_anomaly": anomalous},
            scored=True,
        ))
    return Stream(name=name, windows=windows, meta={})


def _ceiling_settings():
    settings = load_settings()
    window = dataclasses.replace(settings.window, size_s=60, slide_s=60, min_events=1,
                                 baseline_windows=6, baseline_windows_long=12)
    return settings.with_overrides(window=window)


def test_the_ceiling_is_what_a_trivial_reader_of_the_digest_recovers():
    """A spike on the loudest rendered ratio separates the classes, without any model call."""
    ceiling = state_ceiling(_ceiling_settings(), [_cpu_stream(spikes=True)])

    assert ceiling["by_stream"]["s"] == pytest.approx(1.0)
    assert ceiling["windows"] == 40


def test_a_sustained_fault_is_absorbed_by_the_rolling_baseline_and_the_ceiling_says_so():
    """The reader inherits the tracker's blind spot instead of hiding it.

    A fault that outlasts the short window becomes the new median, so `max |ratio - 1|` collapses
    right where the fault is worst. That is exactly why the tracker carries a long horizon, and a
    ceiling of 1.0 here would mean the measurement, not the data, was wrong.
    """
    ceiling = state_ceiling(_ceiling_settings(), [_cpu_stream(spikes=False)])

    assert 0.5 < ceiling["by_stream"]["s"] < 1.0


def test_a_recording_with_one_class_has_no_ceiling_rather_than_a_perfect_one():
    stream = _cpu_stream(spikes=True, count=12)          # the first 12 windows are all healthy
    stream.windows = [dataclasses.replace(window, expected={"is_anomaly": False})
                      for window in stream.windows]

    ceiling = state_ceiling(_ceiling_settings(), [stream])

    assert ceiling["by_stream"]["s"] is None


def test_a_recording_with_no_events_has_no_ceiling():
    stream = _cpu_stream(spikes=True, count=0)

    ceiling = state_ceiling(_ceiling_settings(), [stream])

    assert ceiling["by_stream"]["s"] is None
    assert ceiling["windows"] == 0


def test_the_ceiling_is_per_recording_because_two_hosts_do_not_share_a_scale():
    """Pooling the scores would measure the scale difference between the hosts, not the signal."""
    low = _cpu_stream(spikes=True, name="low", scale=0.01)
    high = _cpu_stream(spikes=True, name="high", scale=1.0)

    ceiling = state_ceiling(_ceiling_settings(), [low, high])

    assert set(ceiling["by_stream"]) == {"low", "high"}
    assert "pooled" not in ceiling
    assert all(value is not None for value in ceiling["by_stream"].values())


def test_the_ceiling_is_measured_on_the_text_as_well_as_the_fields(tmp_path):
    """A field reader can see numbers the rendering dropped; the engine cannot.

    If the two ceilings disagree, the gap belongs to the renderer, and pitching the engine against the
    field ceiling would be asking it to read information its input never carried.
    """
    stream = _cpu_stream(spikes=True)

    ceiling = state_ceiling(_ceiling_settings(), [stream])

    assert set(ceiling["text_by_stream"]) == set(ceiling["by_stream"])
    for name, text in ceiling["text_by_stream"].items():
        fields = ceiling["by_stream"][name]
        if text is None or fields is None:
            continue
        # Not an ordering: the text reader parses printed ratios and the field reader prefers the long
        # horizon, so either can be a hair higher (the demo stream has text 1.000 against fields 0.995).
        # What must hold is that the two readers of the same windows agree, because a large gap would
        # mean the renderer dropped information the ceiling was crediting to the state.
        assert abs(text - fields) <= 0.10, (name, text, fields)


def test_a_window_without_a_ratio_scores_zero_rather_than_none():
    """The first windows render 'No baseline is available yet', which is not evidence of an incident."""
    assert text_deviation("No baseline is available yet for this host (fewer than 3 prior windows).") is None
    assert text_deviation("CPU 0.19 (baseline 0.17, 1.07x).") == pytest.approx(0.07)


def test_the_text_reader_takes_the_loudest_ratio_it_can_see():
    text = "CPU 0.19 (baseline 0.17, 1.07x). | load 1m 0.10 (baseline 0.07, 1.32x)."

    assert text_deviation(text) == pytest.approx(0.32)


def test_the_engine_column_is_what_the_model_did_with_the_same_text(tmp_path):
    """The ceiling is only useful next to the number it accuses.

    Round 10 established the two readers agree on SMD (0.827/0.827), so the shortfall belongs to the
    model; printing the model's own AUC on the same line is what makes that read off the table instead
    of out of a document.
    """
    result = _round(tmp_path, accepted=True)
    result.baseline.auc_by_recording = {"machine-1-6": 0.5166, "machine-2-2": 0.2986}
    result.ceiling = {
        "by_stream": {"machine-1-6": 0.8269, "machine-2-2": 0.5972},
        "text_by_stream": {"machine-1-6": 0.8269, "machine-2-2": 0.5981},
        "windows": 948,
    }

    table = format_round(result)

    assert "engine" in table
    assert "0.517" in table and "0.299" in table
    assert "is the renderer's; a gap with the third is the model's" in table


def test_a_measurement_carries_the_engine_auc_per_recording():
    """A rate without its sample size is a rumour; an AUC per recording is the same rule."""
    measurement = measure(auc_by_recording={"machine-1-6": 0.5166})

    payload = measurement.to_dict()

    assert payload["auc_by_recording"] == {"machine-1-6": 0.5166}
    assert Measurement.from_dict(json.loads(json.dumps(payload))).auc_by_recording ==         {"machine-1-6": 0.5166}


def test_the_table_shows_the_ceiling_beside_the_candidates(tmp_path):
    """A round table that hides the ceiling invites attributing the state's signal to the model."""
    result = _round(tmp_path, accepted=True)
    result.ceiling = {
        "by_stream": {"machine-1-6": 0.8269, "machine-2-2": None},
        "text_by_stream": {"machine-1-6": 0.7901, "machine-2-2": None},
        "windows": 948,
    }

    table = format_round(result)

    assert "state ceiling" in table
    assert "machine-1-6" in table and "0.827" in table
    assert "0.790" in table
    assert "n/a" in table
    assert "reads only the characters the engine" in table



# --------------------------------------------------------------------------------------
# the deployment floor
# --------------------------------------------------------------------------------------
def test_a_configuration_that_ships_a_threshold_paging_on_most_windows_is_rejected():
    """Round 5's trap: the at-budget curve is swept by the loop, so it cannot see the shipped bar.

    `host-scoring-deviation` alone measured recall@b 0.060 -> 0.142 and was accepted, while the same
    table printed "shipped threshold recall 0.993 fpr 0.935". A configuration that pages on 93.5% of a
    stream is not deployable, whatever its at-budget numbers say.
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    trial = at_budget("host-scoring-deviation", recall=0.142, fpr=0.094)
    trial.fpr = 0.935

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "deploy"
    assert "94% of windows" in verdict.reason
    assert "deploy ceiling" in verdict.reason


def test_the_deploy_ceiling_is_not_a_comparison_with_the_baseline():
    """A baseline that alerts on everything is a finding about the deployment, not a licence."""
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0
    trial = at_budget("trial", recall=0.142, fpr=0.094)
    trial.fpr = 0.90

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "deploy"


def test_a_deployable_configuration_passes_the_floor():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    trial = at_budget("host-scoring-deviation", recall=0.142, fpr=0.094)
    trial.fpr = 0.098

    assert decide(baseline, trial, EvolveRules()).accepted


def test_the_ceiling_is_a_rule_not_a_constant():
    trial = at_budget("trial", recall=0.142, fpr=0.094)
    trial.fpr = 0.60  # what 0.87 on a domain whose answers all land near 0.35 does

    assert decide(at_budget("baseline", recall=0.060, fpr=0.046), trial,
                  EvolveRules()).rule == "deploy"
    assert decide(at_budget("baseline", recall=0.060, fpr=0.046), trial,
                  EvolveRules(deploy_fpr_ceiling=0.70)).accepted


def test_the_table_says_the_deployed_configuration_already_breaks_the_ceiling():
    result = _round(None, accepted=True)
    result.baseline.fpr = 1.0

    table = format_round(result)

    assert "already alerts on 100% of windows" in table
    assert "above the 50% deploy ceiling" in table
    assert "not a" in table and "licence" in table


# --------------------------------------------------------------------------------------
# replaying a stored round
# --------------------------------------------------------------------------------------
def test_a_single_field_candidate_round_trips_through_its_record():
    candidate = next(entry for entry in catalogue() if len(entry.changes()) == 1)

    restored = Candidate.from_dict(candidate.to_dict())

    assert restored == candidate


def test_a_patch_candidate_round_trips_with_every_companion_change():
    candidate = next(entry for entry in catalogue() if entry.name == "host-scoring-deviation")
    assert len(candidate.changes()) == 3

    restored = Candidate.from_dict(candidate.to_dict())

    assert restored == candidate
    assert restored.describe() == candidate.describe()


def test_a_record_from_before_patches_still_loads():
    """Rounds recorded by the single-field shape are the ones most worth re-judging."""
    restored = Candidate.from_dict({
        "name": "events-8", "hypothesis": "h", "section": "llm",
        "field": "max_context_events", "value": 8, "veto": None,
    })

    assert restored.changes() == (("llm", "max_context_events", 8),)


def test_a_measurement_round_trips_without_inventing_numbers():
    original = measure("trial", tokens=None)
    original.recall_at_budget = None
    original.windows = 0

    restored = Measurement.from_dict(original.to_dict())

    assert restored.tokens_per_report is None
    assert restored.recall_at_budget is None
    assert restored.candidate == "trial"
    assert Measurement.from_dict({}).tokens_per_report is None


def test_rejudging_a_stored_round_agrees_with_itself_under_the_recorded_rules(tmp_path):
    """Same measurements, same rules, same verdict — the replay has to be a function of the files."""
    result = _round(tmp_path, accepted=True)
    result.baseline.fpr = 0.94
    result.trials[0][1].fpr = 0.94
    written = persist(tmp_path, result)

    replay = rejudge(Path(written["round"]), EvolveRules(deploy_fpr_ceiling=1.0))

    assert replay.changed == []
    assert "no verdict changed" in format_rejudge(replay)


def test_rejudging_a_stored_round_shows_the_verdict_the_new_rule_moves(tmp_path):
    result = _round(tmp_path, accepted=True)
    result.baseline.fpr = 1.0    # the deployed configuration alerts on everything
    result.trials[0][1].fpr = 0.94  # the candidate changes that, and is still far above the ceiling
    written = persist(tmp_path, result)

    replay = rejudge(Path(written["round"]), EvolveRules())

    assert replay.changed == ["events-8"]
    table = format_rejudge(replay)
    assert "-> reject" in table
    assert "now  rejected" in table
    assert "was accepted" in table
    assert "only the rules moved" in table


def test_a_threshold_change_is_judged_by_what_would_be_deployed():
    """The one column that can see a threshold move: the recorded configuration itself.

    A threshold-only candidate leaves the at-budget curve identical, because the loop sweeps its own
    cut. Before this path existed the catalogue could propose thresholds forever and every round would
    report "no difference".
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0     # the shipped threshold alerts on every window
    baseline.recall = 1.0
    trial = at_budget("anomaly-p-87", recall=0.060, fpr=0.046)   # identical at-budget pair
    trial.fpr = 0.098
    trial.recall = 0.142

    verdict = decide(baseline, trial, EvolveRules())

    assert verdict.accepted, verdict.reason
    assert verdict.rule == "operational"
    assert "1000 alerts per 1,000" in verdict.reason
    assert "98 per 1,000" in verdict.reason


def test_a_deployability_fix_does_not_license_an_unbounded_bill():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046, tokens=100.0)
    baseline.fpr, baseline.recall = 1.0, 1.0
    trial = at_budget("anomaly-p-87", recall=0.060, fpr=0.046, tokens=300.0)
    trial.fpr, trial.recall = 0.098, 0.142

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "gain"


def test_a_threshold_that_silences_the_detector_is_not_deployable():
    """Fewer alerts is not the goal: the recorded cut must also beat the cut the budget rule picks."""
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0
    baseline.recall = 1.0
    trial = at_budget("anomaly-p-40", recall=0.060, fpr=0.046)
    trial.fpr = 0.027
    trial.recall = 0.007          # below the budget cut's recall, so it is worse than doing nothing

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "operational"
    assert "0.007 against 0.060 at the 10% budget" in verdict.reason


def test_inheriting_the_broken_configuration_is_not_an_operational_win():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr, baseline.recall = 1.0, 1.0
    trial = at_budget("events-8", recall=0.060, fpr=0.046, tokens=60.0)
    trial.fpr, trial.recall = 1.0, 1.0

    verdict = decide(baseline, trial, EvolveRules())

    assert verdict.accepted and verdict.rule == "cost"


def test_a_saving_bought_by_paging_on_fewer_failures_is_rejected():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr, baseline.recall = 1.0, 1.0
    trial = at_budget("anomaly-p-40", recall=0.060, fpr=0.046, tokens=60.0)
    trial.fpr, trial.recall = 0.027, 0.007

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "silence"
    assert "0.007" in verdict.reason
    assert "40% smaller bill with silence" in verdict.reason


def test_a_threshold_that_pages_on_fewer_failures_than_the_free_cut_is_rejected():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr, baseline.recall = 1.0, 1.0
    trial = at_budget("anomaly-p-40", recall=0.060, fpr=0.046)
    trial.fpr, trial.recall = 0.027, 0.007

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "operational"
    assert "0.007 against 0.060" in verdict.reason


def test_the_silence_rule_names_the_failures_it_stops_paging_on():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046, tokens=100.0)
    baseline.recall = 0.900
    trial = at_budget("budget-cut", recall=0.060, fpr=0.046, tokens=40.0)
    trial.recall = 0.100

    verdict = decide(baseline, trial, EvolveRules())

    assert verdict.rule == "silence"
    assert "0.900 -> 0.100" in verdict.reason
    assert "10% false-positive budget" in verdict.reason


def test_deployable_is_a_predicate_over_the_recorded_numbers():
    rules = EvolveRules()

    def shipped(recall: float, fpr: float, budget_recall: float = 0.060) -> Measurement:
        m = at_budget("x", recall=budget_recall, fpr=0.05, raw_recall=recall)
        m.fpr = fpr
        return m

    assert deployable(shipped(0.142, 0.098), rules)
    assert not deployable(shipped(0.142, 0.60), rules)              # above the ceiling
    assert not deployable(shipped(0.007, 0.027), rules)             # worse than the budget cut
    empty = shipped(0.142, 0.098)
    empty.recall = None                                             # nothing was scored
    empty.fpr = None
    assert not deployable(empty, rules)
    assert alerts_per_1000(shipped(0.1, 0.25)) == 250.0
    assert alerts_per_1000(empty) is None


def test_the_shipped_block_reports_alerts_per_thousand_windows():
    result = _round(tmp_path=None, accepted=True)
    text = format_round(result)
    assert "fp/1k" in text


def test_inheriting_the_deployed_threshold_is_not_a_new_deployability_claim():
    """On SMD the shipped 0.33 alerts on every window, so a prompt-budget cut inherits fpr 1.000.

    Blaming a candidate for a defect it does not touch would reject every cost change on a domain whose
    deployment is already broken. The inherited defect is reported, not charged.
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0
    baseline.recall = 1.0
    trial = at_budget("events-8", recall=0.060, fpr=0.046, tokens=60.0)
    trial.fpr = 1.0
    trial.recall = 1.0

    verdict = decide(baseline, trial, EvolveRules())

    assert verdict.accepted, verdict.reason
    assert verdict.rule == "cost"


def test_a_hair_thin_move_in_the_shipped_rate_still_inherits_the_deployment():
    """Round 9: a rendering candidate moved the shipped rate from 1.000 to 0.999.

    Exact equality called that a new deployability claim, so the deploy floor rejected it — hiding that
    the label column had already rejected it for losing recall at the budget. Half a percent of windows is
    not a different deployment, so the candidate inherits the defect and is judged on its labels.
    """
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0
    baseline.recall = 1.0
    trial = at_budget("ratio-floor", recall=0.045, fpr=0.049)
    trial.fpr = 0.999
    trial.recall = 1.0

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "recall", verdict.reason
    assert "0.060 -> 0.045" in verdict.reason


def test_the_inheritance_tolerance_is_a_rule_not_a_constant():
    baseline = at_budget("baseline", recall=0.060, fpr=0.046)
    baseline.fpr = 1.0
    baseline.recall = 1.0
    trial = at_budget("ratio-floor", recall=0.045, fpr=0.049)
    trial.fpr = 0.999
    trial.recall = 1.0

    verdict = decide(baseline, trial, EvolveRules(deploy_fpr_tolerance=0.0))

    assert not verdict.accepted
    assert verdict.rule == "deploy", verdict.reason

def test_the_fingerprint_follows_the_rule_values_not_the_clock():
    assert rules_fingerprint(EvolveRules()) == rules_fingerprint(EvolveRules())
    assert len(rules_fingerprint(EvolveRules())) == 12
    assert rules_fingerprint(EvolveRules()) != rules_fingerprint(EvolveRules(min_gain=0.20))


def test_a_stored_round_records_the_ruleset_that_judged_it(tmp_path):
    result = _round(tmp_path, accepted=True)
    written = persist(tmp_path, result)
    payload = json.loads(Path(written["round"]).read_text(encoding="utf-8"))

    assert payload["rules_hash"] == rules_fingerprint(result.rules)
    ledger = (tmp_path / "var" / "evolve" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(ledger[-1])["rules_hash"] == rules_fingerprint(result.rules)


def test_an_audit_of_a_ledger_under_its_own_rules_moves_nothing(tmp_path):
    persist(tmp_path, _round(tmp_path, accepted=True))

    result = audit(tmp_path)

    assert result.rounds == 1
    assert result.trials == 1
    assert result.moved == 0
    assert result.entries == []
    assert "every recorded verdict survives today's rules." in format_audit(result)


def test_an_audit_names_the_stored_verdict_a_stricter_rule_would_flip(tmp_path):
    persist(tmp_path, _round(tmp_path, accepted=True))

    result = audit(tmp_path, EvolveRules(min_gain=0.50))

    assert result.moved == 1
    moving = result.entries[0]["changed"][0]
    assert moving["candidate"] == "events-8"
    assert moving["was"] == "cost" and moving["now"] == "gain"
    assert moving["was_accepted"] is True and moving["now_accepted"] is False
    table = format_audit(result)
    assert "events-8 cost -> gain" in table
    assert "1 verdict(s) would be decided differently today" in table


def test_an_audit_counts_a_rule_relabel_as_something_other_than_a_move(tmp_path):
    candidate = Candidate(name="events-8", hypothesis="smaller prompt",
                          section="llm", field="max_context_events", value=8)
    baseline = at_budget("baseline", recall=0.060, fpr=0.046, tokens=3350.0)
    baseline.fpr = 1.0
    baseline.recall = 1.0
    trial = at_budget("events-8", recall=0.187, fpr=0.089, tokens=3000.0)
    trial.fpr = 0.291
    trial.recall = 0.291
    stored = Verdict("events-8", True, "quality", "found more incidents")
    result = RoundResult(
        started_at="2026-11-18T00:00:00+00:00",
        finished_at="2026-11-18T00:01:00+00:00",
        stream={"service": "SMD"},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, stored)],
    )
    persist(tmp_path, result)

    replayed = audit(tmp_path)

    assert replayed.moved == 0
    assert replayed.relabelled >= 1
    table = format_audit(replayed)
    assert "same verdict" in table
    assert "no verdict moves" in table


def test_an_audit_reports_rounds_whose_objective_no_longer_exists(tmp_path):
    result = _round(tmp_path, accepted=True)
    written = persist(tmp_path, result)
    path = Path(written["round"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rules_hash"] = "000000000000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    replayed = audit(tmp_path, EvolveRules(min_gain=0.50))

    assert replayed.stale == 1
    assert "measured under an objective that no longer exists" in format_audit(replayed)


def test_an_audit_of_an_empty_ledger_is_a_statement_about_the_rules(tmp_path):
    result = audit(tmp_path / "nothing-here")

    assert result.rounds == 0
    assert result.entries == []
    assert result.fingerprint == rules_fingerprint(EvolveRules())


def test_the_audit_is_a_check_because_it_can_exit_nonzero(tmp_path, monkeypatch):
    from brian import cli

    persist(tmp_path, _round(tmp_path, accepted=True))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit"]) == 0
    assert cli.main(["evolve", "--audit", "--min-gain", "0.50"]) == 0
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
    assert cli.main(["evolve", "--audit", "--strict", "--min-gain", "0.50"]) == 1

def test_the_grader_names_promotion_as_a_capability_without_claiming_L3():
    result = grade(has_catalogue=True, has_history=True, promotes=True)

    # Promotion (state file + provenance + automatic revert) is real work the loop does alone, but no
    # running process picks the change up, so the rung does not move.
    assert result["level"] == "L2"
    assert any("deployment state file" in line for line in result["capabilities"])
    assert result["missing_for_next"] == ["a running process that picks the change up without a restart"]


def test_the_grader_still_asks_for_implementation_when_nothing_is_promoted():
    result = grade(has_catalogue=True, has_history=True)

    assert result["missing_for_next"] == [
        "autonomous implementation: applying an accepted change without a human"]


def test_promotion_alone_does_not_earn_L3():
    assert grade(has_catalogue=True, has_history=True, promotes=True)["level"] == "L2"
    assert grade(has_catalogue=True, has_history=True, promotes=True,
                 applies_changes=True)["level"] == "L3"


def _service_domain(*, recall: float, found: int, tokens: float, reports: int,
                    windows: int = 543, faults: int = 279) -> Measurement:
    """A measurement shaped like round 19's RCAEval round: faults known, budget cut known."""
    m = at_budget("x", recall=recall, fpr=0.0, raw_recall=recall, tokens=tokens)
    m.reports = reports
    m.windows = windows
    m.tokens_per_window = tokens * reports / windows
    m.confusion = {"tp": found, "fp": 0, "tn": windows - faults, "fn": faults - found}
    return m


def test_the_bill_per_incident_is_the_other_denominator():
    """Round 19: 5.3% more tokens per report bought 12.6x more found failures.

    Per report the reader looked more expensive; per incident it was an order of magnitude cheaper.
    """
    baseline = _service_domain(recall=0.068, found=19, tokens=1618, reports=228)
    trial = _service_domain(recall=0.860, found=240, tokens=1703, reports=240)

    before = tokens_per_incident(baseline)
    after = tokens_per_incident(trial)

    assert before is not None and after is not None
    assert before > after * 10
    verdict = decide(baseline, trial, EvolveRules())
    assert verdict.accepted
    assert verdict.rule == "quality"
    assert "bill per incident" in verdict.reason


def test_a_rise_in_both_bills_is_still_a_rejection():
    """A quality gain that costs more per report *and* more per incident is not a Pareto move."""
    baseline = _service_domain(recall=0.500, found=140, tokens=1618, reports=228)
    trial = _service_domain(recall=0.540, found=151, tokens=3000, reports=300)
    trial.tokens_per_window = 3000.0 / 4.0

    verdict = decide(baseline, trial, EvolveRules())

    assert not verdict.accepted
    assert verdict.rule == "cost"
    assert "does not fall" in verdict.reason


def test_the_incident_bill_is_none_when_there_is_no_fault_to_divide_by():
    assert tokens_per_incident(at_budget("x", recall=0.5, raw_recall=0.5)) is None
    bare = at_budget("x", recall=0.5, raw_recall=0.5)
    bare.confusion = {"tp": 0, "fp": 0, "tn": 10, "fn": 0}
    assert tokens_per_incident(bare) is None


# --------------------------------------------------------------------------------------
# the bar a deployment can compute
# --------------------------------------------------------------------------------------
def test_the_label_free_cut_is_the_quantile_a_deployment_could_read_off_its_own_traffic():
    scores = [0.1, 0.2, 0.5, 0.9, 0.95, 0.99, 0.995, 0.999, 0.9995, 0.9999]
    assert label_free_cut(scores, 0.0) == pytest.approx(0.9999)
    assert label_free_cut(scores, 1.0) == pytest.approx(0.1)
    ten = label_free_cut(scores, 0.10)
    twenty = label_free_cut(scores, 0.20)
    assert ten is not None and twenty is not None
    assert twenty < ten  # a looser alert rate needs a lower bar, or it would not alert more
    assert label_free_cut([], 0.1) is None


def test_the_label_free_rates_are_counted_against_each_recordings_own_scores():
    """One bar per recording: two hosts do not share a scale, and neither do their bars."""
    high = [_Score(0.95, True), _Score(0.90, True), _Score(0.10, False), _Score(0.20, False)]
    low = [_Score(0.30, True), _Score(0.25, True), _Score(0.05, False), _Score(0.01, False)]
    groups = [("high", high), ("low", low)]

    rates = free_cut_rates(groups, budget=0.25)

    assert rates["recall"] == pytest.approx(1.0)
    assert rates["fpr"] == pytest.approx(0.0)
    # one pooled bar cannot serve both scales: it sits above the low recording's positives
    pooled = free_cut_rates([("all", high + low)], budget=0.25)
    assert pooled["recall"] is not None and pooled["recall"] < rates["recall"]


def test_the_deployability_reference_is_the_bar_a_deployment_can_compute():
    """Round 20: the guard measured against the label-fitted bound, so it rejected the honest form.

    The shipped configuration (recall 0.211) is 6.6% short of the bar a deployment can compute (0.226) and
    inside the tolerance the rules allow. Against the fitted bound (0.860) the same configuration is 75%
    short, which is what the loop reported before the free-cut column existed.
    """
    measurement = at_budget("reader", recall=0.211, fpr=0.0, raw_recall=0.211)
    measurement.recall_at_budget = 0.860
    measurement.recall_at_free_cut = 0.226
    measurement.fpr_at_free_cut = 0.130

    assert own_cut_shortfall(measurement) == pytest.approx((0.226 - 0.211) / 0.226, abs=1e-9)
    assert own_cut_shortfall(measurement) < EvolveRules().operational_tolerance
    measurement.recall_at_free_cut = None  # an old record falls back to the bound it recorded
    assert own_cut_shortfall(measurement) == pytest.approx((0.860 - 0.211) / 0.860, abs=1e-9)
    assert own_cut_shortfall(measurement) > EvolveRules().operational_tolerance


def test_a_measurement_remembers_both_bars():
    original = at_budget("reader", recall=0.2, raw_recall=0.2)
    original.recall_at_free_cut = 0.226
    original.fpr_at_free_cut = 0.130

    restored = Measurement.from_dict(original.to_dict())

    assert restored.recall_at_free_cut == pytest.approx(0.226)
    assert restored.fpr_at_free_cut == pytest.approx(0.130)


def test_the_table_prints_the_bar_a_deployment_can_compute_next_to_the_fitted_one():
    baseline = measure("baseline", recall=0.405, tokens=1618.0)
    baseline.recall_at_budget = 0.068
    baseline.recall_at_free_cut = 0.136
    baseline.fpr_at_free_cut = 0.130
    trial = measure("deviation-bar-free", recall=0.211, tokens=1703.0)
    trial.recall_at_budget = 0.860
    trial.recall_at_free_cut = 0.226
    trial.fpr_at_free_cut = 0.0
    result = RoundResult(started_at="t", finished_at="t", stream={}, rules=EvolveRules(),
                         baseline=baseline,
                         trials=[(Candidate("deviation-bar-free", "h", "digest", "scoring", "deviation"),
                                  trial, Verdict("deviation-bar-free", True, "quality", "ok"))])

    table = format_round(result)

    assert "label-free cut" in table
    assert "0.136" in table and "0.226" in table
    assert "controls alert rate, not false-positive rate" in table
