# -*- coding: utf-8 -*-
"""Round 59 tests: the page says what the next run pays, not only where the debt is.

Round 57 named the largest payable debtor on the campaign page. That answers "where is the debt?" and stops
short of "what happens if I pay it?": the operator runs `--rerun-unrecorded --rerun-limit 1`, reads the
`balance` line, and then has to work out the next step from a table sorted by a different column. The plan
is the queue's own order, printed next to the debt, so a one-round-at-a-time recharge reads as a two-step
instead of as arithmetic.
"""
import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.streams import load_stream_file, summarise

SMD_ROW = {"domain": "SMD", "rounds": 4, "without": 4, "share": 1.0, "ready": 4}
RCA_ROW = {"domain": "RCAEval RE1-OB", "rounds": 2, "without": 1, "share": 0.5, "ready": 1}


def _stream_file(root: Path, relative: str, name: str) -> dict:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": "SMD",
        "streams": [{
            "name": name,
            "windows": [
                {"start": "2026-11-17T00:00:00+00:00", "service": "host", "scored": True, "anomaly": False},
                {"start": "2026-11-17T00:05:00+00:00", "service": "host", "scored": True, "anomaly": True},
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _round(root: Path, name: str, *, source: str, applied) -> None:
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    stream = _stream_file(root, "data/smd/stream-%s.json" % name, name)
    stamp = "2026-11-18T04:00:00+00:00"
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": stamp, "finished_at": stamp,
        "stream": dict(stream, source=source), "stream_path": None,
        "trials": [], "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": stamp, "finished_at": stamp, "accepted": 0, "rejected": 0,
            "applied": applied, "rules_hash": "test", "stream_path": None,
            "baseline_tokens_per_report": 3358.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_plan_lists_only_payable_debts_in_queue_order(tmp_path):
    _round(tmp_path, "round-one.json", source="SMD", applied=["v020"])
    _round(tmp_path, "round-two.json", source="RCAEval RE1-OB", applied=["v019"])
    plan = cli._debt_plan(tmp_path)
    # The plan is the queue's order, not insertion order: two domains at the same share are ordered by the
    # name, so the assertion is that the plan repeats `_debt_by_domain`'s payable rows in the same sequence.
    assert [row["domain"] for row in plan] == [row[0] for row in cli._debt_by_domain(tmp_path) if row[4]]
    assert sorted(row["domain"] for row in plan) == ["RCAEval RE1-OB", "SMD"]
    assert all(row["ready"] for row in plan)


def test_a_domain_nobody_can_pay_is_not_in_the_plan(tmp_path):
    # An `(unattributed)` round keeps no candidate definitions, so no command lowers it: it is a fact on the
    # page, not a step in the plan.
    evolve = tmp_path / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    (evolve / "ledger.jsonl").write_text(json.dumps({
        "round": "round-lost.json", "started_at": "2026-11-18T04:00:00+00:00",
        "finished_at": "2026-11-18T04:00:00+00:00", "accepted": 0, "rejected": 0, "applied": ["v020"],
        "rules_hash": "test", "stream_path": None, "baseline_tokens_per_report": 1.0,
    }) + "\n", encoding="utf-8", newline="\n")
    assert cli._debt_plan(tmp_path) == []


def test_the_page_names_the_next_run_and_who_follows(tmp_path):
    page = format_campaign(campaign(tmp_path), plan=[SMD_ROW, RCA_ROW])
    assert ("measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays SMD by one round; "
            "the command prints the balance at the end") in page
    assert "then              RCAEval RE1-OB: 2 of 2 round(s) unrecorded (50%), 1 re-runnable" not in page
    assert "then              RCAEval RE1-OB: 1 of 2 round(s) unrecorded (50%), 1 re-runnable" in page


def test_with_one_debt_left_the_page_does_not_promise_a_second(tmp_path):
    page = format_campaign(campaign(tmp_path), plan=[SMD_ROW])
    assert "pays SMD by one round" in page
    assert "then " not in page


def test_nothing_payable_is_stated_rather_than_left_blank(tmp_path):
    page = format_campaign(campaign(tmp_path), plan=[])
    assert ("measure next      nothing is payable by re-running: every unrecorded round keeps no candidate "
            "definitions or no unique file") in page


def test_a_caller_that_passes_no_plan_gets_no_next_step(tmp_path):
    page = format_campaign(campaign(tmp_path))
    assert "measure next" not in page
    assert "then " not in page


def test_the_line_pays_the_plans_first_entry_not_the_largest_share(tmp_path):
    # The queue pays in its own order, so the line must name plan[0] even when plan[1] has the bigger share.
    bigger = dict(RCA_ROW, share=0.9, without=9, rounds=10)
    page = format_campaign(campaign(tmp_path), plan=[SMD_ROW, bigger])
    assert "pays SMD by one round" in page
    assert "then              RCAEval RE1-OB: 9 of 10 round(s) unrecorded (90%), 1 re-runnable" in page
