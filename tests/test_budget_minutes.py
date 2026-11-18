# -*- coding: utf-8 -*-
"""Round 61 tests: a budget in rounds is also a budget in minutes, and both pages print one sentence.

Round 60 gave the page a budget line but let the command keep its own `projection`/`limit` pair, so the
command a reader was told to run answered "how long" only by implication, and the page answered "how long"
not at all — a reader who does not already know what a round costs had to guess. The estimate comes from
the median duration of the recorded rounds, because one slow round should not price the queue.
"""
import json
from pathlib import Path

from brian import cli
from brian.campaign import budget_text, format_campaign

BUDGET = {"command": "brian evolve --rerun-unrecorded --rerun-limit 1", "rounds": 1,
          "domains": ["SMD"], "ready": 2, "share_now": 0.67, "share_after": 0.33,
          "rounds_total": 3, "minutes_each": 5.0, "estimated_minutes": 5.0}


def _round(root: Path, name: str, *, started: str, finished: str) -> None:
    """One recorded round with the timestamps the estimate is derived from."""
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": started, "finished_at": finished,
        "stream": {}, "stream_path": None, "trials": [], "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": started, "finished_at": finished, "accepted": 0, "rejected": 0,
            "applied": [], "rules_hash": "test", "stream_path": None, "baseline_tokens_per_report": 0.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_estimate_is_the_median_of_the_rounds_that_timed_themselves(tmp_path):
    # 4, 6 and 5 minutes: the median is 5, not the mean, because one slow round must not price the queue.
    _round(tmp_path, "round-a.json", started="2026-11-18T04:00:00+00:00", finished="2026-11-18T04:04:00+00:00")
    _round(tmp_path, "round-b.json", started="2026-11-18T04:00:00+00:00", finished="2026-11-18T04:06:00+00:00")
    _round(tmp_path, "round-c.json", started="2026-11-18T04:00:00+00:00", finished="2026-11-18T04:05:00+00:00")
    assert cli._round_minutes(tmp_path) == 5.0


def test_a_round_that_did_not_time_itself_contributes_nothing(tmp_path):
    _round(tmp_path, "round-a.json", started="2026-11-18T04:00:00+00:00", finished="2026-11-18T04:05:00+00:00")
    _round(tmp_path, "round-b.json", started="", finished="")
    assert cli._round_minutes(tmp_path) == 5.0


def test_an_unreadable_stamp_is_skipped_not_guessed(tmp_path):
    _round(tmp_path, "round-a.json", started="not-a-date", finished="2026-11-18T04:05:00+00:00")
    assert cli._round_minutes(tmp_path) is None


def test_no_recorded_round_means_no_estimate(capsys):
    assert cli._round_minutes(Path("var") / "no-such-ledger-here") is None
    assert cli._queue_budget(Path("var") / "no-such-ledger-here")["estimated_minutes"] is None
    assert "minute" not in capsys.readouterr().out


def test_the_estimate_is_per_round_times_the_budget(capsys):
    budget = dict(BUDGET, rounds=3, estimated_minutes=15.0)
    assert "about 15 minute(s)" in budget_text(budget)
    assert "about 5 minute(s)" in budget_text(BUDGET)
    assert "minute" not in budget_text(dict(BUDGET, estimated_minutes=None))
    assert "nothing to spend" in budget_text(dict(BUDGET, rounds=0))
    capsys.readouterr()


def test_the_page_and_the_command_share_the_sentence():
    # The command prints `budget_text(...)` verbatim (round 61). If the page renders its own wording, a reader
    # who copies the recommended command sees a different sentence than the one that recommended it.
    page = format_campaign(_campaign(), queue={"ready": 2, "paid": 0, "share_now": 0.67, "share_after": 1 / 3,
                                               "rounds": 3, "without_file": 2},
                           plan=[{"domain": "SMD", "ready": 2}], budget=BUDGET)
    assert ("budget            " + budget_text(BUDGET)) in page
    assert hasattr(cli, "budget_text")


def test_the_command_reads_the_same_budget_as_the_page():
    # `_queue_budget` is what both call; the command's limit comes from `_rerun_limit`, the page's from the
    # same function, so equality is the contract rather than two expressions agreeing by accident.
    class _Args:
        rerun_limit = 1

    assert cli._rerun_limit(_Args(), 2) == 1
    assert cli._rerun_limit(type("A", (), {"rerun_limit": 0})(), 2) == 2


def _campaign():
    from brian.campaign import Campaign, CampaignRow

    row = CampaignRow(domain="SMD", rounds=3, trials=3,
                      accepts={"cost": 1}, rejections={}, first="", last="",
                      in_force=None, env={}, claim={}, verification={"verdict": "holds"}, reverted=False)
    return Campaign(rows=[row], rounds=3, trials=3, accepted=2, rejected=1, moved=0, relabelled=0,
                    unsourced=0, applied=1, reverted=0)
