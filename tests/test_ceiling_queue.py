"""The queue can reach the rounds that named their file (round 73).

Round 71 counted two families of ceiling debt in one column: rounds whose stream file was unknown, and rounds
that named their file but never recorded the ceilings they were judged under. Only the first family was
reachable by `--rerun-unrecorded`, so round 72's gate correctly refused to charge the second: a debt nothing
could lower. This round gives the queue the second path -- `--with-ceilings` -- and with it the charge.

The collision below is the other half of the same run: `persist` named the record from `started_at`, and two
re-runs inside one second wrote one file, so the first round was retired naming evidence that was the second
round's measurement. A record is identified by its content, not by its timestamp (boundary 157).
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.evolve import EvolveRules, Measurement, RoundResult, format_ceiling_shortfall, persist
from brian.streams import load_stream_file, summarise


def _stream(root: Path, relative: str, dataset: str, windows: int, step_hours: int) -> dict:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": dataset,
        "streams": [{
            "name": "host-1",
            "windows": [
                {"start": "2026-11-1%dT00:00:00+00:00" % (index * step_hours), "service": "host",
                 "scored": True, "anomaly": index == 0}
                for index in range(windows)
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _write_round(root, name, *, source="SMD", limits=None, applied=None, shape=None, stream_path=None):
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "round": name,
        "started_at": "2026-11-18T06:00:00+00:00",
        "finished_at": "2026-11-18T06:05:00+00:00",
        "stream": dict(shape or {"source": source, "windows": 3, "scored": 2}, source=source),
        "trials": [],
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.297},
        "accepted": [],
        "applied": list(applied or []),
    }
    if stream_path is not None:
        record["stream_path"] = stream_path
    if limits is not None:
        record["limits"] = limits
    (directory / name).write_text(json.dumps(record), encoding="utf-8")
    line = {"round": name, "started_at": record["started_at"], "accepted": 0, "rejected": 0,
            "applied": list(applied or []), "rules_hash": "abc123abc123",
            "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _measurement() -> Measurement:
    return Measurement(candidate="baseline", recall=0.30, fpr=0.10, precision=0.70, reports=10,
                       tokens_per_report=3356.0, tokens_per_window=900.0, latency_p95_ms=12.0,
                       recall_at_budget=0.297, fpr_at_budget=0.092)


def test_the_named_family_is_listed_as_reachable(tmp_path):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    matters = cli._ceiling_matters(tmp_path)
    assert [entry[1] for entry in matters] == ["round-named.json"]
    assert matters[0][3] == "data/smd/stream-one.json"


def test_a_round_with_its_ceilings_or_with_nothing_at_stake_is_not_listed(tmp_path):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-recorded.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json", limits={"max_minutes": 4.0})
    _write_round(tmp_path, "round-idle.json", applied=[], shape=shape,
                 stream_path="data/smd/stream-one.json")
    assert cli._ceiling_matters(tmp_path) == []


def test_the_page_names_how_many_are_reachable():
    rows = [{"domain": "SMD", "rounds": 3, "missing": 2, "share": 2 / 3}]
    text = format_ceiling_shortfall(rows, payable=1, named=1)
    assert "2 of 3 round(s) do not record the ceilings they were judged under" in text
    assert "1 of them name their file, so --with-ceilings can re-measure them" in text


def test_nothing_payable_but_something_reachable_says_so():
    rows = [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=0, named=2)
    assert "none of them can be re-run from the record as it stands" in text
    assert "2 of them name their file" in text


def test_named_is_clamped_to_what_payable_left():
    rows = [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=1, named=5)
    assert "1 of them name their file" in text


def test_two_rounds_in_one_second_are_two_records(tmp_path):
    rules = EvolveRules()
    stream = {"source": "SMD", "windows": 3, "scored": 2}
    first = RoundResult(started_at="2026-11-18T06:15:36+00:00", finished_at="2026-11-18T06:15:41+00:00",
                        stream=dict(stream), rules=rules, baseline=_measurement())
    second = RoundResult(started_at="2026-11-18T06:15:36+00:00", finished_at="2026-11-18T06:15:36+00:00",
                         stream=dict(stream), rules=rules, baseline=_measurement())
    written_first = persist(tmp_path, first, write_versions=False)
    written_second = persist(tmp_path, second, write_versions=False)
    directory = tmp_path / "var" / "evolve"
    first_name = Path(str(written_first["round"])).name
    second_name = Path(str(written_second["round"])).name
    assert first_name != second_name
    assert sorted(path.name for path in directory.glob("round-*.json")) == sorted([first_name, second_name])
    assert json.loads((directory / first_name).read_text(encoding="utf-8"))["finished_at"] == first.finished_at
    assert json.loads((directory / second_name).read_text(encoding="utf-8"))["finished_at"] == second.finished_at


def test_without_the_flag_the_queue_only_mentions_the_named_family(tmp_path, monkeypatch, capsys):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "0"]) == 0
    text = capsys.readouterr().out
    assert "1 round(s) name their file and never recorded their ceilings; add --with-ceilings" in text
    assert not list((tmp_path / "var" / "evolve").glob("round-2026*.json"))
