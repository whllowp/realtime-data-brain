# -*- coding: utf-8 -*-
"""Round 58 tests: the re-run queue writes down what it paid into the round it paid.

Round 55 made the drain print its balance; round 57 made the queue aim at the largest payable debt. Both
were terminal output — a reader who opens the ledger afterwards cannot tell an aimed payment from an
incidental one, and a queue entry whose successors say nothing about why they were measured leaves the next
person re-deriving the decision. The record of the queue's work belongs in the round it paid.
"""
import json
from pathlib import Path

from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, persist

STREAM = {"source": "SMD", "windows": 1355, "scored": 1235, "anomalous": 192, "events": 6775}
QUEUE = {"paid": "SMD", "paid_round": "round-smd-unrecorded.json",
         "largest_at_start": {"domain": "SMD", "rounds": 4, "without": 4, "share": 1.0, "ready": 4},
         "share_after": 0.0}


def _measurement(name: str = "baseline") -> Measurement:
    return Measurement(candidate=name, recall=0.30, fpr=0.05, precision=0.5, reports=20,
                       tokens_per_report=3358.0, tokens_per_window=1000.0, latency_p95_ms=40.0,
                       recall_at_budget=0.297, fpr_at_budget=0.091)


def _result(*, queue=None) -> RoundResult:
    return RoundResult(started_at="2026-11-18T04:00:00+00:00", finished_at="2026-11-18T04:05:00+00:00",
                       stream=dict(STREAM), rules=EvolveRules(), baseline=_measurement(),
                       trials=[], ceiling={}, proposal={}, queue=dict(queue or {}))


def test_a_queue_entry_round_trips_through_the_record():
    result = _result(queue=QUEUE)
    assert result.to_dict()["queue"] == QUEUE


def test_a_round_that_paid_nothing_writes_null():
    # `None`, not `{}`: a round run by hand was not an entry of any queue, and an empty object would read as
    # "this was a queue entry that paid nothing".
    assert _result().to_dict()["queue"] is None


def test_persist_writes_the_queue_entry_into_the_round_file(tmp_path):
    written = persist(tmp_path, _result(queue=QUEUE), write_versions=False)
    path = Path(written["round"])
    assert path.is_file(), written
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["queue"] == QUEUE
    assert document["stream_path"] is None


def test_a_record_written_without_a_queue_entry_still_loads(tmp_path):
    # Every round before this one carries no `queue` key; the reader must not need it.
    written = persist(tmp_path, _result(), write_versions=False)
    path = Path(written["round"])
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["queue"] is None
    document.pop("queue")
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    again = json.loads(path.read_text(encoding="utf-8"))
    assert "queue" not in again


def test_the_candidate_the_queue_measured_is_still_in_the_record(tmp_path):
    # The queue's value is that it measured the question the old record asked; that is only checkable if the
    # successor keeps the candidate definition. `_recorded_candidates` reads exactly this shape.
    candidate = Candidate(name="events-4", hypothesis="fewer context events", section="llm",
                          field="max_context_events", value=4)
    result = _result(queue=QUEUE)
    result.trials = [(candidate, _measurement("events-4"), _verdict(candidate))]
    written = persist(tmp_path, result, write_versions=False)
    document = json.loads(Path(written["round"]).read_text(encoding="utf-8"))
    assert document["trials"][0]["candidate"]["changes"] == [["llm", "max_context_events", 4]]


def _verdict(candidate: Candidate):
    from brian.evolve import Verdict

    return Verdict(candidate=candidate.name, accepted=True, rule="cost", reason="cheaper")
