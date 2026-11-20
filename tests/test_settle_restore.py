# -*- coding: utf-8 -*-
"""Round 126: a withdrawn decision can be taken up again by a new decision.

Round 125 could take a decision back and then had no way to put one back: `--settle-link` refuses a withdrawn pair
(recording it again is a new decision) and `--settle-reaffirm` refuses it too (a re-take would revive something the
record says no longer stands), so a contradiction a person had correctly withdrawn could only be settled by editing
the store by hand. `--settle-restore` records that new decision, and the store keeps both moments: withdrawn on X
because of ...; taken up again on Y because of ... .
"""
import json
from datetime import timedelta

import pytest

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (active_settlements, audit, format_audit, is_withdrawn, load_settlements,
                          replacement_links, restore_settlement, restored_settlements, settle_link,
                          settlement_summary, withdraw_settlement, withdrawn_settlements)

from test_provenance_block import RETIRED, SUCCESSOR, _mismatch
from test_untested_answer import NOW

AFTER = NOW + timedelta(days=1)
LATER = NOW + timedelta(days=2)


def _withdrawn(tmp_path):
    """One pair whose decision a person correctly took back -- the state a new decision starts from."""
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times",
                        by="alice", now=NOW)


def test_a_new_decision_keeps_the_withdrawal_and_records_why(tmp_path):
    _withdrawn(tmp_path)
    decision = restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                                  by="bob", now=AFTER)
    assert decision["changed"] is True and decision["redecisions"] == 1
    stored = load_settlements(tmp_path)[0]
    # Both moments stay: the withdrawal is why the pair was ever in doubt, and a reader who saw it must still find
    # it (round 125's rule that a decision is kept, applied to the other direction).
    assert [entry["by"] for entry in stored["withdrawn"]] == ["alice"]
    assert stored["redecisions"] == [{"at": AFTER.isoformat(),
                                      "why": "the third claim is dated after the decision", "by": "bob"}]
    assert stored["at"] == NOW.isoformat()  # the decision's own date never moves
    assert is_withdrawn(stored) is False


def test_the_same_new_decision_twice_is_a_no_op(tmp_path):
    _withdrawn(tmp_path)
    first = restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="read the books again", by="alice", now=AFTER)
    again = restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="read the books again", by="alice", now=LATER)
    assert first["changed"] is True and again["changed"] is False
    assert again["at"] == AFTER.isoformat()
    assert len(load_settlements(tmp_path)[0]["redecisions"]) == 1


def test_a_new_decision_needs_a_reason(tmp_path):
    _withdrawn(tmp_path)
    with pytest.raises(ValueError, match="needs a reason"):
        restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="  ", by="alice", now=AFTER)
    assert load_settlements(tmp_path)[0].get("redecisions") is None


def test_only_a_recorded_decision_can_be_taken_up_again(tmp_path):
    _mismatch(tmp_path)
    with pytest.raises(ValueError, match="no settlement recorded"):
        restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="nothing was ever decided", by="alice", now=AFTER)


def test_a_standing_decision_cannot_be_taken_up_again(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    with pytest.raises(ValueError, match="still stands"):
        restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="nothing to restore", by="alice", now=AFTER)


def test_the_question_follows_the_newest_event(tmp_path):
    _withdrawn(tmp_path)
    assert is_withdrawn(load_settlements(tmp_path)[0]) is True
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    assert is_withdrawn(load_settlements(tmp_path)[0]) is False
    # And it can be taken back again: the newest event decides, not "was it ever withdrawn".
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="it is wrong after all", by="bob", now=LATER)
    assert is_withdrawn(load_settlements(tmp_path)[0]) is True
    assert len(load_settlements(tmp_path)[0]["withdrawn"]) == 2


