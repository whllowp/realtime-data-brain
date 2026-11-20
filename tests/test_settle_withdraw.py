# -*- coding: utf-8 -*-
"""Round 125: a decision can be withdrawn, and the contradiction it settled is charged again.

The withdrawal is written into the decision it withdraws -- kept, dated, attributed -- and because `replacement_links`
reads only the decisions still standing, the pair goes back to being a contradiction. `--strict` therefore gets
*stricter* after a withdrawal, never looser (boundary 292): taking a decision back must not quietly make a defect
disappear.
"""
import json

import pytest

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (active_settlements, audit, format_audit, is_withdrawn, load_settlements,
                          reaffirm_link, replacement_links, settle_link, settlement_summary, unexplained_decisions,
                          withdraw_settlement, withdrawn_settlements)

from test_provenance_block import RETIRED, SUCCESSOR, _mismatch
from test_untested_answer import NOW

OTHER = "round-other.json"


def _settled(tmp_path):
    """One settled pair, and the books agree with it -- the state a withdrawal starts from."""
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)


def test_a_withdrawal_keeps_the_decision_and_records_why(tmp_path):
    _settled(tmp_path)
    decision = withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise three times",
                                   by="alice", now=NOW)
    assert decision["changed"] is True and decision["withdrawals"] == 1
    stored = load_settlements(tmp_path)[0]
    assert stored["at"] == NOW.isoformat()  # the decision keeps the moment it was taken
    assert stored["successor"] == SUCCESSOR
    assert stored["withdrawn"] == [{"at": NOW.isoformat(), "why": "the books have said otherwise three times",
                                    "by": "alice"}]
    assert is_withdrawn(stored) is True


def test_the_same_withdrawal_twice_is_a_no_op(tmp_path):
    _settled(tmp_path)
    first = withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    again = withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    assert first["changed"] is True and again["changed"] is False
    assert len(load_settlements(tmp_path)[0]["withdrawn"]) == 1


def test_a_withdrawal_needs_a_reason(tmp_path):
    _settled(tmp_path)
    with pytest.raises(ValueError, match="needs a reason"):
        withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="   ", by="alice", now=NOW)
    assert load_settlements(tmp_path)[0].get("withdrawn") is None


def test_only_a_recorded_decision_can_be_withdrawn(tmp_path):
    _settled(tmp_path)
    with pytest.raises(ValueError, match="only a recorded decision"):
        withdraw_settlement(tmp_path, OTHER, SUCCESSOR, why="nothing to take back", by="alice", now=NOW)


def test_a_withdrawn_decision_owes_nothing(tmp_path):
    _settled(tmp_path)
    before = settlement_summary(load_settlements(tmp_path))
    assert before["pairs"] == 1 and before["withdrawn"] == 0 and before["stored"] == 1

    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    after = settlement_summary(load_settlements(tmp_path))
    # The decision is still stored, still counted as stored -- and owed nothing: no note, no re-take, no
    # explanation (boundary 293).
    assert after["stored"] == 1 and after["withdrawn"] == 1 and after["pairs"] == 0
    assert after["unexplained"] == 0
    assert withdrawn_settlements(load_settlements(tmp_path))[0]["record"] == RETIRED
    assert active_settlements(load_settlements(tmp_path)) == []
    assert unexplained_decisions(load_settlements(tmp_path)) == []


def test_the_contradiction_comes_back(tmp_path):
    from brian.evolve import history
    from brian.promote import LINK_BROKEN, provenance_block

    _settled(tmp_path)
    assert provenance_block(tmp_path, 20) is None  # the books agree with the decision

    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    assert provenance_block(tmp_path, 20)[0] == LINK_BROKEN
    found = replacement_links(history(tmp_path, include_retired=True), load_settlements(tmp_path))
    assert [item["kind"] for item in found] == ["mismatch"]


def test_the_audit_prints_the_withdrawal_apart_from_the_standing_decisions(tmp_path):
    _settled(tmp_path)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    printed = format_audit(audit(tmp_path))
    # The row pads its columns (`%-26s`), so compare through one normalised copy: a padded row is not a sentence
    # with double spaces in it (round 121's lesson, hit again).
    flat = " ".join(printed.split())
    assert "1 decision(s) were withdrawn by a person, and the contradiction each one settled is back" in flat
    assert "round-old.json -> round-new.json" in flat
    assert "does not settle anything, so the pair is charged again" in flat
    # A withdrawn decision is not a decision that still stands: the settled block must not name it (boundary 291).
    assert "settled by a recorded decision, not by agreement" not in flat


