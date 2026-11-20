# -*- coding: utf-8 -*-
"""Round 119: a decision can be re-taken, and one that has not been is chargeable.

Round 118 could say "the books moved under this decision" and nothing else: no command re-took the decision, so
the fact stayed on the page for ever and `--strict` could not charge it (boundary 271). `--settle-reaffirm` is
that command. It keeps the decision's **first** date -- when the question was settled -- and appends a
re-affirmation: when it was last confirmed, by whom, why, and **what it covered** (the claims standing on that
successor at that moment). `moved_settlements` then compares claims against the newest moment, so a claim that
arrived before the re-affirmation is covered and only a later one moves the evidence again.

Two kinds stay unchargeable, on purpose: a decision whose pair was retargeted or deleted has nothing to re-affirm,
and the remedy there is a new decision, not a re-dating (boundaries 273/275).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from brian import cli
from brian.evolve import (load_settlements, moved_clearable, moved_settlements, reaffirm_link, settle_link,
                          settlement_summary)

from test_provenance_block import RETIRED, SUCCESSOR
from test_settle_note import _settled_without_reason
from test_untested_answer import NOW

LATER = NOW + timedelta(days=2)
LATEST = NOW + timedelta(days=4)


def _settled(tmp_path):
    """A decision on `round-old.json`, and the *other* claimant settled too -- otherwise the fixture keeps a live
    mismatch, and every `--strict` assertion below would be answered by that instead of by the moved decision."""
    _settled_without_reason(tmp_path)


def _later_claim(root, *, name="round-third.json", at=LATER):
    """A claim on the same successor, dated after the decision, and consistent with the successor's own record.

    A claim the successor does not name is a fresh contradiction (round 118's own subject), and `--strict` would
    charge that instead of the moved decision -- so the successor is pointed at the new claim as well.
    """
    from test_provenance_block import _round

    _round(root, name, retired={"re_measured_as": SUCCESSOR, "at": at.isoformat()})
    # Settled when it arrived, so it is a claim the books cover rather than a contradiction `--strict` charges;
    # its own decision is dated now, which is *after* the decision under test, so that one is the stale row.
    settle_link(root, name, SUCCESSOR, why="decided when it arrived", by="alice", now=at)
    return name


def _ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _backdate_decision(root, days=30):
    """Move the decision itself into the past, so a claim dated a few days ago can be *after* it."""
    path = root / "var" / "evolve" / "settlements.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    for entry in stored["settlements"]:
        if entry.get("record") == RETIRED:
            entry["at"] = _ago(days).isoformat()
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8", newline="\n")


def _records(root):
    from brian.evolve import history

    return history(root, include_retired=True)


def test_a_reaffirmation_moves_the_moment_the_decision_speaks_about(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    before = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert [row["claims"] if "claims" in row else row["claimants"] for row in before] == [["round-third.json"]]

    again = reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="read today's books", by="alice",
                          claimants=["round-third.json"], now=LATER + timedelta(hours=1))
    assert again["changed"] is True and again["reaffirmations"] == 1

    after = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert after == []  # the claim that arrived before the re-affirmation is now covered


def test_the_first_date_is_kept_and_the_reaffirmation_is_a_second_one(tmp_path):
    _settled(tmp_path)
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="first re-read", by="alice", now=LATER)
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="second re-read", by="bob", now=LATEST)
    stored = load_settlements(tmp_path)[0]
    assert stored["at"] == NOW.isoformat()  # when the question was settled
    assert [n["why"] for n in stored["reaffirmations"]] == ["first re-read", "second re-read"]
    assert [n["by"] for n in stored["reaffirmations"]] == ["alice", "bob"]
    assert settlement_summary(load_settlements(tmp_path))["reaffirmed"] == 1


def test_what_it_covered_is_recorded(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="re-read", claimants=["round-third.json"], now=LATER)
    assert load_settlements(tmp_path)[0]["reaffirmations"][0]["claimants"] == ["round-third.json"]


def test_a_later_claim_stales_the_decision_again(tmp_path):
    _settled(tmp_path)
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="re-read", now=LATER)
    _later_claim(tmp_path, name="round-fourth.json", at=LATEST)
    rows = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert [row["claimants"] for row in rows] == [["round-fourth.json"]]


def test_a_reaffirmation_needs_a_reason(tmp_path):
    _settled(tmp_path)
    with pytest.raises(ValueError, match="needs a reason"):
        reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="   ", now=LATER)


def test_a_decision_whose_pair_is_gone_cannot_be_reaffirmed(tmp_path):
    _settled(tmp_path)
    path = tmp_path / "var" / "evolve" / "round-old.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["retired"] = {"re_measured_as": "round-other.json"}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="no longer names"):
        reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="re-read", now=LATER)


def test_an_unrecorded_decision_cannot_be_reaffirmed(tmp_path):
    _settled(tmp_path)
    with pytest.raises(ValueError, match="no settlement recorded"):
        reaffirm_link(tmp_path, "round-other.json", SUCCESSOR, why="re-read", now=LATER)


def test_the_same_moment_twice_changes_nothing(tmp_path):
    _settled(tmp_path)
    first = reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="re-read", now=LATER)
    second = reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="re-read", now=LATER)
    assert first["changed"] is True and second["changed"] is False
    assert len(load_settlements(tmp_path)[0]["reaffirmations"]) == 1


def test_moved_clearable_keeps_only_the_pair_that_still_exists(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    rows = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert len(moved_clearable(rows)) == 1
    assert rows[0]["clearable"] is True

    path = tmp_path / "var" / "evolve" / "round-old.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("retired", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    gone = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert moved_clearable(gone) == []
    assert gone[0]["clearable"] is False


def test_the_cli_records_the_reaffirmation_and_says_what_it_covered(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _later_claim(tmp_path, at=_ago(3))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "read today's books", "--settle-by", "alice",
                     "--json-out", "again.json"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "reaffirmed round-old.json -> round-new.json" in flat
    assert "the decision keeps its first date; this re-affirmation is on" in flat
    assert "what it covered:" in flat and "round-third.json" in flat
    assert "re-affirmed by: alice" in flat
    payload = json.loads((tmp_path / "again.json").read_text(encoding="utf-8"))
    assert [item["changed"] for item in payload["reaffirmed"]] == [True]
    assert payload["reaffirm_refused"] == 0


def test_the_audit_stops_calling_a_reaffirmed_decision_stale(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _later_claim(tmp_path, at=_ago(3))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit"]) == 0
    assert "rest on books that have moved" in " ".join(capsys.readouterr().out.split())

    assert cli.main(["evolve", "--settle-reaffirm", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "read today's books", "--settle-by", "alice"]) == 0
    capsys.readouterr()
    assert cli.main(["evolve", "--audit"]) == 0
    assert "rest on books that have moved" not in " ".join(capsys.readouterr().out.split())


def test_the_history_row_says_whether_the_decision_was_ever_retaken(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _later_claim(tmp_path, at=_ago(3))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    assert "never re-taken" in " ".join(capsys.readouterr().out.split())

    assert cli.main(["evolve", "--settle-reaffirm", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "read today's books", "--settle-by", "alice"]) == 0
    capsys.readouterr()
    # After the re-affirmation the CLI just took, so the decision is moved again -- and the row must then say it has
    # been re-taken once (a row that is no longer moved shows nothing, which is the point of the mark).
    _later_claim(tmp_path, name="round-fourth.json", at=_ago(-1))
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "re-taken 1 time(s), last on" in flat


def test_a_cap_charges_only_the_clearable_kind(tmp_path, monkeypatch):
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _later_claim(tmp_path, at=_ago(3))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0            # no cap asked for: nothing charged
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved", "0"]) == 1
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved", "1"]) == 0


def test_a_cap_without_strict_is_refused(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--max-moved", "0"]) == 2
    assert "caps a --strict charge; without --strict nothing reads it" in capsys.readouterr().out


def test_a_decision_whose_pair_is_gone_is_reported_but_never_charged(tmp_path, monkeypatch):
    """Boundary 275: only the clearable kind is gated -- no command can re-affirm a pair the ledger denies."""
    _settled(tmp_path)
    path = tmp_path / "var" / "evolve" / "round-old.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("retired", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved", "0"]) == 0
