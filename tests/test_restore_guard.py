# -*- coding: utf-8 -*-
"""Round 137: the restore door is as strict as the re-take door.

Round 136's ordered plan found that `--settle-restore` would take a decision up again on a record that now names a
different successor -- the exact assertion `reaffirm_link` has refused since round 119. One constraint, two write
doors, one of them unchecked (boundary 323): restoring such a pair manufactured the debt round 133 had just stopped
counting as payable. Now the restore door refuses too, the withdrawal plan separates the half no restore can pay,
and both pages and file commands say which half is which.
"""
from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (history, load_settlements, restore_settlement, withdraw_settlement,
                          withdrawal_plan)
from test_drop_batch import _two_unpayable
from test_provenance_block import _round
from test_reaffirm_batch import FILE, LATER, SUCCESSOR, _plan, _two_stale


def _payable_pairs(root):
    rows = [row for row in _plan(root) if row["origin"] == FILE and row.get("pairs")]
    assert rows, "the fixture must owe a payable re-take"
    return [(str(item["record"]), str(item["successor"])) for item in rows[0]["pairs"]]


def _withdrawn(root, count=1):
    pairs = _payable_pairs(root)[:count]
    for record, successor in pairs:
        withdraw_settlement(root, record, successor, why="the earlier call was wrong", by="bob")
    return pairs


def _retarget(root, record, successor="round-moved-away.json"):
    _round(root, record, retired={"re_measured_as": successor, "at": LATER.isoformat()})
    return root


def _plan_row(root):
    rows = [row for row in withdrawal_plan(history(root, include_retired=True), load_settlements(root))
            if row["origin"] == FILE]
    assert rows, "the fixture must owe a withdrawal on %s" % FILE
    return rows[0]


def test_a_restore_refuses_a_pair_the_record_no_longer_names(tmp_path):
    root = _two_stale(tmp_path)
    pair = _withdrawn(root)[0]
    _retarget(root, pair[0])
    try:
        restore_settlement(root, pair[0], pair[1], why="taking it back", by="alice")
    except ValueError as error:
        message = str(error)
    else:  # pragma: no cover - the guard is the subject of this test
        raise AssertionError("a retargeted pair must not be restored")
    assert "no longer names" in message
    assert "round-moved-away.json" in message
    assert ("--settle-drop %s=%s" % pair) in message
    assert "--settle-link" in message
    assert load_settlements(root)[0].get("redecisions") in (None, [])


def test_the_withdrawal_plan_separates_what_a_restore_can_pay(tmp_path):
    root = _two_stale(tmp_path)
    pair = _withdrawn(root)[0]
    _retarget(root, pair[0])
    row = _plan_row(root)
    assert row["decisions"] == 0
    assert row["unpayable"] == 1
    assert row["waiting"] == 1
    assert [item["record"] for item in row["unpayable_pairs"]] == [pair[0]]
    assert row["unpayable_pairs"][0]["names"] == "round-moved-away.json"
    assert row["stuck"] == ["brian evolve --settle-drop %s=%s --settle-why TEXT" % pair]
    assert row["pair"] == ""
    assert row["batch"] == ""


def test_a_restore_still_pays_the_payable_half(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    payable, stuck = _withdrawn(root, count=2)
    _retarget(root, stuck[0])
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-restore-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "taken up again %s -> %s" % payable in printed
    assert "leaving 1 withdrawal(s) no restore can pay: the records name other successors" in printed
    assert "--settle-drop-file %s" % FILE in printed
    assert [item for item in load_settlements(root) if item.get("redecisions")]


def test_a_file_holding_only_unreachable_withdrawals_says_so(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    for record, successor in _withdrawn(root, count=2):
        _retarget(root, record)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-restore-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "2 withdrawal(s) on %s cannot be taken up again" % FILE in printed
    assert "--settle-drop-file %s" % FILE in printed


def test_the_page_names_the_unreachable_half(tmp_path):
    root = _two_stale(tmp_path)
    pair = _withdrawn(root)[0]
    _retarget(root, pair[0])
    page = " ".join(format_campaign(campaign(root)).split())
    assert "1 withdrawal(s) on %s cannot be taken up again" % FILE in page
    assert "--settle-drop-file %s" % FILE in page


def test_the_what_if_counts_the_whole_queue_and_says_which_half_it_cannot_reach(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    pair = _withdrawn(root)[0]
    _retarget(root, pair[0])
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--campaign", "--what-if-records", FILE]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "owes 1 unanswered withdrawal(s)" in printed
    assert "NONE of that reaches 1 withdrawal(s) here: the records name other successors" in printed
    assert "`brian evolve --settle-drop-file %s --settle-why TEXT`" % FILE in printed


def test_a_pair_whose_record_still_names_it_is_restored(tmp_path):
    """The guard must not become a second wall: the ordinary case still goes through."""
    root = _two_stale(tmp_path)
    pair = _withdrawn(root)[0]
    change = restore_settlement(root, pair[0], pair[1], why="read today's books", by="alice")
    assert change["changed"] is True
    assert withdrawal_plan(history(root, include_retired=True), load_settlements(root)) == []
    restored = [item for item in load_settlements(root) if item.get("redecisions")]
    assert [str(item.get("record") or "") for item in restored] == [pair[0]]
    assert SUCCESSOR  # the fixture's successor is what a payable pair names
