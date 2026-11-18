# -*- coding: utf-8 -*-
"""Round 30 tests: a reshaping is proposed, and judged against the promise it is meant to restore.

Round 27 printed the window count that restores a claim's span; round 28 measured it and called it a
comparability device. Round 30 makes it a proposal and gives it its own rule, because judged against the
baseline it is accepted for 0.297 -> 0.427 -- a number nobody was promised -- and judged against the promise
it answers the question the claim actually made: 0.427 against 0.507 is a refusal that names both numbers.

Running the round twice also exposed a stale seed: a reshaping accepted under the old `quality` branch was
grown around by the next round, though today's rules reject it. `_accepted_measurements` now re-decides a
stored acceptance from its own recorded baseline, and refuses to seed from a round measured at another
sampling density.
"""
import json

from brian.evolve import Candidate, EvolveRules, Measurement, catalogue, decide
from brian.promote import load_promotions, rescaled_candidate
from brian.propose import _accepted_measurements, _same_shape, propose

RULES = EvolveRules()

RECORDED = {"streams": 2, "windows": 948, "scored": 828, "anomalous": 134, "events": 4740,
            "cadence_s": 3000.0, "source": "SMD"}
OTHER_SAMPLING = {"streams": 2, "windows": 1355, "scored": 1235, "anomalous": 192, "events": 6775,
                  "cadence_s": 2100.0, "source": "SMD"}

CANDIDATE = {
    "name": "host-rolling-rank-0-85-200",
    "hypothesis": "a longer window keeps the rank calibrated",
    "changes": [["digest", "rolling_window", 200], ["digest", "scoring", "auto"],
                ["gate", "rank_probability", 0.85]],
    "veto": None,
}


def _version_file(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 20,
        "candidate": CANDIDATE,
        "change": "digest.scoring = 'auto', digest.rolling_window = 200, gate.rank_probability = 0.85",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
        # The claim is read from the version's recorded measurement, not from a `claim` key: `load_promotions`
        # calls `Claim.from_dict(payload["measurement"])`, which is also what the live round reads.
        "measurement": {"recall_at_budget": 0.507, "fpr_at_budget": 0.09, "tokens_per_report": 3358.0},
        "created_at": "2026-11-18T00:03:10+00:00",
        "stream": dict(RECORDED),
    }
    (directory / "v020.json").write_text(json.dumps(payload), encoding="utf-8")


def _measurement(recall_at_budget=0.427, promise=0.507, **overrides):
    payload = {
        # The shipped cut sits where the at-budget cut does: a fixture that shipped a much stricter cut
        # would be rejected by the operational guard and hide the rule under test.
        "candidate": "reshape", "recall": recall_at_budget, "fpr": 0.080, "precision": 0.7,
        "reports": 200, "tokens_per_report": 3356.0, "tokens_per_window": 40.0, "windows": 1235,
        "recall_at_budget": recall_at_budget, "fpr_at_budget": 0.080, "budget": 0.10,
        "promise": promise,
    }
    payload.update(overrides)
    return Measurement.from_dict(payload)


def _baseline():
    return _measurement(recall_at_budget=0.297, promise=None, fpr=0.092, fpr_at_budget=0.092,
                        candidate="baseline")


