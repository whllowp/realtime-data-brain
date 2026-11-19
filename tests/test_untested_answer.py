"""Round 89: a check that ran and compared nothing is not an answer, and must not reset the clock.

Round 88 taught `--verify` to record the clearing it just earned. But `--verify` also returns
`unmeasurable` -- the claim or the re-measurement has no recall at the budget, or the two streams are
sampled differently -- and that verdict cleared the objection just the same. Worse, clearing the row
meant the next audit observed the dispute again with *today's* date, so a debt answered by a failed
comparison looked freshly arrived and the stale charge never fired again.

That is the round's defect: a clock that a non-answer can reset is not a clock. The measurement is still
recorded -- someone did look -- but it answers nothing, the row keeps its first sighting, and the page says
which of the two things happened (boundary 188).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import Measurement, audit, format_audit, repair_legacy
from brian.promote import (cleared_summary, in_force_disputes, in_force_history,
                           load_promotions, observe_in_force_disputes, record_clearing, stale_in_force_disputes)
from test_in_force_provenance import _state
from test_provenance_block import _legacy, _round, _stream, _version

NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)


def _copied(root: Path) -> None:
    _legacy(root)
    _round(root, "round-other.json", applied=())
    repair_legacy(root)
    _version(root)
    _stream(root)
    _state(root, slug="smd")


def _observed(root: Path, *, at: datetime = NOW):
    return observe_in_force_disputes(root, in_force_disputes(root), now=at)


def _store(root: Path) -> dict:
    return json.loads((root / "var" / "evolve" / "in_force.json").read_text(encoding="utf-8"))


def _untested_measurement() -> Measurement:
    """What a stream with three windows produces: no at-budget recall, so nothing was compared."""
    return Measurement(candidate="digest.rolling_window = 200", recall=0.5, fpr=0.02, precision=0.9,
                       reports=3, tokens_per_report=100.0, tokens_per_window=20.0, latency_p95_ms=1.0,
                       recall_at_budget=None, fpr_at_budget=None, windows=3, budget=0.1)


def _tested_measurement() -> Measurement:
    return Measurement(candidate="digest.rolling_window = 200", recall=0.9, fpr=0.02, precision=0.9,
                       reports=3, tokens_per_report=100.0, tokens_per_window=20.0, latency_p95_ms=1.0,
                       recall_at_budget=0.507, fpr_at_budget=0.05, windows=3, budget=0.1)


def test_an_untested_clearing_keeps_the_row_and_records_the_attempt(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entries = record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="unmeasurable",
                              reason="the claim has no recall at the budget", tested=False)
    assert [entry["tested"] for entry in entries] == [False]
    observed = _store(tmp_path)["observed"]
    assert len(observed) == 1
    assert observed[0]["first_seen"] == NOW.isoformat()
    assert observed[0]["untested"]["at"] == (NOW + timedelta(days=3)).isoformat()
    assert observed[0]["untested"]["reason"] == "the claim has no recall at the budget"


def test_a_tested_clearing_still_removes_the_row(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="holds", tested=True)
    assert _store(tmp_path)["observed"] == []
    entries, _trimmed = in_force_history(tmp_path)
    assert entries[0]["tested"] is True


def test_an_unanswerable_verdict_does_not_make_the_debt_young_again(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    stored = record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="unmeasurable",
                             tested=False)
    assert stored
    # The objection is still the same objection: the same first sighting, and three days older.
    rows = in_force_disputes(tmp_path)
    assert len(rows) == 1
    aged = _observed(tmp_path, at=NOW + timedelta(days=30))[0]
    assert aged[0]["first_seen"] == NOW.isoformat()
    assert aged[0]["age_days"] == 30.0


def test_the_stale_charge_still_fires_after_an_unanswerable_measurement(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="unmeasurable", tested=False)
    promotions = load_promotions(tmp_path)
    # The version's own verification now says `unmeasurable`, dated after the objection: round 88 read that as
    # an answer. It compared nothing, so the debt is exactly as old as it was.
    payload = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    version = json.loads(payload.read_text(encoding="utf-8"))
    version["verification"] = {"checked_at": (NOW + timedelta(days=3)).isoformat(), "verdict": "unmeasurable"}
    payload.write_text(json.dumps(version), encoding="utf-8", newline="\n")
    rows = _observed(tmp_path, at=NOW + timedelta(days=30))[0]
    assert [row["version"] for row in stale_in_force_disputes(rows, load_promotions(tmp_path))] == [20]
    assert promotions


def test_a_tested_verdict_does_clear_the_stale_charge(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    payload = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    version = json.loads(payload.read_text(encoding="utf-8"))
    version["verification"] = {"checked_at": (NOW + timedelta(days=3)).isoformat(), "verdict": "holds"}
    payload.write_text(json.dumps(version), encoding="utf-8", newline="\n")
    rows = _observed(tmp_path, at=NOW + timedelta(days=30))[0]
    assert stale_in_force_disputes(rows, load_promotions(tmp_path)) == []


def test_the_summary_counts_an_unanswerable_clearing_apart(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="unmeasurable", tested=False)
    entries, trimmed = in_force_history(tmp_path)
    summary = cleared_summary(entries, trimmed=trimmed)
    assert summary["measured"] == 1 and summary["untested"] == 1
    assert summary["written"] == 1


def test_the_page_says_which_check_ran_and_why_it_did_not_count(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="unmeasurable",
                    reason="the claim has no recall at the budget", tested=False)
    rows = _observed(tmp_path, at=NOW + timedelta(days=30))[0]
    entries, _trimmed = in_force_history(tmp_path)
    page = format_audit(audit(tmp_path, in_force=rows, in_force_stale=stale_in_force_disputes(
        rows, load_promotions(tmp_path)), in_force_cleared=entries))
    assert "[stale: a check on" in page
    assert "could not test the claim]" in page
    assert "measuring again on the same stream compares nothing again" in page
    assert "1 clearing(s) rested on a check that ran and could not test the claim" in page


def test_the_page_does_not_call_a_mere_absence_a_failed_comparison(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    rows = _observed(tmp_path, at=NOW + timedelta(days=30))[0]
    page = format_audit(audit(tmp_path, in_force=rows, in_force_stale=stale_in_force_disputes(
        rows, load_promotions(tmp_path))))
    assert "[stale: no verdict that could test the claim]" in page
    assert "could not test the claim" not in page


def test_the_campaign_counts_them_and_says_they_answer_nothing():
    result = campaign(Path("."), in_force_history={
        "total": 2, "measured": 2, "unexplained": 0, "written": 2, "untested": 1,
        "median_age_days": 20.0, "trimmed": 0,
        "by_domain": {"smd": {"measured": 2, "unexplained": 0}},
    })
    page = format_campaign(result)
    assert "1 of those clearings rested on a check that could not test the claim" in page
    assert "they answer nothing" in page
    assert result.to_dict()["in_force_untested"] == 1


def test_verify_records_an_unanswerable_check_without_answering(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _copied(tmp_path)
    _observed(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _untested_measurement())
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "not answered  1 in-force observation(s) were measured" in out
    assert "must not reset the clock it was supposed to close (boundary 188)" in out
    assert "cleared         1 in-force observation(s) were answered" not in out
    stored = _store(tmp_path)
    assert len(stored["observed"]) == 1 and stored["observed"][0]["untested"]
    assert stored["cleared"][-1]["tested"] is False


def test_verify_records_a_real_answer_as_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _copied(tmp_path)
    _observed(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _tested_measurement())
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "cleared         1 in-force observation(s) were answered by this check" in out
    stored = _store(tmp_path)
    assert stored["observed"] == []
    assert stored["cleared"][-1]["tested"] is True
