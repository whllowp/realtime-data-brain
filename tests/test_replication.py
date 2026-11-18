# -*- coding: utf-8 -*-
"""Round 39: a pooled at-budget gain must replicate on the recordings it is pooled over.

The defect these tests pin: `recall_at_budget` is one division over every recording's incidents. On SMD's
check stream the baseline reads 0.297 pooled — 0.491 on machine-1-6 and 0.037 on machine-2-2 — because
the second host carries most of the incidents. The catalogue's `host-features-3` reads 0.312 pooled by
lifting machine-2-2 to 0.220 while dropping machine-1-6 to 0.382: a +5% pooled reading that is one host's
improvement and one host's regression, on a deployment that runs on both.
"""
from brian.evolve import (
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    _replication_loss,
    decide,
    format_round,
)

WINDOWS = 1235
TOKENS = 3356.0
FAULTS = 192
BUDGET = 0.10


def _measurement(name: str, recall_at_budget: float, *, split=None,
                 tokens: float = TOKENS) -> Measurement:
    scattered = split if split is not None else {}
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
        confusion={"tp": int(round(recall_at_budget * FAULTS)),
                   "fn": FAULTS - int(round(recall_at_budget * FAULTS)),
                   "fp": int(round(0.080 * (WINDOWS - FAULTS))),
                   "tn": WINDOWS - FAULTS - int(round(0.080 * (WINDOWS - FAULTS)))},
        recall_at_budget=recall_at_budget,
        fpr_at_budget=0.080,
        threshold_at_budget=0.5,
        budget=BUDGET,
        alert_budget=BUDGET,
        prevalence=FAULTS / WINDOWS,
        recall_by_recording={key: value[0] for key, value in scattered.items()},
        fpr_by_recording={key: value[1] for key, value in scattered.items()},
    )


BASELINE = _measurement("baseline", 0.2971,
                        split={"smd-machine-1-6": (0.4911, 0.090), "smd-machine-2-2": (0.0371, 0.070)})
HOST_FEATURES = _measurement("host-features-3", 0.3120,
                             split={"smd-machine-1-6": (0.3822, 0.080),
                                    "smd-machine-2-2": (0.2201, 0.080)})


def _candidate(name: str = "host-features-3", **kwargs) -> Candidate:
    return Candidate(
        name=name,
        hypothesis="render three features instead of eight",
        section="digest",
        field="host_features",
        value=kwargs.pop("value", ["cpu_r", "load_1", "mem_free"]),
        **kwargs,
    )


def test_the_recording_that_fell_is_named():
    reason = _replication_loss(BASELINE, HOST_FEATURES, EvolveRules().min_quality_gain)
    assert reason is not None
    assert "smd-machine-1-6" in reason
    assert "0.491 -> 0.382" in reason
    assert "0.297 -> 0.312 pooled" in reason
    assert "not a replicated one" in reason


def test_a_single_recording_cannot_be_split():
    one = _measurement("baseline", 0.2971, split={"smd-machine-1-6": (0.4911, 0.090)})
    other = _measurement("trial", 0.4120, split={"smd-machine-1-6": (0.4000, 0.090)})
    assert _replication_loss(one, other, 0.05) is None


def test_a_loss_under_the_bar_is_not_a_loss():
    baseline = _measurement("baseline", 0.2971,
                            split={"a": (0.4000, 0.090), "b": (0.2000, 0.090)})
    trial = _measurement("trial", 0.3200,
                         split={"a": (0.3900, 0.090), "b": (0.2600, 0.090)})
    assert _replication_loss(baseline, trial, 0.05) is None


def test_a_pooled_gain_that_is_one_recording_is_rejected():
    verdict = decide(BASELINE, HOST_FEATURES, EvolveRules())
    assert verdict.accepted is False
    assert verdict.rule == "replicate"
    assert "smd-machine-1-6" in verdict.reason


def test_a_gain_that_holds_on_both_recordings_is_still_accepted():
    trial = _measurement("host-features-3", 0.4120,
                         split={"smd-machine-1-6": (0.5600, 0.090), "smd-machine-2-2": (0.2600, 0.080)})
    verdict = decide(BASELINE, trial, EvolveRules())
    assert verdict.accepted is True
    assert verdict.rule == "quality"


def test_a_cost_change_with_the_same_split_is_not_blamed_for_it():
    # The same per-recording numbers, but the bill falls and the pooled recall does not move: the cost
    # branch answers, and a candidate that only cuts tokens has no replication claim to make.
    trial = _measurement("events-4", 0.2971,
                         split={"smd-machine-1-6": (0.4911, 0.090), "smd-machine-2-2": (0.0371, 0.070)},
                         tokens=1900.0)
    verdict = decide(BASELINE, trial, EvolveRules())
    assert verdict.accepted is True
    assert verdict.rule == "cost"


def test_the_per_recording_numbers_survive_the_record():
    document = HOST_FEATURES.to_dict()
    restored = Measurement.from_dict(document)
    assert restored.recall_by_recording == HOST_FEATURES.recall_by_recording
    assert restored.fpr_by_recording == HOST_FEATURES.fpr_by_recording


def test_the_round_prints_the_split_next_to_the_table():
    candidate = _candidate()
    result = RoundResult(
        started_at="2026-11-18T10:00:00Z",
        finished_at="2026-11-18T10:05:00Z",
        stream={"streams": 2, "windows": 1355, "scored": 1235},
        rules=EvolveRules(),
        baseline=BASELINE,
        trials=[(candidate, HOST_FEATURES,
                 Verdict("host-features-3", False, "replicate",
                         "recall at the 10% budget rises 0.297 -> 0.312 pooled"))],
    )
    text = format_round(result)
    assert "recall@b by recording:" in text
    assert "smd-machine-1-6 0.491 -> 0.382" in text
    assert "smd-machine-2-2 0.037 -> 0.220" in text


def test_a_round_without_per_recording_numbers_prints_no_split():
    baseline = _measurement("baseline", 0.2971)
    trial = _measurement("events-4", 0.2971, tokens=1900.0)
    candidate = _candidate("events-4")
    result = RoundResult(
        started_at="2026-11-18T10:00:00Z",
        finished_at="2026-11-18T10:05:00Z",
        stream={"streams": 1, "windows": WINDOWS, "scored": WINDOWS},
        rules=EvolveRules(),
        baseline=baseline,
        trials=[(candidate, trial, Verdict("events-4", True, "cost", "1900 < 3356"))],
    )
    assert "recall@b by recording" not in format_round(result)