def _reshaped(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    return rescaled_candidate(promotion, RECORDED, OTHER_SAMPLING)


def test_the_rescale_carries_the_promise_it_is_restoring(tmp_path):
    shape = _reshaped(tmp_path)

    assert shape is not None
    assert shape.promise == 0.507
    assert "v020" in shape.provenance
    assert "the span its claim was made over" in shape.hypothesis


def test_a_promise_travels_with_the_measurement_not_only_the_candidate():
    # The first version read `trial.candidate.promise`. A measurement keeps the candidate's *name*, so the
    # branch never fired and the stored record filed the reshape under `quality`.
    trial = _measurement()
    verdict = decide(_baseline(), trial, RULES)

    assert trial.promise == 0.507
    assert trial.candidate == "reshape"
    assert verdict.rule == "reshape"


def test_a_reshape_that_reaches_the_promise_is_accepted():
    verdict = decide(_baseline(), _measurement(recall_at_budget=0.51), RULES)

    assert verdict.accepted
    assert verdict.rule == "reshape"
    assert "restores the promised 0.507" in verdict.reason


def test_a_reshape_below_the_promise_is_refused_even_though_it_beats_the_baseline():
    # 0.427 is a 44% gain over the 0.297 baseline and still not what the version promised. Accepting it
    # would tell the operator a number that was never promised.
    verdict = decide(_baseline(), _measurement(recall_at_budget=0.427), RULES)

    assert not verdict.accepted
    assert verdict.rule == "reshape"
    assert "0.427 of the promised 0.507" in verdict.reason
    assert "comparability, not the promise" in verdict.reason


def test_a_reshape_without_an_at_budget_recall_cannot_claim_the_promise():
    verdict = decide(_baseline(), _measurement(recall_at_budget=None), RULES)

    assert not verdict.accepted
    assert verdict.rule == "reshape"


def test_an_ordinary_candidate_is_not_judged_against_a_promise():
    verdict = decide(_baseline(), _measurement(promise=None), RULES)

    assert verdict.accepted
    assert verdict.rule == "quality"


def test_the_same_shape_is_seedable_and_another_density_is_not():
    assert _same_shape(dict(RECORDED), dict(RECORDED)) is True
    assert _same_shape(dict(RECORDED), dict(OTHER_SAMPLING)) is False
    # A record without a cadence still answers with its window count: 948 against 1355 is the other
    # sampling, not a bigger stream.
    assert _same_shape({"windows": 948}, dict(OTHER_SAMPLING)) is False
    # Nothing recorded means nothing to compare: the older rounds stay usable.
    assert _same_shape(None, dict(OTHER_SAMPLING)) is True


def _round(*, stream, accepted_name="host-rolling-rank-0-85-200", rule="quality",
           promise=None, trial_recall=0.507, baseline_recall=0.224):
    return {
        "finished_at": "2026-11-18T00:03:10+00:00",
        "stream": dict(stream),
        # Round 31: the round measured the deployment at the window the trial moves to, so the acceptance
        # can be re-decided at the geometry the change was made on.
        "references": {"286": {"candidate": "baseline@286", "recall_at_budget": trial_recall,
                               "fpr_at_budget": 0.08}},
        "baseline": {"candidate": "baseline", "recall": 0.30, "fpr": 0.10, "precision": 0.6,
                     "reports": 100, "tokens_per_report": 3355.0, "tokens_per_window": 40.0,
                     "windows": stream.get("windows", 1),
                     "recall_at_budget": baseline_recall, "fpr_at_budget": 0.09, "budget": 0.10},
        "trials": [{
            "candidate": dict(CANDIDATE, name=accepted_name, promise=promise,
                              changes=[["digest", "rolling_window", 286], ["digest", "scoring", "auto"],
                                       ["gate", "rank_probability", 0.85]]),
            "measurement": {"candidate": accepted_name, "recall": trial_recall, "fpr": 0.08,
                            "precision": 0.7,
                            "reports": 100, "tokens_per_report": 3356.0, "tokens_per_window": 40.0,
                            "windows": stream.get("windows", 1), "recall_at_budget": trial_recall,
                            "fpr_at_budget": 0.08, "budget": 0.10, "promise": promise},
            "verdict": {"candidate": accepted_name, "accepted": True, "rule": rule, "reason": "ok"},
        }],
    }


def test_a_stored_acceptance_that_todays_rules_refuse_does_not_seed_the_next_round():
    # The dry run recorded this reshaping as accepted under the old `quality` branch (0.427 > 0.297 x 1.05).
    # Under today's rules it is a `reshape` refusal, so it must not become the point the next round grows
    # around.
    rounds = [_round(stream=OTHER_SAMPLING, promise=0.507, trial_recall=0.427)]

    assert _accepted_measurements(rounds, source="SMD", rules=RULES,
                                  shape=OTHER_SAMPLING) == []
    # Without the rules it is still the ledger's opinion -- the old behaviour, kept for callers that only
    # want a catalogue of accepted names.
    assert len(_accepted_measurements(rounds, source="SMD")) == 1


def test_a_promise_recorded_only_on_the_candidate_is_still_re_judged():
    # The first reshape round's measurement predates the `promise` field; the candidate kept it.
    rounds = [_round(stream=OTHER_SAMPLING, promise=None, trial_recall=0.427)]
    rounds[0]["trials"][0]["candidate"]["promise"] = 0.507

    assert _accepted_measurements(rounds, source="SMD", rules=RULES, shape=OTHER_SAMPLING) == []


def test_an_acceptance_from_another_sampling_does_not_seed_this_one():
    rounds = [_round(stream=RECORDED)]

    assert len(_accepted_measurements(rounds, source="SMD", rules=RULES,
                                      shape=OTHER_SAMPLING)) == 0
    assert len(_accepted_measurements(rounds, source="SMD", rules=RULES,
                                      shape=RECORDED)) == 1


def test_a_neighbour_that_is_the_reshape_is_not_proposed_as_an_ordinary_candidate(tmp_path):
    shape = _reshaped(tmp_path)
    rounds = [_round(stream=OTHER_SAMPLING, accepted_name="host-rolling-rank-0-85-200@hint286-0-9",
                     trial_recall=0.427)]
    rounds[0]["trials"][0]["candidate"]["changes"] = [
        ["digest", "rolling_window", 286], ["digest", "scoring", "auto"],
        ["gate", "rank_probability", 0.9]]

    proposal = propose(rounds, reshaped=shape, rules=RULES, shape=OTHER_SAMPLING, deployed={})

    assert proposal.candidates[0].name.endswith("@hint286")
    written = [tuple(candidate.changes()) for candidate in proposal.candidates]
    assert len(written) == len(set(written)), "the reshape must appear once, judged by its promise"
    assert tuple(shape.changes()) not in written[1:]


def test_the_reshape_is_the_first_candidate_and_the_catalogue_still_follows(tmp_path):
    shape = _reshaped(tmp_path)

    proposal = propose([], reshaped=shape, rules=RULES, shape=OTHER_SAMPLING, deployed={})

    assert proposal.candidates[0] is shape
    assert proposal.candidates[1].name in {candidate.name for candidate in catalogue()}


def test_a_reshape_that_describes_the_deployment_is_not_measured(tmp_path):
    shape = _reshaped(tmp_path)
    deployed = {"digest": {"rolling_window": 286, "scoring": "auto"}, "gate": {"rank_probability": 0.85}}

    proposal = propose([], reshaped=shape, rules=RULES, shape=OTHER_SAMPLING, deployed=deployed)

    assert shape not in proposal.candidates


def test_a_candidate_round_trips_its_promise_and_provenance(tmp_path):
    shape = _reshaped(tmp_path)

    rebuilt = Candidate.from_dict(shape.to_dict())

    assert rebuilt.promise == shape.promise
    assert rebuilt.provenance == shape.provenance
