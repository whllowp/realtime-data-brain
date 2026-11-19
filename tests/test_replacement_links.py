"""Both ends of a replacement link, checked against each other (rounds 81-82).

A retirement writes `retired.re_measured_as`; the re-run queue writes `queue.paid_round`. They are two
claims by two commands and neither checked the other, so the ledger could hold "A was re-measured as B" next
to "B replaced C" and never notice. `--audit --strict` charges a disagreement as a build defect (exit 2,
like round 33's `inconsistent`), and `--history` marks every row a finding involves.

Round 82: several rounds can name the same successor (a queue pays one re-run per round, so two rounds
re-measured into one record is normal). One finding now names all of them instead of whichever the loop
reached first.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.evolve import replacement_links


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


def _successor(name="round-new.json", predecessor="round-old.json", family="file"):
    return {"paid": "SMD", "paid_round": predecessor, "family": family}


def test_a_link_both_ends_agree_on_is_not_a_finding():
    records = [
        {"round": "round-old.json", "retired": {"at": "2026-11-18T07:00:00+00:00",
                                                "re_measured_as": "round-new.json"}},
        {"round": "round-new.json", "queue": _successor()},
    ]
    assert replacement_links(records) == []


def test_a_successor_replaced_by_two_nominations_offends_once_per_nominator():
    """Round 82: two rounds name `round-new`; it replaced one of them, so the other is a mismatch."""
    records = [
        {"round": "round-new.json", "queue": _successor(predecessor="round-other.json")},
        {"round": "round-old.json", "retired": {"re_measured_as": "round-new.json"}},
        {"round": "round-other.json", "retired": {"re_measured_as": "round-new.json"}},
    ]
    findings = replacement_links(records)
    assert [item["kind"] for item in findings] == ["mismatch"]
    finding = findings[0]
    assert finding["round"] == "round-old.json"
    # Every participant is named, so a reader on any of the three rows learns about it.
    assert finding["names"] == ["round-new.json", "round-old.json", "round-other.json"]
    assert "round-old.json names round-new.json" in finding["detail"]


def test_a_single_naming_round_with_a_different_successor_is_a_mismatch():
    records = [
        {"round": "round-old.json", "retired": {"re_measured_as": "round-new.json"}},
        {"round": "round-new.json", "queue": _successor(predecessor="round-other.json")},
        {"round": "round-other.json", "retired": {"re_measured_as": "round-new.json"}},
    ]
    findings = replacement_links(records)
    # round-other agrees with the successor; round-old is the sentence that cannot be true.
    assert [(item["round"], item["kind"], item["names"]) for item in findings] == [
        ("round-old.json", "mismatch", ["round-new.json", "round-old.json", "round-other.json"]),
    ]


def test_a_successor_naming_a_round_that_was_never_retired_is_a_defect():
    records = [
        {"round": "round-old.json"},
        {"round": "round-new.json", "queue": _successor()},
    ]
    findings = replacement_links(records)
    assert [(item["round"], item["kind"], item["names"]) for item in findings] == [
        ("round-new.json", "unretired", ["round-new.json", "round-old.json"]),
    ]


def test_a_link_pointing_at_a_missing_round_is_dangling_from_either_side():
    retired_side = [{"round": "round-old.json", "retired": {"re_measured_as": "round-gone.json"}}]
    successor_side = [{"round": "round-new.json", "queue": _successor(predecessor="round-gone.json")}]
    assert [(item["kind"], item["names"]) for item in replacement_links(retired_side)] == [
        ("dangling", ["round-gone.json", "round-old.json"]),
    ]
    assert [(item["kind"], item["names"]) for item in replacement_links(successor_side)] == [
        ("dangling", ["round-gone.json", "round-new.json"]),
    ]


def test_a_successor_written_before_the_field_is_legacy_not_a_defect():
    """A record from before round 78 has no `queue` block; that end was unbackfillable, not wrong."""
    records = [
        {"round": "round-old.json", "retired": {"re_measured_as": "round-new.json"}},
        {"round": "round-new.json"},
    ]
    findings = replacement_links(records)
    assert [(item["round"], item["kind"]) for item in findings] == [("round-old.json", "legacy")]


def test_the_history_page_marks_every_row_a_finding_names(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json", queue=_successor(predecessor="round-other.json"))
    _round(tmp_path, "round-other.json", retired={"re_measured_as": "round-new.json"})
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--history"])
    out = capsys.readouterr().out
    assert "links             1 replacement link(s) disagree with their other end" in out
    # All three participants carry the mark, so no row is left looking innocent.
    assert out.count("[broken link: mismatch with") == 3


def test_the_history_page_says_when_every_link_agrees(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json", queue=_successor())
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--history"])
    assert "every replacement link agrees with the other end it names" in capsys.readouterr().out


def test_the_history_json_carries_the_links(tmp_path, monkeypatch):
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json", queue=_successor(predecessor="round-other.json"))
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "history.json"
    cli.main(["evolve", "--history", "--json-out", str(target)])
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["links"][0]["kind"] == "mismatch"
    assert "round-other.json" in payload["links"][0]["names"]


def _mismatched(root: Path) -> None:
    _round(root, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(root, "round-new.json", queue=_successor(predecessor="round-other.json"))
    _round(root, "round-other.json", retired={"re_measured_as": "round-new.json"})


def test_the_audit_names_a_broken_link(tmp_path, monkeypatch, capsys):
    _mismatched(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--audit"])
    out = capsys.readouterr().out
    assert "1 replacement link(s) disagree" in out
    assert "`--strict` charges these as a build defect" in out


def test_strict_exits_two_on_a_broken_link(tmp_path, monkeypatch, capsys):
    """Round 33's split applies: the books contradicting each other is a build defect, not a moved verdict."""
    _mismatched(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--audit", "--strict"])
    assert code == 2
    out = capsys.readouterr().out
    assert "replacement link(s) disagree" in out
    assert "no re-deciding settles it, so --strict exits 2" in out


def test_a_legacy_link_does_not_fail_the_gate(tmp_path, monkeypatch):
    """A successor written before round 78 could not name its predecessor: history, not a defect."""
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_the_campaign_page_prints_the_link_debt_it_charges(tmp_path, monkeypatch, capsys):
    """Round 82: `--campaign --strict` exited 2 on this in round 81 while the page said nothing."""
    _mismatched(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--campaign"])
    out = capsys.readouterr().out
    assert "links             1 replacement link(s) disagree" in out
    assert "--strict charges them as a build defect" in out


def test_the_campaign_page_says_a_legacy_link_is_reported_not_charged(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", retired={"re_measured_as": "round-new.json"})
    _round(tmp_path, "round-new.json")
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--campaign"])
    out = capsys.readouterr().out
    assert "have no successor-side record (written before round 78)" in out
    assert "every link that has both ends agrees" in out


def test_the_campaign_page_counts_a_legacy_link_beside_a_broken_one(tmp_path, monkeypatch, capsys):
    """Both states can exist at once: one disagreement, one end that could never be written."""
    _mismatched(tmp_path)
    _round(tmp_path, "round-pre78.json", retired={"re_measured_as": "round-legacy.json"})
    _round(tmp_path, "round-legacy.json")
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--campaign"])
    out = capsys.readouterr().out
    assert "1 replacement link(s) disagree" in out
    assert "1 more have no successor-side record (written before round 78) and are reported, not charged" in out
