# -*- coding: utf-8 -*-
"""Round 130: the two batch commands can be previewed before they write.

Round 83's rule -- a command that edits recorded history must be able to show the edit first -- reached the batch
commands only now. They write N decisions in one go, and the only way to see which ones was to run them, which is
how a reason gets typed for a pair nobody meant to touch.
"""
import json

from brian import cli
from brian.evolve import load_settlements

from test_reaffirm_batch import _two_stale
from test_restore_file import ONE, _plan as _withdrawal_plan, _two_on_one_file


def _restored(tmp_path):
    return [bool(item.get("redecisions")) for item in load_settlements(tmp_path)]


def test_a_dry_run_lists_the_pairs_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = _restored(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol", "--dry-run"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "would take up 2 decision(s) on data/smd/stream-one.json in one dry run: nothing was recorded" in printed
    assert "round-old.json=round-new.json (withdrawn" in printed
    assert "nothing was written; afterwards that file would owe no unanswered withdrawal" in printed
    assert _restored(tmp_path) == before
    assert len(_withdrawal_plan(tmp_path)[0]["records"]) == 2


def test_the_dry_run_leaves_the_file_payable(tmp_path, monkeypatch):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--dry-run"]) == 0
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 0
    assert _restored(tmp_path) == [True, True]
    assert _withdrawal_plan(tmp_path) == []


def test_a_reaffirm_dry_run_lists_the_pairs_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _two_stale(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = [len(item.get("reaffirmations") or []) for item in load_settlements(tmp_path)]
    assert cli.main(["evolve", "--settle-reaffirm-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol", "--dry-run"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "would re-take 3 decision(s) on data/smd/stream-one.json in one dry run: nothing was recorded" in printed
    assert "(never)" in printed and "(again)" in printed
    assert "nothing was written; drop --dry-run to re-take them" in printed
    after = [len(item.get("reaffirmations") or []) for item in load_settlements(tmp_path)]
    assert after == before


def test_a_dry_run_still_honours_settle_kind(tmp_path, monkeypatch, capsys):
    _two_stale(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm-file", ONE, "--settle-why", "read today's books",
                     "--settle-kind", "again", "--dry-run"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "would re-take 1 decision(s) on data/smd/stream-one.json" in printed
    assert "leaving 2 decision(s) of the other kind alone" in printed


def test_a_dry_run_still_refuses_a_file_with_nothing_owed(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", "data/smd/nowhere.json", "--settle-why", "x",
                     "--dry-run"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "no unanswered withdrawal is recorded against data/smd/nowhere.json" in printed


def test_the_dry_run_flag_must_preview_something(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--dry-run"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert ("--dry-run previews --settle-reaffirm-file, --settle-restore-file, --settle-drop-file or "
            "--settle-reanchor-file" in printed)


def test_a_dry_run_does_not_touch_the_json_payload(tmp_path, monkeypatch):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "nothing.json"
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--dry-run", "--json-out", str(target)]) == 0
    assert not target.exists()
    assert _restored(tmp_path) == [False, False]


def test_the_preview_and_the_run_agree_on_the_pairs(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol", "--json-out", str(tmp_path / "run.json")]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    for item in payload["restored"]:
        assert "%s=%s" % (item["record"], item["successor"]) in preview


def test_the_restore_preview_says_what_the_books_would_owe(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--dry-run"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert ("after this run data/smd/stream-one.json would owe 0 unanswered withdrawal(s), and the ledger would go "
            "from 2 to 0") in printed
    assert "the repair bill is not previewed" in printed


def test_the_reaffirm_preview_says_what_the_plan_would_owe(tmp_path, monkeypatch, capsys):
    _two_stale(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm-file", ONE, "--settle-why", "read today's books",
                     "--dry-run"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert ("after this run data/smd/stream-one.json would owe 0 stale decision(s), and the ledger would go from 3 "
            "to 0") in printed
    assert "the repair bill is not previewed" in printed


def test_the_previewed_numbers_match_the_books_afterwards(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books", "--dry-run",
                     "--json-out", str(tmp_path / "preview.json")]) == 0
    capsys.readouterr()
    assert not (tmp_path / "preview.json").exists()  # a preview writes nothing, not even its own payload
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 0
    capsys.readouterr()
    assert cli.main(["evolve", "--campaign"]) == 0
    after = " ".join(capsys.readouterr().out.split())
    assert "have no answer" not in after
    assert _withdrawal_plan(tmp_path) == []
