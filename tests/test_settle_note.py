# -*- coding: utf-8 -*-
"""Round 114: a decision can gain the explanation it lacked, without its date or successor moving."""
import pytest

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit, load_settlements, settle_link, settle_note
from brian.promote import provenance_block, repair_history

from test_provenance_block import RETIRED, SUCCESSOR, _mismatch
from test_untested_answer import NOW


def _settled_without_reason(tmp_path):
    """The store shape rounds before 113 could write: a decision with no sentence explaining it."""
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="a reason that came with the decision", now=NOW)
    store = load_settlements(tmp_path)
    for item in store:
        item.pop("why", None)
        item.pop("by", None)
    path = tmp_path / "var" / "evolve" / "settlements.json"
    import json

    path.write_text(json.dumps({"format": "brian.settled/1", "settlements": store}, indent=2) + "\n",
                    encoding="utf-8")


def test_a_note_records_the_reason_without_moving_the_decision(tmp_path):
    _settled_without_reason(tmp_path)
    before = load_settlements(tmp_path)[0]
    noted = settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    assert noted["changed"] is True
    assert noted["at"] == before["at"]
    assert "why" not in noted or noted.get("note")
    entry = load_settlements(tmp_path)[0]
    # The decision keeps its date and its successor; only the explanation is appended (boundary 257).
    assert entry["at"] == before["at"]
    assert entry["successor"] == SUCCESSOR
    assert entry["notes"] == [{"at": NOW.isoformat(), "why": "the queue paid it", "by": "alice"}]


def test_a_note_needs_a_reason(tmp_path):
    _settled_without_reason(tmp_path)
    with pytest.raises(ValueError) as failure:
        settle_note(tmp_path, RETIRED, SUCCESSOR, why="   ", now=NOW)
    assert "a note needs a reason" in str(failure.value)
    assert load_settlements(tmp_path)[0].get("notes") is None


def test_a_note_needs_a_settlement_to_explain(tmp_path):
    _mismatch(tmp_path)
    with pytest.raises(ValueError) as failure:
        settle_note(tmp_path, RETIRED, SUCCESSOR, why="nothing to explain", now=NOW)
    assert "no settlement recorded" in str(failure.value)
    assert "--settle-link" in str(failure.value)


def test_the_same_note_twice_is_a_no_op(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    again = settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    assert again["changed"] is False
    assert len(load_settlements(tmp_path)[0]["notes"]) == 1


def test_the_notes_survive_a_reload(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="first reason", by="alice", now=NOW)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="second reason", by="bob", now=NOW)
    notes = load_settlements(tmp_path)[0]["notes"]
    assert [note["why"] for note in notes] == ["first reason", "second reason"]
    assert [note["by"] for note in notes] == ["alice", "bob"]


def test_the_audit_prints_an_explanation_apart_from_the_reason_it_came_with(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    flat = " ".join(format_audit(audit(tmp_path)).split())
    assert "explained later, on %s, by alice" % NOW.isoformat() in flat
    assert "were explained after the fact: the decision keeps its date" in flat
    # The original decision still reads as unexplained: a later note is not the reason it came with.
    assert "carry no reason from the moment they were taken" in flat


def test_the_campaign_counts_explanations_added_later(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "1 of those decisions were explained after the fact" in flat
    assert "a later note does not make it explained at the time" in flat


def test_the_cli_appends_and_records_the_amendment(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--settle-note", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "the queue paid it", "--settle-by", "alice"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "noted           %s -> %s" % (RETIRED, SUCCESSOR) in printed
    assert "the decision keeps its date" in printed
    assert "explained by: alice" in printed
    assert "prints this apart from a reason the decision came with" in printed
    # Round 111's rule: the amendment reports itself in the plan's own numbers, even when they do not move.
    assert "repair plan" in printed
    assert repair_history(tmp_path)["last"]["action"] == "settle-note"


def test_the_cli_refuses_a_note_without_a_reason(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-note", "%s=%s" % (RETIRED, SUCCESSOR)]) == 2
    assert "a note needs a reason" in capsys.readouterr().out


def test_the_cli_refuses_a_malformed_note(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-note", RETIRED, "--settle-why", "x"]) == 2
    assert "takes RECORD=SUCCESSOR" in capsys.readouterr().out


def test_a_note_does_not_change_whether_the_link_is_charged(tmp_path):
    _settled_without_reason(tmp_path)
    assert provenance_block(tmp_path, 20) is None
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    # A note explains a decision; it does not reopen the contradiction (boundary 257).
    assert provenance_block(tmp_path, 20) is None
