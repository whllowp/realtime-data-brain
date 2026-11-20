"""Round 143: a walk has an ordinal, and both a payload and a page can name it.

Round 141 made every `--json-out` write announce itself, round 142 made the queue walk hand over the record it keeps,
and the campaign summarised the recorded walks. What none of them said was *which* record in
`var/evolve/queue_runs.json` a reader was looking at: the payload held one record, the page held another, the store
held all of them, and the only way to line them up was to compare timestamps by eye.

So the store's own position becomes the label. These tests pin it: the campaign hands every walk over with its
ordinal, the last-run line says which walk it was, and a payload says where it sits in the store.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.promote import last_queue_run, record_queue_run

from test_from_queue import _elsewhere_only, _run
from test_untested_answer import NOW


def _walked(root: Path, measured: int, at) -> None:
    record_queue_run(root, {"limit": measured, "runnable": measured, "measured": measured, "skipped": 0,
                            "cleared": 0, "worst": 0, "stopped_at": 0,
                            "attempted": [{"version": 20, "code": 0, "why": ""}]}, now=at)


def _page(root: Path):
    """The page `--campaign` prints: the caller is what tells it which run was the last one, so the test does too."""
    return campaign(root, queue_run=last_queue_run(root))


def test_the_campaign_hands_every_walk_over_with_its_ordinal(tmp_path):
    _walked(tmp_path, 1, NOW)
    _walked(tmp_path, 2, NOW + timedelta(minutes=5))
    payload = _page(tmp_path).to_dict()
    assert [item["ordinal"] for item in payload["walks"]] == [1, 2]
    assert [item["measured"] for item in payload["walks"]] == [1, 2]
    # The summary still names the last run on its own, because that is the one a reader acts on.
    assert payload["queue_run"]["measured"] == 2
    assert payload["queue_run"]["at"] == (NOW + timedelta(minutes=5)).isoformat()


def test_the_page_says_which_walk_the_last_run_was(tmp_path):
    _walked(tmp_path, 1, NOW)
    _walked(tmp_path, 2, NOW + timedelta(minutes=5))
    printed = format_campaign(_page(tmp_path))
    # "The last queue run" was one description of the store; the ordinal makes it the same object as the payload's.
    assert "(walk 2 of 2)" in printed


def test_a_single_walk_says_so_rather_than_counting_itself(tmp_path):
    _walked(tmp_path, 1, NOW)
    printed = format_campaign(_page(tmp_path))
    assert "(walk 1 of 1)" in printed


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_payload_says_which_record_it_is(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    _walked(tmp_path, 1, NOW)
    target = tmp_path / "walk.json"
    assert _run(tmp_path, monkeypatch, "--from-queue", "1", "--json-out", str(target)) == 0
    payload = _payload(target)
    store = payload["store"]
    assert store["path"] == "var/evolve/queue_runs.json"
    assert store["ordinal"] == 2 and store["of"] == 2
    stored = json.loads((tmp_path / "var" / "evolve" / "queue_runs.json").read_text(encoding="utf-8"))["runs"]
    assert stored[store["ordinal"] - 1]["at"] == payload["walks"][0]["at"]


def test_a_walk_that_measured_nothing_still_has_a_place_in_the_store(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    from brian import cli  # local: the fixture patches `cli.origin_queue` only for this test
    from test_evidence_paths import THIRD

    monkeypatch.setattr(cli, "origin_queue",
                        lambda root: [{"version": 20, "origin": THIRD, "origin_exists": False}])
    target = tmp_path / "walk.json"
    assert _run(tmp_path, monkeypatch, "--from-queue", "1", "--json-out", str(target)) == 0
    payload = _payload(target)
    assert payload["walks"][0]["measured"] == 0
    assert payload["store"]["ordinal"] == 1 and payload["store"]["of"] == 1
