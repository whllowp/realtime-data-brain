# -*- coding: utf-8 -*-
"""Round 45 tests: `--strict` counts a refusal nobody re-tested, and one command can re-test several domains.

Round 44 gave the campaign a per-domain count of overdue objections and a command that re-measures one
domain's worth. Two gaps remained. First, `--strict` did not read that count, so a ledger whose oldest
objection had never been re-tested still exited 0 — the same failure round 33 fixed for unreplayable
comparisons, in a younger family. Second, retiring objections across domains meant one command per domain,
and an operator who has to remember where a refusal lives re-tests the loudest stream, not the oldest
objection.
"""
import json
from pathlib import Path

from brian import cli

NOW_DAYS = 30.0


def _stamp(days_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _withheld_file(root: Path, number: int, *, days_ago: float, checks=None) -> Path:
    directory = root / "var" / "evolve" / "withheld"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "number": number,
        "round": "round-20261118T010000Z.json",
        "created_at": _stamp(days_ago),
        "stream": {"source": "SMD", "windows": 1355, "scored": 1235},
        "candidate": {"name": "host-features-3", "hypothesis": "h",
                      "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]]},
        "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
        "bar": 0.05,
        "loss": {"recording": "smd-machine-1-6", "before": 0.491, "after": 0.382, "drop": 0.109},
        "measured": {},
        "reason": "recall at the 10% budget rises 0.297 -> 0.312 pooled",
        "rule": "replicate",
        "checks": list(checks or []),
    }
    path = directory / ("w%03d.json" % number)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _check(days_ago: float, verdict: str = "still") -> dict:
    return {"checked_at": _stamp(days_ago), "verdict": verdict, "reason": "r", "loss": {}}


def test_a_fresh_ledger_owes_nothing(tmp_path):
    assert cli._withheld_debt(tmp_path, 14.0) == (0, None)


def test_an_overdue_refusal_is_a_debt_with_its_age(tmp_path):
    _withheld_file(tmp_path, 1, days_ago=NOW_DAYS)

    due, oldest = cli._withheld_debt(tmp_path, 14.0)

    assert due == 1
    assert oldest is not None and 29.0 < oldest < 31.0


def test_a_recent_check_clears_the_debt_and_keeps_the_evidence_young(tmp_path):
    _withheld_file(tmp_path, 1, days_ago=200.0, checks=[_check(2.0)])

    due, oldest = cli._withheld_debt(tmp_path, 14.0)

    # Nothing is due, and the age counts from the newest measurement, not from the day the objection was
    # written: a record re-checked yesterday stops being the one that has gone longest without one.
    assert due == 0
    assert oldest is not None and 1.0 < oldest < 3.0


def test_strict_exits_one_when_an_objection_was_never_re_tested():
    # No verdict moved and nothing is unreplayable; the only debt is a check that has not been run.
    assert cli._strict_exit(True, 0, 0, 0, due=2, oldest_days=40.0, horizon=14.0) == 1


def test_an_unrun_check_still_exits_zero_without_strict():
    assert cli._strict_exit(False, 0, 0, 0, due=2, oldest_days=40.0) == 0


def test_the_debt_prints_its_own_line(capsys):
    code = cli._strict_exit(True, 0, 0, 0, due=2, oldest_days=40.0, horizon=14.0)

    text = capsys.readouterr().out
    assert code == 1
    assert "2 refusal(s) are older than the 14-day horizon and were not measured again" in text
    assert "oldest evidence 40 day(s) old" in text
    assert "a refusal nobody re-tests is an assertion the loop cannot lose" in text
    assert "pass --verify-withheld to re-measure them." in text


def test_a_build_defect_outranks_an_unrun_check(capsys):
    # Exit 2 is the harder stop: a record this build wrote wrongly is a different claim from a check that
    # has not run yet, and a reader must not trade one for the other.
    assert cli._strict_exit(True, 0, 3, 1, due=2, oldest_days=40.0) == 2
    assert "refusal(s) are older" not in capsys.readouterr().out


def test_a_due_refusal_fails_the_gate_even_when_no_verdict_moved():
    # The point of boundary 74: exit 0 may only mean the check ran on every record and passed.
    assert cli._strict_exit(True, 0, 0, 0, due=1) == 1
    assert cli._strict_exit(True, 5, 0, 0, due=0) == 1
    assert cli._strict_exit(True, 0, 0, 0, due=0) == 0


def test_withheld_pairs_are_parsed_in_order(tmp_path):
    pairs = cli._withheld_pairs(["smd=data/smd/stream.json",
                                 "rcaeval-re1-ob=data/rcaeval/stream-holdout.json"])

    assert [domain for domain, _path in pairs] == ["smd", "rcaeval-re1-ob"]
    assert [path for _domain, path in pairs] == [Path("data/smd/stream.json"),
                                                 Path("data/rcaeval/stream-holdout.json")]


def test_a_pair_without_a_domain_is_refused():
    for value in ("data/smd/stream.json", "smd=", "=data/smd/stream.json"):
        try:
            cli._withheld_pairs([value])
        except ValueError as error:
            assert "domain=stream.json" in str(error)
        else:  # pragma: no cover - the assert above is the point
            raise AssertionError("accepted %r" % (value,))


def test_a_domain_is_matched_by_slug_not_by_its_exact_name(tmp_path):
    # The live defect of round 45: a refusal stores `"source": "SMD"` while `--withheld-stream smd=...` says
    # `"smd"`, and the raw comparison reported "nothing to re-measure" for a record that was right there.
    # A filter that fails silently is worse than one that fails loudly.
    _withheld_file(tmp_path, 1, days_ago=NOW_DAYS)

    for name in ("smd", "SMD", "Smd", " smd "):
        assert [record.name for record in cli._refusals_on(tmp_path, name)] == ["host-features-3"]
    assert cli._refusals_on(tmp_path, "rcaeval-re1-ob") == []


def test_the_campaign_reports_the_debt_the_gate_charges(tmp_path, monkeypatch):
    # A ledger whose only round cannot move, plus one objection nobody re-tested: `--campaign` exits 1 for
    # the objection alone, and the campaign page still says why.
    directory = tmp_path / "var" / "evolve"
    directory.mkdir(parents=True)
    (directory / "round-20261118T010000Z.json").write_text(json.dumps({
        "round": "round-20261118T010000Z.json",
        "started_at": "2026-11-18T01:00:00+00:00",
        "stream": {"source": "SMD"},
        "rules": {},
        "baseline": {"candidate": "baseline", "recall": 0.3, "fpr": 0.05, "precision": 0.6,
                     "reports": 10, "tokens_per_report": 100.0, "tokens_per_window": 1.0,
                     "windows": 10, "recall_at_budget": 0.3, "fpr_at_budget": 0.05, "budget": 0.1},
        "trials": [],
    }), encoding="utf-8")
    (directory / "ledger.jsonl").write_text(
        json.dumps({"round": "round-20261118T010000Z.json", "stream": {"source": "SMD"}}) + "\n",
        encoding="utf-8")
    _withheld_file(tmp_path, 1, days_ago=NOW_DAYS)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--campaign"]) == 0
    assert cli.main(["evolve", "--campaign", "--strict"]) == 1
    # And a horizon the operator shortens to zero still owes the same debt, because the evidence is old.
    assert cli.main(["evolve", "--audit", "--strict", "--refusal-expiry-days", "0.001"]) == 1
