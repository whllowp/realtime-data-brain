# -*- coding: utf-8 -*-
"""Round 124: a decision re-taken repeatedly is evidence about the decision, not about the books.

`settlements.json` kept the last confirmation plus a list of re-affirmations, so "re-taken three times" was
countable and "re-taken in this order, covering these claims" was not. The timeline is what a reader can check
against the ledger; the count is derived from it.
"""
from brian import cli
from brian.campaign import format_campaign, campaign
from brian.evolve import (frequent_retakes, reaffirm_link, retake_timeline, settlement_summary)

from test_provenance_block import RETIRED, SUCCESSOR
from test_settle_note import _settled_without_reason
from test_settlement_reaffirm import _ago


def _retake(root, *, days, by="alice", note="read today's books"):
    reaffirm_link(root, RETIRED, SUCCESSOR, why=note, by=by, now=_ago(days))


def _thrice_re_taken(root):
    """One decision re-taken three times, each reading a later book."""
    _settled_without_reason(root)
    for days in (9, 6, 3):
        _retake(root, days=days)
    return root


def test_the_timeline_keeps_every_re_take_in_order(tmp_path):
    _thrice_re_taken(tmp_path)
    timeline = retake_timeline([{"record": RETIRED, "successor": SUCCESSOR, "at": _ago(30).isoformat(),
                                 "why": "decided first", "by": "alice",
                                 "reaffirmations": [{"at": (day.isoformat()), "why": "read", "by": "bob",
                                                     "claimants": ["round-other.json"]}
                                                    for day in (_ago(9), _ago(3))]}])
    entries = timeline["%s=%s" % (RETIRED, SUCCESSOR)]
    # The first entry is the decision itself, and its kind says so: "taken" and "re-taken" are different facts
    # about the same pair (boundary 289).
    assert [entry["kind"] for entry in entries] == ["taken", "retaken", "retaken"]
    assert [entry["by"] for entry in entries] == ["alice", "bob", "bob"]
    assert entries[0]["why"] == "decided first"
    assert [entry["at"] for entry in entries] == sorted(entry["at"] for entry in entries)


def test_a_single_decision_has_a_one_entry_timeline(tmp_path):
    _settled_without_reason(tmp_path)
    from brian.promote import load_settlements

    entries = retake_timeline(load_settlements(tmp_path))["%s=%s" % (RETIRED, SUCCESSOR)]
    assert len(entries) == 1
    assert entries[0]["kind"] == "taken"


def test_three_re_takes_name_the_decision_and_nothing_else_does(tmp_path):
    _settled_without_reason(tmp_path)
    assert frequent_retakes(_load(tmp_path)) == []
    _retake(tmp_path, days=9)
    _retake(tmp_path, days=6)
    # Two re-takes are still "the books moved twice": the threshold is where repetition becomes evidence about the
    # decision rather than about the ledger (boundary 288).
    assert frequent_retakes(_load(tmp_path)) == []
    third = _ago(3)
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="read today's books", by="alice", now=third)
    rows = frequent_retakes(_load(tmp_path))
    assert [(row["record"], row["retakes"], row["last_by"]) for row in rows] == [(RETIRED, 3, "alice")]
    assert rows[0]["last_at"] == third.isoformat()


def test_the_summary_counts_them_apart(tmp_path):
    _thrice_re_taken(tmp_path)
    summary = settlement_summary(_load(tmp_path))
    assert summary["reaffirmed"] == 1
    assert summary["repeated"] == 1


def test_the_campaign_names_the_decisions_that_keep_needing_one(tmp_path):
    _thrice_re_taken(tmp_path)
    page = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "have been re-taken at least 3 time(s), which usually means the decision is wrong" in page
    assert "%s -> %s (3 time(s)" % (RETIRED, SUCCESSOR) in page


def test_the_campaign_says_nothing_about_a_decision_taken_once(tmp_path):
    _settled_without_reason(tmp_path)
    page = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "have been re-taken at least 3 time(s)" not in page


def test_the_payloads_carry_the_timeline_and_the_names(tmp_path):
    _thrice_re_taken(tmp_path)
    payload = campaign(tmp_path).to_dict()
    timeline = payload["retake_timeline"]["%s=%s" % (RETIRED, SUCCESSOR)]
    assert [entry["kind"] for entry in timeline].count("retaken") == 3
    assert [(item["record"], item["retakes"]) for item in payload["frequent_retakes"]] == [(RETIRED, 3)]


def test_the_mark_sits_only_on_the_record_the_decision_settles(tmp_path, monkeypatch, capsys):
    _thrice_re_taken(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if "keeps going stale" in line]
    # The successor was never re-taken: naming it as re-taken would be a fact about a different record (round 124's
    # demo printed the mark on both ends of the pair).
    assert rows
    assert all("[retired, re-measured as %s]" % SUCCESSOR in row for row in rows)


def test_the_history_row_says_the_decision_keeps_going_stale(tmp_path, monkeypatch, capsys):
    _thrice_re_taken(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "[re-taken 3 time(s): the decision keeps going stale]" in flat


def test_a_row_re_taken_twice_is_not_marked(tmp_path, monkeypatch, capsys):
    _settled_without_reason(tmp_path)
    _retake(tmp_path, days=9)
    _retake(tmp_path, days=6)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "the decision keeps going stale" not in flat


def _load(root):
    from brian.promote import load_settlements

    return load_settlements(root)
