"""Round 104: the walks are a history, and a blocked record is not paid for twice.

Round 103 recorded each walk as one event. Two things follow that the store did not yet answer: what the recorded
walks add up to (a trend, not a last line), and which versions each walk actually attempted -- without that the next
walk cannot tell "nobody has tried this" from "this was tried and the books refused it twice". A record blocked by a
contradictory ledger fails identically on every walk, so paying again is a second bill for the same refusal.
"""

from __future__ import annotations

from brian.campaign import campaign, format_campaign
from brian.promote import (blocked_versions, last_queue_run, queue_history, queue_runs,
                           record_queue_run)

from test_clearing_evidence_file import FILE
from test_evidence_paths import THIRD
from test_from_queue import _elsewhere_only, _run, _second_record
from test_untested_answer import NOW


def _walk(tmp_path, **run):
    payload = {"limit": 2, "runnable": 2, "measured": 1, "skipped": 0, "skipped_origins": [], "cleared": 0,
               "worst": 0, "stopped_at": 0, "attempted": [{"version": 20, "code": 0}]}
    payload.update(run)
    return record_queue_run(tmp_path, payload, now=NOW)


def test_an_unwalked_queue_has_no_history(tmp_path):
    history = queue_history(tmp_path)
    assert history["runs"] == 0
    assert history["measured"] == 0
    assert history["last_at"] == ""
    assert history["mean_measured"] == 0.0


def test_the_history_adds_up_the_walks_it_holds(tmp_path):
    _walk(tmp_path, measured=2, cleared=1, skipped=1, skipped_origins=[THIRD])
    _walk(tmp_path, measured=1, cleared=0, worst=2, stopped_at=21,
          attempted=[{"version": 21, "code": 2}])
    history = queue_history(tmp_path)
    assert history["runs"] == 2
    assert history["measured"] == 3
    assert history["cleared"] == 1
    assert history["stopped"] == 1
    assert history["skipped"] == 1
    assert history["mean_measured"] == 1.5
    assert history["last_at"] == NOW.isoformat()
    assert history["skipped_origins"] == {THIRD: 1}


def test_the_campaign_reads_two_walks_as_a_trend(tmp_path):
    _walk(tmp_path, measured=2, cleared=1)
    _walk(tmp_path, measured=1, cleared=0, worst=2, stopped_at=21)
    result = campaign(tmp_path, queue_history=queue_history(tmp_path), queue_run=last_queue_run(tmp_path))
    page = format_campaign(result)
    assert ("2 queue run(s) recorded: measured 3 promise(s) in total, 1 of those left a clearing, 1 run(s) ended "
            "on a build defect (1.5 promise(s) per run on average)") in page
    assert result.to_dict()["queue_history"]["measured"] == 3


def test_one_walk_is_a_fact_and_not_a_trend(tmp_path):
    _walk(tmp_path, measured=2)
    page = format_campaign(campaign(tmp_path, queue_history=queue_history(tmp_path)))
    assert "queue run(s) recorded" not in page


def test_the_trend_says_when_the_store_no_longer_holds_the_whole_history(tmp_path):
    _walk(tmp_path)
    _walk(tmp_path)
    path_runs = tmp_path / "var" / "evolve" / "queue_runs.json"
    payload = path_runs.read_text(encoding="utf-8").replace('"trimmed": 0', '"trimmed": 7')
    path_runs.write_text(payload, encoding="utf-8")
    page = format_campaign(campaign(tmp_path, queue_history=queue_history(tmp_path)))
    assert "7 older run(s) fell out of the store, so this is the recent window" in page


def test_an_origin_skipped_on_every_walk_is_named_as_such(tmp_path):
    _walk(tmp_path, skipped=1, skipped_origins=[THIRD])
    _walk(tmp_path, skipped=1, skipped_origins=[THIRD])
    page = format_campaign(campaign(tmp_path, queue_history=queue_history(tmp_path)))
    assert "origins skipped on more than one walk: %s 2" % THIRD in page


def test_a_blocked_version_is_the_one_whose_latest_walk_was_refused(tmp_path):
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}, {"version": 21, "code": 1}])
    blocked = blocked_versions(tmp_path)
    assert list(blocked) == [20]
    assert blocked[20]["at"] == NOW.isoformat()
    # A later walk that did not hit the wall settles it: the record is not blocked any more.
    _walk(tmp_path, attempted=[{"version": 20, "code": 1}])
    assert blocked_versions(tmp_path) == {}


def test_the_queue_lists_a_blocked_record_and_does_not_measure_it(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}])
    code = _run(tmp_path, monkeypatch, "--from-queue", "2")
    out = capsys.readouterr().out
    assert "still blocked    1 promise(s) ended on a build defect last time and nothing has changed since: " in out
    assert "fix the books, or pass --retry-blocked to measure them again anyway" in out
    assert "---- v020" not in out
    assert "---- v021" in out
    assert code in (0, 1)
    stored = last_queue_run(tmp_path)
    assert [item["version"] for item in stored["attempted"]] == [21]


def test_retry_blocked_measures_the_record_again(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}])
    code = _run(tmp_path, monkeypatch, "--from-queue", "2", "--retry-blocked")
    out = capsys.readouterr().out
    assert "still blocked" not in out
    assert "---- v020" in out
    assert code in (0, 1)


def test_the_queue_row_still_names_the_origin_it_measures(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    _run(tmp_path, monkeypatch, "--from-queue", "1")
    stored = last_queue_run(tmp_path)
    assert stored["attempted"][0]["version"] == 20
    assert stored["runnable"] == 1
    assert (tmp_path / FILE).is_file()


def test_a_walk_that_measures_nothing_is_still_a_walk(tmp_path, monkeypatch, capsys):
    """The observation behind "nobody has measured this" is the walk itself, even when it measured nothing."""
    _elsewhere_only(tmp_path)
    (tmp_path / FILE).unlink()
    _run(tmp_path, monkeypatch, "--from-queue", "1")
    stored = last_queue_run(tmp_path)
    assert stored["measured"] == 0
    assert stored["runnable"] == 0
    assert stored["skipped"] == 1
    assert stored["skipped_origins"] == [FILE]


def test_a_walk_stopped_by_the_books_says_that_and_not_that_the_file_is_missing(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}])
    _run(tmp_path, monkeypatch, "--from-queue", "1")
    out = capsys.readouterr().out
    assert "and the last walk's refusal still stands" in out
    assert "none of those files is in this checkout" not in out
    assert queue_runs(tmp_path)["runs"][-1]["measured"] == 0

