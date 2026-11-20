# -*- coding: utf-8 -*-
"""Round 135: the third batch family gets a preview, and the page's what-if counts it.

`--dry-run` knew two of the three file-shaped commands. Round 133 then stopped counting unpairable pairs as payable,
which made the page honest and made "what if I paid it all" quietly mean "the payable part" -- the same shape of
untruth the whole thread keeps finding, one layer up (boundaries 305, 319).
"""
import json

from brian import cli
from brian.evolve import load_settlements, preview_withdrawal_change, withdrawal_plan
from test_drop_batch import _two_unpayable
from test_reaffirm_batch import _two_stale
from test_restore_file import ONE


def _dry(*extra):
    return cli.main(["evolve", "--settle-drop-file"] + list(extra))


def test_the_drop_file_command_can_be_previewed(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = _dry(ONE, "--settle-why", "both records name other successors now", "--dry-run")
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert ("would withdraw 2 decision(s) on data/smd/stream-one.json in one dry run: nothing was recorded") in printed
    assert printed.count("(the record now names round-moved") == 2
    assert "nothing was written; drop --dry-run to withdraw them (boundary 304)." in printed
    # nothing was recorded: the plan still owes the same two pairs.
    rows = [row for row in cli.reaffirm_plan(cli.history(root, include_retired=True), load_settlements(root))
            if row["origin"] == ONE]
    assert rows and rows[0]["unpayable"] == 2
    # the fixture also keeps one payable pair on this file, so a preview that wrote would show up as stale 0
    assert rows[0]["stale"] == 1


def test_the_drop_preview_says_where_the_debt_goes(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert _dry(ONE, "--settle-why", "both moved", "--dry-run") == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "would owe 0 decision(s) no re-take can pay" in printed
    assert ("2 more withdrawal(s) would wait in the withdrawal queue (the queue goes from 0 to 2, and 2 of them "
            "cannot be taken up again)") in printed
    assert "dropping a decision does not settle anything" in printed
    # the preview is a computation, not a write.
    stored = load_settlements(root)
    assert not [item for item in stored if item.get("withdrawn")]
    assert withdrawal_plan(cli.history(root, include_retired=True), stored) == []


def test_dropping_for_real_empties_the_family_the_preview_described(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert _dry(ONE, "--settle-why", "both moved", "--dry-run") == 0
    capsys.readouterr()
    assert _dry(ONE, "--settle-why", "both moved", "--settle-by", "bob") == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "withdrawn 2 of 2 decision(s), 0 already withdrawn, 0 refused" in printed
    rows = [row for row in cli.reaffirm_plan(cli.history(root, include_retired=True), load_settlements(root))
            if row["origin"] == ONE]
    assert rows == [] or rows[0]["unpayable"] == 0
    assert withdrawal_plan(cli.history(root, include_retired=True), load_settlements(root))


def test_preview_withdrawal_change_leaves_the_store_alone():
    stored = [{"record": "a.json", "successor": "b.json", "at": "2026-01-01T00:00:00+00:00"}]
    after = preview_withdrawal_change(stored, [["a.json", "b.json"]], why="why", by="bob")
    assert stored[0].get("withdrawn") is None
    assert len(after[0]["withdrawn"]) == 1
    assert after[0]["withdrawn"][0]["by"] == "bob"


def test_the_what_if_counts_the_decisions_no_re_take_can_pay(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--campaign", "--what-if-records", ONE, "--json-out", "cake.json"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert ("owes 0 unanswered withdrawal(s), 1 stale decision(s) and 2 decision(s) no re-take can pay"
            in printed)
    assert "paying the decisions no re-take can pay (2 pair(s), `--settle-drop-file data/smd/stream-one.json`)" in printed
    assert "would owe 0 decision(s) no re-take can pay, and 2 more withdrawal(s) would wait" in printed
    assert "the pair becomes an unanswered withdrawal" in printed
    assert "nothing to pay: neither plan lists that file" not in printed
    assert json.loads((root / "cake.json").read_text(encoding="utf-8")) is not None


def test_a_file_owing_only_unpayable_work_is_not_called_empty(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--campaign", "--what-if-records", ONE]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "nothing to pay" not in printed
    assert "settle-drop-file" in printed


def test_the_dry_run_guard_names_the_third_family(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--audit", "--dry-run"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    # Round 156: read from the table, not from a copy of it (rounds 135/147/152 each updated one copy).
    assert cli.previews_sentence() in printed