def test_the_strict_gate_gets_stricter_after_a_withdrawal(tmp_path, monkeypatch):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    # The pair is a contradiction again, so `--strict` exits 2 -- a withdrawal must never buy a clean gate
    # (boundary 292).
    assert cli.main(["evolve", "--audit", "--strict"]) == 2


def test_the_history_row_says_the_decision_was_withdrawn(tmp_path, monkeypatch, capsys):
    from brian.evolve import history

    _settled(tmp_path)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    printed = " ".join(format_audit(audit(tmp_path)).split())
    assert "withdrawn by a person" in printed

    # The row marker: everything the page prints, through the same code path the command uses.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    captured = " ".join(capsys.readouterr().out.split())
    assert "the decision on round-new.json was withdrawn on %s: the pair is charged again" % NOW.isoformat() in captured
    assert "[settled with round-new.json" not in captured
    assert history(tmp_path, include_retired=True)  # the records themselves were never touched


def test_the_campaign_names_the_withdrawal_and_counts_it(tmp_path):
    _settled(tmp_path)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    result = campaign(tmp_path)
    assert [item["record"] for item in result.settlements_withdrawn] == [RETIRED]
    assert [item["record"] for item in result.to_dict()["settlements_withdrawn"]] == [RETIRED]
    page = " ".join(format_campaign(result).split())
    assert "1 decision(s) were withdrawn by a person, so the contradiction each one settled is charged again" in page
    assert "round-old.json -> round-new.json" in page


def test_the_plan_stops_naming_the_command_the_withdrawal_makes_impossible(tmp_path):
    from brian.promote import provenance_block, repair_plan
    from test_settle_link import _walk

    _mismatch(tmp_path)
    why = provenance_block(tmp_path, 20)[1]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])
    before = repair_plan(tmp_path)
    assert before and before[0]["pairs"][0]["withdrawn"] is False
    assert before[0]["remedy"] == "brian evolve --settle-link %s=%s" % (RETIRED, SUCCESSOR)

    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the retirement mark is the surviving claim", by="alice", now=NOW)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    after = repair_plan(tmp_path)
    # `--settle-link` refuses a withdrawn pair, so the plan must not hand the reader that command (boundary 261).
    assert after and after[0]["pairs"][0]["withdrawn"] is True
    assert "--settle-link" not in after[0]["remedy"]
    assert "was withdrawn" in after[0]["remedy"]


def test_a_withdrawn_decision_cannot_be_re_taken_or_re_recorded(tmp_path):
    _settled(tmp_path)
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="wrong decision", by="alice", now=NOW)
    with pytest.raises(ValueError, match="withdrawn on"):
        reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="still standing", by="alice", now=NOW)
    # Re-recording it would be a new decision, and this build has no command for one yet (item 112).
    with pytest.raises(ValueError, match="new decision and this build has no command"):
        settle_link(tmp_path, RETIRED, SUCCESSOR, why="decided again", by="alice", now=NOW)


def test_the_cli_withdraws_and_says_whether_a_contradiction_waits(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-drop", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "the books have said otherwise three times", "--settle-by", "alice",
                     "--json-out", str(tmp_path / "drop.json")]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "withdrawn round-old.json -> round-new.json" in printed
    assert "the decision stays in the store, with this withdrawal on" in printed
    assert "withdrawn by: alice" in printed
    assert "the contradiction it settled is back" in printed

    payload = json.loads((tmp_path / "drop.json").read_text(encoding="utf-8"))
    assert payload["withdrawn"]["record"] == RETIRED and payload["withdrawn"]["changed"] is True


def test_the_cli_refuses_a_withdrawal_without_a_reason(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-drop", "%s=%s" % (RETIRED, SUCCESSOR)]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "a withdrawal needs a reason" in printed
    assert load_settlements(tmp_path)[0].get("withdrawn") is None
