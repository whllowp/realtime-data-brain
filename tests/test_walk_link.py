"""Round 144: the re-run queue keeps a store of its walks, and each round names the one that paid it.

Round 143 gave every walk of the *origin* queue a place in `var/evolve/queue_runs.json`. Item 130 asked for the
same on the rounds -- and the premise was wrong: that store holds the walks that measure promises, and those
walks pay no rounds at all. The rounds are paid by the other queue (`--rerun-unrecorded`, `--with-ceilings`),
which kept no record of itself, so a round's `queue` block said who paid it and never which invocation, and
"walk 3" had nothing to point at.

The tests below hold the three limits: an invocation reserves its slot *before* it measures (so the round it
pays can name a number that exists), an invocation with nothing to pay reserves nothing (a store of empty
entries would make "walk 12" mean "somebody typed the command twelve times"), and a row on `--history` says
which walk paid it.
"""

import json
from pathlib import Path

from brian import cli
from brian import evolve as evolve_module
from brian.evolve import EVOLVE_DIR, EvolveRules, Measurement, RoundResult, persist
from brian.promote import begin_rerun_run, finish_rerun_run, rerun_runs
from brian.streams import load_stream_file, summarise

SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 2, "cadence_s": 300.0}


def _measurement() -> Measurement:
    return Measurement(candidate="baseline", recall=0.30, fpr=0.10, precision=0.70, reports=10,
                       tokens_per_report=3356.0, tokens_per_window=900.0, latency_p95_ms=12.0,
                       recall_at_budget=0.297, fpr_at_budget=0.092)


def _stream(root: Path, name: str = "stream-one.json", windows: int = 3) -> dict:
    target = root / "data" / "smd" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": "smd",
        "streams": [{
            "name": "host-1",
            "windows": [{"start": "2026-11-1%dT00:00:00+00:00" % index, "service": "host",
                         "scored": True, "anomaly": index == 0} for index in range(windows)],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _round(root: Path, name: str, *, shape=None, applied=("v020",), candidates=True,
           queue=None) -> dict:
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    trials = []
    if candidates:
        trials = [{"candidate": {"name": "events-8", "hypothesis": "fewer events keep the signature",
                                 "changes": [["llm", "max_context_events", 8]]},
                   "measurement": {}, "verdict": {}}]
    payload = {"round": name, "started_at": "2026-11-20T06:00:00+00:00",
               "finished_at": "2026-11-20T06:05:00+00:00",
               "stream": dict(shape or SHAPE, source="SMD"), "trials": trials, "accepted": ["events-8"],
               "rejected": [], "applied": list(applied), "rules_hash": "abc",
               "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.25, "fpr_at_budget": 0.12}}
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line.update({"rules_hash": "abc", "baseline_tokens_per_report": 3356.0})
    if queue is not None:
        # The ledger line wins over the round file, so a `queue` block only reaches the page if both carry it.
        payload["queue"] = dict(queue)
        (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        line["queue"] = dict(queue)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


# --- the store --------------------------------------------------------------

def test_the_entry_carries_the_walk_it_was_paid_by():
    plain = cli._queue_entry("file", "SMD", "round-a.json", {"domain": "SMD"}, {"share_after": 0.2},
                             {"families": 1})
    assert plain["walk"] == 0
    numbered = cli._queue_entry("file", "SMD", "round-a.json", {"domain": "SMD"}, {"share_after": 0.2},
                                {"families": 1}, walk=3)
    assert numbered["walk"] == 3


def test_an_invocation_reserves_its_slot_before_it_measures(tmp_path):
    first = begin_rerun_run(tmp_path, {"limit": 2, "families": ["file"]})
    assert first["walk"] == 1
    stored = rerun_runs(tmp_path)["runs"]
    assert len(stored) == 1
    # Reserved, not finished: a command that dies mid-walk leaves a record saying exactly that.
    assert stored[0]["finished"] == "" and stored[0]["paid"] == 0
    second = begin_rerun_run(tmp_path, {"limit": 1, "families": ["file", "ceiling"], "with_ceilings": True})
    assert second["walk"] == 2
    assert [run["limit"] for run in rerun_runs(tmp_path)["runs"]] == [2, 1]


def test_finishing_updates_the_entry_it_reserved(tmp_path):
    walk = begin_rerun_run(tmp_path, {"limit": 1, "families": ["file"]})["walk"]
    entry = finish_rerun_run(tmp_path, walk, {"paid": 1, "rounds": ["round-new.json"], "failures": 0})
    assert entry["paid"] == 1 and entry["finished"]
    stored = rerun_runs(tmp_path)["runs"]
    # The ordinal is the identity: finishing must not append a second record.
    assert len(stored) == 1
    assert stored[0]["rounds"] == ["round-new.json"]
    assert finish_rerun_run(tmp_path, 9, {"paid": 0}) is None


# --- the command ------------------------------------------------------------

def test_a_walk_with_nothing_to_pay_reserves_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "walk " not in text
    # No work, no walk: a store of empty entries would make the ordinals mean nothing.
    assert rerun_runs(tmp_path)["runs"] == []


def test_a_walk_that_pays_a_round_records_which_walk_and_which_round(tmp_path, monkeypatch, capsys):
    shape = _stream(tmp_path)
    _round(tmp_path, "round-a.json", shape=shape)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    monkeypatch.setattr(evolve_module, "measure", lambda *args, **kwargs: _measurement())
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "walk              1 of the re-run queue's recorded invocation(s)" in text

    runs = rerun_runs(tmp_path)["runs"]
    assert len(runs) == 1
    assert runs[0]["paid"] == 1
    paid_round = runs[0]["rounds"][0]
    assert paid_round

    record = json.loads((tmp_path / EVOLVE_DIR / paid_round).read_text(encoding="utf-8"))
    assert record["queue"]["walk"] == 1
    # The row that says who paid must say which walk, or "the file queue paid" names one of a dozen runs.
    assert cli._paid_round_names(tmp_path, 1) == [paid_round]
    assert cli._paid_round_names(tmp_path, 2) == []


def test_the_history_row_names_the_walk(tmp_path, monkeypatch, capsys):
    shape = _stream(tmp_path)
    _round(tmp_path, "round-a.json", shape=shape)
    # A round paid before this store existed: the family, and no walk to name.
    _round(tmp_path, "round-old.json", shape=shape, applied=(), candidates=False,
           queue={"paid": "SMD", "paid_round": "round-earlier.json", "family": "ceiling", "shape": {}})
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    monkeypatch.setattr(evolve_module, "measure", lambda *args, **kwargs: _measurement())
    monkeypatch.chdir(tmp_path)

    cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "1"])
    capsys.readouterr()
    cli.main(["evolve", "--history"])
    text = capsys.readouterr().out
    assert "[paid by the file queue, walk 1]" in text
    # An older record has the family and no walk; the row must keep saying the part it knows.
    assert "[paid by the ceiling queue]" in text


def test_a_record_without_a_walk_still_reads_the_way_it_did(tmp_path):
    rules = EvolveRules()
    result = RoundResult(started_at="2026-11-20T06:00:00+00:00", finished_at="2026-11-20T06:05:00+00:00",
                         stream=dict(SHAPE, source="SMD"), rules=rules, baseline=_measurement())
    result.queue = {"paid": "SMD", "paid_round": "round-a.json", "family": "file", "shape": {"families": 1}}
    written = persist(tmp_path, result, write_versions=False)
    record = json.loads((tmp_path / EVOLVE_DIR / Path(str(written["round"])).name).read_text(encoding="utf-8"))
    assert "walk" not in record["queue"]
