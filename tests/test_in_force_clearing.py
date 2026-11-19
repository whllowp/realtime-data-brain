"""Round 87: a cleared dispute must cite what cleared it.

Round 86 recorded when a dispute was first seen and returned it once when it stopped being true, but the store
kept nothing: the page could say "one version was disputed and no longer is", never *why*. Two very different
events look identical there -- a re-measurement of the deployed version (the one action that answers a copied
end) and the ledger simply stopping to reproduce the objection. The second is not a resolution, it is an
absence of observation, and folding the two together lets a debt disappear and be counted as paid.

The store therefore keeps a `cleared` history with a citation per entry, and the campaign counts the two kinds
apart with the median age. Nothing is charged for an unexplained clearing: the store is a local observation
file, and a gate that fails on a missing local file punishes a fresh clone (boundaries 154, 171, 183).

This round also found a page that stayed silent: `format_audit` called the link printer only when `links or
backfilled` was non-empty, so a cleared history printed nothing until an unrelated block existed. One property,
`Audit.link_content`, now decides for both call sites (boundary 184).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit, repair_legacy
from brian.promote import (CLEARED_BY_LEDGER, CLEARED_BY_MEASUREMENT, IN_FORCE_HISTORY_LIMIT, IN_FORCE_STORE,
                           cleared_summary, in_force_history, load_promotions, observe_in_force_disputes)
from test_in_force_provenance import _state
from test_provenance_block import _legacy, _round, _stream, _version

NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)


def _copied(root: Path) -> None:
    """A deployed version whose producing record's link was repaired: the round-85 'copied' ledger."""
    _legacy(root)
    _round(root, "round-other.json", applied=())
    repair_legacy(root)
    _version(root)
    _stream(root)
    _state(root, slug="smd")


def _heal_the_record(root: Path) -> None:
    """Stop the ledger disputing the record without measuring anything: the successor names its own predecessor."""
    path = root / "var" / "evolve" / "round-new.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    queue = payload.get("queue") or {}
    queue.pop("backfilled", None)
    queue["paid_round"] = "round-old.json"
    payload["queue"] = queue
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _verdict(root: Path, *, at: "datetime") -> None:
    """A verdict stamped at a given moment: the citation compares it with the first sighting, not with now."""
    path = root / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["verification"] = {"verdict": "holds", "checked_at": at.isoformat()}
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _rows(root: Path):
    from brian.promote import in_force_disputes
    return in_force_disputes(root)


def test_a_clearing_with_no_measurement_is_not_a_resolution(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW + timedelta(days=1))
    assert [item["by"] for item in cleared] == [CLEARED_BY_LEDGER]
    assert cleared[0]["checked_at"] is None
    assert cleared[0]["cleared_at"] == (NOW + timedelta(days=1)).isoformat()
    assert cleared[0]["age_days"] == 1.0


def test_a_clearing_with_a_newer_measurement_cites_it(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _verdict(tmp_path, at=NOW)
    _heal_the_record(tmp_path)
    rows = _rows(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, rows, now=NOW,
                                           promotions=load_promotions(tmp_path))
    assert [item["by"] for item in cleared] == [CLEARED_BY_MEASUREMENT]
    assert cleared[0]["checked_at"] is not None


def test_a_measurement_older_than_the_objection_does_not_cite_it(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _verdict(tmp_path, at=NOW - timedelta(days=3))
    _heal_the_record(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW,
                                           promotions=load_promotions(tmp_path))
    assert [item["by"] for item in cleared] == [CLEARED_BY_LEDGER]


def test_the_clearing_is_kept_in_the_store_and_read_back(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW + timedelta(days=2))
    stored = json.loads((tmp_path / "var" / "evolve" / IN_FORCE_STORE).read_text(encoding="utf-8"))
    assert stored["observed"] == []
    assert len(stored["cleared"]) == 1
    entries, trimmed = in_force_history(tmp_path)
    assert len(entries) == 1 and trimmed == 0
    assert entries[0]["age_days"] == 2.0


def test_the_history_is_capped_and_says_how_much_it_dropped(tmp_path):
    # A state file that grows without a bound is a file nobody can read, and a trimmed history that does not
    # say so looks complete.
    _copied(tmp_path)
    path = tmp_path / "var" / "evolve" / IN_FORCE_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"domain": "smd", "version": 20 + index, "kind": "backfilled end",
                "first_seen": NOW.isoformat(), "last_seen": NOW.isoformat(), "observations": 1,
                "cleared_at": NOW.isoformat(), "age_days": 1.0, "by": CLEARED_BY_LEDGER,
                "checked_at": None} for index in range(IN_FORCE_HISTORY_LIMIT)]
    path.write_text(json.dumps({"format": "brian.inforce/2", "observed": [], "cleared": entries,
                                "trimmed": 0}), encoding="utf-8", newline="\n")
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW + timedelta(days=1))
    kept, trimmed = in_force_history(tmp_path)
    assert len(kept) == IN_FORCE_HISTORY_LIMIT
    assert trimmed == 1
    assert cleared_summary(kept, trimmed=trimmed)["trimmed"] == 1


