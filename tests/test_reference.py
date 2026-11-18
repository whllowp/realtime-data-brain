# -*- coding: utf-8 -*-
"""Round 31: a candidate that moves the measurement window is compared at that window.

The defect these tests pin: on SMD's 1.49x stream the in-force version reads 0.297 at window 200 and
0.427 at the comparable window 286. A candidate that only moved the window was therefore accepted for
0.297 -> 0.427 while changing nothing about what the version promised. The comparison, not the
candidate, was wrong: a window is how much traffic a sample covers, so a candidate that changes it has
to be measured against the deployment at that same window.
"""
import inspect

from brian.config import load_settings
from brian.evolve import (
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    _reference_candidate,
    configuration_window,
    decide,
    format_round,
    run_round,
)

RECALL_AT_WINDOW_200 = 0.2971
RECALL_AT_WINDOW_286 = 0.4271
PROMISE = 0.5075
TOKENS = 3356.0
WINDOWS = 1235


def _measurement(name: str, recall_at_budget: float, tokens: float = TOKENS) -> Measurement:
    faults = 192
    return Measurement(
        candidate=name,
        recall=recall_at_budget,
        fpr=0.080,
        precision=None,
        reports=221,
        tokens_per_report=tokens,
        tokens_per_window=tokens * 221 / WINDOWS,
        latency_p95_ms=None,
        windows=WINDOWS,
        confusion={"tp": int(round(recall_at_budget * faults)), "fn": faults
                   - int(round(recall_at_budget * faults)),
                   "fp": int(round(0.080 * (WINDOWS - faults))),
                   "tn": WINDOWS - faults - int(round(0.080 * (WINDOWS - faults)))},
        recall_at_budget=recall_at_budget,
        fpr_at_budget=0.080,
        threshold_at_budget=0.5,
        budget=0.10,
        alert_budget=0.10,
        prevalence=faults / WINDOWS,
    )


def _result(references=None, claim=None) -> RoundResult:
    baseline = _measurement("baseline", RECALL_AT_WINDOW_200)
    candidate = Candidate(name="reshape@hint286", hypothesis="the same settings at 286 windows",
                          section="digest", field="rolling_window", value=286, promise=PROMISE)
    trial = _measurement("reshape@hint286", RECALL_AT_WINDOW_286)
    verdict = Verdict("reshape@hint286", False, "reshape",
                      "reaches 0.427 of the promised 0.507 at the 10% budget")
    return RoundResult(
        started_at="2026-11-18T01:00:00Z",
        finished_at="2026-11-18T01:10:00Z",
        stream={"streams": 2, "windows": 1355, "scored": 1235},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, verdict)],
        references=references if references is not None else
        {"286": _measurement("baseline@286", RECALL_AT_WINDOW_286)},
        claim=claim if claim is not None else {
            "version": 20, "promise": PROMISE, "measured": RECALL_AT_WINDOW_200,
            "verdict": "unmeasurable",
            "reason": "the re-check measured 1235 scored windows against the 828 the claim was made on",
            "age_days": 0.01, "due": False,
        },
    )


def test_the_window_a_configuration_measures_with_is_read_from_the_configuration():
    settings = load_settings()
    assert configuration_window(settings) == settings.digest.rolling_window
    moved = Candidate(name="moved", hypothesis="a longer window", section="digest",
                      field="rolling_window", value=286)
    assert configuration_window(settings, moved) == 286


def test_the_reference_is_the_deployment_with_only_the_window_changed():
    reference = _reference_candidate(286)
    assert reference.name == "baseline@286"
    assert tuple(reference.changes()) == (("digest", "rolling_window", 286),)


def test_a_geometry_change_is_not_credited_as_a_detection_gain():
    rules = EvolveRules()
    moved = _measurement("reshape@hint286", RECALL_AT_WINDOW_286)
    # The comparison round 30 made: against the deployment as it stands, the move looks like +44%.
    assert decide(_measurement("baseline", RECALL_AT_WINDOW_200), moved, rules).accepted
    # The comparison that is true to the mechanism: at the same window, the move adds nothing.
    verdict = decide(_measurement("baseline@286", RECALL_AT_WINDOW_286), moved, rules)
    assert not verdict.accepted
    assert verdict.rule == "gain"
    assert "did not rise" in verdict.reason


def test_the_round_states_what_the_deployment_promised_and_whether_it_can_be_tested():
    text = format_round(_result())
    assert "baseline@286" in text
    assert "in force          v020 promised recall 0.507 at the 10% budget" in text
    assert "on this stream    recall 0.297  unmeasurable" in text
    assert "a gain over it is a gain on this sampling, not a" in text
    assert "report a geometry change as a detection gain (boundary 68)" in text


def test_a_round_without_a_claim_does_not_invent_one():
    result = _result()
    result.claim = {}
    text = format_round(result)
    assert "in force" not in text
    assert "on this stream" not in text


def test_the_record_carries_the_references_and_the_claim():
    payload = _result().to_dict()
    assert payload["references"]["286"]["recall_at_budget"] == RECALL_AT_WINDOW_286
    assert payload["claim"]["version"] == 20
    assert payload["claim"]["promise"] == PROMISE


def test_a_round_can_be_given_the_claim_it_is_measuring():
    assert "claim" in inspect.signature(run_round).parameters


HINTED = Candidate(name="host-rolling-rank-0-85-200@hint286", hypothesis="the same settings at 286",
                   section="digest", field="rolling_window", value=286)


def _window_record(reference: bool) -> dict:
    record = {
        "finished_at": "2026-11-18T01:15:44+00:00",
        "stream": {"source": "SMD", "windows": 1355, "cadence_s": 2100.0},
        "accepted": [HINTED.name],
        "trials": [{"candidate": HINTED.to_dict(),
                    "verdict": {"candidate": HINTED.name, "accepted": True, "rule": "quality",
                                "reason": "ok"}}],
    }
    if reference:
        record["references"] = {"286": {"candidate": "baseline@286", "recall_at_budget": 0.427}}
    return record


def test_a_window_change_accepted_without_a_reference_does_not_seed():
    from brian.propose import _accepted_measurements

    dropped: list = []
    assert _accepted_measurements([_window_record(reference=False)], source="SMD",
                                  dropped=dropped) == []
    assert dropped == [286]
    # With the reference the round took, the acceptance was decided at the geometry it moved to.
    assert [candidate.name for candidate in
            _accepted_measurements([_window_record(reference=True)], source="SMD")] == [HINTED.name]


def test_the_reason_names_the_window_that_was_refused_a_seed():
    from brian.propose import Mode, propose

    proposal = propose([_window_record(reference=False)], source="SMD",
                       mode=Mode("expand", "forced for the test"))
    assert proposal.mode == "catalogue"
    assert "decided against a baseline at another geometry" in proposal.reason
    assert "286" in proposal.reason
