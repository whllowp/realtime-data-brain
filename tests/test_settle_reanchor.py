# -*- coding: utf-8 -*-
"""Round 138: a decision can be re-anchored to the successor its record now names.

Rounds 133 and 137 both refuse a pair whose record names a different successor, and both are right: the plan may
not offer work the command declines, and no door may assert what the ledger denies. Together they left the decision
with no forward step -- the old target stayed unpairable for ever, and the alternatives were "record a brand-new
decision" (leaving the old one behind) or "drop it" (throwing the decision away). The refusals raise a question
neither answered: the record moved, the decision did not.
"""
import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (history, load_settlements, moved_settlements, reanchor_settlement,
                          settlement_summary)
from test_provenance_block import _round
from test_settlement_reaffirm import _ago
from test_reaffirm_batch import LATER, _plan, _two_stale
from test_restore_guard import _payable_pairs, _retarget


def _unpairable(root):
    """A settled pair whose record now names another successor.

    No withdrawal: a withdrawn decision is not pending work at all (round 133's boundary 314), and the point of this
    round is the pair the *plan* counts but no command could pay -- retargeted, still standing, still owed.
    """
    pairs = _payable_pairs(root)
    pair = pairs[0]
    _retarget(root, pair[0])
    return pair


def test_reanchoring_moves_the_decision_to_what_the_record_names(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    change = reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    assert change["changed"] is True
    assert change["from"] == pair[1]
    assert change["to"] == "round-moved-away.json"
    entry = [item for item in load_settlements(root) if item.get("record") == pair[0]][0]
    assert entry["successor"] == "round-moved-away.json"
    assert entry["successor_history"] == [{"from": pair[1], "to": "round-moved-away.json",
                                          "at": change["at"], "why": "the record moved", "by": "alice"}]
    # Nothing is owed any more: the decision names the successor the record names, and the retirement that names
    # it is the premise of the decision, not a competing claim. A re-anchor does not create a re-take debt; a new
    # claim does (the next test shows that).
    row = [item for item in _plan(root) if item["origin"] == "data/smd/stream-one.json"][0]
    assert row["unpayable"] == 0
    assert pair[0] not in [item["record"] for item in row["pairs"]]
    assert pair[0] not in [item["record"] for item in row["unpayable_pairs"]]
    assert [item for item in moved_settlements(history(root, include_retired=True), load_settlements(root))
            if item["record"] == pair[0]] == []


def test_reanchoring_keeps_the_first_target(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    reanchor_settlement(root, pair[0], why="first move", by="alice")
    _round(root, pair[0], retired={"re_measured_as": "round-third-target.json", "at": LATER.isoformat()})
    second = reanchor_settlement(root, pair[0], why="second move", by="bob")
    assert second["from"] == "round-moved-away.json"
    assert second["to"] == "round-third-target.json"
    entry = [item for item in load_settlements(root) if item.get("record") == pair[0]][0]
    assert [move["to"] for move in entry["successor_history"]] == ["round-moved-away.json",
                                                                   "round-third-target.json"]


def test_reanchoring_the_same_target_twice_changes_nothing(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    again = reanchor_settlement(root, pair[0], why="the record moved again", by="alice")
    assert again["changed"] is False
    assert again["history"] == 1


def test_reanchoring_needs_a_reason_and_a_decision(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    for call, fragment in (
            (lambda: reanchor_settlement(root, pair[0], why=""), "needs a reason"),
            (lambda: reanchor_settlement(root, "", why="x"), "needs the record"),
            (lambda: reanchor_settlement(root, "round-nowhere.json", why="x"), "no settlement recorded")):
        try:
            call()
        except ValueError as error:
            assert fragment in str(error)
        else:  # pragma: no cover - the guard is the subject
            raise AssertionError("expected a refusal containing %r" % fragment)


def test_a_record_without_a_named_successor_cannot_be_reanchored(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    _round(root, pair[0], retired=None)
    try:
        reanchor_settlement(root, pair[0], why="nothing to point at")
    except ValueError as error:
        assert "names no successor" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a record with no successor must refuse")


def test_the_summary_counts_a_reanchored_decision_apart(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    summary = settlement_summary(load_settlements(root))
    assert summary["reanchored"] == 1
    assert summary["pairs"] >= 1


def test_the_page_says_the_target_moved(tmp_path):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    page = " ".join(format_campaign(campaign(root)).split())
    assert "1 decision(s) were re-anchored to the successor the record now names" in page
    assert "%s -> round-moved-away.json" % pair[0] in page
    assert "a re-anchored decision was not taken on its new successor" in page
    assert json.dumps(campaign(root).to_dict()["settlements"] is not None)


def test_the_history_row_says_the_target_moved(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--history"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert ("[re-anchored on " in printed) and ("%s -> round-moved-away.json]" % pair[1] in printed)


def test_the_cli_records_it_and_names_the_evidence_gap(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    monkeypatch.chdir(root)
    target = tmp_path / "reanchor.json"
    code = cli.main(["evolve", "--settle-reanchor", pair[0], "--settle-why", "the record moved",
                     "--settle-by", "alice", "--json-out", str(target)])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "re-anchored %s %s -> round-moved-away.json" % (pair[0], pair[1]) in printed
    assert "the earlier target stays in successor_history" in printed
    assert "was not taken on the new successor" in printed
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["reanchored"]["to"] == "round-moved-away.json"
    assert payload["reanchored"]["changed"] is True


def test_the_cli_refuses_without_a_reason(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reanchor", pair[0]]) == 2
    assert "needs a reason" in " ".join(capsys.readouterr().out.split())


def test_a_reanchored_decision_is_payable_again(tmp_path, monkeypatch, capsys):
    """The point of the whole round: the pair leaves the unpairable half, and a *new* claim is then payable."""
    root = _two_stale(tmp_path)
    pair = _unpairable(root)
    before = [item for item in _plan(root) if item["origin"] == "data/smd/stream-one.json"][0]
    assert before["unpayable"] == 1
    assert pair[0] not in [item["record"] for item in before["pairs"]]
    reanchor_settlement(root, pair[0], why="the record moved", by="alice")
    after = [item for item in _plan(root) if item["origin"] == "data/smd/stream-one.json"][0]
    assert after["unpayable"] == 0
    # a third record now claims the same successor, later than the re-anchor: the pair owes a re-take, and it is
    # payable -- before this round it would have been counted as unpairable work nobody could do
    successor = "round-moved-away.json"
    _round(root, "round-late.json", retired={"re_measured_as": successor,
                                             "at": _ago(-1).isoformat()})
    owed = [item for item in _plan(root) if item["origin"] == "data/smd/stream-one.json"][0]
    assert owed["unpayable"] == 0
    assert pair[0] in [item["record"] for item in owed["pairs"]]
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reaffirm-file", "data/smd/stream-one.json", "--settle-why",
                     "a later claim arrived on the successor the decision now names", "--settle-by", "alice"]) == 0
    assert "reaffirmed" in " ".join(capsys.readouterr().out.split())
