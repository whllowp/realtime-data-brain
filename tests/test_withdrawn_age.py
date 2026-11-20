# -*- coding: utf-8 -*-
"""Round 127: a pair withdrawn and never taken up again has an age, and the gate can charge it.

Round 125 made the withdrawal keep the pair a standing contradiction, and reported it; nothing charged it, because
at the time no command could clear it (boundary 271). Round 126 built `--settle-restore`, which is the condition for
charging. The charge matters in exactly one state: the books have moved on, so the contradiction the withdrawal used
to raise is gone, and the pair simply sits undecided -- a withdrawal that *is* still a contradiction is already
charged at 2 by the link rules, and charging it twice would let one defect cost two answers (boundary 298).
"""
import json
from datetime import timedelta

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (audit, format_audit, load_settlements, restore_settlement, settle_link,
                          unanswered_withdrawals, withdraw_settlement)

from test_provenance_block import OTHER, RETIRED, SUCCESSOR, _mismatch, _round
from test_untested_answer import NOW

LATER = NOW + timedelta(days=2)


def _quiet_withdrawal(tmp_path, days_ago=2.0):
    """A withdrawn decision whose contradiction never existed: one claimant, and the successor names it.

    This is the state the age charge exists for. Round 125's own fixture leaves a `mismatch` standing, so the pair is
    already charged at exit 2 and an age charge there would buy nothing -- and a state where the books still
    contradict each other cannot be "quiet" at all.
    """
    _round(tmp_path, RETIRED, retired={"re_measured_as": SUCCESSOR})
    _round(tmp_path, SUCCESSOR, queue={"paid": "SMD", "paid_round": RETIRED, "family": "file"})
    # Dated in the real past: `NOW` is 2026-12-01, later than the machine clock, and the CLI computes the age of a
    # withdrawal against `datetime.now()` -- a fixture dated at `NOW - 2 days` is two days in the future there
    # (rounds 119/122 hit the same trap, from the other side).
    from test_settlement_reaffirm import _ago

    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice",
                now=_ago(days_ago + 1))
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times", by="alice",
                        now=_ago(days_ago))


def _sentence(tmp_path):
    return " ".join(format_audit(audit(tmp_path)).split())


def test_a_withdrawal_nobody_answered_has_an_age(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times", by="alice",
                        now=NOW)
    rows = unanswered_withdrawals(load_settlements(tmp_path), now=NOW + timedelta(days=5))
    assert len(rows) == 1
    assert rows[0]["record"] == RETIRED and rows[0]["successor"] == SUCCESSOR
    assert rows[0]["at"] == NOW.isoformat() and rows[0]["age_days"] == 5.0
    assert rows[0]["why"] == "the books have said otherwise three times" and rows[0]["by"] == "alice"


def test_a_pair_taken_up_again_owes_nothing(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times", by="alice",
                        now=NOW)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision", by="bob",
                       now=NOW + timedelta(days=1))
    # Both moments stay on the record, and the pair is not in the queue: the question is what was said last.
    assert load_settlements(tmp_path)[0]["withdrawn"]
    assert unanswered_withdrawals(load_settlements(tmp_path), now=NOW + timedelta(days=9)) == []


def test_the_age_is_counted_from_the_newest_withdrawal(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="first attempt to correct it", by="alice", now=NOW)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="read the books again", by="bob", now=NOW + timedelta(days=1))
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after it", by="bob", now=LATER)
    rows = unanswered_withdrawals(load_settlements(tmp_path), now=LATER + timedelta(days=1))
    assert len(rows) == 1
    assert rows[0]["at"] == LATER.isoformat() and rows[0]["age_days"] == 1.0 and rows[0]["by"] == "bob"


