# -*- coding: utf-8 -*-
"""Round 41 tests: a refusal's dependence on the replication bar, per stream, re-decided not re-measured.

Round 40 stored the numbers behind each `replicate` refusal. The obvious next question is which of those
refusals would have gone the other way at a looser bar — asked of the stored measurements, with no Laya call
and no new round. The first column of the table is the bar in force, so it must be zero: that is the table's
own control. A table computed over only the rounds that can be replayed must also say how many could not be.
"""
import json

from brian.evolve import (EvolveRules, Measurement, Verdict, _bar_ladder, decide, format_sensitivity,
                          refusal_sensitivity)

WINDOWS = 1235
FAULTS = 192

BASELINE_SPLIT = {"smd-machine-1-6": 0.4911, "smd-machine-2-2": 0.0371}
TRIAL_SPLIT = {"smd-machine-1-6": 0.3822, "smd-machine-2-2": 0.2201}

CANDIDATE = {
    "name": "host-features-3",
    "hypothesis": "three features instead of eight",
    "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
    "veto": None,
}

WINDOWED = {
    "name": "host-rolling-rank-0-85-200@hint286",
    "hypothesis": "the same configuration at a window covering the same span",
    "changes": [["digest", "rolling_window", 286]],
    "veto": None,
}


def _measurement(name, *, pooled=0.2971, split=None, tokens=3356.0, window=None, recall=None):
    scattered = split or {}
    document = {
        "candidate": name,
        "recall": pooled if recall is None else recall,
        "fpr": 0.092,
        "precision": None,
        "reports": 221,
        "tokens_per_report": tokens,
        "tokens_per_window": tokens * 221 / WINDOWS,
        "windows": WINDOWS,
        "recall_at_budget": pooled,
        "fpr_at_budget": 0.092,
        "threshold_at_budget": 0.5,
        "budget": 0.10,
        "alert_budget": 0.10,
        "prevalence": FAULTS / WINDOWS,
        "recall_by_recording": dict(scattered),
        "fpr_by_recording": {key: 0.09 for key in scattered},
        "window": window,
    }
    return document


def _round_file(root, name, *, source="SMD", candidate=None, trial=None, stored=None,
                references=None, baseline=None, retired=False):
    """Write a round file and its ledger line the way `run_round` and `persist` do."""
    chosen = dict(candidate or CANDIDATE)
    payload = {
        "round": name,
        "started_at": "2026-11-18T10:00:00+00:00",
        "finished_at": "2026-11-18T10:05:00+00:00",
        "stream": {"source": source, "windows": 1355, "scored": WINDOWS},
        "rules": EvolveRules().to_dict(),
        "baseline": dict(baseline or _measurement("baseline", pooled=0.2971,
                                                  split=BASELINE_SPLIT)),
        "trials": [{
            "candidate": chosen,
            "measurement": dict(trial or _measurement("host-features-3", pooled=0.3120,
                                                      split=TRIAL_SPLIT, tokens=2908.0,
                                                      recall=0.339)),
            "verdict": dict(stored or {"candidate": chosen["name"], "accepted": False,
                                       "rule": "replicate", "reason": "stored by hand"}),
        }],
        "references": dict(references or {}),
    }
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    line = {"round": name, "started_at": payload["started_at"], "accepted": [],
            "rejected": [chosen["name"]], "applied": [], "rules_hash": "test",
            "baseline_tokens_per_report": 3356.0}
    if retired:
        line["retired"] = {"at": "2026-11-18T10:06:00+00:00", "re_measured_as": None}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return name


def test_the_ladder_is_the_bar_twice_and_four_times():
    assert _bar_ladder(0.05) == [0.05, 0.1, 0.2]
    assert _bar_ladder(0.20) == [0.2, 0.4, 0.8]


def test_explicit_bars_are_kept_deduped_and_ordered():
    assert _bar_ladder(0.05, [0.2, 0.05, 0.11, 0.2]) == [0.05, 0.11, 0.2]


def test_an_empty_ledger_has_nothing_to_test(tmp_path):
    result = refusal_sensitivity(tmp_path)
    assert result.refused == 0
    assert result.rows == []
    assert "nothing to test" in format_sensitivity(result)


def test_a_refusal_flips_only_above_the_loss_it_records(tmp_path):
    _round_file(tmp_path, "round-20261118T100000Z.json")
    result = refusal_sensitivity(tmp_path)
    assert result.bar == 0.05
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.domain == "SMD"
    assert row.refused == 1
    # The bar in force cannot accept what it just refused: the table's own control.
    assert row.flips["0.050"] == 0
    assert row.flips["0.100"] == 0
    # The recorded loss is 0.1089, so 0.200 clears it and 0.110 is just above it.
    assert row.flips["0.200"] == 1
    tuned = refusal_sensitivity(tmp_path, bars=[0.11])
    assert tuned.rows[0].flips["0.110"] == 1


