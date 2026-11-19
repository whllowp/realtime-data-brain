"""Round 86: a recomputed dispute gets a history, and only the clearable kind is escalated.

Round 85 asked the provenance question about the versions already in force, and the answer is recomputed from
the ledger on every run: it can say *that* a deployed version sits on a copied record, never *for how long*.
The ledger cannot supply the age either -- it records what changed, not when the loop noticed -- so the first
sighting is written beside the ledger rather than inside it (boundary 180).

Escalation is limited to the copied end. A contradiction is already charged (no measurement settles which book
is right); a copy has exactly one remedy, `--verify` of the deployed version, so an aged copy is charged exit 1
and the charge names that command (boundary 181). The judgement itself is made once, in
`stale_in_force_disputes`, and handed to both the page and the gate -- round 86's own first version decided it
twice, and the audit page came to promise a charge the gate did not make.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.campaign import campaign
from brian.evolve import audit, format_audit, repair_legacy
from brian.promote import (IN_FORCE_STORE, LINK_BACKFILLED, LINK_BROKEN, load_promotions,
                           observe_in_force_disputes, stale_in_force_disputes)
from test_in_force_provenance import _state
from test_provenance_block import _legacy, _round, _stream, _version

NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)


def _copied(root: Path) -> None:
    """A deployed version whose producing record's link was repaired: the round 85 'copied' ledger."""
    _legacy(root)
    _round(root, "round-other.json", applied=())
    repair_legacy(root)
    _version(root)
    _stream(root)
    _state(root, slug="smd")


def _store(root: Path, rows) -> None:
    path = root / "var" / "evolve" / IN_FORCE_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "brian.inforce/1", "observed": rows}), encoding="utf-8",
                    newline="\n")


