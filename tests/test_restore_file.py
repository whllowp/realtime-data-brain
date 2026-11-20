# -*- coding: utf-8 -*-
"""Round 129: one command pays a whole file's unanswered withdrawals.

Round 128 grouped the withdrawal debt by stream file and then printed a command that paid one pair each, so a reader
who took the grouping seriously still typed one reason per decision -- a grouping that costs exactly what it was meant
to save (boundary 301). `--settle-restore-file` resolves its pairs from the same plan the page prints (boundary 283)
and hands them to the single-pair branch, so it inherits that branch's refusals, its per-pair blocks and its JSON
(boundary 284).
"""
import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import load_settlements, withdrawal_plan

from test_withdrawal_plan import ONE, THIRD, _pair, _rounds

SECOND_SUCCESSOR = "round-twelfth.json"


def _two_on_one_file(tmp_path):
    _pair(tmp_path, "round-old.json", "round-new.json", days_ago=3.0)
    _pair(tmp_path, "round-tenth.json", SECOND_SUCCESSOR, days_ago=1.0, by="bob")


def _plan(tmp_path):
    return withdrawal_plan(_rounds(tmp_path), load_settlements(tmp_path))


def test_the_file_command_restores_both_decisions(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why",
                     "both claims are dated after their decisions", "--settle-by", "carol"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "re-taking 2 decision(s) on data/smd/stream-one.json in one command" in printed
    assert "taken up again round-old.json -> round-new.json" in printed
    assert "taken up again round-tenth.json -> round-twelfth.json" in printed
    assert "re-taken 2 of 2 decision(s), 0 was already standing, 0 refused" in printed
    stored = load_settlements(tmp_path)
    assert all(item.get("redecisions") for item in stored)
    assert _plan(tmp_path) == []


def test_the_file_command_leaves_the_other_file_alone(tmp_path, monkeypatch, capsys):
    _pair(tmp_path, "round-old.json", "round-new.json", days_ago=3.0)
    _pair(tmp_path, "round-tenth.json", SECOND_SUCCESSOR, origin=THIRD, days_ago=1.0, by="bob")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 0
    row = _plan(tmp_path)[0]
    assert row["origin"] == THIRD and row["decisions"] == 1


def test_an_unknown_file_is_refused(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", "data/smd/nowhere.json",
                     "--settle-why", "read today's books"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "no unanswered withdrawal is recorded against data/smd/nowhere.json" in printed


def test_the_file_command_needs_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", "  ", "--settle-why", "read today's books"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "--settle-restore-file takes the stream file the withdrawn decisions are about" in printed


def test_one_pair_still_takes_the_single_pair_form(tmp_path, monkeypatch, capsys):
    _pair(tmp_path, "round-old.json", "round-new.json", days_ago=3.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "re-taking 1 decision(s) on data/smd/stream-one.json in one command" in printed
    assert "re-taken 1 of 1" not in printed  # one pair prints the block, not a headline


def test_several_pairs_in_one_call_list_them_in_the_payload(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "restored.json"
    assert cli.main(["evolve", "--settle-restore", "round-old.json=round-new.json",
                     "round-tenth.json=%s" % SECOND_SUCCESSOR, "--settle-why", "read today's books",
                     "--settle-by", "carol", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [item["record"] for item in payload["restored"]] == ["round-old.json", "round-tenth.json"]
    assert payload["restore_refused"] == 0


def test_a_refused_pair_does_not_stop_the_walk(tmp_path, monkeypatch, capsys):
    _pair(tmp_path, "round-old.json", "round-new.json", days_ago=3.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-restore", "round-old.json=round-new.json",
                     "round-never.json=round-new.json", "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "cannot take that decision up again" in printed
    assert "re-taken 1 of 2 decision(s), 0 was already standing, 1 refused" in printed
    assert load_settlements(tmp_path)[0].get("redecisions")


def test_the_plan_and_the_page_name_the_file_command(tmp_path):
    _two_on_one_file(tmp_path)
    row = _plan(tmp_path)[0]
    assert row["batch"] == ("brian evolve --settle-restore-file data/smd/stream-one.json "
                            "--settle-why TEXT --settle-by NAME")
    printed = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "start with brian evolve --settle-restore-file data/smd/stream-one.json" in printed


def test_one_withdrawal_still_names_the_single_pair(tmp_path):
    _pair(tmp_path, "round-old.json", "round-new.json", days_ago=3.0)
    printed = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "start with brian evolve --settle-restore round-old.json=round-new.json" in printed


def test_the_gate_is_paid_by_the_file_command(tmp_path, monkeypatch):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-per-file", "1"]) == 1
    assert cli.main(["evolve", "--settle-restore-file", ONE, "--settle-why", "read today's books",
                     "--settle-by", "carol"]) == 0
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-per-file", "1"]) == 0
