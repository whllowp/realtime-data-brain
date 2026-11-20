# -*- coding: utf-8 -*-
"""Round 134: one command for a file's unpairable decisions, and the row that says why.

Round 133 names the pairs no re-take can pay, with one `--settle-drop` per pair -- truthful, and a retyped reason per
decision. The history row that carries a settled decision said nothing about it at all, so the fact lived only on
another page. Both halves are about the same thing: the reader should be able to act, and to see why, without
recomputing the rule by hand.
"""
import json

from brian import cli
from brian.evolve import load_settlements, reaffirm_plan
from test_reaffirm_batch import SUCCESSOR, _two_stale
from test_unpayable_plan import _retarget


def _two_unpayable(root):
    """Two settled records on one file that now name other successors."""
    _retarget(root, "round-second.json", "round-moved-away.json")
    _retarget(root, "round-third.json", "round-moved-elsewhere.json")
    return root


def _rows(root):
    return reaffirm_plan(cli.history(root, include_retired=True), load_settlements(root))


def test_one_command_withdraws_several_decisions(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-drop", "round-second.json=" + SUCCESSOR,
                     "round-third.json=" + SUCCESSOR,
                     "--settle-why", "both records name other successors now",
                     "--settle-by", "bob", "--json-out", "batch.json"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert printed.count("withdrawn round-second.json ->") == 1
    assert printed.count("withdrawn round-third.json ->") == 1
    assert "withdrawn 2 of 2 decision(s), 0 already withdrawn, 0 refused" in printed
    payload = json.loads((root / "batch.json").read_text(encoding="utf-8"))
    assert [item["record"] for item in payload["withdrawn"]] == ["round-second.json", "round-third.json"]
    assert payload["drop_refused"] == 0 and payload["drop_skipped"] == 0


def test_a_refused_pair_does_not_stop_the_batch(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-drop", "round-second.json=" + SUCCESSOR,
                     "round-nobody.json=" + SUCCESSOR,
                     "--settle-why", "one of these is unknown", "--json-out", "batch.json"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "cannot withdraw that decision:" in printed
    assert "withdrawn 1 of 2 decision(s), 0 already withdrawn, 1 refused" in printed
    payload = json.loads((root / "batch.json").read_text(encoding="utf-8"))
    assert payload["drop_refused"] == 1
    assert [item["record"] for item in payload["withdrawn"]] == ["round-second.json"]


def test_a_pair_already_withdrawn_is_reported_not_counted(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    cli.main(["evolve", "--settle-drop", "round-second.json=" + SUCCESSOR,
              "--settle-why", "the record names another successor", "--settle-by", "bob"])
    capsys.readouterr()
    code = cli.main(["evolve", "--settle-drop", "round-second.json=" + SUCCESSOR,
                     "--settle-why", "the record names another successor", "--settle-by", "bob"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "was already withdrawn on" in printed
    # One pair prints one block, no total (round 120's rule for the sibling commands).
    assert "decision(s)," not in printed


def test_the_file_command_withdraws_every_unpairable_decision(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    before = _rows(root)[0]
    assert before["unpayable"] == 2
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-drop-file", before["origin"],
                     "--settle-why", "both records name other successors now", "--settle-by", "bob"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "dropping 2 decision(s) on %s in one command" % before["origin"] in printed
    assert "withdrawn 2 of 2 decision(s)" in printed
    assert [row["unpayable"] for row in _rows(root)] == [0]


def test_the_file_command_says_so_when_nothing_is_unpairable(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-drop-file", "data/smd/stream-nine.json",
                     "--settle-why", "nothing here"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "no decision recorded against data/smd/stream-nine.json cannot be re-taken" in printed


def test_the_history_row_says_the_decision_cannot_be_re_taken(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--history"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "[cannot be re-taken:" in line]
    assert lines, "no row says why the decision cannot be re-taken"
    # The settled record's own row carries the reason, with the successor the record names now -- and only that row,
    # because the successor is the claim the decision dropped, not a decision (round 124's placement rule).
    for line in lines:
        assert "[retired, re-measured as " in line, "the mark landed on a row that is not a retired record"
        named = line.split("[retired, re-measured as ", 1)[1].split("]", 1)[0]
        assert "[cannot be re-taken: the record names %s]" % named in line
    assert any("round-moved-away.json" in line for line in lines)
    assert any("round-moved-elsewhere.json" in line for line in lines)


def test_the_history_row_stays_quiet_when_the_pair_is_payable(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--history"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "cannot be re-taken" not in printed
