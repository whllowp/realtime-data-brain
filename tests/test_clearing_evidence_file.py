"""Round 91: a citation is only as good as the file behind it.

Round 90 made every clearing name its evidence. Two holes followed: nothing checked the file existed when the
entry was written (so a typo or a moved stream produced a citation nobody can open), and nothing ever asked
again (a cleared file ages into a dead link without any page saying so).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit
from brian.promote import (EVIDENCE_MISSING, EVIDENCE_PRESENT, EVIDENCE_UNNAMED, cleared_summary,
                           clearing_evidence, in_force_disputes, in_force_history, record_clearing)
from test_untested_answer import NOW, _copied, _observed, _store

FILE = "data/smd/stream-one.json"


def _clear(root: Path, *, days: float, path: str = FILE, tested: bool = True,
           verdict: str = "holds") -> list:
    return record_clearing(root, 20, now=NOW + timedelta(days=days), verdict=verdict,
                           reason="measured again by --verify", tested=tested, stream_path=path)


def test_a_citation_to_a_file_that_is_there_is_an_answer(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entry = _clear(tmp_path, days=3)[0]
    assert entry["evidence"] == EVIDENCE_PRESENT
    assert entry["tested"] is True
    assert _store(tmp_path)["observed"] == []


def test_a_citation_to_a_file_that_is_gone_is_an_attempt(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entry = _clear(tmp_path, days=3, path="data/smd/moved-away.json")[0]
    assert entry["evidence"] == EVIDENCE_MISSING
    assert entry["tested"] is False
    observed = _store(tmp_path)["observed"]
    assert len(observed) == 1                      # the debt is still owed
    assert observed[0]["first_seen"] == NOW.isoformat()
    assert observed[0]["untested"]["evidence"] == EVIDENCE_MISSING


def test_an_unnamed_measurement_is_not_a_missing_one(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entry = _clear(tmp_path, days=3, path="")[0]
    assert entry["evidence"] == EVIDENCE_UNNAMED
    assert entry["tested"] is True                 # history, not a defect


def test_the_read_time_check_counts_what_is_gone_now(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6, path="data/smd/stream-two.json")   # never written: already gone
    entries, _trimmed = in_force_history(tmp_path)
    evidence = clearing_evidence(entries, tmp_path)
    assert evidence["present"] == 1
    assert evidence["missing"] == 1
    assert evidence["unnamed"] == 0
    assert evidence["missing_files"] == ["data/smd/stream-two.json"]


def test_deleting_the_file_later_does_not_rewrite_the_entry(tmp_path):
    """The stored entry said `present` when it was written, and the store must not be rewritten to say otherwise."""
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    (tmp_path / FILE).unlink()
    entries, _trimmed = in_force_history(tmp_path)
    assert entries[0]["evidence"] == EVIDENCE_PRESENT
    assert clearing_evidence(entries, tmp_path)["missing"] == 1


def test_the_summary_says_when_each_file_last_answered(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6)
    summary = cleared_summary(in_force_history(tmp_path)[0])
    assert summary["last_by_file"] == {FILE: (NOW + timedelta(days=6)).isoformat()}


def test_the_history_page_marks_a_file_it_cannot_open(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    (tmp_path / FILE).unlink()
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--history"])
    out = capsys.readouterr().out
    assert "evidence         1 of them name a file this checkout cannot open: %s" % FILE in out
    assert "[evidence missing]" in out


def test_the_campaign_prints_the_last_date_and_the_dead_links(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    (tmp_path / FILE).unlink()
    entries, trimmed = in_force_history(tmp_path)
    record = dict(cleared_summary(entries, trimmed=trimmed), evidence_missing=clearing_evidence(entries, tmp_path))
    page = format_campaign(campaign(tmp_path, in_force_history=record))
    assert "clearings by stream file: %s 1 (last %s)" % (FILE, (NOW + timedelta(days=3)).isoformat()) in page
    assert "1 clearing(s) name a file this checkout cannot open: %s" % FILE in page


def test_the_audit_marks_an_entry_written_against_a_missing_file(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3, path="data/smd/gone.json")
    page = format_audit(audit(tmp_path, in_force=in_force_disputes(tmp_path),
                              in_force_cleared=in_force_history(tmp_path)[0]))
    assert "[evidence missing: data/smd/gone.json]" in page


def test_a_present_citation_is_not_marked(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    page = format_audit(audit(tmp_path, in_force=in_force_disputes(tmp_path),
                              in_force_cleared=in_force_history(tmp_path)[0]))
    assert "[evidence missing" not in page
    assert "of %s" % FILE in page


def test_a_stream_file_that_is_gone_is_reported_before_anything_is_measured(tmp_path, monkeypatch, capsys):
    """Round 91: `load_stream_file` raised out of `_evolve_streams`, so the check died with a traceback."""
    _copied(tmp_path)
    _observed(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/gone.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 2
    assert "cannot read data/smd/gone.json: no such file" in out
    assert "the promise records the file it was measured on: %s" % FILE in out
    assert in_force_history(tmp_path)[0] == []      # nothing was cleared by a check that never ran

