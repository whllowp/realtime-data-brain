# -*- coding: utf-8 -*-
"""Round 107 tests: the refusal history, and the two pages that read it.

`_blocked` already records one refusing walk; `_refused_twice` adds a second, so two of the two walks refused v020.
"""
import json

from brian.campaign import campaign, format_campaign
from brian.promote import blocked_versions, refusal_history

from test_blocked_dossier import _blocked, _walk


def _refused_twice(tmp_path, *, standing: bool = True):
    """Two walks refuse v020; when standing is False the books are then repaired so nothing refuses it."""
    _blocked(tmp_path)
    why = blocked_versions(tmp_path)[20]["why"]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])
    if not standing:
        document = tmp_path / "var" / "evolve" / "round-new.json"
        payload = json.loads(document.read_text(encoding="utf-8"))
        payload["queue"] = {"paid": "SMD", "paid_round": "round-old.json", "family": "file"}
        document.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return why


def test_a_promise_never_refused_has_no_history(tmp_path):
    # A clean walk on a promise nothing refused: the store keeps the attempt, the history keeps nothing.
    from test_queued_clearing import _queued_record

    _queued_record(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 0}])
    assert refusal_history(tmp_path) == {}


def test_the_history_counts_the_walks_that_refused_it(tmp_path):
    _refused_twice(tmp_path)
    entry = refusal_history(tmp_path)[20]
    assert entry["count"] == 2
    assert entry["walks"] == 2
    assert entry["first_at"] <= entry["last_at"]


def test_the_history_says_whether_the_refusal_still_stands(tmp_path):
    _refused_twice(tmp_path, standing=True)
    assert refusal_history(tmp_path)[20]["standing"] is True


def test_a_repaired_ledger_leaves_a_history_that_no_longer_stands(tmp_path):
    _refused_twice(tmp_path, standing=False)
    entry = refusal_history(tmp_path)[20]
    assert entry["standing"] is False
    # The refusal is history now, not deleted: the walk did happen and the row still says so.
    assert entry["count"] == 2
    assert 20 not in blocked_versions(tmp_path)


def test_the_history_keeps_each_distinct_reason(tmp_path):
    why = _refused_twice(tmp_path)
    assert refusal_history(tmp_path)[20]["reasons"] == [why]


def test_the_campaign_prints_the_history_with_its_window(tmp_path):
    _refused_twice(tmp_path)
    lines = format_campaign(campaign(tmp_path, refusal_history=refusal_history(tmp_path)))
    assert "v020 was refused by the queue 2 time(s) among 2 recorded walk(s)" in lines
    assert "and the refusal still stands" in lines


def test_the_campaign_says_a_refusal_no_longer_stands(tmp_path):
    _refused_twice(tmp_path, standing=False)
    lines = format_campaign(campaign(tmp_path, refusal_history=refusal_history(tmp_path)))
    assert "and it no longer stands" in lines


def test_the_history_page_names_the_count_and_the_first_and_last(tmp_path, monkeypatch, capsys):
    import argparse

    from brian import cli

    _refused_twice(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    printed = capsys.readouterr().out
    assert "was refused by the queue 2 time(s) among 2 recorded walk(s): walk 1 to 2, first " in printed
    assert "the last walk (2 of 2) refused v020" in printed
    # The row itself says only the short fact; the counts live on the header line (boundary 235).
    assert "[refused by the queue, still standing]" in printed


def test_the_history_row_says_when_a_refusal_stopped_standing(tmp_path, monkeypatch, capsys):
    import argparse

    from brian import cli

    _refused_twice(tmp_path, standing=False)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    assert "and it no longer stands" in capsys.readouterr().out


def test_the_campaign_json_carries_the_history(tmp_path):
    _refused_twice(tmp_path)
    payload = campaign(tmp_path, refusal_history=refusal_history(tmp_path)).to_dict()
    assert payload["refusal_history"]["20"]["count"] == 2
    assert payload["refusal_history"]["20"]["standing"] is True
