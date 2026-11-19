"""Round 90: a clearing names the measurement that earned it.

The chain had two holes: the entry recorded *when* a check ran but not *on what*, and the store kept every
clearing as an anonymous row. So "the debt was paid" could not be traced to a file a reader can open -- the same
defect round 87 fixed one level up (a clearing that does not cite its cause), one level down (a citation that
does not name its evidence).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit
from brian.promote import cleared_summary, in_force_disputes, in_force_history, record_clearing
from test_untested_answer import NOW, _copied, _observed, _store

FILE = "data/smd/stream-one.json"


def _clear(root: Path, *, days: float, verdict: str = "holds", tested: bool = True,
           path: str = FILE) -> list:
    return record_clearing(root, 20, now=NOW + timedelta(days=days), verdict=verdict,
                           reason="measured again by --verify", tested=tested, stream_path=path)


def test_a_clearing_names_the_file_and_its_place_in_the_chain(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    first = _clear(tmp_path, days=3)
    assert first[0]["stream_path"] == FILE
    assert first[0]["attempt"] == 1
    # A second clearance of the same version is attempt 2, not another "cleared once".
    _observed(tmp_path)
    second = _clear(tmp_path, days=6)
    assert second[0]["attempt"] == 2
    entries, _trimmed = in_force_history(tmp_path)
    assert [entry["attempt"] for entry in entries] == [1, 2]


def test_an_untested_attempt_names_the_file_it_read(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3, verdict="unmeasurable", tested=False)
    observed = _store(tmp_path)["observed"]
    assert observed[0]["untested"]["stream_path"] == FILE


def test_an_unnamed_measurement_is_recorded_as_empty(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entry = _clear(tmp_path, days=3, path="")[0]
    assert entry["stream_path"] == ""


def test_the_summary_counts_clearings_by_file(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)          # a clearance removes the row, so the next one needs it re-observed
    _clear(tmp_path, days=6, path="data/smd/stream-two.json")
    summary = cleared_summary(in_force_history(tmp_path)[0])
    assert summary["by_file"] == {"data/smd/stream-one.json": 1, "data/smd/stream-two.json": 1}


def test_the_page_names_the_file_and_the_attempt(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6)
    rows = in_force_disputes(tmp_path)
    page = format_audit(audit(tmp_path, in_force=rows,
                              in_force_cleared=in_force_history(tmp_path)[0]))
    assert "of %s" % FILE in page
    assert "attempt 2" in page


def test_an_unnamed_measurement_is_printed_as_unnamed(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3, path="")
    page = format_audit(audit(tmp_path, in_force=in_force_disputes(tmp_path),
                              in_force_cleared=in_force_history(tmp_path)[0]))
    assert "(unnamed stream)" in page


def test_the_campaign_counts_clearings_by_file(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    entries, trimmed = in_force_history(tmp_path)
    page = format_campaign(campaign(tmp_path,
                                    in_force_history=cleared_summary(entries, trimmed=trimmed)))
    assert "clearings by stream file: %s 1" % FILE in page


def test_the_history_page_lists_the_chain(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    _observed(tmp_path)
    _clear(tmp_path, days=6)
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--history"])
    out = capsys.readouterr().out
    assert "2 recorded clearing(s)" in out
    assert "attempt 1" in out and "attempt 2" in out
    assert FILE in out


def test_the_history_json_carries_the_chain(tmp_path, monkeypatch):
    _copied(tmp_path)
    _observed(tmp_path)
    _clear(tmp_path, days=3)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "history.json"
    cli.main(["evolve", "--history", "--json-out", str(target)])
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["in_force_clearings"][0]["stream_path"] == FILE
    assert payload["in_force_clearings"][0]["attempt"] == 1
