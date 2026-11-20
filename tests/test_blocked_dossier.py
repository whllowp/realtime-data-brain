# -*- coding: utf-8 -*-
"""Round 105 tests: the refusal names the book that refuses it, and the dossier is per origin file."""
import argparse
import json

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
    entry = blocked_dossier(tmp_path)[FILE]
    assert entry["attempts"] == 2
    # One promise refused twice is a retried promise, not two broken promises (boundary 236).
    assert entry["retried"] == [20]
    assert entry["records"] == [20]


def test_a_refusal_of_a_version_with_no_record_is_counted_not_dropped(tmp_path):
    _blocked(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}, {"version": 21, "code": 2}])
    dossier = blocked_dossier(tmp_path)
    # v021 has no record in this ledger, so its origin cannot be named; it is still a refused promise and must
    # appear somewhere rather than vanish from the count (round 47's rule: an unattributable record is reported).
    assert dossier[FILE]["records"] == [20]
    assert dossier["(unnamed)"]["records"] == [21]


def test_a_refusal_that_no_longer_stands_is_reported_and_dropped(tmp_path):
    from brian.promote import blocked_versions, stale_refusals

    _blocked(tmp_path)
    # A walk records the sentence that refused it; the fixture's synthetic attempt has none, so it is written here
    # as the walk would have written it.
    why = blocked_versions(tmp_path)[20]["why"]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])
    # The books move: the successor now names the round that retired into it, so the other claimant is the one
    # left naming it and today's sentence is not the one on file.
    document = tmp_path / "var" / "evolve" / "round-new.json"
    payload = json.loads(document.read_text(encoding="utf-8"))
    payload["queue"] = {"paid": "SMD", "paid_round": "round-old.json", "family": "file"}
    document.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    stale = stale_refusals(tmp_path)
    assert stale and stale[0]["version"] == 20
    assert stale[0]["was"] != stale[0]["now"]
    # ... and the queue takes the promise back without `--retry-blocked`.
    assert 20 not in (blocked_dossier(tmp_path).get(FILE) or {}).get("records", [])


def test_a_refusal_that_still_stands_is_not_dropped(tmp_path):
    from brian.promote import stale_refusals

    _blocked(tmp_path)
    assert stale_refusals(tmp_path) == []
    assert 20 in blocked_dossier(tmp_path)[FILE]["records"]


def test_no_refusal_no_dossier(tmp_path):
    _elsewhere_only(tmp_path)
    assert blocked_dossier(tmp_path) == {}


def test_the_campaign_names_the_file_and_how_often(tmp_path):
    _blocked(tmp_path)
    result = campaign(tmp_path, blocked_dossier=blocked_dossier(tmp_path))
    assert FILE in result.blocked_dossier
    page = format_campaign(result)
    assert "blocked by the books: %s 1 promise(s) (v020)" % FILE in page
    # Round 106: promises, refusals and retries are three numbers, so one block reads as "this file is broken"
    # and three blocks on one promise reads as "this record is broken".
    assert "1 refusal(s) across none retried" in page
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
    # Round 107: the row carries the short fact, the header carries the count and the stamps.
    assert "[refused by the queue, still standing]" in out
    assert "was refused by the queue 1 time(s) among" in out
    # The row names the walk; the campaign names the book that refused it.
    assert "one of the records in a mismatch link" not in out


def test_a_record_that_produced_nothing_is_not_marked(tmp_path, monkeypatch, capsys):
    _queued_record(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    assert "[refused by the queue" not in capsys.readouterr().out
