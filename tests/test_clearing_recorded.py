"""Round 88: a check that answers an objection should record that itself.

Round 87 deduced every clearing by comparing timestamps afterwards, which makes the fact depend on the ledger
still holding both ends: rewrite the record, retire the round, or run the audit in a checkout that never saw the
objection, and the clearing becomes unobservable. A `--verify` run knows the answer the moment it has it, so
`record_clearing` writes the entry then, and the page says which citations were written by the measurement rather
than deduced.

The round also found the loop arguing with itself: round 86 escalates an aged copied end by naming
`brian evolve --verify …`, and round 84 made `--verify` refuse exactly that case, so the charge it printed could
never be cleared. The refusal is right as a default -- a promise must not be stacked on an end the books cannot
attest -- but measuring is not stacking, so `--accept-copied` measures, records and does not apply. That is
boundary 185: a remedy must be reachable by the command the diagnosis names.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import Measurement, audit, format_audit, repair_legacy
from brian.promote import (CLEARED_BY_LEDGER, CLEARED_BY_MEASUREMENT, cleared_summary, in_force_history,
                           load_promotions, observe_in_force_disputes, record_clearing)
from test_in_force_provenance import _state
from test_provenance_block import _legacy, _mismatch, _round, _stream, _version

NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)


def _copied(root: Path) -> None:
    """A deployed version whose producing record's link was repaired from one end."""
    _legacy(root)
    _round(root, "round-other.json", applied=())
    repair_legacy(root)
    _version(root)
    _stream(root)
    _state(root, slug="smd")


def _rows(root: Path):
    from brian.promote import in_force_disputes
    return in_force_disputes(root)


def _observed(root: Path, *, at: datetime = NOW):
    return observe_in_force_disputes(root, _rows(root), now=at)


def _store(root: Path) -> dict:
    return json.loads((root / "var" / "evolve" / "in_force.json").read_text(encoding="utf-8"))


def _broken_row(root: Path) -> None:
    """A contradiction: two records both claim `round-new`, so no measurement settles which book is right."""
    _mismatch(root)
    _version(root)
    _stream(root)
    _state(root, slug="smd")


def _measurement() -> Measurement:
    return Measurement(candidate="digest.rolling_window = 200", recall=0.9, fpr=0.01, precision=0.9,
                       reports=5, tokens_per_report=10.0, tokens_per_window=2.0, latency_p95_ms=1.0,
                       recall_at_budget=0.9, fpr_at_budget=0.01, windows=10, budget=0.1)


def test_a_clearing_written_by_the_measurement_cites_itself(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    entries = record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="holds")
    assert [entry["by"] for entry in entries] == [CLEARED_BY_MEASUREMENT]
    assert entries[0]["cited_by"] == "measurement"
    assert entries[0]["age_days"] == 3.0
    assert entries[0]["verdict"] == "holds"
    assert entries[0]["checked_at"] is not None
    # The observation it answered is gone from the live rows: the copy is not a live debt once a verdict covers it.
    assert _store(tmp_path)["observed"] == []
    stored, trimmed = in_force_history(tmp_path)
    assert trimmed == 0 and len(stored) == 1 and stored[0]["cited_by"] == "measurement"


def test_a_contradiction_is_not_cleared_by_measuring_the_version(tmp_path):
    _broken_row(tmp_path)
    rows = _rows(tmp_path)
    assert rows and rows[0]["broken"] is True
    observe_in_force_disputes(tmp_path, rows, now=NOW)
    assert record_clearing(tmp_path, 20, now=NOW + timedelta(days=3)) == []
    assert [item["version"] for item in _store(tmp_path)["observed"]] == [20]


def test_nothing_is_written_when_no_observation_awaits_the_answer(tmp_path):
    _copied(tmp_path)
    assert record_clearing(tmp_path, 20, now=NOW) == []
    assert not (tmp_path / "var" / "evolve" / "in_force.json").exists()


