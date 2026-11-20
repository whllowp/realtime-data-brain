# -*- coding: utf-8 -*-
"""Round 139: a retargeted file owes the same step once per decision, so one command pays it.

Round 138 gave a single decision a way to follow its record. A file that was retargeted owes that step N times, and
typing it N times is how one gets skipped -- the same reason every other family of debt in this ledger grew a file
command (rounds 128/129/137). The batch takes its records from `reanchor_plan`, which is the list the campaign page
prints, so the command can only pay what the reader was already shown (round 101).
"""
import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (history, load_settlements, preview_reanchor_change, reanchor_plan,
                          withdraw_settlement)
from test_reaffirm_batch import _plan, _two_stale
from test_restore_guard import _retarget

FILE = "data/smd/stream-one.json"
MOVED = "round-moved-away.json"


def _payable(root):
    """The pairs the file still owes and a re-take can pay -- read from the plan, never assumed."""
    rows = [row for row in _plan(root) if row["origin"] == FILE]
    return [(str(item["record"]), str(item["successor"])) for item in rows[0]["pairs"]] if rows else []


def _retargeted(root, count=2):
    """Retarget the first `count` payable decisions: the fixture's own way of moving a record."""
    pairs = _payable(root)[:count]
    assert len(pairs) == count, "the fixture must owe %d payable decision(s)" % count
    for record, _successor in pairs:
        _retarget(root, record)
    return pairs


def _rows(root):
    return reanchor_plan(history(root, include_retired=True), load_settlements(root))


def test_the_plan_groups_the_records_one_file_owes(tmp_path):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    rows = _rows(root)
    assert len(rows) == 1
    assert rows[0]["origin"] == FILE
    assert [item["record"] for item in rows[0]["pairs"]] == [record for record, _s in pairs]
    assert [item["from"] for item in rows[0]["pairs"]] == [successor for _r, successor in pairs]
    assert {item["to"] for item in rows[0]["pairs"]} == {MOVED}
    # A plan nobody can run is a warning, so the row carries the command that runs it (boundary 261).
    assert rows[0]["batch"] == ("brian evolve --settle-reanchor-file %s --settle-why TEXT --settle-by NAME" % FILE)


def test_a_decision_that_already_names_the_record_successor_is_not_listed(tmp_path):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    with open(root / "var" / "evolve" / "settlements.json", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored  # the store exists before anything is asserted about it
    # Re-anchor exactly one of the two: the plan must forget it and keep the other.
    from brian.evolve import reanchor_settlement
    reanchor_settlement(root, pairs[0][0], why="the record moved", by="alice")
    rows = _rows(root)
    assert [item["record"] for item in rows[0]["pairs"]] == [pairs[1][0]]
    reanchor_settlement(root, pairs[1][0], why="the record moved", by="alice")
    assert _rows(root) == []


def test_a_withdrawn_decision_owes_no_reanchor(tmp_path):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    withdraw_settlement(root, pairs[0][0], pairs[0][1], why="the earlier call was wrong", by="bob")
    rows = _rows(root)
    assert [item["record"] for item in rows[0]["pairs"]] == [pairs[1][0]]


def test_the_preview_changes_a_copy_and_writes_nothing(tmp_path):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    records = [record for record, _s in pairs]
    before = {item["record"]: dict(item) for item in load_settlements(root)}
    after = {item["record"]: item for item in
             preview_reanchor_change(load_settlements(root), history(root, include_retired=True), records)}
    for record in records:
        assert after[record]["successor"] == MOVED
        assert [move["to"] for move in after[record]["successor_history"]] == [MOVED]
        assert after[record]["successor_history"][0]["why"] == "previewed"
    # The store on disk is untouched: a preview that writes is the mistake this thread keeps finding (boundary 304).
    stored = {item["record"]: item for item in load_settlements(root)}
    for record in records:
        assert stored[record]["successor"] == before[record]["successor"]
        assert "successor_history" not in stored[record]


def test_the_file_command_reanchors_every_decision_on_it(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", "the record was retargeted",
                     "--settle-by", "carol"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "re-anchoring 2 decision(s) on data/smd/stream-one.json in one command" in printed
    assert "re-anchored 2 of 2 decision(s), 0 needed no change, 0 refused" in printed
    stored = {item["record"]: item for item in load_settlements(root)}
    for record, successor in pairs:
        assert stored[record]["successor"] == MOVED
        assert stored[record]["successor_history"][0]["from"] == successor
    # Nothing is owed on that half any more: the decision names what the record names.
    row = [item for item in _plan(root) if item["origin"] == FILE][0]
    assert row["unpayable"] == 0


def test_a_file_with_nothing_left_says_so(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", "x", "--settle-by", "carol"]) == 0
    capsys.readouterr()
    assert cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", "x", "--settle-by", "carol"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert ("no decision on data/smd/stream-one.json still names a successor its record has left" in printed)
    assert "--campaign" in printed


def test_the_dry_run_previews_the_whole_file_and_writes_nothing(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    before = {item["record"]: item["successor"] for item in load_settlements(root)}
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", "the record was retargeted",
                     "--settle-by", "carol", "--dry-run"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert ("would re-anchor 2 decision(s) on data/smd/stream-one.json in one dry run: nothing was recorded"
            in printed)
    for record, successor in pairs:
        assert "%s (%s -> round-moved-away.json)" % (record, successor) in printed
    assert "nothing was written; drop --dry-run to re-anchor them (boundary 304)" in printed
    # The two numbers a re-anchor moves, and only those two (boundary 330).
    assert ("after this run data/smd/stream-one.json would owe 0 decision(s) no re-take can pay, and 0 "
            "decision(s) would still name a successor their record has left") in printed
    assert "a re-anchored decision was not taken on its new successor" in printed
    assert {item["record"]: item["successor"] for item in load_settlements(root)} == before


def test_one_refused_record_does_not_stop_the_rest(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor", pairs[0][0], "round-nowhere.json",
                     "--settle-why", "the record was retargeted", "--settle-by", "carol"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "cannot re-anchor that decision: no settlement recorded for round-nowhere.json" in printed
    assert "re-anchored 1 of 2 decision(s), 0 needed no change, 1 refused" in printed
    stored = {item["record"]: item for item in load_settlements(root)}
    assert stored[pairs[0][0]]["successor"] == MOVED


def test_the_what_if_asks_the_fourth_family(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--campaign", "--what-if-records", FILE]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert ("re-anchoring the decisions whose record moved (2 record(s), `--settle-reanchor-file "
            "data/smd/stream-one.json`)") in printed
    assert ("after this run data/smd/stream-one.json would owe 0 decision(s) no re-take can pay, and 0 "
            "decision(s) would still name a successor their record has left") in printed
    # and it still refuses the empty answer: the file does owe work, all of which the fourth family counts
    assert "nothing to pay" not in printed


def test_the_campaign_line_names_the_command(tmp_path):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    page = " ".join(format_campaign(campaign(root)).split())
    assert "2 decision(s) still name a successor their record has left" in page
    assert ("%s round-new.json -> round-moved-away.json" % pairs[0][0]) in page
    assert "--settle-reanchor-file data/smd/stream-one.json" in page
    # The page is a plan, not a receipt: it says nothing was re-anchored yet.
    assert "decision(s) were re-anchored to the successor the record now names" not in page
