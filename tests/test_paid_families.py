"""The queue's bookkeeping is on the page, not only in the records (round 79).

Round 78 made each round the queue produces say which family paid for it and what shape of invocation did
(`queue.family`, `queue.shape`). Nothing read those fields: the campaign page counted rounds, trials, verdicts
and refusals, and the history page listed files and ceilings, so the one question a drain's report should
answer -- "was the ceiling family ever actually paid?" -- required opening the records one at a time.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign


def _round(root: Path, name: str, *, source="SMD", queue=None, stream_path="data/smd/stream-one.json"):
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "round": name,
        "started_at": "2026-11-18T06:00:00+00:00",
        "finished_at": "2026-11-18T06:05:00+00:00",
        "stream": {"source": source, "windows": 3, "scored": 3},
        "trials": [],
        "baseline": None,
        "accepted": [],
        "applied": ["v020"],
    }
    if stream_path is not None:
        record["stream_path"] = stream_path
    if queue is not None:
        record["queue"] = queue
    (directory / name).write_text(json.dumps(record), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": record["started_at"], "accepted": 0, "rejected": 0,
            "applied": ["v020"], "rules_hash": "abc123abc123", "stream_path": stream_path,
            "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_campaign_counts_which_family_the_queue_paid(tmp_path):
    _round(tmp_path, "round-a.json",
           queue={"paid": "SMD", "paid_round": "round-old.json", "family": "file",
                  "shape": {"families": 2, "file_rounds": 1, "ceiling_rounds": 1, "ceilings_skipped": 0}})
    _round(tmp_path, "round-b.json",
           queue={"paid": "SMD", "paid_round": "round-old2.json", "family": "ceiling",
                  "shape": {"families": 2, "file_rounds": 1, "ceiling_rounds": 1, "ceilings_skipped": 0}})
    result = campaign(tmp_path)
    assert result.paid_families == {"file": 1, "ceiling": 1}
    assert result.paid_unstated == 0
    text = format_campaign(result)
    assert "queue paid        2 round(s) were produced by the re-run queue: 1 ceiling, 1 file" in text


def test_a_record_from_before_the_family_was_written_is_not_counted_as_a_file(tmp_path):
    """Round 58 wrote a `queue` block with no family; silence there is a third state, not the default."""
    _round(tmp_path, "round-old.json",
           queue={"paid": "SMD", "paid_round": "round-x.json", "largest_at_start": {}, "share_after": 0.0})
    _round(tmp_path, "round-new.json", queue={"paid": "SMD", "paid_round": "round-y.json",
                                              "family": "ceiling"})
    result = campaign(tmp_path)
    assert result.paid_families == {"ceiling": 1}
    assert result.paid_unstated == 1
    text = format_campaign(result)
    assert "1 more say a queue paid them without naming the family (records from before round 78)" in text


def test_a_ledger_the_queue_never_touched_prints_nothing(tmp_path):
    _round(tmp_path, "round-plain.json")
    result = campaign(tmp_path)
    assert result.paid_families == {}
    assert "queue paid" not in format_campaign(result)


def test_the_history_line_says_which_queue_paid_the_round(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-file.json", queue={"paid": "SMD", "paid_round": "r1.json", "family": "file"})
    _round(tmp_path, "round-ceiling.json", queue={"paid": "SMD", "paid_round": "r2.json",
                                                  "family": "ceiling"})
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--history"])
    out = capsys.readouterr().out
    assert "[paid by the file queue]" in out
    assert "[paid by the ceiling queue]" in out


def test_the_history_json_carries_the_queue_block(tmp_path, monkeypatch, capsys):
    block = {"paid": "SMD", "paid_round": "r1.json", "family": "ceiling",
             "shape": {"families": 1, "file_rounds": 0, "ceiling_rounds": 1, "ceilings_skipped": 0}}
    _round(tmp_path, "round-a.json", queue=block)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "history.json"
    cli.main(["evolve", "--history", "--json-out", str(target)])
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["rounds"][0]["queue"]["family"] == "ceiling"
    assert payload["rounds"][0]["queue"]["shape"]["ceiling_rounds"] == 1
