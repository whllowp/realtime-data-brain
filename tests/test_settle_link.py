# -*- coding: utf-8 -*-
"""Round 112: a settlement is a recorded decision, and it ends a charge without producing evidence."""
import json

import pytest

from brian import cli
from brian.evolve import audit, history, load_settlements, replacement_links, settle_link
from brian.promote import blocked_pairs, blocked_versions, provenance_block, repair_history, repair_plan

from test_provenance_block import OTHER, RETIRED, SUCCESSOR, _mismatch
from test_untested_answer import NOW


def _settled(tmp_path):
    _mismatch(tmp_path)
    return settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid this one", now=NOW)


def _walk(root, attempted):
    from test_queue_history import _walk as write_walk

    write_walk(root, attempted=attempted)


def test_a_settlement_is_recorded_with_its_time_and_reason(tmp_path):
    entry = _settled(tmp_path)
    assert entry["record"] == RETIRED and entry["successor"] == SUCCESSOR
    assert entry["at"] == NOW.isoformat()
    assert entry["why"] == "the queue paid this one"
    assert entry["changed"] is True
    assert load_settlements(tmp_path)[-1]["successor"] == SUCCESSOR


def test_settling_twice_changes_nothing(tmp_path):
    _settled(tmp_path)
    again = settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    assert again["changed"] is False
    assert len(load_settlements(tmp_path)) == 1


def test_a_settlement_needs_two_real_rounds(tmp_path):
    _mismatch(tmp_path)
    with pytest.raises(ValueError):
        settle_link(tmp_path, "", SUCCESSOR)
    with pytest.raises(ValueError):
        settle_link(tmp_path, RETIRED, RETIRED)
    with pytest.raises(ValueError):
        settle_link(tmp_path, "round-nobody.json", SUCCESSOR)


def test_a_settled_pair_is_no_longer_a_contradiction(tmp_path):
    _mismatch(tmp_path)
    before = [item for item in replacement_links(history(tmp_path, include_retired=True))
              if item["kind"] == "mismatch"]
    assert [item["claimant"] for item in before] == [RETIRED]
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    after = [item for item in replacement_links(history(tmp_path, include_retired=True),
                                                load_settlements(tmp_path)) if item["kind"] == "mismatch"]
    assert after == []


def test_a_settlement_does_not_rewrite_the_record_it_settles(tmp_path):
    _settled(tmp_path)
    successor = next(item for item in history(tmp_path, include_retired=True)
                     if item.get("round") == SUCCESSOR)
    # The successor's own end still names whoever the queue paid: a decision silences one claimant's claim,
    # it does not edit the books (round 112).
    assert successor["queue"]["paid_round"] == OTHER
    retired = next(item for item in history(tmp_path, include_retired=True) if item.get("round") == RETIRED)
    assert retired["retired"]["re_measured_as"] == SUCCESSOR


def test_the_gate_stops_charging_a_settled_record(tmp_path):
    _mismatch(tmp_path)
    assert provenance_block(tmp_path, 20)[0] == "broken link"
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    assert provenance_block(tmp_path, 20) is None


def test_the_plan_names_the_settlement_command(tmp_path):
    _mismatch(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": ""}])
    plan = repair_plan(tmp_path)
    assert plan and plan[0]["pairs"] == [{"record": RETIRED, "successor": SUCCESSOR}]
    assert plan[0]["remedy"] == "brian evolve --settle-link %s=%s" % (RETIRED, SUCCESSOR)


def test_settling_clears_the_refusal(tmp_path):
    _mismatch(tmp_path)
    why = provenance_block(tmp_path, 20)[1]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])
    assert 20 in blocked_versions(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    assert 20 not in blocked_versions(tmp_path)
    assert repair_plan(tmp_path) == []


def test_the_audit_prints_the_decisions_apart(tmp_path):
    from brian.evolve import format_audit

    _mismatch(tmp_path)
    assert "--strict" not in ""
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    report = audit(tmp_path)
    assert [item["record"] for item in report.settled] == [RETIRED]
    printed = format_audit(report)
    flat = " ".join(printed.split())
    assert "were settled by a recorded decision, not by agreement" in flat
    assert "%s -> %s" % (RETIRED, SUCCESSOR) in flat
    assert "it is not a second witness" in flat
    # A decided pair is not printed as a link that agrees: round 82's wording must not swallow it.
    assert "every replacement link agrees" not in flat or "settled" in flat


def test_the_cli_records_a_settlement_and_its_effect_on_the_plan(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    why = provenance_block(tmp_path, 20)[1]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (RETIRED, SUCCESSOR), "--settle-why", "queue paid"]) == 0
    printed = capsys.readouterr().out
    assert "settled         %s -> %s" % (RETIRED, SUCCESSOR) in printed
    assert "this is a decision, not a measurement" in printed
    # Round 111's missing case: a repair that actually freed a file from the plan.
    assert "repair plan      1 file(s) off the table" in printed
    stored = repair_history(tmp_path)
    assert stored["last"]["action"] == "settle-link"
    # The mismatch fixture's record has no moves, so the plan keys its promise under the unnamed bucket.
    assert stored["last"]["freed"] == ["(unnamed)"]


def test_a_malformed_settlement_is_refused(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "round-old.json"]) == 2
    assert "takes RECORD=SUCCESSOR" in capsys.readouterr().out


def test_a_settlement_for_a_round_that_does_not_exist_is_refused(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "round-nobody.json=%s" % SUCCESSOR]) == 2
    assert "no recorded round named record" in capsys.readouterr().out


