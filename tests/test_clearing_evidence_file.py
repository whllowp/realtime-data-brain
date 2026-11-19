"""A citation is only as good as the file behind it (rounds 91 and 92).

Round 90 made every clearing name its evidence. Round 91 checked the file existed when the entry was written and
again when a reader opens the page. Round 92 gave the check a way to run when the file it was pointed at is gone --
the record names what it measured -- and gave the dead links an age, because the oldest one is where the
incompleteness started.
"""


from __future__ import annotations

import json

from datetime import timedelta
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit
from brian.promote import (EVIDENCE_MISSING, EVIDENCE_PRESENT, EVIDENCE_UNNAMED, cleared_summary,
                           clearing_evidence, in_force_disputes, in_force_history, record_clearing)
from test_clearing_recorded import _measurement
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


def test_a_missing_stream_file_falls_back_to_the_recorded_one(tmp_path, monkeypatch, capsys):
    """Round 92: the record names the file it was measured on, so the check can run there -- and says so."""
    _copied(tmp_path)
    _observed(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/gone.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 1, out                                   # the copied end still refuses to apply
    assert "stream file    %s  (substituted: data/smd/gone.json does not exist in this checkout)" % FILE in out
    stored, _trimmed = in_force_history(tmp_path)
    assert stored[0]["stream_path"] == FILE
    assert stored[0]["substituted_from"] == "data/smd/gone.json"


def test_a_missing_stream_file_with_no_recorded_file_is_reported(tmp_path, monkeypatch, capsys):
    """Nothing to stand in: the record does not say which file it was measured on."""
    _copied(tmp_path)
    _observed(tmp_path)
    version_path = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    payload["stream_path"] = ""
    version_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/gone.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 2
    assert "cannot read data/smd/gone.json: no such file" in out
    assert "do not say which file they were measured on: v020" in out
    assert in_force_history(tmp_path)[0] == []      # nothing was cleared by a check that never ran


def test_the_summary_says_which_dead_link_is_the_oldest(tmp_path):
    """Round 92: a count says the books are incomplete; the oldest one says where that started."""
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6)
    (tmp_path / FILE).unlink()
    entries, _trimmed = in_force_history(tmp_path)
    evidence = clearing_evidence(entries, tmp_path, now=NOW + timedelta(days=10))
    assert evidence["oldest"] == {"checked_at": (NOW + timedelta(days=3)).isoformat(), "version": 20,
                                  "stream_path": FILE, "age_days": 7.0}
    assert "oldest" not in entries[0]               # computed, never stored (boundary 194)


def test_the_campaign_names_the_oldest_dead_link(tmp_path):
    """Round 92: the oldest dead link is the one to open first, so it gets its own line and its own age."""
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6)
    (tmp_path / FILE).unlink()
    entries, trimmed = in_force_history(tmp_path)
    evidence = clearing_evidence(entries, tmp_path, now=NOW + timedelta(days=10))
    record = dict(cleared_summary(entries, trimmed=trimmed), evidence_missing=evidence)
    page = format_campaign(campaign(tmp_path, in_force_history=record))
    assert "2 clearing(s) name a file this checkout cannot open: %s" % FILE in page
    assert ("the oldest of them was written %s (v020, 7.0 day(s) ago)"
            % (NOW + timedelta(days=3)).isoformat()) in page


def test_the_campaign_says_nothing_about_an_oldest_link_when_none_is_dead(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    entries, trimmed = in_force_history(tmp_path)
    evidence = clearing_evidence(entries, tmp_path, now=NOW + timedelta(days=10))
    record = dict(cleared_summary(entries, trimmed=trimmed), evidence_missing=evidence)
    page = format_campaign(campaign(tmp_path, in_force_history=record))
    assert "the oldest of them" not in page
