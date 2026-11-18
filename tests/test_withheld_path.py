# -*- coding: utf-8 -*-
"""Round 46 tests: a refusal names the stream file it was measured on.

`--verify-withheld` re-measures an objection on the stream it was raised against. Until this round the
operator had to remember which file that was, so an objection's re-check depended on a memory rather than
on the ledger. The round now writes the path into the round record and into the refusal, and
`--verify-withheld` reads it back — but it refuses to *guess*: `"SMD"` is two files in this repo sampled
differently, and a path derived from the dataset name would re-check the objection on whichever sampling
happened to be at it, then report the difference as a result (the defect round 26 and round 44 each had in
a different place).
"""
import json
from pathlib import Path

from brian import cli
from brian.evolve import (
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    format_withheld,
    persist,
    withheld_records,
    withheld_streams,
)

STREAM = {"source": "SMD", "windows": 1355, "scored": 1235, "events": 6775, "cadence_s": 2100.0}
LOSS = {"recording": "smd-machine-1-6", "before": 0.491, "after": 0.382, "drop": 0.109,
        "tolerance": 0.05, "pooled_before": 0.297, "pooled_after": 0.312, "budget": 0.1}
CANDIDATE = {
    "name": "host-features-3",
    "hypothesis": "three host features render more useful text than eight",
    "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
}


def _measurement(name="baseline", *, split=None):
    return Measurement(candidate=name, recall=0.6, fpr=0.05, precision=0.6, reports=10,
                       tokens_per_report=100.0, tokens_per_window=1.0, latency_p95_ms=1.0,
                       recall_at_budget=0.3, fpr_at_budget=0.05, budget=0.1,
                       recall_by_recording=dict(split or {}),
                       fpr_by_recording={key: 0.05 for key in (split or {})})


def _round(*, stream_path="data/smd/stream-check.json", refuse=True):
    candidate = Candidate.from_dict(CANDIDATE)
    verdicts = []
    if refuse:
        verdicts.append((candidate, _measurement("host-features-3", split={"smd-machine-1-6": 0.382,
                                                                          "smd-machine-2-2": 0.22}),
                         Verdict("host-features-3", False, "replicate", "falls on one recording",
                                 detail=dict(LOSS))))
    return RoundResult(started_at="2026-11-18T03:00:00+00:00",
                       finished_at="2026-11-18T03:05:00+00:00",
                       stream=dict(STREAM), rules=EvolveRules(), baseline=_measurement(),
                       trials=verdicts, stream_path=stream_path)


def _without_path(root: Path) -> Path:
    """A record written before the field existed: readable, but it cannot say which file it came from."""
    folder = root / "var" / "evolve" / "withheld"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "withheld": 9, "round": "round-20261118T020000Z.json",
        "created_at": "2026-11-18T02:00:00+00:00", "stream": dict(STREAM),
        "candidate": dict(CANDIDATE), "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
        "bar": 0.05, "loss": dict(LOSS), "measured": {}, "reason": "r", "rule": "replicate",
        "checks": [],
    }
    path = folder / "w009.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_round_and_the_refusal_both_name_the_file(tmp_path):
    written = persist(tmp_path, _round(), write_versions=False)

    assert len(written["withheld"]) == 1
    record = withheld_records(tmp_path)[0]
    assert record.stream_path == "data/smd/stream-check.json"
    round_payload = json.loads(Path(written["round"]).read_text(encoding="utf-8"))
    assert round_payload["stream_path"] == "data/smd/stream-check.json"


def test_a_round_without_a_path_writes_a_record_that_admits_it(tmp_path):
    persist(tmp_path, _round(stream_path=""), write_versions=False)

    assert withheld_records(tmp_path)[0].stream_path == ""


def test_the_ledger_resolves_each_domain_to_its_own_file(tmp_path):
    persist(tmp_path, _round(), write_versions=False)

    paths, missing = withheld_streams(tmp_path)

    assert paths == {"SMD": Path("data/smd/stream-check.json")}
    assert missing == []


def test_a_record_without_a_path_is_reported_rather_than_guessed(tmp_path):
    persist(tmp_path, _round(), write_versions=False)
    _without_path(tmp_path)

    paths, missing = withheld_streams(tmp_path)

    assert paths == {"SMD": Path("data/smd/stream-check.json")}
    assert [record.label for record in missing] == ["w009"]
    assert missing[0].domain == "SMD"


def test_the_self_described_pairs_are_sorted_and_name_what_is_missing(tmp_path):
    persist(tmp_path, _round(), write_versions=False)
    _without_path(tmp_path)

    pairs, missing = cli._self_described_pairs(tmp_path)

    assert pairs == [("SMD", Path("data/smd/stream-check.json"))]
    assert [record.label for record in missing] == ["w009"]


def test_an_empty_ledger_names_no_pairs(tmp_path):
    pairs, missing = cli._self_described_pairs(tmp_path)

    assert pairs == [] and missing == []


def test_the_path_survives_a_round_trip(tmp_path):
    persist(tmp_path, _round(), write_versions=False)
    record = withheld_records(tmp_path)[0]

    assert record.to_dict()["stream_path"] == "data/smd/stream-check.json"
    assert record.to_dict()["stream_path"] == record.stream_path
    assert type(record).from_dict(record.to_dict()).stream_path == record.stream_path


def test_the_page_says_how_many_records_can_be_re_checked_without_a_path(tmp_path):
    persist(tmp_path, _round(), write_versions=False)

    text = format_withheld(withheld_records(tmp_path))

    assert "stream files" not in text  # every record names its file: nothing to warn about


def test_the_page_warns_when_a_record_cannot_name_its_file(tmp_path):
    persist(tmp_path, _round(), write_versions=False)
    _without_path(tmp_path)

    text = format_withheld(withheld_records(tmp_path))

    assert "stream files      1 of 2 record(s) say which file they were measured on" in text
    assert "need --withheld-stream" in text
