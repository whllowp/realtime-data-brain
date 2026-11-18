"""The ceiling debt becomes a charge, but only where the queue can pay it (round 72).

Round 71 printed how many rounds never recorded their ceilings and whether a re-run would leave none. The
number was still a warning: no exit code read it, so a ledger whose every round was judged under an unstated
ceiling passed `--strict`. The charge follows the rule the other caps learned (rounds 50 and 62): a share the
queue can pay is a gate, a share nothing can lower is a sentence on the page -- and the paragraph prints either
way, because silence reads as "nothing owed" (boundary 151).
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.streams import load_stream_file, summarise


def _stream(root: Path, relative: str, dataset: str, windows: int, step_hours: int) -> dict:
    """A stream file whose geometry is unique among the files in this root (round 62's fixture lesson)."""
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


def test_a_payable_debt_above_the_cap_fails_the_check(capsys):
    assert cli._strict_exit(True, 0, 0, 0, ceiling_missing=3, ceiling_rounds=4,
                            ceiling_payable=3, max_ceiling_debt=0.5) == 1
    text = capsys.readouterr().out
    assert "3 of 4 round(s) do not record the ceilings they were judged under (75%), above the 50% asked for:" in text
    assert "re-running the queue would leave none, so --strict exits 1 until it is paid." in text


def test_a_debt_nobody_can_pay_is_printed_and_not_charged(capsys):
    assert cli._strict_exit(True, 0, 0, 0, ceiling_missing=4, ceiling_rounds=5,
                            ceiling_payable=2, max_ceiling_debt=0.5) == 0
    text = capsys.readouterr().out
    assert "above the 50% asked for:" in text
    assert "only 2 of them can be re-run, so --strict does not charge it" in text
    assert "nothing was charged; the debt is on the page above." in text


def test_a_debt_above_the_cap_but_without_the_flag_prints_nothing(capsys):
    assert cli._strict_exit(True, 0, 0, 0, ceiling_missing=4, ceiling_rounds=4,
                            ceiling_payable=4) == 0
    assert capsys.readouterr().out == ""


def test_a_debt_under_the_cap_prints_nothing(capsys):
    assert cli._strict_exit(True, 0, 0, 0, ceiling_missing=1, ceiling_rounds=4,
                            ceiling_payable=1, max_ceiling_debt=0.5) == 0
    assert capsys.readouterr().out == ""


def test_no_missing_rounds_never_charges(capsys):
    assert cli._strict_exit(True, 0, 0, 0, ceiling_missing=0, ceiling_rounds=9,
                            ceiling_payable=0, max_ceiling_debt=0.0) == 0
    assert capsys.readouterr().out == ""


def test_the_cap_needs_strict(monkeypatch, capsys):
    assert cli.main(["evolve", "--audit", "--max-ceiling-debt", "0.5"]) == 2
    assert "without --strict nothing reads it" in capsys.readouterr().out


def test_the_gate_charges_on_a_ledger_the_queue_can_pay(tmp_path, monkeypatch, capsys):
    # A payable round is one the queue can measure again: no recorded file, exactly one candidate file. A round
    # that named its file but recorded no ceilings is a debt the queue never touches, so it is not charged.
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-a.json", applied=["v020"], shape=shape)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-ceiling-debt", "0.5",
                     "--rerun-limit", "0"]) == 1
    text = capsys.readouterr().out
    assert "1 of 1 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:" in text
    assert "re-running the queue would leave none, so --strict exits 1 until it is paid." in text


def test_a_round_that_named_its_file_is_now_payable(tmp_path, monkeypatch, capsys):
    """Round 73: a round with a recorded file and no ceilings is reachable by `--with-ceilings`.

    Round 72 charged only rounds whose file was unknown, so this family was a debt with no command behind it
    and correctly went uncharged. Once the queue can re-measure a named round, the same ledger owes what it
    said it owed -- the charge follows the queue's reach, not the family's age.
    """
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-a.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-ceiling-debt", "0.5",
                     "--rerun-limit", "0"]) == 1
    text = capsys.readouterr().out
    assert "1 of 1 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:" in text
    assert "re-running the queue would leave none, so --strict exits 1 until it is paid." in text


def test_a_round_that_named_its_file_and_decides_nothing_is_not_charged(tmp_path, monkeypatch, capsys):
    """A re-run pays a debt only when the round still decides something; otherwise it is history, not a debt."""
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-a.json", applied=[], shape=shape, stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-ceiling-debt", "0.5",
                     "--rerun-limit", "0"]) == 0
    text = capsys.readouterr().out
    assert "only 0 of them can be re-run, so --strict does not charge it" in text


def test_the_unrecorded_page_reports_what_the_debt_would_be_after_a_drain(tmp_path, monkeypatch):
    _write_round(tmp_path, "round-a.json", applied=["v020"])
    _write_round(tmp_path, "round-b.json", limits={"max_minutes": 4.0})
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "unrecorded.json"
    assert cli.main(["evolve", "--unrecorded", "--json-out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ceilings"]["missing"] == 1
    assert payload["ceilings"]["rounds"] == 2
    assert payload["ceilings"]["after"] == max(0, payload["ceilings"]["missing"] - payload["ceilings"]["payable"])
    assert payload["ceilings"]["by_domain"][0]["domain"] == "SMD"


def test_the_history_json_carries_each_rounds_ceilings_and_the_shortfall(tmp_path, monkeypatch):
    _write_round(tmp_path, "round-a.json", limits={"max_minutes_by_domain": {"smd": 4.0}})
    _write_round(tmp_path, "round-b.json")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "history.json"
    assert cli.main(["evolve", "--history", "--json-out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    rows = {row["round"]: row for row in payload["rounds"]}
    assert rows["round-a.json"]["ceilings"]["max_minutes_by_domain"] == {"smd": 4.0}
    assert rows["round-a.json"]["ceilings_text"] == "one run 4.0 minute(s) on smd"
    assert rows["round-b.json"]["ceilings"] is None
    assert rows["round-b.json"]["ceilings_match_today"] is None
    assert payload["ceilings"]["missing"] == 1
    assert payload["ceilings"]["rounds"] == 2
