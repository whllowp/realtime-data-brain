# -*- coding: utf-8 -*-
"""Round 109: the history, the file's bill and the plan all cite the same walks."""
from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import blocked_dossier, blocked_versions, refusal_history, walks_phrase

from test_blocked_dossier import _blocked
from test_clearing_evidence_file import FILE
from test_origin_queue import _elsewhere_only
from test_queue_history import _walk


def _refused_twice_apart(tmp_path):
    """Refused in walk 1, clean in walk 2, refused in walk 3: two refusals, two different walks."""
    _blocked(tmp_path)
    why = blocked_versions(tmp_path)[20]["why"]
    _walk(tmp_path, attempted=[{"version": 20, "code": 1}])
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])


def _plan(tmp_path, monkeypatch, *extra):
    monkeypatch.chdir(tmp_path)
    return cli.main(["evolve", "--verify", "--from-queue", "0"] + list(extra))


def test_a_walk_phrase_reads_like_a_sentence():
    assert walks_phrase([]) == ""
    assert walks_phrase([0]) == ""
    assert walks_phrase([3]) == "walk 3"
    assert walks_phrase([1, 2]) == "walks 1 and 2"
    assert walks_phrase([1, 2, 4]) == "walks 1, 2 and 4"


def test_the_history_keeps_every_walk_it_was_refused_in(tmp_path):
    _refused_twice_apart(tmp_path)
    assert refusal_history(tmp_path)[20]["refused_walks"] == [1, 3]


def test_the_files_bill_names_the_same_walks_as_the_history(tmp_path):
    _refused_twice_apart(tmp_path)
    entry = blocked_dossier(tmp_path)[FILE]
    assert entry["refused_walks"] == [1, 3]


def test_the_campaign_names_the_walks_not_only_their_number(tmp_path):
    _refused_twice_apart(tmp_path)
    lines = format_campaign(campaign(tmp_path, refusal_history=refusal_history(tmp_path),
                                     blocked_dossier=blocked_dossier(tmp_path)))
    assert "its promises were refused in walks 1 and 3, 2 time(s)" in lines


def test_the_still_blocked_notice_names_the_walks_and_the_count(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    assert _plan(tmp_path, monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "still blocked" in printed
    assert "refused in walk 1, 1 time(s), last at " in printed


def test_the_plan_marks_a_promise_the_books_have_refused(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    # `--retry-blocked` is what keeps a refused promise in the plan: without it the notice above already said so.
    assert _plan(tmp_path, monkeypatch, "--retry-blocked") == 0
    printed = capsys.readouterr().out
    assert "[refused by the books in walk 1, and the refusal still stands]" in printed


def test_the_plan_marks_an_unrefused_promise_with_nothing(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    assert _plan(tmp_path, monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "never measured where this promise was made)" in printed
    assert "refused by the books" not in printed