def test_settlements_survive_a_corrupt_store_read(tmp_path):
    path = tmp_path / "var" / "evolve"
    path.mkdir(parents=True)
    (path / "settlements.json").write_text("{not json", encoding="utf-8")
    assert load_settlements(tmp_path) == []


def test_blocked_pairs_names_the_successor_not_the_other_claimant(tmp_path):
    _mismatch(tmp_path)
    pairs = blocked_pairs(tmp_path, 20)
    assert pairs == [{"record": RETIRED, "successor": SUCCESSOR}]
    assert blocked_pairs(tmp_path, 21) == []


def test_the_history_page_reads_a_settlement_not_a_disagreement(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "every replacement link that no decision settled agrees" in flat
    assert "1 pair(s) were settled by a recorded decision, not by agreement" in flat
    assert "%s -> %s" % (RETIRED, SUCCESSOR) in flat
    assert "[settled with %s by a recorded decision]" % SUCCESSOR in flat
    assert "disagree with their other end" not in flat


def test_the_history_page_still_charges_an_unsettled_pair(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "disagree with their other end" in flat
    assert "settled by a recorded decision" not in flat
    assert "settled with" not in flat


def test_the_campaign_carries_the_decision_in_its_page_and_its_json(tmp_path):
    from brian.campaign import campaign, format_campaign

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    result = campaign(tmp_path)
    flat = " ".join(format_campaign(result).split())
    assert "1 pair(s) were settled by a recorded decision, not by agreement" in flat
    assert "%s -> %s" % (RETIRED, SUCCESSOR) in flat
    payload = result.to_dict()
    assert payload["settlements"] == [{"record": RETIRED, "successor": SUCCESSOR,
                                       "at": NOW.isoformat(), "why": "the queue paid round-other",
                                       "by": ""}]


def test_a_settlement_needs_a_reason(tmp_path):
    _mismatch(tmp_path)
    with pytest.raises(ValueError) as error:
        settle_link(tmp_path, RETIRED, SUCCESSOR, now=NOW)
    assert "needs a reason" in str(error.value)
    assert "nobody dares review" in str(error.value)
    assert load_settlements(tmp_path) == []


def test_a_recorded_pair_stays_idempotent_without_a_reason(tmp_path):
    _settled(tmp_path)
    again = settle_link(tmp_path, RETIRED, SUCCESSOR, now=NOW)
    assert again["changed"] is False
    assert again["why"] == "the queue paid this one"


def test_the_decider_is_stored_when_it_is_named(tmp_path):
    _mismatch(tmp_path)
    entry = settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", by="alice", now=NOW)
    assert entry["by"] == "alice"
    assert load_settlements(tmp_path)[-1]["by"] == "alice"


def test_the_audit_says_when_no_decider_was_recorded(tmp_path):
    from brian.evolve import format_audit

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", now=NOW)
    flat = " ".join(format_audit(audit(tmp_path)).split())
    assert "and no decider was recorded" in flat
    assert "1 of them were recorded without --settle-by" in flat
    assert "nobody can be asked to defend" in flat
    assert ", by alice" not in flat


def test_the_audit_names_the_decider_when_there_is_one(tmp_path):
    from brian.evolve import format_audit

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="the queue paid round-other", by="alice", now=NOW)
    flat = " ".join(format_audit(audit(tmp_path)).split())
    assert ", by alice" in flat
    assert "no decider was recorded" not in flat


def test_the_audit_reports_a_settlement_written_without_a_reason(tmp_path):
    from brian.evolve import format_audit, SETTLEMENTS, EVOLVE_DIR

    _mismatch(tmp_path)
    path = tmp_path / EVOLVE_DIR / SETTLEMENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "brian.settled/1", "settlements": [
        {"record": RETIRED, "successor": SUCCESSOR, "at": NOW.isoformat(), "why": ""}]}, indent=2) + "\n",
                    encoding="utf-8")
    flat = " ".join(format_audit(audit(tmp_path)).split())
    # Round 114 gave an old decision a way to gain a reason, so the sentence now says what is true of the
    # decision itself: the reason did not come with it (boundary 190 -- the sentence and its assertion move
    # together when the mechanism under them does).
    assert "1 of them carry no reason from the moment they were taken" in flat
    assert "written before --settle-why was required" in flat
    assert "a later note does not make the original decision explained" in flat


def test_the_cli_refuses_a_settlement_without_a_reason(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (RETIRED, SUCCESSOR)]) == 2
    printed = capsys.readouterr().out
    assert "cannot settle that link: a settlement needs a reason" in printed
    assert load_settlements(tmp_path) == []


def test_the_cli_prints_the_decider_or_says_it_is_missing(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "queue paid", "--settle-by", "alice"]) == 0
    printed = capsys.readouterr().out
    assert "decided by: alice" in printed
    assert "because: queue paid" in printed


def test_the_cli_names_the_flag_when_no_decider_is_given(tmp_path, monkeypatch, capsys):
    _mismatch(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-link", "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "queue paid"]) == 0
    printed = capsys.readouterr().out
    assert "decided by: (not recorded -- pass --settle-by NAME" in printed


def test_the_campaign_counts_decisions_by_decider(tmp_path):
    from brian.campaign import campaign, format_campaign

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="queue paid", by="alice", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "decided by: alice 1" in flat
    assert "no decider recorded" not in flat


def test_the_campaign_names_the_unattributed_decisions(tmp_path):
    from brian.campaign import campaign, format_campaign

    _mismatch(tmp_path)
    settle_link(tmp_path, RETIRED, SUCCESSOR, why="queue paid", now=NOW)
    flat = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "decided by: and no decider recorded 1" in flat
