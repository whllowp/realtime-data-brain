"""Round 83: the missing end of a `legacy` link can be written, and the repair has to admit it copied.

Round 81 printed both ends of a replacement; round 82 named every participant. The `legacy` kind was the one
nobody could repair: a successor written before round 78 recorded no predecessor, so the retired round names
it and it names nothing back. Repairing it is a copy, not a discovery -- which is why the repaired block
carries `backfilled` and why every page counts it apart from the links the queue wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.evolve import (BACKFILL_KEY, backfilled_links, format_repairs, legacy_links, repair_legacy,
                          replacement_links)

RETIRED = "round-old.json"
SUCCESSOR = "round-new.json"


def _round(root: Path, name: str, *, source="SMD", queue=None, retired=None,
           stream_path="data/smd/stream-one.json"):
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
        "stream_path": stream_path,
    }
    if queue is not None:
        record["queue"] = queue
    if retired is not None:
        record["retired"] = retired
    (directory / name).write_text(json.dumps(record), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": record["started_at"], "accepted": 0, "rejected": 0,
            "applied": ["v020"], "rules_hash": "abc123abc123", "stream_path": stream_path,
            "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _legacy(root: Path) -> None:
    _round(root, RETIRED, retired={"at": "2026-11-18T07:00:00+00:00", "re_measured_as": SUCCESSOR})
    _round(root, SUCCESSOR)


def _mismatch(root: Path) -> None:
    _round(root, RETIRED, retired={"re_measured_as": SUCCESSOR})
    _round(root, "round-other.json", retired={"re_measured_as": SUCCESSOR})
    _round(root, SUCCESSOR, queue={"paid": "SMD", "paid_round": "round-other.json", "family": "file"})


def _payload(root: Path, name: str) -> dict:
    return json.loads((root / "var" / "evolve" / name).read_text(encoding="utf-8"))


def test_legacy_links_returns_the_pair_a_repair_needs():
    records = [
        {"round": RETIRED, "retired": {"re_measured_as": SUCCESSOR}},
        {"round": SUCCESSOR},
    ]
    # Round 82 made a finding's `names` a participant list; a repair needs the pair, and reading the list as
    # a filename is how round 83's first attempt skipped every link.
    assert legacy_links(records) == [{"retired": RETIRED, "successor": SUCCESSOR}]


def test_a_link_whose_successor_named_it_back_is_not_legacy():
    records = [
        {"round": RETIRED, "retired": {"re_measured_as": SUCCESSOR}},
        {"round": SUCCESSOR, "queue": {"paid": "SMD", "paid_round": RETIRED, "family": "file"}},
    ]
    assert legacy_links(records) == []


def test_the_plan_writes_nothing_and_says_what_it_would_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy(tmp_path)
    rows = repair_legacy(tmp_path, dry_run=True)
    assert rows[0]["retired"] == RETIRED
    assert rows[0]["successor"] == SUCCESSOR
    assert rows[0]["would_write"] is True
    assert rows[0]["written"] is False
    assert "queue" not in _payload(tmp_path, SUCCESSOR)
    text = format_repairs(rows, dry_run=True)
    assert "would write" in text
    assert "1 of 1 legacy link(s) would be repaired." in text


def test_the_repair_writes_the_other_end_and_marks_it_copied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy(tmp_path)
    rows = repair_legacy(tmp_path, at="2026-11-20T00:00:00Z")
    assert rows[0]["written"] is True
    queue = _payload(tmp_path, SUCCESSOR)["queue"]
    assert queue["paid_round"] == RETIRED
    # Round 80 keys `paid by domain` on `paid`; a repair does not know which domain the queue would have
    # charged, and inventing one would put a guess into that table.
    assert queue["paid"] == ""
    assert queue[BACKFILL_KEY]["from"] == RETIRED
    assert queue[BACKFILL_KEY]["at"] == "2026-11-20T00:00:00Z"
    assert queue[BACKFILL_KEY]["why"]
    found = backfilled_links([{"round": SUCCESSOR, "queue": queue}])
    assert found == [SUCCESSOR]
    assert [item for item in replacement_links([
        {"round": RETIRED, "retired": {"re_measured_as": SUCCESSOR}},
        {"round": SUCCESSOR, "queue": queue},
    ]) if item["kind"] == "legacy"] == []


def test_a_disagreement_is_never_repaired(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _mismatch(tmp_path)
    assert repair_legacy(tmp_path, dry_run=True) == []
    assert repair_legacy(tmp_path) == []
    assert "queue" not in _payload(tmp_path, RETIRED)


def test_a_second_apply_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _legacy(tmp_path)
    assert len(repair_legacy(tmp_path)) == 1
    assert repair_legacy(tmp_path) == []
    assert "no legacy link" in format_repairs(repair_legacy(tmp_path))


def test_the_cli_plans_then_writes_and_every_page_counts_the_repair(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _legacy(tmp_path)
    assert cli.main(["evolve", "--repair-links"]) == 0
    plan = capsys.readouterr().out
    assert "would write" in plan
    assert "nothing was written; add --apply" in plan
    assert "queue" not in _payload(tmp_path, SUCCESSOR)

    assert cli.main(["evolve", "--repair-links", "--apply"]) == 0
    wrote = capsys.readouterr().out
    assert "wrote" in wrote
    assert "does not treat them as independent confirmation" in wrote

    cli.main(["evolve", "--history"])
    assert "were backfilled by a repair" in capsys.readouterr().out

    cli.main(["evolve", "--audit"])
    audited = capsys.readouterr().out
    assert "backfilled by a repair, so their two ends agree by copying" in audited
    # A copied end is not a defect: the gate must not charge it, or every repair turns the build red.
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
