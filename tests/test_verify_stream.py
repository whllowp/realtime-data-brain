# -*- coding: utf-8 -*-
"""Round 47 tests: a promise names the stream file it was made on.

Round 46 gave a *refusal* the file it was measured on; a *version* still had only the dataset summary, so
`--verify` could only stand on the promise's own recording if the operator remembered which file it was -- and
`SMD` is two files here sampled differently (828 scored windows at 3000 s against 1235 at 2100 s). Falling back
to the demo stream would grade every version against a recording none of them was measured on, which is the
kind of answer that looks like one.
"""
import json

from brian.cli import _promotion_stream
from brian.evolve import (Candidate, EvolveRules, Measurement, RoundResult, Verdict, persist)
from brian.promote import load_promotions

STREAM = {"source": "SMD", "streams": 2, "windows": 948, "scored": 828, "anomalous": 134}

CANDIDATE = {
    "name": "host-rolling-rank-0-85-200",
    "hypothesis": "a rank over the last K windows rather than a level",
    "changes": [["digest", "rolling_window", 200]],
    "veto": None,
}


def _measurement(name="host-rolling-rank-0-85-200"):
    return Measurement.from_dict({
        "candidate": name,
        "recall": 0.507,
        "fpr": 0.09,
        "precision": 0.41,
        "reports": 172,
        "tokens_per_report": 3358.0,
        "tokens_per_window": 606.0,
        "windows": 828,
        "recall_at_budget": 0.507,
        "fpr_at_budget": 0.09,
        "budget": 0.10,
        "window": 200,
    })


def _version_file(root, number=20, *, stream_path="data/smd/stream.json"):
    folder = root / "var" / "evolve" / "versions"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": number,
        "created_at": "2026-11-18T01:15:11+00:00",
        "candidate": CANDIDATE,
        "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "measurement": {"recall_at_budget": 0.507, "fpr_at_budget": 0.09, "tokens_per_report": 3358.0},
        "stream": dict(STREAM),
        "stream_path": stream_path or None,
        "verdict": {"candidate": CANDIDATE["name"], "accepted": True, "rule": "quality", "reason": "r"},
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
    }
    path = folder / ("v%03d.json" % number)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _round_result(*, stream_path="data/smd/stream.json"):
    candidate = Candidate(name="events-8", hypothesis="h", section="events", field="limit", value=8)
    result = RoundResult(
        started_at="2026-11-18T01:15:11+00:00",
        finished_at="2026-11-18T01:15:21+00:00",
        stream=dict(STREAM),
        rules=EvolveRules(),
        baseline=_measurement("baseline"),
        trials=[(candidate, _measurement("events-8"),
                 Verdict(candidate="events-8", accepted=True, rule="cost", reason="cheaper"))],
    )
    result.stream_path = stream_path
    return result


def test_a_version_records_the_stream_file_it_was_measured_on(tmp_path):
    written = persist(tmp_path, _round_result())
    version = json.loads(open(written["versions"][0], encoding="utf-8").read())
    assert version["stream_path"] == "data/smd/stream.json"
    assert version["stream"]["source"] == "SMD"
    promotions = load_promotions(tmp_path)
    assert [item.stream_path for item in promotions] == ["data/smd/stream.json"]


def test_a_round_that_did_not_name_a_file_writes_none(tmp_path):
    written = persist(tmp_path, _round_result(stream_path=""))
    version = json.loads(open(written["versions"][0], encoding="utf-8").read())
    assert version["stream_path"] is None
    assert [item.stream_path for item in load_promotions(tmp_path)] == [""]


def test_a_promotion_round_trips_its_stream_path(tmp_path):
    _version_file(tmp_path, 20, stream_path="data/smd/stream-late.json")
    promotion = load_promotions(tmp_path)[0]
    assert promotion.stream_path == "data/smd/stream-late.json"
    assert promotion.to_dict()["stream_path"] == "data/smd/stream-late.json"


def test_the_resolver_takes_the_file_every_version_names(tmp_path):
    _version_file(tmp_path, 19)
    _version_file(tmp_path, 20)
    resolved, missing, by_path = _promotion_stream(load_promotions(tmp_path))
    assert resolved == "data/smd/stream.json"
    assert missing == []
    assert list(by_path) == ["data/smd/stream.json"]


def test_the_resolver_lists_a_version_that_names_no_file(tmp_path):
    _version_file(tmp_path, 19, stream_path="")
    _version_file(tmp_path, 20)
    resolved, missing, _by_path = _promotion_stream(load_promotions(tmp_path))
    assert resolved is None
    assert [item.version for item in missing] == [19]


def test_one_named_file_does_not_excuse_a_version_with_no_file(tmp_path):
    # The point of refusing is that `--verify` would otherwise check v019 and silently skip v020's claim.
    _version_file(tmp_path, 19, stream_path="data/smd/stream.json")
    _version_file(tmp_path, 20, stream_path="")
    resolved, missing, by_path = _promotion_stream(load_promotions(tmp_path))
    assert resolved is None
    assert len(missing) == 1
    assert list(by_path) == ["data/smd/stream.json"]


def test_the_resolver_refuses_to_choose_between_two_files(tmp_path):
    _version_file(tmp_path, 19, stream_path="data/smd/stream.json")
    _version_file(tmp_path, 20, stream_path="data/smd/stream-check.json")
    resolved, missing, by_path = _promotion_stream(load_promotions(tmp_path))
    assert resolved is None
    assert missing == []
    assert sorted(by_path) == ["data/smd/stream-check.json", "data/smd/stream.json"]


def test_every_named_version_keeps_its_own_file_in_the_groups(tmp_path):
    _version_file(tmp_path, 19, stream_path="data/smd/stream.json")
    _version_file(tmp_path, 20, stream_path="data/smd/stream-check.json")
    _resolved, _missing, by_path = _promotion_stream(load_promotions(tmp_path))
    assert [item.version for item in by_path["data/smd/stream.json"]] == [19]
    assert [item.version for item in by_path["data/smd/stream-check.json"]] == [20]
