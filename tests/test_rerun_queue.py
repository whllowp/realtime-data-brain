"""The queue can be drained in one invocation, and the campaign page carries its balance (round 54)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brian import cli
from brian.campaign import Campaign, CampaignRow, format_campaign
from brian.streams import load_stream_file, summarise


SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 0,
         "names": ["host-1"], "cadence_s": 300.0, "span_s": 600.0}


def _args(**overrides) -> argparse.Namespace:
    base = {"rerun_unrecorded": True, "rerun_limit": 1, "stream_file": None, "apply": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_the_default_limit_is_one_round():
    assert cli._rerun_limit(_args(), 16) == 1


def test_a_limit_above_the_queue_is_the_queue():
    assert cli._rerun_limit(_args(rerun_limit=40), 16) == 16


def test_zero_means_the_whole_remaining_queue():
    # `0` is the explicit "drain it" instruction named by §6 item 41; it is not the default (boundary 114).
    assert cli._rerun_limit(_args(rerun_limit=0), 16) == 16
    assert cli._rerun_limit(_args(rerun_limit=0), 0) == 0


def test_an_empty_queue_is_zero_even_with_a_limit():
    assert cli._rerun_limit(_args(rerun_limit=5), 0) == 0


def _campaign() -> Campaign:
    row = CampaignRow(domain="SMD", rounds=12, trials=60, accepts={"cost": 3}, rejections={"gain": 2},
                      first="2026-11-17T20:00:00+00:00", last="2026-11-18T00:00:00+00:00",
                      in_force="20", env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
                      claim={"recall_at_budget": 0.507}, verdict="holds",
                      verification={"verdict": "holds"})
    return Campaign(rows=[row], rounds=12, trials=60, accepted=3, rejected=2, moved=0, relabelled=0,
                    unsourced=0, applied=4, reverted=0, unattributable=11)


def test_the_campaign_page_prints_the_queue_balance():
    page = format_campaign(_campaign(), queue={"ready": 16, "paid": 2, "share_now": 0.95,
                                               "share_after": 0.65})

    assert ("queue             16 round(s) can be re-run from the record; 2 already re-measured; "
            "all of them would take the share 95% -> 65%") in page


def test_no_queue_line_when_nothing_can_be_re_run():
    page = format_campaign(_campaign(), queue={"ready": 0, "paid": 0, "share_now": 0.95,
                                               "share_after": 0.95})

    assert "queue             " not in page
    assert "can be re-run from the record" not in page


def test_no_queue_line_when_the_caller_passed_none():
    page = format_campaign(_campaign())

    assert "can be re-run from the record" not in page


def test_the_queue_line_follows_the_debt_it_pays():
    page = format_campaign(_campaign(), queue={"ready": 2, "paid": 0, "share_now": 0.5,
                                               "share_after": 0.25})
    lines = page.splitlines()

    debt = next(index for index, line in enumerate(lines) if line.startswith("file unknown"))
    queue = next(index for index, line in enumerate(lines) if line.startswith("queue"))
    assert queue == debt + 1


def _stream_file(root: Path, name: str = "stream-one.json") -> dict:
    moments = [{"start": "2026-01-01T%02d:%02d:00+00:00" % (index * 5 // 60, index * 5 % 60),
                "service": "host-1", "scored": index < 2, "anomaly": index == 2}
               for index in range(3)]
    path = root / "data" / "smd" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "brian.stream/1", "dataset": "SMD", "window_s": 300,
                                "overrides": {},
                                "streams": [{"name": "host-1", "windows": moments}]}), encoding="utf-8")
    return summarise(load_stream_file(path).streams)


def test_the_queue_limit_reads_the_real_ready_count(tmp_path):
    # The limit is applied to the count the ledger produces, not to a constant: three nameable rounds leave
    # `_rerun_limit(args, ready)` free to say three.
    _stream_file(tmp_path)
    for index in range(3):
        folder = tmp_path / "var" / "evolve"
        folder.mkdir(parents=True, exist_ok=True)
        name = "old-%d" % index
        (folder / ("round-%s.json" % name)).write_text(
            json.dumps({"round": name, "started_at": "2026-11-17T20:10:46+00:00",
                        "stream": dict(SHAPE, source="SMD"), "stream_path": None, "rules": {},
                        "baseline": {}, "trials": [], "ceiling": {}, "proposal": {}}), encoding="utf-8")
        with (folder / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"round": "round-%s.json" % name, "applied": 1,
                                     "accepted": [], "rejected": [], "rules_hash": "abc"}) + "\n")

    ready = len([item for item in cli._unrecorded_matters(tmp_path) if len(item[3]) == 1])

    assert ready == 3
    assert cli._rerun_limit(_args(rerun_limit=0), ready) == 3
