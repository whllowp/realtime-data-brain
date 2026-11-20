"""Round 158: the history row says when it rests on a file a person claimed, and how far that assertion went.

Round 153 says it before a re-measurement, round 155 carries its age, round 157 keeps a repaired age marked as
repaired. The history page said nothing -- and it is the page a reader opens after asking "why does this round
look like that?".
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR, claim_mark

CLAIM = {"from": "", "to": "data/smd/stream-one.json", "at": "2026-11-20T08:38:50+00:00",
         "geometry": "disagrees", "why": "claimed by hand"}


def _record(entry=None, **overrides):
    payload = {"round": "round-claimed.json", "stream_path": "data/smd/stream-one.json",
               "stream_path_history": [dict(entry)] if entry else []}
    payload.update(overrides)
    return payload


def test_a_record_with_no_claim_carries_no_mark():
    assert claim_mark({}) == ""
    assert claim_mark(_record()) == ""


def test_a_claim_the_geometry_confirmed_carries_no_mark():
    # `agrees` means the file was checked against the geometry the round recorded: nothing to warn about.
    assert claim_mark(_record(dict(CLAIM, geometry="agrees"))) == ""


def test_a_disputed_claim_is_marked_on_the_row():
    assert claim_mark(_record(CLAIM)) == "  [rests on a file a person claimed]"


def test_the_mark_says_how_far_the_assertion_travelled():
    mark = claim_mark(_record(dict(CLAIM, claim_hops=2, claim_origin="round-out-of-reach.json")))

    assert "inherited 2 time(s)" in mark


def test_the_mark_says_when_its_age_was_completed_by_a_repair():
    mark = claim_mark(_record(dict(CLAIM, claim_since_source="backfilled")))

    # Round 157's rule, on the page that shows the round rather than the campaign row (boundary 379).
    assert "its age was completed by a repair" in mark


def _fixture(root):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    claimed = {"round": "round-claimed.json", "started_at": "2026-11-20T08:00:00+00:00",
               "finished_at": "2026-11-20T08:05:00+00:00",
               "stream": {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1},
               "stream_path": "data/smd/stream-one.json", "trials": [], "accepted": [], "rejected": [],
               "applied": ["v020"],
               "stream_path_history": [dict(CLAIM, claim_hops=1, claim_since="2026-11-20T08:38:50+00:00",
                                            claim_since_source="derived")]}
    plain = {"round": "round-plain.json", "started_at": "2026-11-20T08:10:00+00:00",
             "finished_at": "2026-11-20T08:15:00+00:00",
             "stream": {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1},
             "stream_path": "data/smd/stream-two.json", "trials": [], "accepted": [], "rejected": [],
             "applied": ["v020"]}
    for payload in (claimed, plain):
        (ledger / payload["round"]).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                              "applied", "stream")}
        line["rules_hash"] = "abc"
        with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")


def test_the_history_page_marks_the_row_that_rests_on_a_claim(tmp_path, monkeypatch, capsys):
    _fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0
    # The history row carries no round name (the index and the timestamps identify it), so the file mark is
    # what tells the two rows apart.
    rows = [line for line in capsys.readouterr().out.splitlines()
            if "[data/smd/stream-one.json]" in line or "[data/smd/stream-two.json]" in line]

    claimed = [row for row in rows if "[data/smd/stream-one.json]" in row][0]
    plain = [row for row in rows if "[data/smd/stream-two.json]" in row][0]
    assert "[rests on a file a person claimed, inherited 1 time(s)]" in claimed
    assert "rests on a file a person claimed" not in plain
    # The mark sits on its own row next to the file, not in a paragraph above the list: a reader scans rows.
    assert "data/smd/stream-one.json" in claimed
