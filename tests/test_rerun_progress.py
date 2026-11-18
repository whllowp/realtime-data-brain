"""The re-run queue says how far it has been paid down (round 53)."""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.streams import load_stream_file, summarise


SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 0,
         "names": ["host-1"], "cadence_s": 300.0, "span_s": 600.0}


def _stream_file(root: Path, name: str = "stream-one.json") -> dict:
    moments = [{"start": "2026-01-01T%02d:%02d:00+00:00" % (index * 5 // 60, index * 5 % 60),
                "service": "host-1", "scored": index < 2, "anomaly": index == 2}
               for index in range(3)]
    path = root / "data" / "smd" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "brian.stream/1", "dataset": "SMD", "window_s": 300,
                                "overrides": {},
                                "streams": [{"name": "host-1", "windows": moments}]}), encoding="utf-8")
    # The geometry match is exact on `windows`/`scored`: a fixture that guesses the summary disagrees with
    # the file it just wrote and every candidate comes back empty (round 52's fixture lesson).
    return summarise(load_stream_file(path).streams)


def _round_file(root: Path, name: str, *, stream_path: str = "", applied=None,
                retired=None, shape: dict = None, source: str = "SMD") -> None:
    folder = root / "var" / "evolve"
    folder.mkdir(parents=True, exist_ok=True)
    record = {"round": name, "started_at": "2026-11-17T20:10:46+00:00",
              "finished_at": "2026-11-17T20:20:00+00:00",
              "stream": dict(shape or SHAPE), "stream_path": stream_path or None,
              "rules": {}, "baseline": {}, "trials": [], "ceiling": {}, "proposal": {}}
    if source:
        record["stream"] = dict(record["stream"], source=source)
    (folder / ("round-%s.json" % name)).write_text(json.dumps(record), encoding="utf-8")
    line = {"round": "round-%s.json" % name, "started_at": record["started_at"],
            "accepted": [], "rejected": [], "applied": applied, "rules_hash": "aaaaaaaaaaaa",
            "baseline_tokens_per_report": 3358.0}
    if retired is not None:
        line["retired"] = retired
    with (folder / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")

def test_the_queue_counts_what_is_left_and_what_it_would_reach(tmp_path):
    _stream_file(tmp_path)
    _round_file(tmp_path, "old-one", applied=1)
    _round_file(tmp_path, "old-two", applied=1)
    _round_file(tmp_path, "new-one", stream_path="data/smd/stream-one.json", applied=1)

    progress = cli._rerun_progress(tmp_path)

    assert progress["ready"] == 2
    assert progress["paid"] == 0
    assert progress["rounds"] == 3
    assert progress["without_file"] == 2
    assert progress["share_now"] == 2 / 3
    # Each re-run retires the nameless round and writes one that names its file, so the total stays and the
    # numerator falls: running the whole queue leaves nothing unattributed.
    assert progress["share_after"] == 0.0


def test_a_retirement_that_names_a_file_pays_the_round_off(tmp_path):
    _stream_file(tmp_path)
    _round_file(tmp_path, "old-one", applied=1,
                retired={"at": "2026-11-18T04:21:34+00:00", "re_measured_as": "round-new-one.json"})
    _round_file(tmp_path, "new-one", stream_path="data/smd/stream-one.json", applied=1)

    progress = cli._rerun_progress(tmp_path)

    assert progress["paid"] == 1
    assert progress["ready"] == 0
    assert progress["rounds"] == 1
    assert progress["without_file"] == 0
    assert progress["share_now"] == 0.0


def test_a_retirement_into_a_nameless_successor_pays_nothing(tmp_path):
    # Round 52's defect, in queue terms: the old round is crossed out, but the successor still cannot say
    # which file it measured, so the question was not answered and the count must not move.
    _stream_file(tmp_path)
    _round_file(tmp_path, "old-one", applied=1,
                retired={"at": "2026-11-18T04:21:34+00:00", "re_measured_as": "round-new-one.json"})
    _round_file(tmp_path, "new-one", applied=1)

    progress = cli._rerun_progress(tmp_path)

    assert progress["paid"] == 0
    assert progress["without_file"] == 1
    assert progress["ready"] == 1


def test_the_projection_never_promises_below_zero(tmp_path):
    _stream_file(tmp_path)
    _round_file(tmp_path, "old-one", applied=1)

    progress = cli._rerun_progress(tmp_path)

    assert progress["share_after"] == 0.0
    assert progress["remaining"] == progress["ready"] == 1


def test_an_empty_ledger_has_no_share(tmp_path):
    progress = cli._rerun_progress(tmp_path)

    assert progress["rounds"] == 0
    assert progress["share_now"] == 0.0
    assert progress["share_after"] == 0.0
    assert progress["ready"] == 0