def test_the_oldest_withdrawal_comes_first(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    settle_link(tmp_path, OTHER, SUCCESSOR, why="decided when it arrived", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="old one", by="alice", now=NOW - timedelta(days=3))
    withdraw_settlement(tmp_path, OTHER, SUCCESSOR, why="newer one", by="alice", now=NOW)
    rows = unanswered_withdrawals(load_settlements(tmp_path), now=NOW)
    assert [row["record"] for row in rows] == [RETIRED, OTHER]
    assert [row["age_days"] for row in rows] == [3.0, 0.0]


def test_a_withdrawal_that_is_still_a_contradiction_is_not_charged_twice(tmp_path):
    # Round 125's state: the pair is back among the mismatches, so `--strict` already exits 2 for the link. The age
    # charge must be there on the page and must not be the reason for the exit -- one defect, one answer.
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times", by="alice",
                        now=NOW - timedelta(days=4))
    printed = _sentence(tmp_path)
    assert "1 replacement link(s) disagree" in printed
    assert "1 decision(s) were withdrawn by a person" in printed


def test_the_page_prints_the_withdrawal_with_its_age_and_command(tmp_path, monkeypatch, capsys):
    _quiet_withdrawal(tmp_path, days_ago=2.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "1 decision(s) were withdrawn and nobody has taken them up again" in printed
    assert "%s -> %s (withdrawn" % (RETIRED, SUCCESSOR) in printed
    assert "day(s) ago)" in printed
    assert "--settle-restore RECORD=SUCCESSOR --settle-why TEXT" in printed
    assert "the withdrawal stays on the record" in printed


def test_the_gate_charges_only_past_the_age_asked_for(tmp_path, monkeypatch, capsys):
    _quiet_withdrawal(tmp_path, days_ago=2.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
    assert "have been withdrawn for longer than" not in " ".join(capsys.readouterr().out.split())

    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-days", "1"]) == 1
    printed = " ".join(capsys.readouterr().out.split())
    assert "1 decision(s) have been withdrawn for longer than the 1 day(s) asked for:" in printed
    assert "each of those pairs is a contradiction nobody has ruled on" in printed
    assert "--settle-restore RECORD=SUCCESSOR --settle-why TEXT" in printed

    # Past nothing: an age of 2 days does not clear a 5-day bar, and the page still prints the row -- the age
    # is the fact and the cap only decides whether the gate fails (boundary 267).
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-days", "5"]) == 0
    quiet = " ".join(capsys.readouterr().out.split())
    assert "1 withdrawal(s) have no answer yet" in quiet
    assert "have been withdrawn for longer than" not in quiet


def test_the_cap_without_strict_is_refused(tmp_path, monkeypatch, capsys):
    _quiet_withdrawal(tmp_path, days_ago=2.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--max-withdrawn-days", "1"]) == 2
    assert "caps a --strict charge; without --strict nothing reads it" in " ".join(capsys.readouterr().out.split())


def test_the_campaign_names_the_oldest_withdrawal_and_the_command(tmp_path, monkeypatch, capsys):
    _quiet_withdrawal(tmp_path, days_ago=2.0)
    result = campaign(tmp_path)
    printed = " ".join(format_campaign(result).split())
    assert "1 withdrawal(s) have no answer: %s -> %s was taken back on" % (RETIRED, SUCCESSOR) in printed
    assert "nothing has replaced it" in printed
    assert "brian evolve --settle-restore %s=%s --settle-why TEXT --settle-by NAME" % (RETIRED, SUCCESSOR) in printed
    assert result.to_dict()["settlements_unanswered"][0]["record"] == RETIRED

    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign", "--json-out", str(tmp_path / "c127.json")]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "c127.json").read_text(encoding="utf-8"))
    assert payload["settlements_unanswered"][0]["successor"] == SUCCESSOR


def test_taking_the_decision_up_again_clears_the_charge(tmp_path, monkeypatch, capsys):
    _quiet_withdrawal(tmp_path, days_ago=2.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-days", "1"]) == 1
    capsys.readouterr()
    assert cli.main(["evolve", "--settle-restore", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "the third claim is dated after the decision", "--settle-by", "bob"]) == 0
    capsys.readouterr()
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-days", "1"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "have been withdrawn for longer than" not in printed
    assert "1 decision(s) carry a withdrawal a later decision answered" in printed
