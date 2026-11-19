"""Round 100: the population behind round 99's single-record sentence.

Round 99 made one page say "this is the first verdict taken on the file the promise was born with". That answers
"is this check unusual?" for one record, and nothing about the book: how many promises have never been tested
where they were made, which file each was born with, and which one to open first.

So the campaign counts the queue (`never_tested_at_origin`), names the first command that would test a promise
where it was made, `--verify` says when the check it is running will not touch that place, and `--history` marks
the clearing that was the first verdict at the origin.
"""

from __future__ import annotations

import argparse
import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (load_promotions, never_tested_at_origin, record_clearing, repaired_paths,
                           set_stream_path, write_verification)

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_evidence_paths import THIRD
from test_repaired_check import OTHER, _repaired, _verify
from test_untested_answer import NOW, _copied, _observed


def _campaign(root) -> str:
    return format_campaign(campaign(root, in_force_history={"repaired_paths": repaired_paths(root)}))


def _elsewhere_only(root) -> None:
    """A repaired record whose only verdict was taken on the substitute."""
    _repaired(root)
    promotion = load_promotions(root)[0]
    write_verification(root, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "failed", "reason": "recall fell",
                        "stream_path_moves": 1, "checked_path": OTHER, "on_original_path": False})


def _at_origin(root) -> None:
    """A repaired record with one verdict on the substitute and one on the original."""
    _repaired(root)
    promotion = load_promotions(root)[0]
    write_verification(root, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "failed", "reason": "recall fell",
                        "stream_path_moves": 1, "checked_path": OTHER, "on_original_path": False})
    write_verification(root, promotion,
                       {"checked_at": (NOW + __import__("datetime").timedelta(days=1)).isoformat(),
                        "verdict": "holds", "reason": "measured again", "stream_path_moves": 1,
                        "checked_path": FILE, "on_original_path": True})


def test_the_queue_counts_records_with_no_verdict_where_the_promise_was_made(tmp_path):
    _elsewhere_only(tmp_path)
    queue = never_tested_at_origin(repaired_paths(tmp_path))
    assert queue["records"] == 1
    assert queue["versions"][0]["version"] == 20
    assert queue["versions"][0]["verdicts"] == 1
    assert queue["by_file"] == {FILE: 1}


def test_a_record_with_a_verdict_at_the_origin_is_not_in_the_queue(tmp_path):
    _at_origin(tmp_path)
    assert never_tested_at_origin(repaired_paths(tmp_path))["records"] == 0


def test_a_record_never_checked_at_all_is_in_the_queue_with_zero_verdicts(tmp_path):
    _repaired(tmp_path)
    queue = never_tested_at_origin(repaired_paths(tmp_path))
    # "never tested" includes "never checked": a page that dropped it would report the debt as smaller than it is.
    assert queue["records"] == 1
    assert queue["versions"][0]["verdicts"] == 0


def test_the_queue_splits_by_the_file_each_promise_was_born_with(tmp_path):
    from test_repaired_check import _add_files

    _repaired(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    queue = never_tested_at_origin(repaired_paths(tmp_path))
    # The origin is where the promise was born, and the record still holds no verdict there.
    assert queue["by_file"] == {FILE: 1}
    assert len(queue["versions"]) == 1


def test_the_campaign_names_the_queue_and_the_first_command_that_would_answer_it(tmp_path):
    _elsewhere_only(tmp_path)
    page = _campaign(tmp_path)
    assert "1 record(s) hold no verdict on the file their promise was born with: v020 (1 verdict(s), all " \
           "elsewhere)" in page
    assert "-- start with brian evolve --verify --verify-version 020 --stream-file %s" % FILE in page


def test_the_campaign_says_when_the_origin_file_cannot_be_measured_here(tmp_path):
    _elsewhere_only(tmp_path)
    (tmp_path / FILE).unlink()
    page = _campaign(tmp_path)
    assert "the file it was born with is not in this checkout, so it cannot be measured there" in page


def test_the_campaign_splits_the_queue_by_file_when_there_are_several(tmp_path):
    _elsewhere_only(tmp_path)
    # A second record born with a different file, also never measured there: a queue over two origins has to say
    # which file owns which record, or "2 records" reads as one problem when it is two.
    versions = tmp_path / "var" / "evolve" / "versions"
    payload = json.loads((versions / "v020.json").read_text(encoding="utf-8"))
    payload["version"] = 21
    payload["stream_path_history"] = [{"from": THIRD, "to": OTHER, "at": NOW.isoformat(),
                                       "why": "the file the record named does not exist in this checkout"}]
    payload["stream_path"] = OTHER
    for key in ("verification", "checks", "reverted_at"):
        payload.pop(key, None)
    (versions / "v021.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    page = _campaign(tmp_path)
    assert "never tested at their origin:" in page
    assert "%s 1 record(s)" % THIRD in page and "%s 1 record(s)" % FILE in page


def test_a_check_on_the_substitute_names_the_place_the_promise_was_never_tested(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=OTHER)
    out = capsys.readouterr().out
    assert "this promise has never been measured on the file it was born with (%s), which exists here:" % FILE in out
    assert "measuring there is what would test the promise where it was made (boundary 218)" in out


def test_a_check_on_the_original_does_not_print_the_advice(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=FILE)
    out = capsys.readouterr().out
    assert "has never been measured on the file it was born with" not in out


def test_the_advice_is_not_printed_when_the_origin_file_is_gone(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    (tmp_path / FILE).unlink()
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=OTHER)
    out = capsys.readouterr().out
    assert "has never been measured on the file it was born with" not in out


def test_the_history_marks_the_clearing_that_was_the_first_verdict_at_the_origin(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", reason="measured again", stream_path=FILE,
                    first_original=True)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    out = capsys.readouterr().out
    assert "[first verdict at the origin]" in out


def test_an_ordinary_clearing_carries_no_such_mark(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", reason="measured again", stream_path=FILE)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    out = capsys.readouterr().out
    assert "clearings" in out and "[first verdict at the origin]" not in out