def _ago(days: float) -> str:
    """A stamp relative to the real clock: a test that passes no `now` is aged against it, not against NOW."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _stored_copy(days_ago: float = 20.0, *, broken: bool = False) -> dict:
    return {"domain": "smd", "version": 20, "broken": broken,
            "kind": LINK_BROKEN if broken else LINK_BACKFILLED,
            "detail": "copied" if not broken else "mismatch",
            "first_seen": _ago(days_ago), "last_seen": _ago(0.0),
            "observations": 4, "age_days": days_ago}


def _rows(root: Path):
    from brian.promote import in_force_disputes
    return in_force_disputes(root)


def test_the_first_sighting_is_written_down(tmp_path):
    _copied(tmp_path)
    aged, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert cleared == []
    assert [item["kind"] for item in aged] == [LINK_BACKFILLED]
    assert aged[0]["first_seen"] == NOW.isoformat()
    assert aged[0]["observations"] == 1
    assert aged[0]["age_days"] == 0.0
    stored = json.loads((tmp_path / "var" / "evolve" / IN_FORCE_STORE).read_text(encoding="utf-8"))
    assert stored["format"] == "brian.inforce/1"
    assert stored["observed"][0]["version"] == 20


def test_the_sighting_is_kept_and_counted_on_later_runs(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    later = NOW + timedelta(days=3)
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=later)
    assert aged[0]["first_seen"] == NOW.isoformat()
    assert aged[0]["observations"] == 2
    assert aged[0]["age_days"] == 3.0


def test_a_dispute_that_stopped_being_true_is_forgotten_once(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    aged, cleared = observe_in_force_disputes(tmp_path, [], now=NOW + timedelta(days=1))
    assert aged == []
    assert [item["version"] for item in cleared] == [20]
    # Reported once, then gone: the page must not keep announcing a debt that stopped existing.
    _, again = observe_in_force_disputes(tmp_path, [], now=NOW + timedelta(days=2))
    assert again == []


def test_a_clean_tree_writes_no_file(tmp_path):
    _round(tmp_path, "round-plain.json")
    observe_in_force_disputes(tmp_path, [], now=NOW)
    assert not (tmp_path / "var" / "evolve" / IN_FORCE_STORE).exists()


def test_a_corrupt_store_is_replaced_rather_than_fatal(tmp_path):
    _copied(tmp_path)
    target = tmp_path / "var" / "evolve" / IN_FORCE_STORE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8", newline="\n")
    aged, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert cleared == []
    assert aged[0]["first_seen"] == NOW.isoformat()


def test_a_fresh_copy_is_not_stale(tmp_path):
    _copied(tmp_path)
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert stale_in_force_disputes(aged, load_promotions(tmp_path)) == []


def test_a_copy_past_the_grace_period_is_stale(tmp_path):
    _copied(tmp_path)
    _store(tmp_path, [{"domain": "smd", "version": 20, "broken": False, "kind": LINK_BACKFILLED,
                       "detail": "copied", "first_seen": (NOW - timedelta(days=20)).isoformat(),
                       "last_seen": NOW.isoformat(), "observations": 4, "age_days": 20.0}])
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert [item["version"] for item in stale_in_force_disputes(aged, load_promotions(tmp_path))] == [20]


def test_a_contradiction_is_never_stale(tmp_path):
    # It is charged the moment it exists (round 85), so an age adds nothing a re-measurement could settle.
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json", queue={"paid": "SMD", "paid_round": "round-other.json"})
    _round(tmp_path, "round-other.json", retired={"re_measured_as": "round-new.json"})
    _version(tmp_path)
    _stream(tmp_path)
    _state(tmp_path, slug="smd")
    _store(tmp_path, [{"domain": "smd", "version": 20, "broken": True, "kind": LINK_BROKEN,
                       "detail": "mismatch", "first_seen": (NOW - timedelta(days=90)).isoformat(),
                       "last_seen": NOW.isoformat(), "observations": 9, "age_days": 90.0}])
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert stale_in_force_disputes(aged, load_promotions(tmp_path)) == []


def test_a_measurement_newer_than_the_objection_clears_the_age(tmp_path):
    _copied(tmp_path)
    _store(tmp_path, [{"domain": "smd", "version": 20, "broken": False, "kind": LINK_BACKFILLED,
                       "detail": "copied", "first_seen": (NOW - timedelta(days=20)).isoformat(),
                       "last_seen": NOW.isoformat(), "observations": 4, "age_days": 20.0}])
    target = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["verification"] = {"verdict": "holds", "checked_at": (NOW - timedelta(days=1)).isoformat()}
    target.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert stale_in_force_disputes(aged, load_promotions(tmp_path)) == []


def test_an_older_measurement_does_not_clear_it(tmp_path):
    _copied(tmp_path)
    _store(tmp_path, [{"domain": "smd", "version": 20, "broken": False, "kind": LINK_BACKFILLED,
                       "detail": "copied", "first_seen": (NOW - timedelta(days=20)).isoformat(),
                       "last_seen": NOW.isoformat(), "observations": 4, "age_days": 20.0}])
    target = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["verification"] = {"verdict": "holds", "checked_at": (NOW - timedelta(days=30)).isoformat()}
    target.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    aged, _ = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert [item["version"] for item in stale_in_force_disputes(aged, load_promotions(tmp_path))] == [20]


def test_the_audit_page_marks_the_stale_row_and_says_so(tmp_path):
    _copied(tmp_path)
    _store(tmp_path, [_stored_copy()])
    rows, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path))
    stale = stale_in_force_disputes(rows, load_promotions(tmp_path))
    page = format_audit(audit(tmp_path, in_force=rows, in_force_cleared=cleared, in_force_stale=stale))
    assert "[stale: no verdict newer than the objection]" in page
    assert "1 copy(ies) have stood longer than 14 day(s) with no verdict newer than the" in page
    assert "`--strict` asks for a re-measurement of the deployed version" in page


def test_the_page_does_not_promise_a_charge_the_gate_does_not_make(tmp_path):
    # The defect this round found live: the page escalated on age alone, the gate on age plus no newer
    # measurement, so a verified version read as stale on the page while --strict exited 0.
    _copied(tmp_path)
    _store(tmp_path, [_stored_copy()])
    target = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["verification"] = {"verdict": "holds", "checked_at": _ago(0.0)}
    target.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    rows, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path))
    stale = stale_in_force_disputes(rows, load_promotions(tmp_path))
    page = format_audit(audit(tmp_path, in_force=rows, in_force_cleared=cleared, in_force_stale=stale))
    assert "have stood longer than" not in page
    assert "[stale:" not in page
    assert "`--strict` asks for a re-measurement" not in page


def test_the_gate_charges_an_aged_copy_and_stops_once_it_is_measured(tmp_path, monkeypatch):
    _copied(tmp_path)
    _store(tmp_path, [_stored_copy()])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 1
    target = tmp_path / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["verification"] = {"verdict": "holds", "checked_at": datetime.now(timezone.utc).isoformat()}
    target.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_campaign_prints_the_age_and_the_stale_line(tmp_path):
    _copied(tmp_path)
    _store(tmp_path, [{"domain": "smd", "version": 20, "broken": False, "kind": LINK_BACKFILLED,
                       "detail": "copied", "first_seen": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                       "last_seen": datetime.now(timezone.utc).isoformat(), "observations": 4, "age_days": 20.0}])
    rows, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path))
    stale = stale_in_force_disputes(rows, load_promotions(tmp_path))
    result = campaign(tmp_path, in_force=rows, in_force_cleared=cleared, in_force_stale=stale)
    payload = result.to_dict()
    assert payload["in_force_stale"] == 1
    assert payload["in_force_cleared"] == 0
    page = "\n".join(cli_lines(tmp_path, rows, cleared, stale))
    assert "v020 (20.0 day(s))" in page
    assert "have stood past the 14-day grace period with no verdict newer than the objection" in page


def cli_lines(root: Path, rows, cleared, stale):
    from brian.campaign import format_campaign
    return format_campaign(campaign(root, in_force=rows, in_force_cleared=cleared,
                                    in_force_stale=stale)).splitlines()
