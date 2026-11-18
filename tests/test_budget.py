# -*- coding: utf-8 -*-
"""Round 60 tests: one line that budgets the next run, and the same facts in `--json-out`.

Round 59 named the command that pays the largest debt and the debt after it, but "if I run it once, where do
I end up?" still had to be assembled from three lines: the queue's size, the plan's first step, and the
projected end. The budget is that sentence, computed by the same helpers the command runs -- a page whose
budget describes a different command than the one it recommends is a page arguing with itself.
"""
import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.streams import load_stream_file, summarise

ROW = {"domain": "RCAEval RE1-OB", "rounds": 2, "without": 2, "share": 1.0, "ready": 2}
SECOND = {"domain": "SMD", "rounds": 2, "without": 2, "share": 1.0, "ready": 2}


def _stream(root: Path, relative: str, name: str) -> dict:
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


def _round(root: Path, name: str, *, source: str) -> None:
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    shape = _stream(root, "data/smd/stream-%s.json" % name, name)
    stamp = "2026-11-18T04:00:00+00:00"
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": stamp, "finished_at": stamp,
        "stream": dict(shape, source=source), "stream_path": None,
        "trials": [], "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": stamp, "finished_at": stamp, "accepted": 0, "rejected": 0,
            "applied": ["v020"], "rules_hash": "test", "stream_path": None,
            "baseline_tokens_per_report": 3358.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _two_debts(root: Path) -> None:
    _round(root, "round-one.json", source="RCAEval RE1-OB")
    _round(root, "round-two.json", source="SMD")


def test_one_round_of_the_queue_is_budgeted_as_one_round(tmp_path):
    _two_debts(tmp_path)
    budget = cli._queue_budget(tmp_path, limit=1)
    assert budget["rounds"] == 1
    assert budget["ready"] == 2
    assert budget["rounds_total"] == 2
    assert budget["share_now"] == 1.0
    assert budget["share_after"] == 0.5
    assert len(budget["domains"]) == 1
    assert budget["command"] == "brian evolve --rerun-unrecorded --rerun-limit 1"


def test_draining_the_queue_is_budgeted_to_zero(tmp_path):
    _two_debts(tmp_path)
    budget = cli._queue_budget(tmp_path, limit=0)
    assert budget["rounds"] == 2
    assert sorted(budget["domains"]) == ["RCAEval RE1-OB", "SMD"]
    assert budget["share_after"] == 0.0


def test_a_limit_bigger_than_the_queue_does_not_invent_rounds(tmp_path):
    _two_debts(tmp_path)
    budget = cli._queue_budget(tmp_path, limit=99)
    assert budget["rounds"] == 2
    assert len(budget["domains"]) == 2


def test_nothing_payable_budgets_nothing(tmp_path):
    budget = cli._queue_budget(tmp_path, limit=1)
    assert budget["rounds"] == 0
    assert budget["domains"] == []
    assert budget["share_after"] == budget["share_now"] == 0.0


def test_the_budget_line_carries_the_command_and_the_movement(tmp_path):
    _two_debts(tmp_path)
    page = format_campaign(campaign(tmp_path), plan=[ROW, SECOND],
                           budget=cli._queue_budget(tmp_path, limit=1))
    assert ("budget            brian evolve --rerun-unrecorded --rerun-limit 1 pays 1 round(s) "
            "(RCAEval RE1-OB); the queue falls 100% -> 50% of 2 round(s)") in page


def test_an_empty_budget_says_nothing_can_be_spent(tmp_path):
    page = format_campaign(campaign(tmp_path),
                           budget={"command": "x", "rounds": 0, "domains": [], "share_now": 0.66,
                                   "share_after": 0.66, "rounds_total": 56})
    assert ("budget            nothing to spend: the queue has no round it can re-run, so the share stays "
            "66%") in page


def test_the_page_and_the_command_agree_on_the_end_state(tmp_path):
    _two_debts(tmp_path)
    # The page's projected end and the progress helper's projection are the same arithmetic; if they ever
    # disagree the page is describing a different command than the queue would run.
    budget = cli._queue_budget(tmp_path, limit=0)
    assert budget["share_after"] == cli._rerun_progress(tmp_path)["share_after"]


def test_json_output_carries_the_plan_and_the_budget(tmp_path, monkeypatch):
    _two_debts(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "campaign.json"
    assert cli.main(["evolve", "--campaign", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [row["domain"] for row in payload["debt_plan"]]
    assert payload["budget"]["rounds"] == 1
    assert payload["budget"]["command"].endswith("--rerun-limit 1")
    assert payload["rounds"] >= 2
