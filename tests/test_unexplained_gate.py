# -*- coding: utf-8 -*-
"""Round 117: a decision nobody explained becomes debt a gate can charge (item 103).

The four states were already counted (round 116); what the gate could not do is fail on the old ones, so
"3 decisions nobody can review" was a sentence on a page and never an exit code. These tests pin the two
halves: the charging (a cap the caller sets, never a default) and the page/JSON that must announce the same
thing the gate charges (round 82).
"""
import json
from datetime import datetime, timedelta, timezone

from brian import cli
from brian.campaign import campaign
from brian.evolve import load_settlements, settle_link, settle_note, unexplained_decisions

from test_provenance_block import RETIRED, SUCCESSOR, _mismatch
from test_settle_note import _settled_without_reason
from test_untested_answer import NOW

STORE = ("var", "evolve", "settlements.json")


def _aged_without_reason(tmp_path, days_ago=5.0):
    """A decision with no reason and no note, taken `days_ago` days before the real clock.

    `_settled_without_reason` stamps `NOW` (a fixed future instant), which makes an age assertion a statement
    about the test's clock rather than about the code's arithmetic (round 86's fixture defect).
    """
    _settled_without_reason(tmp_path)
    path = tmp_path.joinpath(*STORE)
    payload = json.loads(path.read_text(encoding="utf-8"))
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for item in payload["settlements"]:
        item["at"] = moment.isoformat()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return moment


def test_a_decision_with_no_reason_and_no_note_is_the_only_one_listed(tmp_path):
    _settled_without_reason(tmp_path)
    rows = unexplained_decisions(load_settlements(tmp_path))
    assert [row["record"] for row in rows] == [RETIRED]
    assert rows[0]["successor"] == SUCCESSOR
    assert rows[0]["age_days"] is not None


def test_a_decision_explained_at_the_time_is_not_debt(tmp_path):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="two books, one decision", by="alice", now=NOW)
    assert unexplained_decisions(load_settlements(tmp_path)) == []


def test_a_note_added_later_pays_the_debt(tmp_path):
    _aged_without_reason(tmp_path, days_ago=30.0)
    settle_note(tmp_path, RETIRED, SUCCESSOR, why="the queue paid it", by="bob", now=NOW)
    assert unexplained_decisions(load_settlements(tmp_path)) == []


def test_the_gate_charges_an_old_decision_when_the_caller_sets_the_cap(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=10.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-unexplained-days", "3"]) == 1
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 decision(s) have had no explanation for longer than the 3 day(s) asked for" in flat
    assert "`--settle-note RECORD=SUCCESSOR`" in flat


def test_the_gate_says_nothing_when_the_decision_is_younger_than_the_cap(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=1.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-unexplained-days", "3"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "have had no explanation for longer than" not in flat


def test_no_cap_means_no_charge_even_when_everything_is_unexplained(tmp_path, monkeypatch):
    """A default would be red on every ledger whose settlements predate --settle-why (boundary 154)."""
    _aged_without_reason(tmp_path, days_ago=400.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_cap_without_strict_is_refused_rather_than_inert(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=10.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--max-unexplained-days", "3"]) == 2
    assert "--max-unexplained-days caps a --strict charge" in " ".join(capsys.readouterr().out.split())


def test_the_history_page_prints_the_decisions_and_marks_the_ones_past_the_cap(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=10.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history", "--strict", "--max-unexplained-days", "3"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 settled decision(s) have no explanation" in flat
    assert "%s -> %s" % (RETIRED, SUCCESSOR) in flat
    assert "[past the cap: --strict exits 1]" in flat
    assert "--settle-note RECORD=SUCCESSOR" in flat


def test_the_history_page_states_the_fact_without_a_cap(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=1.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "1 settled decision(s) have no explanation" in flat
    assert "[past the cap: --strict exits 1]" not in flat


def test_the_campaign_row_carries_the_state_of_its_own_stream(tmp_path, monkeypatch, capsys):
    _aged_without_reason(tmp_path, days_ago=10.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign", "--json-out", "campaign.json"]) == 0
    payload = json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))
    rows = {row["domain"]: row for row in payload["domains"]}
    assert rows["SMD"]["settlements"] == {"unexplained": 1}
    # And the page's own account still agrees with the rows (round 264: one computation per state).
    assert campaign(tmp_path).settlement_summary["unexplained"] == 1


def test_a_campaign_row_with_no_decision_says_nothing_rather_than_zero(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="decided on the evidence", by="alice", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign", "--json-out", "campaign.json"]) == 0
    payload = json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))
    rows = {row["domain"]: row for row in payload["domains"]}
    assert rows["SMD"]["settlements"] == {"explained": 1}
