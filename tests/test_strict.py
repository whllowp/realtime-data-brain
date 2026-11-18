# -*- coding: utf-8 -*-
"""Round 33 tests: `--strict` has to tell "nothing moved" apart from "nothing could be checked".

Round 32 gave the audit a third outcome — a comparison the record cannot reconstruct — and left it in a
closing paragraph. A check that exits 0 because the only rounds it re-decided happened to survive, while
twenty-three comparisons sat in the footer unexamined, is a check that reports the part it could finish.
"""
import json

from brian import cli
from brian.campaign import campaign, format_campaign

ROUND = "round-20261118T010000Z.json"


def _measurement(name, *, window=None, recall_at_budget=0.5, fpr=0.08, tokens=100.0):
    return {
        "candidate": name,
        "recall": recall_at_budget,
        "fpr": fpr,
        "precision": 0.7,
        "reports": 200,
        "tokens_per_report": tokens,
        "tokens_per_window": 40.0,
        "windows": 1235,
        "recall_at_budget": recall_at_budget,
        "fpr_at_budget": fpr,
        "budget": 0.10,
        "window": window,
    }


CANDIDATE = {
    "name": "host-rolling-rank-0-85-200@hint286",
    "hypothesis": "the same configuration at the window that covers the span its claim was made over",
    "changes": [["digest", "rolling_window", 286]],
}


def _write_round(root, *, with_reference=True, trial_window=286, recorded=None):
    """A round measured at one geometry against a baseline at another, with or without the reference."""
    verdict = recorded or {"candidate": CANDIDATE["name"], "accepted": False, "rule": "gain",
                           "reason": "recall at the budget did not rise"}
    payload = {
        "round": ROUND,
        "started_at": "2026-11-18T01:00:00+00:00",
        "finished_at": "2026-11-18T01:01:00+00:00",
        "stream": {"source": "SMD", "windows": 1355, "scored": 1235, "cadence_s": 2100.0},
        "rules": {
            "min_gain": 0.05, "min_quality_gain": 0.05, "cost_tolerance": 0.05,
            "recall_tolerance": 0.0, "fpr_tolerance": 0.0, "fpr_budget": 0.10, "alert_budget": 0.10,
            "deploy_fpr_ceiling": 0.50, "deploy_fpr_tolerance": 0.005, "operational_tolerance": 0.10,
        },
        "baseline": _measurement("baseline", window=200, recall_at_budget=0.297),
        "trials": [{
            "candidate": dict(CANDIDATE),
            "measurement": _measurement(CANDIDATE["name"], window=trial_window, recall_at_budget=0.427),
            "verdict": dict(verdict),
        }],
        "references": ({"286": _measurement(CANDIDATE["name"], window=286, recall_at_budget=0.427)}
                       if with_reference else {}),
    }
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ROUND).write_text(json.dumps(payload), encoding="utf-8")
    (directory / "ledger.jsonl").write_text(
        json.dumps({"round": ROUND, "stream": {"source": "SMD"}}) + "\n", encoding="utf-8")


def test_the_campaign_counts_what_the_audit_could_not_reconstruct(tmp_path):
    _write_round(tmp_path, with_reference=False)

    result = campaign(tmp_path)

    # Not moved, not unchanged: no answer. The campaign has to carry the third number or it is reporting a
    # ledger it could only partly read.
    assert result.unreconstructable == 1
    assert result.moved == 0
    assert result.to_dict()["comparisons_unreconstructable"] == 1
    text = format_campaign(result)
    assert "0 verdict(s) would be decided differently today, 0 relabelled, 1 not reconstructable" in text
    assert "(1 of them in records that should carry the reference)" in text
    assert "the absence\nof a verdict there is not the absence of a problem" in text


def test_a_campaign_that_could_read_everything_does_not_print_the_column(tmp_path):
    _write_round(tmp_path, with_reference=True)

    text = format_campaign(campaign(tmp_path))

    assert "not reconstructable" not in text


def test_strict_fails_when_the_check_could_not_be_completed(tmp_path, monkeypatch):
    _write_round(tmp_path, with_reference=False)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit"]) == 0
    assert cli.main(["evolve", "--audit", "--strict"]) == 2


def test_a_complete_check_that_finds_nothing_still_exits_zero(tmp_path, monkeypatch):
    _write_round(tmp_path, with_reference=True)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_campaign_shares_the_contract(tmp_path, monkeypatch):
    _write_round(tmp_path, with_reference=False)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--campaign"]) == 0
    assert cli.main(["evolve", "--campaign", "--strict"]) == 2


def test_a_legacy_record_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    # The field did not exist when this was written, so nothing about the record is wrong — but `--strict`
    # has not verified it either, and saying so out loud is the whole point of the third outcome. The stored
    # verdict is the one today's rules reach against the baseline, so nothing moves.
    _write_round(tmp_path, with_reference=False, trial_window=None,
                 recorded={"candidate": CANDIDATE["name"], "accepted": True, "rule": "quality",
                           "reason": "stored under a rule that has since moved"})
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_an_unreadable_ledger_outranks_a_verdict_that_moved(tmp_path, monkeypatch):
    # Both defects in one ledger: one round a rule change would decide differently, one written after the
    # geometry existed that still lost its reference. Exit 2, because the check cannot say how many more
    # verdicts moved in what it could not read.
    _write_round(tmp_path, with_reference=False)
    directory = tmp_path / "var" / "evolve"
    payload = json.loads((directory / ROUND).read_text(encoding="utf-8"))
    payload["round"] = "round-20261118T020000Z.json"
    payload["trials"][0]["verdict"] = {"candidate": CANDIDATE["name"], "accepted": True,
                                       "rule": "quality", "reason": "the stored verdict said it won"}
    payload["rules_hash"] = "000000000000"
    (directory / "round-20261118T020000Z.json").write_text(json.dumps(payload), encoding="utf-8")
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"round": payload["round"], "stream": {"source": "SMD"}}) + "\n")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit", "--strict"]) == 2
