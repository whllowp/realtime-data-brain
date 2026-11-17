"""The improvement mechanism (`Imp`) as a unit, without a Laya service or an LLM.

What is worth testing here is not that a measurement runs — that is `brian eval` and `brian cost` — but
that the loop refuses to accept changes it should refuse, that it remembers those refusals, and that a
change it accepts can be reproduced and reversed from the file it wrote.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from brian.config import ENV_VARS, load_settings
from brian.evolve import (
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    apply_overrides,
    catalogue,
    decide,
    format_round,
    grade,
    history,
    operating_point,
    operating_point_by_recording,
    persist,
    previously_rejected,
)
from brian.llm.prompts import evidence_coverage, evidence_floor


PERFECT = {"log_retention": 1.0, "signature_retention": 1.0,
           "first_error_kept": 1.0, "context_retention": 1.0}


def measure(name: str = "candidate", *, recall: float = 0.875, fpr: float = 0.0,
            tokens: float | None = 100.0, coverage=None) -> Measurement:
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
    def __init__(self, probability, label, error=None):
        self.predicted_probability = probability
        self.expected_anomaly = label
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
        section = getattr(settings, candidate.section)
        assert candidate.field in {item.name for item in dataclasses.fields(section)}, candidate.name
        assert (candidate.section, candidate.field) in ENV_VARS, candidate.name
        assert candidate.apply(settings) is not settings
        assert candidate.hypothesis, candidate.name


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
    assert rejected["events-8"] == "no gain"


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
    with pytest.raises(ValueError, match="no field"):
        apply_overrides(settings, {"gate": {"anomaly_probabilty": 0.5}})

