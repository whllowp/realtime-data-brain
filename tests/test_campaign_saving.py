"""The campaign accumulates what the mechanism never proposed (round 38).

Round 37 wrote the count into each round's own record and printed it on that round only. A saving that is
reported once and then forgotten is indistinguishable from a saving that never happened: the next round
starts from the same ledger and proposes the same way. These tests hold the campaign to the arithmetic —
it adds up what the rounds recorded, per stream, and prints nothing when nothing was saved.
"""
from __future__ import annotations

from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, Verdict, persist


def _measurement() -> Measurement:
    return Measurement(
        candidate="events-8", recall=0.875, fpr=0.0, precision=1.0, reports=8,
        tokens_per_report=100.0, tokens_per_window=25.0, latency_p95_ms=10.0,
    )


def _round(root: Path, *, source: str, skipped: int, stamp: str = "2026-11-18T00:00:00+00:00"):
    candidate = Candidate(name="events-8", hypothesis="the event block dominates the prompt",
                          section="llm", field="max_context_events", value=8)
    verdict = Verdict("events-8", True, "cost", "cheaper")
    result = RoundResult(
        started_at=stamp, finished_at=stamp, stream={"source": source}, rules=EvolveRules(),
        baseline=_measurement(), trials=[(candidate, _measurement(), verdict)],
        proposal={"refused_skipped": skipped},
    )
    return persist(root, result)


def test_the_saving_is_added_up_across_the_rounds_that_recorded_it(tmp_path):
    _round(tmp_path, source="SMD", skipped=3, stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", skipped=2, stamp="2026-11-18T00:01:00+00:00")

    result = campaign(tmp_path)
    table = format_campaign(result)

    assert result.skipped == 5
    assert "5 candidate measurement(s) the mechanism never proposed" in table
    assert "5 candidate measurement(s) not repeated" in table
    assert result.to_dict()["measurements_not_repeated"] == 5
    assert result.rows[0].to_dict()["measurements_not_repeated"] == 5


def test_a_stream_that_saved_nothing_prints_no_saving_line(tmp_path):
    _round(tmp_path, source="demo", skipped=0)

    result = campaign(tmp_path)
    table = format_campaign(result)

    assert result.skipped == 0
    assert "saving" not in table


def test_the_saving_is_attributed_to_the_stream_whose_refusals_bought_it(tmp_path):
    _round(tmp_path, source="SMD", skipped=3, stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="RCAEval RE1-OB", skipped=4, stamp="2026-11-18T00:01:00+00:00")

    rows = {row.domain: row for row in campaign(tmp_path).rows}

    assert rows["SMD"].skipped == 3
    assert rows["RCAEval RE1-OB"].skipped == 4


def test_a_record_that_predates_the_count_is_not_counted_as_a_round_that_saved(tmp_path):
    # Rounds written before round 37 have no key at all, and "absent" must not be read as "zero saved" in
    # the other direction either: it is simply not part of the total.
    candidate = Candidate(name="events-8", hypothesis="h", section="llm",
                          field="max_context_events", value=8)
    verdict = Verdict("events-8", True, "cost", "cheaper")
    persist(tmp_path, RoundResult(
        started_at="2026-11-18T00:00:00+00:00", finished_at="2026-11-18T00:00:00+00:00",
        stream={"source": "SMD"}, rules=EvolveRules(), baseline=_measurement(),
        trials=[(candidate, _measurement(), verdict)],
    ))
    _round(tmp_path, source="SMD", skipped=2, stamp="2026-11-18T00:01:00+00:00")

    assert campaign(tmp_path).skipped == 2
