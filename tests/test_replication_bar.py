# -*- coding: utf-8 -*-
"""Round 40: the replication bar is a number in the ruleset, and a refusal stores the numbers behind it.

Round 39 refused `host-features-3` for raising the pooled recall while lowering machine-1-6, and wrote the
refusal as a sentence. A sentence cannot be re-decided: the bar it used (5%, the quality bar) was hard-coded
inside `decide()`, and nothing in the record said which recording fell by how much against which tolerance.
So the bar became `EvolveRules.replication_tolerance` (default: the quality bar) and the verdict carries a
`detail` dict, which is what lets `--rejudge --replication-tolerance 0.20` disagree without re-measuring.
"""
import argparse
import json

from brian.cli import _rules_from_args
from brian.evolve import (EvolveRules, Measurement, Verdict, decide, format_rejudge, rejudge,
                          replication_loss)

WINDOWS = 1235
FAULTS = 192

BASELINE_SPLIT = {"smd-machine-1-6": 0.4911, "smd-machine-2-2": 0.0371}
TRIAL_SPLIT = {"smd-machine-1-6": 0.3822, "smd-machine-2-2": 0.2201}


def _measurement(name: str, *, pooled: float, split=None, tokens: float = 3356.0,
                 recall: float = None) -> Measurement:
    scattered = split or {}
    recall = pooled if recall is None else recall
    return Measurement(
        candidate=name,
        recall=recall,
        fpr=0.092,
        precision=None,
        reports=221,
        tokens_per_report=tokens,
        tokens_per_window=tokens * 221 / WINDOWS,
        latency_p95_ms=None,
        windows=WINDOWS,
        recall_at_budget=pooled,
        fpr_at_budget=0.092,
        threshold_at_budget=0.5,
        budget=0.10,
        alert_budget=0.10,
        prevalence=FAULTS / WINDOWS,
        recall_by_recording=dict(scattered),
        fpr_by_recording={name: 0.09 for name in scattered},
    )


BASELINE = _measurement("baseline", pooled=0.2971, split=BASELINE_SPLIT)
HOST_FEATURES = _measurement("host-features-3", pooled=0.3120, split=TRIAL_SPLIT, tokens=2908.0,
                             recall=0.339)


def test_the_bar_defaults_to_the_quality_bar():
    # The refusal may not use a stricter measure than the acceptance did, so the default is shared.
    assert EvolveRules().replication_bar == EvolveRules().min_quality_gain


def test_an_explicit_bar_wins_and_is_written_into_the_ruleset():
    rules = EvolveRules(replication_tolerance=0.20)
    assert rules.replication_bar == 0.20
    assert rules.to_dict()["replication_tolerance"] == 0.20
    assert EvolveRules().to_dict()["replication_tolerance"] is None


def test_the_loss_is_reported_as_numbers():
    detail = replication_loss(BASELINE, HOST_FEATURES, 0.05)
    assert detail is not None
    assert detail["recording"] == "smd-machine-1-6"
    assert detail["before"] == 0.4911
    assert detail["after"] == 0.3822
    assert detail["drop"] == 0.1089
    assert detail["tolerance"] == 0.05
    assert detail["pooled_before"] == 0.2971
    assert detail["pooled_after"] == 0.312


def test_one_recording_and_a_small_drop_are_not_losses():
    single = _measurement("baseline", pooled=0.2971, split={"smd-machine-1-6": 0.4911})
    assert replication_loss(single, HOST_FEATURES, 0.05) is None
    # The same measurements stop being a loss at a bar above the drop: that is the judgement the flag moves.
    assert replication_loss(BASELINE, HOST_FEATURES, 0.20) is None


def test_the_verdict_stores_the_numbers_behind_the_refusal():
    verdict = decide(BASELINE, HOST_FEATURES, EvolveRules())
    assert verdict.accepted is False
    assert verdict.rule == "replicate"
    assert verdict.detail["recording"] == "smd-machine-1-6"
    assert verdict.detail["tolerance"] == 0.05
    assert "smd-machine-1-6 falls 0.491 -> 0.382" in verdict.reason


def test_a_looser_bar_accepts_the_same_measurements():
    verdict = decide(BASELINE, HOST_FEATURES, EvolveRules(replication_tolerance=0.20))
    assert verdict.accepted is True
    # Accepted for the bill, not for the labels: the pooled +5% is still one recording's and the cost
    # branch answers once the replication objection is gone (branch order is the objective's order).
    assert verdict.rule == "cost"


def test_a_verdict_carries_its_detail_through_the_record():
    refused = decide(BASELINE, HOST_FEATURES, EvolveRules())
    document = refused.to_dict()
    assert document["detail"]["recording"] == "smd-machine-1-6"
    ordinary = decide(BASELINE, HOST_FEATURES, EvolveRules(replication_tolerance=0.20))
    assert ordinary.to_dict()["detail"] == {}


def test_the_flags_build_one_ruleset():
    args = argparse.Namespace(min_gain=0.07, replication_tolerance=0.20)
    rules = _rules_from_args(args)
    assert rules.min_gain == 0.07
    assert rules.replication_bar == 0.20
    assert _rules_from_args(argparse.Namespace(min_gain=0.05)).replication_bar == 0.05


def _record(path, verdict) -> None:
    payload = {
        "round": path.name,
        "started_at": "2026-11-18T10:00:00+00:00",
        "finished_at": "2026-11-18T10:05:00+00:00",
        "stream": {"source": "SMD", "windows": 1355, "scored": WINDOWS},
        "rules": EvolveRules().to_dict(),
        "baseline": BASELINE.to_dict(),
        "trials": [{
            "candidate": {"name": "host-features-3", "hypothesis": "three features",
                          "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
                          "veto": None},
            "measurement": HOST_FEATURES.to_dict(),
            "verdict": verdict.to_dict(),
        }],
        "references": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_stored_refusal_can_be_disagreed_with_without_measuring(tmp_path):
    path = tmp_path / "round-20261118T100000Z.json"
    _record(path, decide(BASELINE, HOST_FEATURES, EvolveRules()))

    same = rejudge(path, EvolveRules())
    assert same.changed == []
    assert same.withheld == []

    looser = rejudge(path, EvolveRules(replication_tolerance=0.20))
    assert len(looser.changed) == 1
    _candidate, _measurement, old, new = looser.rows[0]
    assert old.rule == "replicate"
    assert new.rule == "cost"
    assert new.accepted is True

    text = format_rejudge(looser)
    assert "was rejected" in text
    assert "smd-machine-1-6 falls 0.491 -> 0.382" in text
    assert "the refusal used tolerance 0.050 against 0.200 in today's rules" in text
    assert "only the rules moved" in text


def test_the_rejudged_entry_keeps_the_detail_of_the_stored_refusal(tmp_path):
    path = tmp_path / "round-20261118T100100Z.json"
    _record(path, decide(BASELINE, HOST_FEATURES, EvolveRules()))
    result = rejudge(path, EvolveRules(replication_tolerance=0.20))
    _candidate, _measurement, old, _new = result.rows[0]
    assert old.detail.get("recording") == "smd-machine-1-6"
    assert old.detail.get("tolerance") == 0.05


def test_a_verdict_without_detail_is_readable(tmp_path):
    path = tmp_path / "round-20261118T100200Z.json"
    _record(path, Verdict("host-features-3", False, "replicate", "no numbers here"))
    result = rejudge(path, EvolveRules())
    assert result.changed == []
    assert result.withheld == []