def test_the_pair_is_decided_again(tmp_path):
    from brian.evolve import history
    from brian.promote import provenance_block

    _withdrawn(tmp_path)
    assert provenance_block(tmp_path, 20)[0] == "broken link"  # a withdrawal restores the charge (boundary 292)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    # A decision is exactly the thing allowed to silence a contradiction, and this is one (boundary 294).
    assert provenance_block(tmp_path, 20) is None
    found = replacement_links(history(tmp_path, include_retired=True), load_settlements(tmp_path))
    assert [item["kind"] for item in found] == []


def test_the_summary_counts_a_pair_decided_twice(tmp_path):
    _withdrawn(tmp_path)
    before = settlement_summary(load_settlements(tmp_path))
    assert before["pairs"] == 0 and before["withdrawn"] == 1 and before["redecided"] == 0
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    after = settlement_summary(load_settlements(tmp_path))
    assert after["pairs"] == 1 and after["withdrawn"] == 0 and after["redecided"] == 1
    assert after["stored"] == 1
    assert [item["record"] for item in restored_settlements(load_settlements(tmp_path))] == [RETIRED]
    assert withdrawn_settlements(load_settlements(tmp_path)) == []
    assert active_settlements(load_settlements(tmp_path))[0]["record"] == RETIRED


def test_the_audit_prints_the_answered_withdrawal(tmp_path):
    _withdrawn(tmp_path)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    printed = " ".join(format_audit(audit(tmp_path)).split())
    # The row pads its columns, so compare through one normalised copy (round 121's lesson).
    assert "1 decision(s) carry a withdrawal a later decision answered" in printed
    assert "round-old.json -> round-new.json" in printed
    assert "was withdrawn by a person" not in printed  # it stands again, so it is not charged as withdrawn
    assert "only a person may do that, and only with a reason" in printed


def test_the_strict_gate_is_clean_again_after_a_new_decision(tmp_path, monkeypatch):
    _withdrawn(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 2
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_history_row_shows_both_moments(tmp_path, monkeypatch, capsys):
    _withdrawn(tmp_path)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "[withdrawn on %s, taken up again on %s]" % (NOW.isoformat(), AFTER.isoformat()) in printed
    assert "[the decision on round-new.json was withdrawn" not in printed
    # Round 124's placement rule, one round over: the pair names both ends, so a mark that does not check which end
    # says the successor was re-decided too. Exactly one row carries it.
    assert printed.count("[withdrawn on ") == 1


def test_the_campaign_names_the_restored_pair(tmp_path):
    _withdrawn(tmp_path)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision",
                       by="bob", now=AFTER)
    result = campaign(tmp_path)
    assert [item["record"] for item in result.settlements_restored] == [RETIRED]
    assert [item["record"] for item in result.to_dict()["settlements_restored"]] == [RETIRED]
    page = " ".join(format_campaign(result).split())
    assert "1 decision(s) were taken up again by a new decision" in page
    assert "withdrawn %s, taken up again %s" % (NOW.isoformat(), AFTER.isoformat()) in page


def _withdrawn_in_the_past(tmp_path):
    """The same state, dated before the machine clock: the CLI stamps its decision with `datetime.now()`."""
    from test_settlement_reaffirm import _ago

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice",
                now=_ago(3))
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times",
                        by="alice", now=_ago(2))


def test_the_cli_takes_the_decision_up_again(tmp_path, monkeypatch, capsys):
    _withdrawn_in_the_past(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "the third claim is dated after the decision", "--settle-by", "bob",
                     "--json-out", str(tmp_path / "restore.json")]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "taken up again round-old.json -> round-new.json" in printed
    assert "the withdrawal stays on record" in printed
    assert "restored by: bob" in printed
    assert "stops charging that pair" in printed
    payload = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert payload["restored"]["record"] == RETIRED and payload["restored"]["changed"] is True
    assert is_withdrawn(load_settlements(tmp_path)[0]) is False


def test_the_cli_refuses_a_new_decision_without_a_reason(tmp_path, monkeypatch, capsys):
    _withdrawn_in_the_past(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore", "%s=%s" % (RETIRED, SUCCESSOR)]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "a new decision needs a reason" in printed
    assert load_settlements(tmp_path)[0].get("redecisions") is None
