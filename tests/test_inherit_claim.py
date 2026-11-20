"""Round 154: the re-measurement inherits the claim it stood on.

Round 153 made the claim visible at the re-measurement and on the page. The record that measurement *writes*
named the file and nothing else, so the claim disappeared from the ledger one minute after a measurement that
stood on it, and `--campaign`'s `claims` count fell although nothing had been repaired away.
"""

import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (EVOLVE_DIR, EvolveRules, Measurement, RoundResult, claim_note, claimed_rounds,
                          inherit_claim, unconfirmed_claim)

CLAIMED = {
    "round": "round-out-of-reach.json",
    "stream_path": "data/smd/stream-one.json",
    "stream_path_history": [
        {"from": "", "to": "data/smd/stream-one.json", "at": "2026-11-20T08:38:50+00:00",
         "geometry": "disagrees", "why": "claimed by hand"},
    ],
}


def test_the_entry_behind_the_note_is_returned_as_an_entry():
    entry = unconfirmed_claim(CLAIMED)

    assert entry is not None and entry["geometry"] == "disagrees"
    assert claim_note(CLAIMED) is not None


def test_a_confirmed_claim_returns_no_entry():
    agreed = dict(CLAIMED, stream_path_history=[dict(CLAIMED["stream_path_history"][0],
                                                     geometry="agrees")])

    assert unconfirmed_claim(agreed) is None
    assert inherit_claim(agreed, "data/smd/stream-one.json") == []


def test_a_claim_about_another_file_is_not_inherited():
    assert inherit_claim(CLAIMED, "data/smd/stream-two.json") == []


def test_the_inherited_entry_names_the_record_it_came_from():
    history = inherit_claim(CLAIMED, "data/smd/stream-one.json")

    assert len(history) == 1
    entry = history[0]
    assert (entry["to"], entry["geometry"], entry["why"]) == (
        "data/smd/stream-one.json", "disagrees", "claimed by hand")
    assert entry["inherited_from"] == "round-out-of-reach.json"
    # The moment is the new record's, not the claim's: "a person claimed this" is true of the file, while
    # "this round measured it" is true of today (boundary 374).
    assert entry["at"] != CLAIMED["stream_path_history"][0]["at"]


def test_the_successor_record_still_counts_as_resting_on_a_claim():
    successor = {"round": "round-20261120T085517Z.json", "stream_path": "data/smd/stream-one.json",
                 "stream_path_history": inherit_claim(CLAIMED, "data/smd/stream-one.json")}

    assert claim_note(successor) is not None
    assert [row["round"] for row in claimed_rounds([successor])] == ["round-20261120T085517Z.json"]
    assert claimed_rounds([successor])[0]["geometry"] == "disagrees"


def test_the_record_carries_its_history_through_serialisation():
    # `persist` writes the round file from `to_dict`, so a field missing there is a field the next page
    # cannot read: the whole point of the round is that the successor's record says where the file came from.
    rules = EvolveRules()
    baseline = Measurement(candidate="baseline", recall=0.0, fpr=0.0, precision=0.0, reports=0,
                           tokens_per_report=0.0, tokens_per_window=0.0, latency_p95_ms=0.0)
    result = RoundResult(started_at="2026-11-20T08:00:00+00:00", finished_at="2026-11-20T08:01:00+00:00",
                         stream={"source": "SMD"}, rules=rules, baseline=baseline)
    result.stream_path = "data/smd/stream-one.json"
    result.stream_path_history = inherit_claim(CLAIMED, "data/smd/stream-one.json")

    payload = result.to_dict()

    assert payload["stream_path_history"][0]["inherited_from"] == "round-out-of-reach.json"
    # and a record that inherits nothing writes the field as absent, not as an empty list a reader would
    # have to interpret
    assert RoundResult(started_at="a", finished_at="b", stream={}, rules=rules,
                       baseline=baseline).to_dict()["stream_path_history"] is None


def test_the_campaign_page_counts_the_inherited_claim(tmp_path):
    page = format_campaign(campaign(tmp_path), claims=claimed_rounds([
        {"round": "round-20261120T085517Z.json", "stream_path": "data/smd/stream-one.json",
         "stream_path_history": inherit_claim(CLAIMED, "data/smd/stream-one.json")}]))

    # Round 160: the page prints the count from `claim_counts` (when its caller supplies one) and the rows
    # themselves on a second line, so the row that actually inherited is what this line names.
    assert ("claims            the rows: round-20261120T085517Z.json data/smd/stream-one.json (disagrees)"
            in page)
    assert "round-20261120T085517Z.json data/smd/stream-one.json (disagrees)" in page


def test_the_queue_fills_the_successor_history_from_the_record_it_re_measures():
    # The seam is one line in the queue's `run_one`, and it is the only place the two records meet. Read the
    # source so the wiring cannot silently drop out while every unit above still passes.
    import inspect

    source = inspect.getsource(cli)
    start = source.index("def run_one(")
    body = source[start: source.index("for domain, name, _why, files in ready[:limit]:")]

    assert "result.stream_path_history = inherit_claim(record, target_file)" in body


def test_a_record_with_no_history_still_round_trips(tmp_path):
    # An older record has no such key at all; `persist` and `history` must not need one (boundary 153).
    (tmp_path / EVOLVE_DIR).mkdir(parents=True)
    record = {"round": "round-old.json", "started_at": "2026-01-01T00:00:00+00:00", "accepted": [],
              "rejected": [], "applied": ["v020"], "trials": [], "stream": {"source": "SMD",
                                                                            "windows": 3, "scored": 2},
              "stream_path": "data/smd/stream-one.json"}
    (tmp_path / EVOLVE_DIR / "round-old.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / EVOLVE_DIR / "ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert claim_note(record) is None
    assert claimed_rounds([record]) == []