def test_a_gain_that_holds_on_both_recordings_is_not_a_refusal(tmp_path):
    _round_file(tmp_path, "round-20261118T100100Z.json",
                trial=_measurement("host-features-3", pooled=0.340,
                                   split={"smd-machine-1-6": 0.520, "smd-machine-2-2": 0.210},
                                   tokens=3000.0, recall=0.34))
    result = refusal_sensitivity(tmp_path)
    assert result.refused == 0
    assert "nothing to test" in format_sensitivity(result)


def test_a_looser_bar_in_force_has_nothing_left_to_depend_on(tmp_path):
    _round_file(tmp_path, "round-20261118T100200Z.json")
    result = refusal_sensitivity(tmp_path, EvolveRules(replication_tolerance=0.20))
    assert result.bar == 0.20
    assert result.bar == result.rules.replication_bar
    assert result.refused == 0


def test_each_stream_owes_its_own_row(tmp_path):
    _round_file(tmp_path, "round-20261118T100300Z.json")
    _round_file(tmp_path, "round-20261118T100400Z.json", source="RCAEval RE1-OB")
    result = refusal_sensitivity(tmp_path)
    assert [row.domain for row in result.rows] == ["RCAEval RE1-OB", "SMD"]
    assert [row.refused for row in result.rows] == [1, 1]
    text = format_sensitivity(result)
    assert "RCAEval RE1-OB" in text
    assert "SMD" in text


def test_only_refusals_that_record_their_numbers_are_counted_as_readable(tmp_path):
    detail = decide(Measurement.from_dict(_measurement("baseline", pooled=0.2971, split=BASELINE_SPLIT)),
                    Measurement.from_dict(_measurement("host-features-3", pooled=0.3120,
                                                       split=TRIAL_SPLIT, tokens=2908.0, recall=0.339)),
                    EvolveRules())
    assert detail.rule == "replicate"
    _round_file(tmp_path, "round-20261118T100500Z.json", stored=detail.to_dict())
    _round_file(tmp_path, "round-20261118T100600Z.json")  # stored verdict has no detail
    result = refusal_sensitivity(tmp_path)
    assert result.refused == 2
    assert result.recorded == 1
    assert "1 of them record the loss" in format_sensitivity(result)


def test_a_retired_record_does_not_owe_a_row(tmp_path):
    _round_file(tmp_path, "round-20261118T100700Z.json", retired=True)
    result = refusal_sensitivity(tmp_path)
    assert result.refused == 0
    assert result.rounds == 0


def test_a_trial_that_cannot_be_replayed_is_counted_not_dropped(tmp_path):
    # A baseline measured at window 200, a trial at 286, and no recorded reference for 286: the only
    # comparison available is one the round never made, so the trial is counted rather than guessed at.
    _round_file(tmp_path, "round-20261118T100800Z.json", candidate=WINDOWED,
                baseline=_measurement("baseline", pooled=0.2971, split=BASELINE_SPLIT, window=200),
                trial=_measurement(WINDOWED["name"], pooled=0.43, split=TRIAL_SPLIT, window=286))
    result = refusal_sensitivity(tmp_path)
    assert result.unreadable == 1
    assert result.refused == 0
    assert "not replayable" in format_sensitivity(result)


def test_the_table_says_the_first_column_is_a_control(tmp_path):
    _round_file(tmp_path, "round-20261118T100900Z.json")
    text = format_sensitivity(refusal_sensitivity(tmp_path))
    assert "bar in force      0.050" in text
    assert "cannot accept what it" in text
    assert "boundary 89" in text


def test_the_document_carries_the_numbers(tmp_path):
    _round_file(tmp_path, "round-20261118T101000Z.json")
    document = refusal_sensitivity(tmp_path).to_dict()
    assert document["bar"] == 0.05
    assert document["bars"] == [0.05, 0.1, 0.2]
    assert document["rows"][0]["flips"] == {"0.050": 0, "0.100": 0, "0.200": 1}
    assert document["refused"] == 1


def test_a_stored_verdict_object_is_not_needed_only_the_measurement(tmp_path):
    # The table re-decides from the measurements, so even a stored verdict that claims an accept does not
    # stop the refusal from being found and counted.
    _round_file(tmp_path, "round-20261118T101100Z.json",
                stored={"candidate": "host-features-3", "accepted": True, "rule": "quality",
                        "reason": "stored by hand"})
    result = refusal_sensitivity(tmp_path)
    assert result.refused == 1
    assert Verdict("host-features-3", True, "quality", "stored by hand").detail == {}
