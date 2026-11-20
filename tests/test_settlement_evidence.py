# -*- coding: utf-8 -*-
"""Round 118 tests: a settlement is a statement about the books as they stood (item 104).

Two things make that statement stale, both visible in the ledger: the settled record no longer claims that
successor (`retargeted`), or a claim on the same successor arrived *after* the decision (`new_claimant`). The
other claimant of a `mismatch` does not count -- round 112 settled one claimant's claim on purpose. The round
prints all three pages and, deliberately, charges nothing: no command in this build can re-open a decision yet.
"""
import json
from datetime import timedelta

from brian import cli
from brian.campaign import campaign
from brian.evolve import history, load_settlements, moved_settlements, settle_note

from test_provenance_block import RETIRED, SUCCESSOR, _round
from test_settle_note import _settled_without_reason
from test_untested_answer import NOW


def _records(root):
    return history(root, include_retired=True)


def _settled_with_notes(tmp_path):
    _settled_without_reason(tmp_path)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="two books named the same successor", by="alice", now=NOW)
    return load_settlements(tmp_path)


def _later_claimant(root):
    """A claim on the same successor that arrives after the decision: the books moved, not just stood."""
    _round(root, "round-third.json",
           retired={"re_measured_as": SUCCESSOR, "at": (NOW + timedelta(days=2)).isoformat()})


def _forget(root, name):
    """Drop a round from both the directory and the ledger: `history` merges the two, so a stub line would keep
    the record alive with no retirement mark and change the reason the page prints."""
    (root / "var" / "evolve" / name).unlink()
    ledger = root / "var" / "evolve" / "ledger.jsonl"
    kept = [line for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("round") != name]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")


def _drop_the_claim(root):
    """The settled record stops naming that successor at all: the pair the decision describes is gone."""
    path = root / "var" / "evolve" / "round-old.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("retired", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_a_settlement_whose_other_claimant_stood_beside_it_is_not_moved(tmp_path):
    """Round 112 settles one claimant's claim and leaves the other end printed, so the other claimant is by
    design -- only a claim that arrived later moves the evidence."""
    _settled_with_notes(tmp_path)
    assert moved_settlements(_records(tmp_path), load_settlements(tmp_path)) == []


def test_the_settled_record_dropping_its_claim_moves_the_evidence(tmp_path):
    _settled_with_notes(tmp_path)
    _drop_the_claim(tmp_path)
    rows = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert len(rows) == 1
    assert rows[0]["record"] == RETIRED and rows[0]["successor"] == SUCCESSOR
    assert rows[0]["reasons"] == ["it carries no retirement mark any more"]


def test_a_claim_that_arrived_after_the_decision_moves_the_evidence(tmp_path):
    _settled_with_notes(tmp_path)
    _later_claimant(tmp_path)
    rows = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert len(rows) == 1
    assert any("arrived after the decision: round-third.json" in reason for reason in rows[0]["reasons"])
    assert rows[0]["claimants"] == ["round-third.json"]


def test_a_settled_record_that_vanished_from_the_ledger_is_reported(tmp_path):
    _settled_with_notes(tmp_path)
    _forget(tmp_path, RETIRED)
    rows = moved_settlements(_records(tmp_path), load_settlements(tmp_path))
    assert rows[0]["reasons"] == ["the settled record is no longer in the ledger"]


def test_the_audit_page_prints_the_moved_decisions(tmp_path, monkeypatch, capsys):
    _settled_with_notes(tmp_path)
    _drop_the_claim(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 of them rest on books that have moved since the decision was taken" in flat
    assert "it carries no retirement mark any more" in flat
    assert "the decision must be taken again (boundary 269)" in flat


def test_the_audit_page_says_nothing_when_the_books_still_stand(tmp_path, monkeypatch, capsys):
    _settled_with_notes(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit"]) == 0
    assert "rest on books that have moved" not in " ".join(capsys.readouterr().out.split())


def test_the_history_page_prints_the_moved_decisions(tmp_path, monkeypatch, capsys):
    _settled_with_notes(tmp_path)
    _later_claimant(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 decision(s) rest on books that have moved since they were taken" in flat
    assert "round-third.json" in flat
    assert "when they move, take it again" in flat


def test_the_campaign_row_counts_a_moved_decision_apart(tmp_path, monkeypatch, capsys):
    _settled_with_notes(tmp_path)
    _drop_the_claim(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign", "--json-out", "campaign.json"]) == 0
    payload = json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))
    assert len(payload["settlements_moved"]) == 1
    assert payload["settlements_moved"][0]["record"] == RETIRED
    rows = {row["domain"]: row for row in payload["domains"]}
    assert rows["SMD"]["settlements"] == {"moved": 1}


def test_nothing_is_charged_for_a_moved_decision_yet(tmp_path, monkeypatch):
    """Boundary 266: no command can re-open a decision in this build, so --strict must not fail on it."""
    _settled_with_notes(tmp_path)
    _drop_the_claim(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_campaign_reads_the_same_two_files_as_the_audit(tmp_path):
    _settled_with_notes(tmp_path)
    _later_claimant(tmp_path)
    result = campaign(tmp_path)
    assert [item["record"] for item in result.settlement_moved] == [RETIRED]


def test_an_unexplained_decision_is_the_previous_round_s_debt_not_this_one(tmp_path):
    """The two debts stay apart: 117 charges "nobody explained it", 118 reports "the books moved under it"."""
    _settled_without_reason(tmp_path)
    assert moved_settlements(_records(tmp_path), load_settlements(tmp_path)) == []
