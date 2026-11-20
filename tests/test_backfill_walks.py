"""Round 147: backfill the payments the ledger recorded before the re-run store existed.

Round 146 taught the store to record a hand-typed payment, and the real ledger still said "0 by the queue, 0 typed
by hand" -- not because nobody paid, but because all 18 of its retirements happened before round 144 started keeping
the book. This round reads the ledger's retirement marks back into the store.

The tests hold the two limits: one entry per **successor** (a batch retirement names one re-measurement for several
rounds, and billing it once per round would say the loop paid twice for one measurement), and every backfilled entry
says it was written after the fact (`backfilled: true`) while keeping the payment's own moment, because the age of
the record and the date of the work are different facts.
"""

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import backfill_walks, format_rerun_walks, rerun_runs, rerun_walks

SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 2, "cadence_s": 300.0}


def _round(root: Path, name: str, *, retired=None, shape=None) -> dict:
    ledger = root / "var" / "evolve"
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": name,
        "started_at": "2026-11-18T04:00:00+00:00",
        "finished_at": "2026-11-18T04:05:00+00:00",
        "stream": dict(shape or SHAPE, source="SMD"),
        "trials": [{"candidate": {"name": "events-8", "hypothesis": "fewer events keep the signature",
                                 "changes": [["llm", "max_context_events", 8]]},
                    "measurement": {}, "verdict": {}}],
        "accepted": ["events-8"], "rejected": [], "applied": [], "rules_hash": "abc",
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.25, "fpr_at_budget": 0.12},
    }
    if retired is not None:
        payload["retired"] = dict(retired)
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line.update({"rules_hash": "abc", "baseline_tokens_per_report": 3356.0})
    if retired is not None:
        line["retired"] = dict(retired)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


def _retired(name: str, successor: str, at: str = "2026-11-18T04:39:39Z") -> dict:
    return {"at": at, "re_measured_as": successor}


# --- the store --------------------------------------------------------------

def test_the_backfill_writes_a_walk_per_payment(tmp_path):
    written = backfill_walks(tmp_path, [("round-a.json", "round-x.json", "2026-11-18T04:39:39Z"),
                                        ("round-b.json", "round-y.json", "2026-11-18T04:39:56Z")])
    assert [entry["walk"] for entry in written] == [1, 2]
    assert all(entry["hand"] and entry["backfilled"] and entry["paid"] == 1 for entry in written)
    # The payment's own moment, not today's: the record is young, the work is not.
    assert written[0]["at"].startswith("2026-11-18T04:39:39")
    stored = rerun_runs(tmp_path)["runs"]
    assert [run["rounds"] for run in stored] == [["round-x.json"], ["round-y.json"]]


def test_one_successor_is_one_payment(tmp_path):
    written = backfill_walks(tmp_path, [("round-a.json", "round-x.json", "2026-11-18T04:39:39Z"),
                                        ("round-b.json", "round-x.json", "2026-11-18T04:39:39Z")])
    # A batch retirement names one re-measurement for several rounds: it was paid once.
    assert len(written) == 1


def test_the_backfill_is_idempotent(tmp_path):
    pairs = [("round-a.json", "round-x.json", "2026-11-18T04:39:39Z")]
    backfill_walks(tmp_path, pairs)
    before = rerun_runs(tmp_path)["runs"]
    assert backfill_walks(tmp_path, pairs) == []
    assert rerun_runs(tmp_path)["runs"] == before


def test_the_summary_says_how_many_were_written_after_the_fact(tmp_path):
    backfill_walks(tmp_path, [("round-a.json", "round-x.json", "2026-11-18T04:39:39Z")])
    summary = rerun_walks(tmp_path)
    assert summary["backfilled"] == 1 and summary["hand"] == 1
    note = format_rerun_walks(summary)
    assert "1 of them were written after the fact from the ledger's retirement marks" in note


# --- the command ------------------------------------------------------------

def test_the_command_writes_the_payments_the_ledger_names(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json", retired=_retired("round-old.json", "round-new.json"))
    _round(tmp_path, "round-new.json")

    assert cli.main(["evolve", "--backfill-walks"]) == 0
    text = capsys.readouterr().out
    assert "backfilled        round-new.json -> walk 1" in text
    assert "1 payment(s) the ledger recorded and the store never saw" in text
    assert rerun_runs(tmp_path)["runs"][0]["backfilled"] is True

    # Running it again is not a second payment.
    assert cli.main(["evolve", "--backfill-walks"]) == 0
    assert "every payment the ledger names already has a walk" in capsys.readouterr().out


def test_the_preview_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json", retired=_retired("round-old.json", "round-new.json"))
    _round(tmp_path, "round-new.json")

    assert cli.main(["evolve", "--backfill-walks", "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "would backfill    round-old.json -> round-new.json" in text
    assert "nothing was written" in text
    assert rerun_runs(tmp_path)["runs"] == []


def test_a_ledger_with_no_retirements_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-old.json")

    assert cli.main(["evolve", "--backfill-walks"]) == 0
    assert "nothing to backfill: no round in the ledger names the re-run that replaced it" in capsys.readouterr().out


# --- the page ---------------------------------------------------------------

def test_the_campaign_names_the_backfilled_share(tmp_path):
    _round(tmp_path, "round-old.json", retired=_retired("round-old.json", "round-new.json"))
    _round(tmp_path, "round-new.json")
    backfill_walks(tmp_path, [("round-old.json", "round-new.json", "2026-11-18T04:39:39Z")])

    page = format_campaign(campaign(tmp_path))
    assert "1 invocation(s) of the re-run queue: 0 by the queue, 1 typed by hand" in page
    assert "written after the fact from the ledger's retirement marks" in page