def test_a_round_86_store_is_still_readable(tmp_path):
    # The v1 store had no `cleared` key at all: that reads as "nothing has cleared yet", not as a defect.
    _copied(tmp_path)
    path = tmp_path / "var" / "evolve" / IN_FORCE_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "brian.inforce/1", "observed": []}), encoding="utf-8",
                    newline="\n")
    assert in_force_history(tmp_path) == ([], 0)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    assert [item["observations"] for item in _read(tmp_path)] == [1]


def _read(root: Path):
    path = root / "var" / "evolve" / IN_FORCE_STORE
    return json.loads(path.read_text(encoding="utf-8"))["observed"]


def test_the_summary_keeps_the_two_kinds_apart():
    entries = [
        {"by": CLEARED_BY_MEASUREMENT, "age_days": 2.0},
        {"by": CLEARED_BY_MEASUREMENT, "age_days": 4.0},
        {"by": CLEARED_BY_LEDGER, "age_days": 9.0},
    ]
    summary = cleared_summary(entries)
    assert summary == {"total": 3, "measured": 2, "unexplained": 1,
                       "median_age_days": 4.0, "trimmed": 0}


def test_the_page_says_what_cleared_it(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW + timedelta(days=3))
    page = format_audit(audit(tmp_path, in_force_cleared=cleared))
    assert "no longer disputed, and the ledger forgets them now" in page
    assert "cleared by no measurement: the ledger no longer reproduces it" in page
    assert "A dispute that left the ledger without a measurement is not resolved, it is unobserved" in page


def test_the_page_prints_the_clearing_even_with_no_link_block(tmp_path):
    # The defect this round found live: `_link_lines` used to be called only when a link or a backfilled end
    # existed, so the campaign reported a clearing that the audit page never mentioned.
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW + timedelta(days=1))
    result = audit(tmp_path, in_force_cleared=cleared)
    assert result.links == [] and result.backfilled == []
    assert result.link_content is True
    assert "no longer disputed" in format_audit(result)


def test_an_audit_with_nothing_to_say_prints_no_link_section(tmp_path):
    _round(tmp_path, "round-plain.json")
    result = audit(tmp_path)
    assert result.link_content is False
    assert "replacement link" not in format_audit(result)


def test_the_campaign_counts_the_two_kinds_apart(tmp_path):
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _verdict(tmp_path, at=NOW)
    _heal_the_record(tmp_path)
    rows = _rows(tmp_path)
    _, cleared = observe_in_force_disputes(tmp_path, rows, now=NOW, promotions=load_promotions(tmp_path))
    observe_in_force_disputes(tmp_path, rows, now=NOW)
    entries, trimmed = in_force_history(tmp_path)
    summary = cleared_summary(entries, trimmed=trimmed)
    result = campaign(tmp_path, in_force_cleared=cleared, in_force_history=summary)
    assert result.to_dict()["in_force_cleared_history"]["total"] == 1
    page = format_campaign(result)
    assert "1 dispute(s) left the in-force list: 1 cleared by a re-measurement" in page
    assert "0 with no measurement on file" in page


def test_the_gate_is_unchanged_by_a_clearing(tmp_path, monkeypatch):
    # A cleared dispute is history; it was never charged, so its clearing is not a charge either.
    _copied(tmp_path)
    observe_in_force_disputes(tmp_path, _rows(tmp_path), now=NOW)
    _heal_the_record(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
