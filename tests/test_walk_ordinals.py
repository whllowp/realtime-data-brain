# -*- coding: utf-8 -*-
"""Round 108: the walk ordinal, the last walk's refusals, and the file's bill."""
import argparse

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import blocked_dossier, latest_refusals, refusal_history

from test_blocked_dossier import _blocked, _walk


def _refused_in_first_and_third(tmp_path):
    """Three walks: v020 is refused in the first and the third, cleaned in the second."""
    _blocked(tmp_path)
    why = blocked_dossier_why(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 1}])
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])


def blocked_dossier_why(root):
    from brian.promote import blocked_versions

    return blocked_versions(root)[20]["why"]


def test_the_history_names_the_walk_not_only_the_stamp(tmp_path):
    _refused_in_first_and_third(tmp_path)
    entry = refusal_history(tmp_path)[20]
    assert entry["first_walk"] == 1
    assert entry["last_walk"] == 3
    assert entry["walks"] == 3
    assert entry["count"] == 2


def test_an_unwalked_queue_has_no_latest_refusals(tmp_path):
    from test_queued_clearing import _queued_record

    _queued_record(tmp_path)
    assert latest_refusals(tmp_path) == {"walk": 0, "walks": 0, "at": "", "versions": []}


def test_the_latest_refusals_name_what_the_last_walk_hit(tmp_path):
    _refused_in_first_and_third(tmp_path)
    latest = latest_refusals(tmp_path)
    assert latest["walk"] == 3
    assert latest["walks"] == 3
    assert latest["versions"] == [20]


def test_the_latest_refusals_are_empty_when_the_last_walk_was_clean(tmp_path):
    _blocked(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 1}])
    assert latest_refusals(tmp_path)["versions"] == []


def test_the_dossier_totals_the_refusals_of_every_promise_born_with_the_file(tmp_path):
    _refused_in_first_and_third(tmp_path)
    entry = blocked_dossier(tmp_path)["data/smd/stream-one.json"]
    assert entry["refused"] == 2
    assert entry["first_walk"] == 1
    assert entry["last_walk"] == 3


def test_the_history_page_says_what_the_last_walk_refused(tmp_path, monkeypatch, capsys):
    _refused_in_first_and_third(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    printed = capsys.readouterr().out
    assert "walk 1 to 3" in printed
    assert "the last walk (3 of 3) refused v020" in printed


def test_the_history_page_says_when_the_last_walk_refused_nothing(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 1}])
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    printed = capsys.readouterr().out
    assert "the last walk (2 of 2) refused no promise" in printed


def test_the_campaign_prints_the_walk_range_and_the_files_bill(tmp_path):
    _refused_in_first_and_third(tmp_path)
    lines = format_campaign(campaign(tmp_path, refusal_history=refusal_history(tmp_path),
                                     blocked_dossier=blocked_dossier(tmp_path)))
    assert "walk 1 to 3, first " in lines
    assert "(walk 3 of 3; its promises were refused 2 time(s) in the recorded walks)" in lines
