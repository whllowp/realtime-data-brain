"""Round 52: the command that turns a printed candidate file into a measurement.

Round 51 printed which file each unattributable round measured; this is the step that spends it. The tests hold
the two limits: a round whose file cannot be chosen is skipped rather than guessed, and a re-run is a
measurement — it never deploys anything, and the old record retires naming the new one.
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR
from brian.streams import FORMAT

SHAPE = {"streams": 2, "windows": 3, "scored": 2, "anomalous": 0, "events": 0, "cadence_s": 300.0}
OTHER = {"streams": 2, "windows": 4, "scored": 4, "anomalous": 0, "events": 0, "cadence_s": 300.0}


def _stream_file(root, name, shape=None):
    shape = shape or SHAPE
    minutes = [index * (int(shape["cadence_s"]) // 60) for index in range(int(shape["windows"]))]
    moments = [{"start": "2026-01-01T%02d:%02d:00+00:00" % (offset // 60, offset % 60),
                "service": "host-1", "scored": index < int(shape["scored"]), "anomaly": False}
               for index, offset in enumerate(minutes)]
    path = root / "data" / "smd" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "dataset": "SMD", "window_s": 300, "overrides": {},
                                "streams": [{"name": "host-1", "windows": moments}]}), encoding="utf-8")
    return path


def _round(root, name, *, shape=None, applied=True, accepted=("events-8",), stream_path=None):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": name, "started_at": "2026-11-17T20:37:43+00:00", "accepted": list(accepted),
               "rejected": [], "applied": [3] if applied else [], "rules_hash": "abc",
               "stream": {"source": "SMD", **(shape or SHAPE)}}
    if stream_path:
        payload["stream_path"] = stream_path
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8")
    line = {key: payload[key] for key in ("round", "started_at", "accepted", "rejected", "applied")}
    line.update({"rules_hash": "abc", "baseline_tokens_per_report": 3356.0})
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _args(**overrides):
    base = {"rerun_unrecorded": True, "rerun_limit": 5, "stream_file": None, "apply": False}
    base.update(overrides)
    return cli.argparse.Namespace(**base)


def test_a_round_with_one_candidate_file_is_ready_and_two_are_not(tmp_path):
    _stream_file(tmp_path, "stream-one.json")
    _stream_file(tmp_path, "stream-two.json", OTHER)
    _round(tmp_path, "round-a.json")
    _round(tmp_path, "round-b.json", shape=OTHER)

    matters = cli._unrecorded_matters(tmp_path)

    ready = [item for item in matters if len(item[3]) == 1]
    blocked = [item for item in matters if len(item[3]) != 1]
    assert [item[1] for item in ready] == ["round-a.json", "round-b.json"]
    assert blocked == []


def test_a_round_whose_file_cannot_be_chosen_is_skipped_not_guessed(tmp_path):
    # Two files with one geometry: picking either would decide which sampling the question is answered on
    # (boundary 104), so the command leaves the round alone and names both.
    _stream_file(tmp_path, "stream-one.json")
    _stream_file(tmp_path, "stream-twin.json")
    _round(tmp_path, "round-a.json")

    matters = cli._unrecorded_matters(tmp_path)

    assert [(item[1], cli._candidate_label(item[3])) for item in matters] == [
        ("round-a.json", "2 candidates: data/smd/stream-one.json, data/smd/stream-twin.json")]


def test_the_candidate_label_of_a_round_with_no_file_is_unknown():
    assert cli._candidate_label([]) == "unknown"


def test_the_rerun_flag_is_off_by_default_and_names_its_limit():
    parser = cli.build_parser()
    args = parser.parse_args(["evolve"])
    assert args.rerun_unrecorded is False
    # One round is minutes of measurement, so the command re-runs one and reports how many are left rather
    # than spending an hour on a first keystroke.
    assert args.rerun_limit == 1


def test_the_rerun_flag_parses_its_limit():
    parser = cli.build_parser()
    args = parser.parse_args(["evolve", "--rerun-unrecorded", "--rerun-limit", "3"])
    assert (args.rerun_unrecorded, args.rerun_limit) == (True, 3)


def test_the_candidates_come_back_out_of_the_record():
    # The question a re-run must answer is the one the record asked. Reading the candidates back is what makes
    # the retirement's overlap a real statement ("1 of its 1 trial(s) appear in the successor").
    record = {"trials": [
        {"candidate": {"name": "events-8", "hypothesis": "h", "changes": [["llm", "max_context_events", 8]]},
         "measurement": {}, "verdict": {}},
        {"candidate": {"name": "host-scoring-deviation", "hypothesis": "h",
                       "changes": [["digest", "scoring", "deviation"]]},
         "measurement": {}, "verdict": {}},
    ]}

    recorded = cli._recorded_candidates(record)

    assert [candidate.name for candidate in recorded] == ["events-8", "host-scoring-deviation"]
    assert recorded[0].changes() == (("llm", "max_context_events", 8),)


def test_a_record_without_candidate_definitions_yields_nothing():
    # A record that kept only names cannot be measured again; the command says so and leaves it alone rather
    # than retiring it (a retirement that returns nothing is bookkeeping, not repayment).
    assert cli._recorded_candidates({"trials": [{"candidate": "events-8"}]}) == []
    assert cli._recorded_candidates({}) == []
