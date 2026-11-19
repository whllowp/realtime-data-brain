"""Round 84: a promise is refused where the record behind it is disputed or was copied.

Rounds 81-83 made a replacement link comparable and repairable, but nothing asked the question a promotion
asks: *is the round that produced this version still a record anybody can believe?* A version names its
producer through `applied`, so a version produced by a round that is a participant in a broken link inherits
that brokenness. Stacking a promise on it entrenches numbers the ledger itself disputes, and `--audit` cannot
settle them either (boundaries 170/173).

Two kinds, two exits: a contradiction is a build defect (`--verify` exits 2, because no re-measurement decides
which book is right), while a copied end is a debt (`--verify` exits 1 until the round is re-run and writes a
successor that names its predecessor itself) -- a gate that can never be cleared is a gate nobody runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import repair_legacy
from brian.promote import LINK_BACKFILLED, LINK_BROKEN, provenance_block

RETIRED = "round-old.json"
SUCCESSOR = "round-new.json"
OTHER = "round-other.json"

CANDIDATE = {
    "name": "host-rolling-rank-0-85-200",
    "hypothesis": "h",
    "changes": [["digest", "rolling_window", 200]],
    "veto": None,
}


def _round(root: Path, name: str, *, source="SMD", applied=("v020",), queue=None, retired=None,
           stream_path="data/smd/stream-one.json", ledger_applied=None) -> None:
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
        "applied": list(applied),
        "stream_path": stream_path,
    }
    if queue is not None:
        record["queue"] = queue
    if retired is not None:
        record["retired"] = retired
    (directory / name).write_text(json.dumps(record), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": record["started_at"], "accepted": 0, "rejected": 0,
            "applied": list(applied) if ledger_applied is None else ledger_applied,
            "rules_hash": "abc123abc123", "stream_path": stream_path,
            "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _version(root: Path, number: int = 20, *, source="SMD") -> None:
    folder = root / "var" / "evolve" / "versions"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": number,
        "created_at": "2026-11-18T01:15:11+00:00",
        "candidate": CANDIDATE,
        "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "measurement": {"recall_at_budget": 0.507, "fpr_at_budget": 0.09},
        "stream": {"source": source, "streams": 1, "windows": 3, "scored": 3, "anomalous": 1},
        "stream_path": "data/smd/stream-one.json",
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
    }
    (folder / ("v%03d.json" % number)).write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _stream(root: Path, relative: str = "data/smd/stream-one.json") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": "SMD",
        "streams": [{
            "name": "host-1",
            "windows": [
                {"start": "2026-11-10T00:00:00+00:00", "service": "host", "scored": True, "anomaly": True},
                {"start": "2026-11-10T00:05:00+00:00", "service": "host", "scored": True, "anomaly": False},
                {"start": "2026-11-10T00:10:00+00:00", "service": "host", "scored": True, "anomaly": False},
            ],
        }],
    }), encoding="utf-8", newline="\n")


def _legacy(root: Path) -> None:
    _round(root, RETIRED, retired={"at": "2026-11-18T07:00:00+00:00", "re_measured_as": SUCCESSOR})
    _round(root, SUCCESSOR)


def _mismatch(root: Path) -> None:
    _round(root, RETIRED, retired={"re_measured_as": SUCCESSOR})
    _round(root, OTHER, retired={"re_measured_as": SUCCESSOR})
    _round(root, SUCCESSOR, queue={"paid": "SMD", "paid_round": OTHER, "family": "file"})


def test_a_version_with_no_link_around_it_is_not_blocked(tmp_path):
    _round(tmp_path, "round-plain.json")
    _version(tmp_path)
    assert provenance_block(tmp_path, 20) is None


def test_a_version_produced_by_a_broken_link_is_blocked_as_a_defect(tmp_path):
    _mismatch(tmp_path)
    _stream(tmp_path)
    kind, detail = provenance_block(tmp_path, 20)
    assert kind == LINK_BROKEN
    assert "mismatch" in detail
    # Every participant is named, so the operator knows which rounds to look at -- not just the one that
    # happened to be drawn first.
    assert OTHER in detail


def test_a_version_from_a_copied_end_is_blocked_but_clearable(tmp_path):
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    kind, detail = provenance_block(tmp_path, 20)
    assert kind == LINK_BACKFILLED
    assert SUCCESSOR in detail
    assert "copied" in detail


def test_a_ledger_line_storing_applied_as_a_count_blocks_nothing(tmp_path):
    # Round 71 met this shape in `--history`: the ledger stores `applied` as a count, and a count cannot name
    # the version a round produced, so it cannot put a version's provenance in question either.
    _round(tmp_path, "round-plain.json", ledger_applied=1)
    assert provenance_block(tmp_path, 20) is None


def test_the_campaign_counts_backfilled_ends_apart_and_says_so(tmp_path):
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    result = campaign(tmp_path)
    text = format_campaign(result)
    assert result.backfilled == 1
    assert result.to_dict()["backfilled_links"] == 1
    assert "were backfilled by a repair: their ends agree by copying, not as two witnesses" in text


def test_a_repaired_end_is_not_counted_as_a_payment(tmp_path):
    # The repair writes a `queue` block so the successor names its predecessor, with `paid`/`family` empty on
    # purpose. Round 79's counter reads an empty family as "a queue paid this without naming the family",
    # which would let the repair invent a payment nobody made.
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    result = campaign(tmp_path)
    text = format_campaign(result)
    assert result.paid_unstated == 0
    assert result.paid_families == {}
    assert "say a queue paid them" not in text
    assert "were backfilled by a repair" in text


def test_the_legacy_sentence_stops_claiming_that_every_link_agrees(tmp_path):
    # Two legacy pairs: one repaired (so its end is a copy), one still unrepaired. The unrepaired one keeps
    # the "no successor-side record" sentence alive, and that sentence must not end by claiming agreement --
    # the repaired pair agrees by construction, which is the opposite of a second witness.
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _round(tmp_path, "round-old2.json", retired={"re_measured_as": "round-new2.json"})
    _round(tmp_path, "round-new2.json")
    result = campaign(tmp_path)
    assert result.link_legacy == 1 and result.backfilled == 1
    text = format_campaign(result)
    assert "have no successor-side record (written before round 78)" in text
    assert "every link that has both ends agrees" not in text
    assert "were backfilled by a repair" in text


def test_verify_exits_two_on_a_version_the_books_dispute(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _mismatch(tmp_path)
    _version(tmp_path)
    _stream(tmp_path)
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json"])
    out = capsys.readouterr().out
    assert code == 2
    assert "not checked: a promise stacked on this record would entrench numbers the ledger itself disputes" in out
    assert "no re-measurement settles which of the two books is right" in out
    assert not (tmp_path / "var" / "evolve" / "verifications").exists()


def test_verify_exits_one_on_a_copied_end_and_writes_no_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _version(tmp_path)
    _stream(tmp_path)
    code = cli.main(["evolve", "--verify", "--verify-version", "20",
                     "--stream-file", "data/smd/stream-one.json"])
    out = capsys.readouterr().out
    assert code == 1
    assert "A copied end is a debt, not a defect" in out
    assert not (tmp_path / "var" / "evolve" / "verifications").exists()
