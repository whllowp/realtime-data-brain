"""Round 146: a payment typed by hand leaves a walk too, so "walk 0" stops meaning two things.

Round 145 read the re-run store for the first time and found the real ledger had 74 rounds and **zero** walks: every
paid version on this machine came from a hand-typed command. That made `queue.walk = 0` carry two meanings at once
-- "not paid by the queue" and "not recorded at all" -- and it made the page's count of walks a count of the queue's
walks, which a reader takes for a count of the loop's work.

The tests below hold the limit of the fix: a hand retirement that names a successor is recorded as a walk of its own
(`hand: true`), a retirement that names none is not a payment and records nothing, and a successor the queue already
paid is not billed a second time. The round it paid was written before the command, so it cannot name the walk --
the page says that rather than adding a number to a record that is already history.
"""

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (begin_rerun_run, format_rerun_walks, rerun_runs, rerun_walks)

SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 2, "cadence_s": 300.0}


def _round(root: Path, name: str, *, queue=None, shape=None) -> dict:
    ledger = root / "var" / "evolve"
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": name,
        "started_at": "2026-11-20T06:00:00+00:00",
        "finished_at": "2026-11-20T06:05:00+00:00",
        "stream": dict(shape or SHAPE, source="SMD"),
        "trials": [{"candidate": {"name": "events-8", "hypothesis": "fewer events keep the signature",
                                 "changes": [["llm", "max_context_events", 8]]},
                    "measurement": {}, "verdict": {}}],
        "accepted": ["events-8"], "rejected": [], "applied": [], "rules_hash": "abc",
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.25, "fpr_at_budget": 0.12},
    }
    if queue is not None:
        payload["queue"] = dict(queue)
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line.update({"rules_hash": "abc", "baseline_tokens_per_report": 3356.0})
    if queue is not None:
        line["queue"] = dict(queue)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


# --- the store --------------------------------------------------------------

def test_a_hand_payment_is_marked_as_one(tmp_path):
    walk = begin_rerun_run(tmp_path, {"hand": True, "limit": 0, "families": []})["walk"]
    stored = rerun_runs(tmp_path)["runs"]
    assert stored[0]["hand"] is True and stored[0]["walk"] == walk
    # A queue walk is the default: the store must not read every payment as automation, nor the reverse.
    begin_rerun_run(tmp_path, {"limit": 1, "families": ["file"]})
    assert [run["hand"] for run in rerun_runs(tmp_path)["runs"]] == [True, False]


def test_the_summary_splits_queue_from_hand(tmp_path):
    begin_rerun_run(tmp_path, {"hand": True, "limit": 0, "families": []})
    begin_rerun_run(tmp_path, {"limit": 1, "families": ["file"]})
    begin_rerun_run(tmp_path, {"hand": True, "limit": 0, "families": []})
    summary = rerun_walks(tmp_path)
    assert (summary["walks"], summary["queued"], summary["hand"]) == (3, 1, 2)
    note = format_rerun_walks(summary)
    assert "3 invocation(s) of the re-run queue: 1 by the queue, 2 typed by hand" in note


# --- the command ------------------------------------------------------------

def test_a_hand_retirement_records_a_walk(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json")
    _round(tmp_path, "round-new.json")

    assert cli.main(["evolve", "--retire", "round-old.json", "--re-measured-as", "round-new.json"]) == 0
    text = capsys.readouterr().out
    assert "typed by hand: the round it paid was written before this command" in text
    stored = rerun_runs(tmp_path)["runs"]
    assert len(stored) == 1
    assert stored[0]["hand"] is True and stored[0]["paid"] == 1
    assert stored[0]["rounds"] == ["round-new.json"]


def test_a_retirement_without_a_successor_is_not_a_payment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json")

    assert cli.main(["evolve", "--retire", "round-old.json"]) == 0
    assert rerun_runs(tmp_path)["runs"] == []
    assert "typed by hand" not in capsys.readouterr().out


def test_a_successor_the_queue_paid_is_not_billed_twice(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json")
    _round(tmp_path, "round-new.json", queue={"paid": "SMD", "paid_round": "round-old.json",
                                              "family": "file", "walk": 4})

    assert cli.main(["evolve", "--retire", "round-old.json", "--re-measured-as", "round-new.json"]) == 0
    text = capsys.readouterr().out
    assert "already recorded: round-new.json was paid by the queue's walk 4" in text
    # The queue's own walk is the one that paid; a second entry would make the count say two payments.
    assert rerun_runs(tmp_path)["runs"] == []


# --- the page ---------------------------------------------------------------

def test_the_campaign_reads_how_much_of_this_was_the_queue(tmp_path):
    _round(tmp_path, "round-old.json")
    _round(tmp_path, "round-new.json")
    begin_rerun_run(tmp_path, {"hand": True, "limit": 0, "families": []})
    begin_rerun_run(tmp_path, {"limit": 1, "families": ["file"]})

    page = format_campaign(campaign(tmp_path))
    assert "re-run walks      2 invocation(s) of the re-run queue: 1 by the queue, 1 typed by hand" in page
