# -*- coding: utf-8 -*-
"""Round 105 tests: the refusal names the book that refuses it, and the dossier is per origin file."""
import argparse

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import blocked_dossier, blocked_versions
from test_clearing_evidence_file import FILE
from test_origin_queue import _elsewhere_only
from test_provenance_block import _mismatch
from test_queue_history import _walk
from test_queued_clearing import _queued_record


def _blocked(root):
    """A promise the walk refused on a contradiction, with the origin it was born with.

    The contradiction has to be a real one: `_queued_record` builds a repaired end (a debt, exit 1), so a walk
    refused on `code == 2` needs the mismatch three rounds that make a version's producing round a participant.
    """
    _queued_record(root)
    _mismatch(root)
    _walk(root, attempted=[{"version": 20, "code": 2}])
    return root


def test_a_refusal_names_the_record_that_refuses_it(tmp_path):
    _blocked(tmp_path)
    blocked = blocked_versions(tmp_path)
    assert blocked[20]["code"] == 2
    assert "one of the records in a mismatch link" in blocked[20]["why"]


def test_a_record_that_was_never_walked_has_no_refusal(tmp_path):
    _elsewhere_only(tmp_path)
    assert blocked_versions(tmp_path) == {}


def test_the_dossier_groups_by_the_origin_file(tmp_path):
    _blocked(tmp_path)
    dossier = blocked_dossier(tmp_path)
    assert list(dossier) == [FILE]
    entry = dossier[FILE]
    assert entry["records"] == [20]
    assert entry["attempts"] == 1
    assert entry["last_at"]
    assert any("mismatch" in reason for reason in entry["reasons"])


def test_the_dossier_counts_each_refusal(tmp_path):
    _blocked(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}])
    assert blocked_dossier(tmp_path)[FILE]["attempts"] == 2


def test_no_refusal_no_dossier(tmp_path):
    _elsewhere_only(tmp_path)
    assert blocked_dossier(tmp_path) == {}


def test_the_campaign_names_the_file_and_how_often(tmp_path):
    _blocked(tmp_path)
    result = campaign(tmp_path, blocked_dossier=blocked_dossier(tmp_path))
    assert FILE in result.blocked_dossier
    page = format_campaign(result)
    assert "blocked by the books: %s 1 promise(s) (v020)" % FILE in page
    assert "refused 1 time(s)" in page
    # The long contradiction is printed once, here, for the file it belongs to.
    assert "one of the records in a mismatch link" in page


def test_the_campaign_json_carries_the_dossier(tmp_path):
    _blocked(tmp_path)
    result = campaign(tmp_path, blocked_dossier=blocked_dossier(tmp_path))
    payload = result.to_dict()["blocked_dossier"]
    assert payload[FILE]["records"] == [20]


def test_the_history_row_says_the_queue_was_refused(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    out = capsys.readouterr().out
    assert "[refused by the queue on " in out
    # The row names the walk; the campaign names the book that refused it.
    assert "one of the records in a mismatch link" not in out


def test_a_record_that_produced_nothing_is_not_marked(tmp_path, monkeypatch, capsys):
    _queued_record(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    assert "[refused by the queue" not in capsys.readouterr().out
