"""Round 48 tests: the sensitivity table names the file each refusal was measured on.

The rows are per dataset -- the deployment state is -- but a refusal's evidence belongs to a *file*, and the
`w###` record written since round 46 names it. Reading the path from the round alone made this table
contradict `--withheld`, which shows the same refusal with its file, so this honours the record first.
"""
from __future__ import annotations

import json
from pathlib import Path

from brian.evolve import (Candidate, EvolveRules, Measurement, RoundResult, SensitivityRow, Verdict,
                          format_sensitivity, persist, refusal_sensitivity)

LOSS = {"recording": "smd-machine-1-6", "before": 0.491, "after": 0.382, "drop": 0.109,
        "tolerance": 0.05, "pooled_before": 0.297, "pooled_after": 0.312, "budget": 0.10}

PERFECT = {"log_retention": 1.0, "signature_retention": 1.0,
           "first_error_kept": 1.0, "context_retention": 1.0}


def _measurement(name, recall=0.312, split=None):
    return Measurement(
        candidate=name, recall=recall, fpr=0.09, precision=0.4, reports=172,
        tokens_per_report=2908.0, tokens_per_window=410.0, latency_p95_ms=10.0,
        coverage=dict(PERFECT), trial_s=1.0, reasoner="offline",
        recall_at_budget=recall, fpr_at_budget=0.09, window=200,
        recall_by_recording=dict(split or {"smd-machine-1-6": 0.382, "smd-machine-2-2": 0.220}),
    )


def _round(root: Path, *, stream_path: str = "", reasons=True,
           stamp: str = "2026-11-18T00:00:00+00:00"):
    """A round whose candidate is refused for `replicate` under today's rules."""
    candidate = Candidate(name="host-features-3", hypothesis="three series rather than eight",
                          section="digest", field="host_features", value="cpu_r,load_1,mem_free")
    baseline = _measurement("baseline", recall=0.297,
                            split={"smd-machine-1-6": 0.491, "smd-machine-2-2": 0.037})
    trial = _measurement("host-features-3")
    verdict = Verdict("host-features-3", False, "replicate", "smd-machine-1-6 falls",
                      detail=dict(LOSS) if reasons else {})
    result = RoundResult(started_at=stamp, finished_at=stamp, stream={"source": "SMD"},
                         rules=EvolveRules(), baseline=baseline, trials=[(candidate, trial, verdict)])
    result.stream_path = stream_path
    return persist(root, result)


def test_a_row_counts_the_file_its_round_recorded(tmp_path):
    _round(tmp_path, stream_path="data/smd/stream-check.json")

    table = refusal_sensitivity(tmp_path)

    row = table.rows[0] if table.rows else None
    assert row is not None, "the round's candidate should be refused for replicate today"
    assert row.files == {"data/smd/stream-check.json": 1}
    assert row.to_dict()["stream_files"] == {"data/smd/stream-check.json": 1}
    assert "measured on       data/smd/stream-check.json (1)" in format_sensitivity(table)


def test_a_refusal_record_supplies_the_file_the_round_did_not(tmp_path):
    _round(tmp_path)  # the round records no path
    folder = tmp_path / "var" / "evolve" / "withheld"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "w001.json").write_text(json.dumps({
        "number": 1, "round": "round-20261118T000000Z.json", "created_at": "2026-11-18T00:00:00+00:00",
        "stream": {"source": "SMD"}, "stream_path": "data/smd/stream-check.json",
        "candidate": {"name": "host-features-3"}, "changes": [["digest", "host_features", "cpu_r"]],
        "bar": 0.05, "loss": dict(LOSS), "measured": {}, "reason": "not a replicated gain",
    }), encoding="utf-8")

    table = refusal_sensitivity(tmp_path)

    # The record is the more authoritative place (round 46 writes the path there), and reading only the
    # round would make this table contradict `--withheld` about the very same refusal.
    assert table.rows[0].files == {"data/smd/stream-check.json": 1}
    assert "measured on       data/smd/stream-check.json (1)" in format_sensitivity(table)


def test_without_a_path_anywhere_the_row_says_so(tmp_path):
    _round(tmp_path)  # no stream path on the round, and no `w###` record to supply one

    table = refusal_sensitivity(tmp_path)

    assert table.rows[0].files == {"(not recorded)": 1}
    assert "none of them say which file they were measured on" in format_sensitivity(table)


def test_two_files_in_one_row_are_flagged_as_pooled(tmp_path):
    _round(tmp_path, stream_path="data/smd/stream.json", stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, stream_path="data/smd/stream-check.json", stamp="2026-11-18T00:01:00+00:00")

    table = refusal_sensitivity(tmp_path)

    row = table.rows[0]
    assert sorted(row.files) == ["data/smd/stream-check.json", "data/smd/stream.json"]
    text = format_sensitivity(table)
    assert "! 2 files here were sampled differently" in text


def test_the_row_keeps_its_counts_while_naming_no_file(tmp_path):
    _round(tmp_path, stamp="2026-11-18T00:00:00+00:00")

    table = refusal_sensitivity(tmp_path)

    row = table.rows[0]
    assert isinstance(row, SensitivityRow)
    assert row.refused == 1
    assert row.recorded == 1  # the verdict carries the numbers behind the refusal
    assert row.files == {"(not recorded)": 1}
