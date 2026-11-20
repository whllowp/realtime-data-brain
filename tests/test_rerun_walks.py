"""Round 145: count the re-run queue's walks, and keep an ordinal honest after the store was trimmed.

Round 144 gave each invocation of the re-run queue a record and made every round it paid name one. Nothing read
the store back, so two states looked identical on every page: a queue walked once and a queue walked twenty times,
a walk that paid five rounds and a walk every round refused. Item 131 asked for the count.

Asking for it exposed the deeper defect: `begin_rerun_run` returned the *position* in the list and
`finish_rerun_run` indexed by `walk - 1`, while the list drops its oldest entries at `RERUN_RUN_LIMIT`. A round
that recorded "walk 5" therefore named a different record after three trims -- or none. The tests below hold the
separation: the ordinal is absolute and lives inside the entry, and a round whose walk the store no longer holds
is printed as such rather than left dangling.
"""

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (_write_rerun_runs, begin_rerun_run, finish_rerun_run, format_rerun_walks,
                           rerun_runs, rerun_walks, walk_held)

SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 2, "cadence_s": 300.0}


def _entry(walk: int, *, paid: int = 0, at: str = "2026-11-20T07:00:00+00:00",
           rounds=None) -> dict:
    return {"walk": walk, "at": at, "started": at, "finished": at, "limit": 1, "ceiling_limit": None,
            "families": ["file"], "with_ceilings": False, "paid": paid, "paid_ceilings": 0,
            "failures": 0, "stopped": False, "rounds": list(rounds or [])}


def _store(root: Path, entries, trimmed: int = 0) -> None:
    _write_rerun_runs(root, entries, trimmed)


def _round(root: Path, name: str = "round-a.json", *, queue=None) -> dict:
    ledger = root / "var" / "evolve"
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": name,
        "started_at": "2026-11-20T06:00:00+00:00",
        "finished_at": "2026-11-20T06:05:00+00:00",
        "stream": dict(SHAPE, source="SMD"),
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
        # The ledger line wins over the round file, so a `queue` block reaches the page only from here.
        line["queue"] = dict(queue)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


# --- the ordinal, after a trim ----------------------------------------------

def test_the_ordinal_is_absolute_and_written_into_the_entry(tmp_path):
    _store(tmp_path, [_entry(21, paid=1)], trimmed=20)
    reserved = begin_rerun_run(tmp_path, {"limit": 1, "families": ["file"]})
    # Position 2 in a store of 2, but walk 22 of a queue that has been walked 22 times.
    assert reserved == {"walk": 22, "of": 22}
    assert rerun_runs(tmp_path)["runs"][-1]["walk"] == 22


def test_finishing_finds_the_entry_by_its_ordinal_not_by_its_position(tmp_path):
    _store(tmp_path, [_entry(21, paid=1)], trimmed=20)
    entry = finish_rerun_run(tmp_path, 21, {"paid": 2, "rounds": ["round-x.json"]})
    assert entry["walk"] == 21 and entry["rounds"] == ["round-x.json"]
    stored = rerun_runs(tmp_path)["runs"]
    assert len(stored) == 1 and stored[0]["paid"] == 2
    # A walk the store no longer holds is not guessed at.
    assert finish_rerun_run(tmp_path, 7, {"paid": 0}) is None


def test_a_walk_held_is_the_store_s_own_record(tmp_path):
    _store(tmp_path, [_entry(21, paid=1)], trimmed=20)
    assert walk_held(tmp_path, 21)["paid"] == 1
    assert walk_held(tmp_path, 5) is None


# --- the count --------------------------------------------------------------

def test_the_summary_counts_the_walks_that_paid_nothing(tmp_path):
    _store(tmp_path, [_entry(1, paid=2), _entry(2, paid=0), _entry(3, paid=1)], trimmed=0)
    summary = rerun_walks(tmp_path)
    assert summary["walks"] == 3
    # A walk every round refused is not the same as a walk that retired five, and the page can now say which.
    assert summary["empty"] == 1
    assert summary["paid"] == 3
    assert summary["last_walk"] == 3


def test_the_summary_says_when_the_store_was_trimmed(tmp_path):
    _store(tmp_path, [_entry(21, paid=1)], trimmed=7)
    summary = rerun_walks(tmp_path)
    assert summary["trimmed"] == 7
    note = format_rerun_walks(summary)
    assert "1 invocation(s) of the re-run queue" in note
    assert "7 earlier walk(s) were trimmed out of the store" in note


def test_an_empty_store_has_nothing_to_say(tmp_path):
    assert rerun_walks(tmp_path)["walks"] == 0
    assert format_rerun_walks(rerun_walks(tmp_path)) is None


# --- the pages --------------------------------------------------------------

def test_the_campaign_counts_the_re_run_walks(tmp_path):
    _round(tmp_path)
    _store(tmp_path, [_entry(1, paid=2), _entry(2, paid=0)], trimmed=0)
    result = campaign(tmp_path)
    page = format_campaign(result)
    assert "re-run walks      2 invocation(s) of the re-run queue" in page
    assert "1 paid no round" in page
    assert result.to_dict()["reruns"]["walks"] == 2


def test_the_campaign_says_nothing_when_the_queue_was_never_walked(tmp_path):
    _round(tmp_path)
    page = format_campaign(campaign(tmp_path))
    assert "re-run walks" not in page


def test_a_round_naming_a_walk_the_store_dropped_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, queue={"paid": "SMD", "paid_round": "round-old.json", "family": "file", "walk": 1})
    _store(tmp_path, [_entry(3, paid=1)], trimmed=2)

    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    # The number stays on the row, because it is what the record says; the row admits it cannot be opened.
    assert "[paid by the file queue, walk 1, no longer in the store]" in text


def test_a_round_naming_a_walk_the_store_holds_prints_it_plainly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, queue={"paid": "SMD", "paid_round": "round-old.json", "family": "file", "walk": 1})
    _store(tmp_path, [_entry(1, paid=1)], trimmed=0)

    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "[paid by the file queue, walk 1]" in text
    assert "no longer in the store" not in text
