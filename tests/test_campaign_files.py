# -*- coding: utf-8 -*-
"""Round 48 tests: a row grouped by dataset must say which stream files it added up.

The campaign's rows are per dataset because the deployment state is per dataset, but a dataset is not a file:
`SMD` is two recordings here sampled differently (828 scored windows at 3000 s against 1235 at 2100 s). A row
that sums rounds from both answers two questions with one total, and until this round it did not say so -- the
reader had to go to `--history` to find out.
"""
from __future__ import annotations

from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, Verdict, persist

PERFECT = {"log_retention": 1.0, "signature_retention": 1.0,
           "first_error_kept": 1.0, "context_retention": 1.0}


def _measurement(tokens: float = 100.0) -> Measurement:
    return Measurement(
        candidate="events-8", recall=0.875, fpr=0.0, precision=1.0, reports=8,
        tokens_per_report=tokens, tokens_per_window=tokens / 4.0, latency_p95_ms=10.0,
        coverage=dict(PERFECT), trial_s=1.0, reasoner="offline",
    )


def _round(root: Path, *, source: str, stream_path: str = "", stamp: str = "2026-11-18T00:00:00+00:00"):
    candidate = Candidate(name="events-8", hypothesis="the event block dominates the prompt",
                          section="llm", field="max_context_events", value=8)
    verdict = Verdict("events-8", True, "cost", "cheaper")
    result = RoundResult(
        started_at=stamp, finished_at=stamp, stream={"source": source}, rules=EvolveRules(),
        baseline=_measurement(), trials=[(candidate, _measurement(60.0), verdict)],
    )
    result.stream_path = stream_path
    return persist(root, result)


def test_a_round_measured_on_one_file_names_it(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json")

    result = campaign(tmp_path)

    assert result.rows[0].files == {"data/smd/stream.json": 1}
    assert result.rows[0].to_dict()["stream_files"] == {"data/smd/stream.json": 1}
    text = format_campaign(result)
    assert "measured on       data/smd/stream.json (1 round(s))" in text
    assert "sampled differently" not in text


def test_two_files_in_one_domain_are_pooled_and_say_so(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", stream_path="data/smd/stream-check.json",
           stamp="2026-11-18T00:01:00+00:00")

    result = campaign(tmp_path)

    assert result.pooled == 1
    assert result.to_dict()["domains_pooling_files"] == 1
    assert sorted(result.rows[0].files) == ["data/smd/stream-check.json", "data/smd/stream.json"]
    text = format_campaign(result)
    assert ("stream files      1 domain(s) add up rounds measured on more than one file: their totals "
            "answer more than one question") in text
    assert "! 2 files here were sampled differently" in text


def test_rounds_without_a_file_are_counted_next_to_the_named_ones(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", stamp="2026-11-18T00:01:00+00:00")
    _round(tmp_path, source="SMD", stamp="2026-11-18T00:02:00+00:00")

    result = campaign(tmp_path)

    assert result.rows[0].files == {"data/smd/stream.json": 1, "(not recorded)": 2}
    text = format_campaign(result)
    assert "data/smd/stream.json (1 round(s)), 2 round(s) without a recorded file" in text
    # One named file is not a pooled row: the unrecorded rounds do not name a second file.
    assert result.pooled == 0


def test_a_domain_with_no_recorded_file_says_so_instead_of_showing_nothing(tmp_path):
    _round(tmp_path, source="RCAEval RE1-OB", stamp="2026-11-18T00:00:00+00:00")

    result = campaign(tmp_path)

    text = format_campaign(result)
    assert "measured on       1 round(s), none of which say which file they measured" in text


def test_the_file_column_never_invents_a_path(tmp_path):
    # A round that recorded no file cannot be attributed to one, however many files exist on disk.
    _round(tmp_path, source="SMD", stamp="2026-11-18T00:00:00+00:00")

    result = campaign(tmp_path)

    assert result.rows[0].files == {"(not recorded)": 1}
    assert result.pooled == 0


def test_two_domains_each_count_their_own_files(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream-check.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="RCAEval RE1-OB", stream_path="data/rcaeval/stream-holdout.json",
           stamp="2026-11-18T00:01:00+00:00")

    result = campaign(tmp_path)

    rows = {row.domain: row for row in result.rows}
    assert rows["SMD"].files == {"data/smd/stream-check.json": 1}
    assert rows["RCAEval RE1-OB"].files == {"data/rcaeval/stream-holdout.json": 1}
    assert result.pooled == 0


def test_the_same_file_spelled_two_ways_is_one_file(tmp_path):
    # Round 54: a record written on Windows says data\smd\stream-check.json, one written by --stream-file
    # says data/smd/stream-check.json. One file, one name -- otherwise the row claims two samplings
    # where there is one (the drain produced 9 + 6 + 1 rounds over two SMD streams and said 3 files).
    _round(tmp_path, source="SMD", stream_path="data/smd/stream-check.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", stream_path="data\\smd\\stream-check.json",
           stamp="2026-11-18T00:01:00+00:00")

    result = campaign(tmp_path)

    assert result.rows[0].files == {"data/smd/stream-check.json": 2}
    assert result.pooled == 0
    assert "were sampled differently" not in format_campaign(result)
