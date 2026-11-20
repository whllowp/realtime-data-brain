# -*- coding: utf-8 -*-
"""Round 116 (item 102): the settlement's account in the JSON, one contradiction refused, and the row
that carries a decision says whether it was ever explained."""
import json

import pytest

from brian import cli
from brian.campaign import campaign
from brian.evolve import load_settlements, settle_link, settle_note, settlement_summary

from test_provenance_block import RETIRED, SUCCESSOR, _mismatch
from test_settle_note import _settled_without_reason
from test_untested_answer import NOW


def test_the_summary_keeps_the_four_states_apart(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="because the queue paid it", by="alice", now=NOW)
    summary = settlement_summary(load_settlements(tmp_path))
    assert summary["pairs"] == 1
    assert summary["explained"] == 1
    assert summary["explained_later"] == 0
    assert summary["unexplained"] == 0
    assert summary["deciders"] == {"alice": 1}
    assert summary["unattributed"] == 0


def test_a_decision_explained_later_is_not_counted_as_unexplained(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="bob", now=NOW)
    summary = settlement_summary(load_settlements(tmp_path))
    assert summary["explained"] == 0          # it still came with no reason
    assert summary["explained_later"] == 1
    assert summary["unexplained"] == 0        # somebody has explained it
    assert summary["explainers"] == {"bob": 1}


def test_a_decision_nobody_explained_is_counted_alone(tmp_path):
    _settled_without_reason(tmp_path)
    summary = settlement_summary(load_settlements(tmp_path))
    assert summary["unexplained"] == 1
    assert summary["explained_later"] == 0
    assert summary["explainers"] == {}
    # No decider was recorded either, and the empty name is its own bucket (boundary 166).
    assert summary["deciders"] == {"": 1}
    assert summary["unattributed"] == 1


def test_an_empty_store_summarises_to_zeros(tmp_path):
    summary = settlement_summary([])
    # Round 124: `repeated` counts decisions re-taken three times or more -- repeating a re-take says the decision
    # keeps going stale, which is evidence about the decision (boundary 288).
    # Round 125: `stored` counts every decision the file holds and `withdrawn` the ones a person took back, so a
    # page can say "3 recorded, 1 withdrawn" without recomputing it from the list (boundary 291).
    assert summary == {"pairs": 0, "explained": 0, "explained_later": 0, "unexplained": 0, "reaffirmed": 0,
                       "repeated": 0, "deciders": {}, "explainers": {}, "unattributed": 0,
                       "stored": 0, "withdrawn": 0, "redecided": 0}


def test_the_audit_json_carries_the_settlement_account(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="bob", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--json-out", "audit.json"]) == 0
    payload = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert payload["settlements"]["pairs"] == 1
    assert payload["settlements"]["explained_later"] == 1
    assert payload["settlements"]["explainers"] == {"bob": 1}


def test_the_campaign_json_carries_the_settlement_account(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="bob", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign", "--json-out", "campaign.json"]) == 0
    payload = json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))
    assert payload["settlement_summary"]["pairs"] == 1
    assert payload["settlement_summary"]["explained_later"] == 1
    # The page and the payload read the same account, not two computations that happen to agree today.
    summary = campaign(tmp_path).settlement_summary
    assert summary == payload["settlement_summary"]


def test_the_history_row_says_a_decision_was_never_explained(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "[settled with %s and no reason was recorded]" % SUCCESSOR in flat


def test_the_history_row_does_not_say_that_once_someone_explained_it(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="bob", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "[settled with %s by a recorded decision]" % SUCCESSOR in flat
    assert "and no reason was recorded" not in flat


def test_one_author_cannot_write_two_reasons_at_one_moment(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="first reason", by="alice", now=NOW)
    with pytest.raises(ValueError) as failure:
        # Same author, same recorded second, different account: the store cannot order them, so it refuses.
        settle_note(tmp_path, RETIRED, SUCCESSOR, why="a different reason", by="alice",
                    now=NOW.replace(microsecond=500000))
    assert "carry the same second" in str(failure.value)
    assert len(load_settlements(tmp_path)[0]["notes"]) == 1


def test_two_authors_at_one_moment_are_two_people_explaining(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="alice's account", by="alice", now=NOW)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="bob's account", by="bob", now=NOW)
    assert len(load_settlements(tmp_path)[0]["notes"]) == 2
