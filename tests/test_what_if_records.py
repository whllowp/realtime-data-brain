# -*- coding: utf-8 -*-
"""Round 132: the campaign page answers "what if I paid this file off".

Round 131 computed the after-state for the dry runs. That left the answer reachable only from a command that also
does something else, so a reader looking at "this file owes two" had to conjure the two batch commands to ask. The
question now belongs to the page -- from the same recomputation, so the page and the commands cannot drift (308).
"""
from brian import cli
from brian.evolve import load_settlements

from test_reaffirm_batch import _two_stale
from test_restore_file import ONE, THIRD, _plan as _withdrawal_plan, _two_on_one_file


def _what_if(*extra):
    return cli.main(["evolve", "--campaign", "--what-if-records"] + list(extra))


def test_a_file_that_owes_withdrawals_previews_paying_them(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _what_if(ONE) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "what if data/smd/stream-one.json owes 2 unanswered withdrawal(s) and 0 stale decision(s)" in printed
    assert "paying the withdrawals (2 pair(s), `--settle-restore-file data/smd/stream-one.json`)" in printed
    assert ("after this run data/smd/stream-one.json would owe 0 unanswered withdrawal(s), and the ledger would go "
            "from 2 to 0") in printed
    assert "nothing here is written: this is the same recomputation the dry runs print" in printed


def test_a_file_that_owes_re_takes_previews_paying_them(tmp_path, monkeypatch, capsys):
    _two_stale(tmp_path)
    (tmp_path / "var" / "evolve").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    assert _what_if(ONE) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "owes 0 unanswered withdrawal(s) and 3 stale decision(s)" in printed
    assert "paying the re-takes (3 pair(s), `--settle-reaffirm-file data/smd/stream-one.json`)" in printed
    assert "would owe 0 stale decision(s), and the ledger would go from 3 to 0" in printed


def test_a_file_that_owes_nothing_says_so(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _what_if(THIRD) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "what if data/smd/third.json owes 0 unanswered withdrawal(s) and 0 stale decision(s)" in printed
    assert "nothing to pay: neither plan lists that file" in printed


def test_the_question_is_not_an_answer(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = [bool(item.get("redecisions")) for item in load_settlements(tmp_path)]
    assert _what_if(ONE) == 0
    capsys.readouterr()
    assert [bool(item.get("redecisions")) for item in load_settlements(tmp_path)] == before
    assert len(_withdrawal_plan(tmp_path)[0]["records"]) == 2


def test_the_page_question_needs_the_page(tmp_path, monkeypatch, capsys):
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--what-if-records", ONE]) == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "--what-if-records asks the campaign page a question; without --campaign nothing prints it" in printed


def test_both_families_are_asked_separately(tmp_path, monkeypatch, capsys):
    """Each family is its own question: paying one does not touch the other, so a missing family must not merge."""
    _two_on_one_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _what_if(ONE) == 0
    first = " ".join(capsys.readouterr().out.split())
    assert "paying the withdrawals" in first
    assert "paying the re-takes" not in first