def test_a_measurement_is_a_new_observation_not_a_double_count(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="holds")
    aged, _cleared = _observed(tmp_path, at=NOW + timedelta(days=4))
    assert aged and aged[0]["first_seen"].startswith((NOW + timedelta(days=4)).isoformat()[:19])
    stored, _trimmed = in_force_history(tmp_path)
    # One clearing, not two: the answered observation was removed before the row was seen again.
    assert len(stored) == 1


def test_the_summary_says_how_many_citations_the_measurement_wrote(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="holds")
    entries, trimmed = in_force_history(tmp_path)
    summary = cleared_summary(entries, trimmed=trimmed)
    assert summary["written"] == 1
    assert summary["measured"] == 1 and summary["unexplained"] == 0
    assert summary["by_domain"] == {"smd": {"measured": 1, "unexplained": 0}}


def test_the_page_tells_a_written_citation_from_a_deduced_one(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW + timedelta(days=3), verdict="holds")
    entries, trimmed = in_force_history(tmp_path)
    page = format_audit(audit(tmp_path, in_force=None, in_force_cleared=entries))
    assert trimmed == 0
    assert "cleared by a re-measurement on" in page
    assert "(written by that measurement)" in page


def test_the_page_does_not_claim_a_citation_it_did_not_write(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    _heal = tmp_path / "var" / "evolve" / "round-new.json"
    payload = json.loads(_heal.read_text(encoding="utf-8"))
    queue = payload.get("queue") or {}
    queue.pop("backfilled", None)
    queue["paid_round"] = "round-old.json"
    payload["queue"] = queue
    _heal.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    _observed(tmp_path, at=NOW + timedelta(days=1))
    entries, _trimmed = in_force_history(tmp_path)
    page = format_audit(audit(tmp_path, in_force=None, in_force_cleared=entries))
    assert entries and entries[0]["cited_by"] == "audit"
    assert "no measurement: the ledger no longer reproduces it" in page
    assert "(written by that measurement)" not in page


def test_the_campaign_prints_the_split_by_stream_and_the_written_share():
    result = campaign(Path("."), in_force_history={
        "total": 2, "measured": 1, "unexplained": 1, "written": 1, "median_age_days": 20.0, "trimmed": 0,
        "by_domain": {"smd": {"measured": 1, "unexplained": 0},
                      "rcaeval-re1-ob": {"measured": 0, "unexplained": 1}},
    })
    page = format_campaign(result)
    assert "1 of those clearings were written by the measurement itself" in page
    assert ("clearings by domain: rcaeval-re1-ob 0 by measurement, 1 without; "
            "smd 1 by measurement, 0 without") in page


def test_the_campaign_keeps_one_stream_out_of_the_split_line():
    result = campaign(Path("."), in_force_history={
        "total": 1, "measured": 1, "unexplained": 0, "written": 1, "median_age_days": 20.0, "trimmed": 0,
        "by_domain": {"smd": {"measured": 1, "unexplained": 0}},
    })
    page = format_campaign(result)
    assert "clearings by domain" not in page
    assert CLEARED_BY_LEDGER not in page


def test_verify_refuses_a_copied_end_by_default_and_names_the_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _copied(tmp_path)
    _observed(tmp_path)
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json"])
    out = capsys.readouterr().out
    assert code == 1
    assert "--accept-copied measures it without applying" in out
    version = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert "verification" not in version


def test_verify_measures_a_copied_end_when_asked_and_clears_the_observation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _copied(tmp_path)
    _observed(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "measured, not applied: the record's end was copied" in out
    assert "cleared         1 in-force observation(s) were answered by this check" in out
    version = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert version["verification"]["verdict"], version
    stored, _trimmed = in_force_history(tmp_path)
    assert [entry["cited_by"] for entry in stored] == ["measurement"]
    # Not applied: a copied end cannot attest a promise, so the deployment state is untouched.
    state = json.loads((tmp_path / "var" / "evolve" / "domains" / "smd" / "current.json").read_text("utf-8"))
    assert state["sources"] == {"BRIAN_DIGEST_ROLLING_WINDOW": "20"}
    assert load_promotions(tmp_path)
