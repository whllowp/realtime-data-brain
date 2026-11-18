# -*- coding: utf-8 -*-
"""Round 49 tests: the header says how much of the page cannot be placed on a sampling.

Round 48 taught the rows to name their files, but the count of rounds that name none lived only in per-row
detail: "54 rounds" read as fully attributable until a reader went row by row. And `--history` -- the page a
reader opens after asking "which file was that?" -- printed no file at all.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.cli import cmd_evolve
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


def test_the_header_says_how_much_cannot_be_attributed(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", stamp="2026-11-18T00:01:00+00:00")
    _round(tmp_path, source="demo", stamp="2026-11-18T00:02:00+00:00")

    result = campaign(tmp_path)

    assert result.unattributable == 2
    assert result.to_dict()["rounds_without_recorded_file"] == 2
    text = format_campaign(result)
    # The proportion, not just the count: "2 of 3" is the fact, "67%" is what a reader decides with.
    assert ("file unknown      2 of 3 round(s) do not say which file they measured, so 67% of the totals "
            "above cannot be placed on a sampling") in text


def test_a_fully_attributed_page_prints_no_such_line(tmp_path):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json")

    result = campaign(tmp_path)

    assert result.unattributable == 0
    assert "file unknown" not in format_campaign(result)


def test_history_names_the_file_and_admits_when_there_is_none(tmp_path, monkeypatch, capsys):
    _round(tmp_path, source="SMD", stream_path="data/smd/stream.json",
           stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", stamp="2026-11-18T00:01:00+00:00")
    monkeypatch.chdir(tmp_path)

    assert cmd_evolve(argparse.Namespace(history=True), None) == 0

    text = capsys.readouterr().out
    assert "[data/smd/stream.json]" in text
    # A round written before round 47 says nothing, and the history page says that instead of leaving the
    # column empty (which a reader would fill in from the dataset name -- the guess rounds 46/47 refused).
    assert "[no file recorded]" in text
