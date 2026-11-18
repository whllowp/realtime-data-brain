"""The campaign says whether the unrecorded share is falling, and refuses to claim a rate too early (round 55).

A single number ("67 % cannot be placed") answers how much is left. It cannot answer whether the loop is
paying the debt down or the ledger is growing around it, which is the question a reader deciding whether to
spend another hour on measurement actually has. Both events are in the record -- a round arrives at
`started_at`, a round leaves at `retired.at` -- so the share at any past moment is recomputed from the ledger
rather than from a second progress file that could disagree with it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian.campaign import Campaign, CampaignRow, _trend_line, format_campaign
from brian.evolve import debt_history


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _record(root: Path, name: str, *, at: datetime, stream_path: str = "", retired=None) -> None:
    folder = root / "var" / "evolve"
    folder.mkdir(parents=True, exist_ok=True)
    record = {"round": "round-%s.json" % name, "started_at": _stamp(at), "finished_at": _stamp(at),
              "stream": {"source": "SMD"}, "stream_path": stream_path or None, "rules": {},
              "baseline": {}, "trials": [], "ceiling": {}, "proposal": {}}
    (folder / ("round-%s.json" % name)).write_text(json.dumps(record), encoding="utf-8")
    line = {"round": "round-%s.json" % name, "started_at": record["started_at"],
            "accepted": [], "rejected": [], "applied": None, "rules_hash": "aaaaaaaaaaaa",
            "baseline_tokens_per_report": 3358.0}
    if retired is not None:
        line["retired"] = retired
    with (folder / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _paid_ledger(root: Path, *, days: float) -> None:
    """Two nameless rounds, a drain two days later, and one round that named its file from the start."""
    start = NOW - timedelta(days=days)
    _record(root, "old-one", at=start)
    _record(root, "old-two", at=start + timedelta(minutes=10))
    drain_at = start + timedelta(days=days / 2.0)
    _record(root, "old-one-new", at=drain_at, stream_path="data/smd/stream.json")
    _record(root, "old-two-new", at=drain_at + timedelta(minutes=1),
            stream_path="data/smd/stream.json")
    _record(root, "fresh", at=NOW - timedelta(minutes=5), stream_path="data/smd/stream.json")


def _mark_the_drain(root: Path, *, days: float) -> None:
    """Rewrite the ledger so the two old rounds were retired into their re-measurements."""
    path = root / "var" / "evolve" / "ledger.jsonl"
    start = NOW - timedelta(days=days)
    drain_at = start + timedelta(days=days / 2.0)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row["round"] == "round-old-one.json":
            row["retired"] = {"at": _stamp(drain_at), "re_measured_as": "round-old-one-new.json",
                              "overlap": {}}
        if row["round"] == "round-old-two.json":
            row["retired"] = {"at": _stamp(drain_at + timedelta(minutes=1)),
                              "re_measured_as": "round-old-two-new.json", "overlap": {}}
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_the_share_is_recomputed_from_the_ledger(tmp_path):
    _paid_ledger(tmp_path, days=4)
    _mark_the_drain(tmp_path, days=4)

    trend = debt_history(tmp_path, now=NOW)

    # Three live rounds, all naming their file: the debt is paid, and it was paid rather than never owed.
    assert trend["rounds"] == 3
    assert trend["without_file"] == 0
    assert trend["share"] == 0.0
    assert trend["paid"] == 2
    assert trend["points"] > 0


def test_the_baseline_is_the_first_round_when_the_window_is_older_than_the_ledger(tmp_path):
    _paid_ledger(tmp_path, days=2)
    _mark_the_drain(tmp_path, days=2)

    # Asking for a week of history on a two-day-old ledger: "a week ago" is an empty ledger, and comparing
    # with nothing would print "no movement" for a debt that was visibly paid.
    trend = debt_history(tmp_path, days=7, now=NOW)

    assert trend["truncated"] is True
    assert trend["asked_days"] == 7
    assert trend["days"] < 7
    assert trend["before_rounds"] >= 1
    assert trend["points"] > 0


def test_no_rate_is_claimed_before_a_day_of_history(tmp_path):
    _paid_ledger(tmp_path, days=0.4)
    _mark_the_drain(tmp_path, days=0.4)

    trend = debt_history(tmp_path, now=NOW)

    assert trend["points"] > 0
    assert trend["per_day"] is None
    assert trend["reaches_zero"] is None
    assert "no rate is claimed" in _trend_line(trend)


def test_a_rate_and_a_date_when_the_history_is_long_enough(tmp_path):
    _paid_ledger(tmp_path, days=10)
    _mark_the_drain(tmp_path, days=10)
    # One round still without a file, so the share is above zero and has somewhere to fall to.
    _record(tmp_path, "still-nameless", at=NOW - timedelta(days=1))

    trend = debt_history(tmp_path, now=NOW)

    assert trend["per_day"] is not None and trend["per_day"] > 0
    assert trend["reaches_zero"] is not None
    line = _trend_line(trend)
    assert "reaches 0%" in line


def test_the_line_says_which_way_and_by_how_much(tmp_path):
    _record(tmp_path, "one", at=NOW - timedelta(days=2))
    _record(tmp_path, "two", at=NOW - timedelta(days=2, minutes=-1))

    trend = debt_history(tmp_path, now=NOW)
    assert trend["share"] == 1.0
    assert "has not moved" in _trend_line(trend)

    _record(tmp_path, "three", at=NOW - timedelta(days=1), stream_path="data/smd/stream.json")
    moved = debt_history(tmp_path, now=NOW)
    assert moved["points"] > 0
    assert "fell" in _trend_line(moved)
    # Three records arrived after the baseline (the baseline is the first one, so it is not counted again).
    assert "3 record(s) arrived in that window" in _trend_line(moved)


def _campaign() -> Campaign:
    row = CampaignRow(domain="SMD", rounds=10, trials=40, in_force="20", verdict="holds",
                      verification={"verdict": "holds"})
    return Campaign(rows=[row], rounds=10, trials=40, unattributable=4)


def test_the_campaign_prints_the_trend_after_the_queue(tmp_path):
    _paid_ledger(tmp_path, days=4)
    _mark_the_drain(tmp_path, days=4)
    trend = debt_history(tmp_path, now=NOW)
    queue = {"ready": 0, "paid": 2, "share_now": 0.0, "share_after": 0.0}

    text = format_campaign(_campaign(), queue=queue, trend=trend)
    lines = text.splitlines()
    queue_index = next(index for index, line in enumerate(lines) if line.startswith("queue"))
    trend_index = next(index for index, line in enumerate(lines) if line.startswith("trend"))

    assert trend_index == queue_index + 1
    assert "fell" in lines[trend_index]


def test_no_trend_line_when_nothing_was_asked_for():
    text = format_campaign(_campaign())

    assert "trend" not in text
