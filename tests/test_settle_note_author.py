# -*- coding: utf-8 -*-
"""Round 115 (item 101): the note's author, the history's account of the notes, and a refusal that names
the command which can explain an old decision."""
import pytest

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import settle_link, settle_note

from test_provenance_block import OTHER, RETIRED, SUCCESSOR, _mismatch
from test_untested_answer import NOW


def _two_decisions(root):
    """Two settled decisions with no reason: one pair, and a second pair built by hand.

    A second contradiction cannot be built from one fixture, so the second entry is written into the store the
    way rounds before 113 wrote them -- dated, and explained by nothing.
    """
    import json

    from test_settle_note import _settled_without_reason

    _settled_without_reason(root)
    store = root / "var" / "evolve" / "settlements.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["settlements"].append({"record": "round-other.json", "successor": "round-again.json",
                                "at": "2026-11-19T21:00:00+00:00", "why": ""})
    store.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_the_campaign_names_who_explained_what(tmp_path):
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "explained later by: alice 1" in flat


def test_an_explanation_without_a_name_is_its_own_bucket(tmp_path):
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    # Never folded into a named explainer: a missing name is a third state, not a default (boundary 166).
    assert "explained later by: and no explainer named 1" in flat
    assert "alice" not in flat


def test_two_notes_from_two_people_both_appear(tmp_path):
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="first", by="alice", now=NOW)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="second", by="bob", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "explained later by: alice 1, bob 1" in flat


def test_the_history_settled_line_carries_the_notes(tmp_path, monkeypatch, capsys):
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="alice", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 of them were explained later: %s -> %s (1 note(s), last by alice on %s)" % (
        RETIRED, SUCCESSOR, NOW.isoformat()) in flat


def test_the_history_says_when_nobody_was_named(tmp_path, monkeypatch, capsys):
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "last by no one named on %s" % NOW.isoformat() in flat


def test_a_refused_new_decision_points_at_the_note_command(tmp_path):
    _mismatch(tmp_path)
    with pytest.raises(ValueError) as failure:
        settle_link(tmp_path, RETIRED, SUCCESSOR)
    message = str(failure.value)
    assert "a settlement needs a reason" in message
    # The refusal is not the only route: an existing decision is explained, never rewritten (round 114).
    assert "--settle-note" in message
    assert "without moving its date" in message


def test_the_cli_refusal_counts_the_decisions_that_need_explaining(tmp_path, monkeypatch, capsys):
    _two_decisions(tmp_path)
    monkeypatch.chdir(tmp_path)
    # A new pair, so the reason refusal fires while two old decisions still lack one.
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (OTHER, SUCCESSOR)]) == 2
    printed = capsys.readouterr().out
    assert "a settlement needs a reason" in printed
    assert "hint            2 recorded decision(s) have no reason at all" in printed
    assert "--settle-note RECORD=SUCCESSOR --settle-why TEXT" in printed


def test_the_cli_refusal_prints_no_hint_when_every_decision_is_explained(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    # The stored decision came with its reason, so there is nothing for the hint to send the caller to.
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid this one", by="alice", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (OTHER, SUCCESSOR)]) == 2
    printed = capsys.readouterr().out
    assert "a settlement needs a reason" in printed
    assert "hint            " not in printed
