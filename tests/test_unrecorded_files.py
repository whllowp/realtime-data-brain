"""Round 51: match a round to a stream file by the geometry it recorded.

`--unrecorded` used to tell the operator to pass "the file it measured", which is exactly the fact the record
lost. These tests hold the matcher to the rule that replaced it: a file is proposed only when its summary agrees
with the round's, several files are listed rather than one chosen, and no file is invented.
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR
from brian.streams import FORMAT, load_stream_file, summarise

RECORD = {"round": "round-20261117T203743Z.json",
          "stream": {"streams": 2, "windows": 948, "scored": 828, "anomalous": 134, "events": 4740,
                     "cadence_s": 3000.0}}

LIBRARY = [("data/smd/stream.json", {"windows": 948, "scored": 828, "cadence_s": 3000.0}),
           ("data/smd/stream-check.json", {"windows": 1355, "scored": 1235, "cadence_s": 2100.0})]


def _stream_file(root, name, *, windows, scored, cadence_s=300):
    """A minimal stream file whose summary is the geometry it is built with."""
    minutes = [index * (cadence_s // 60) for index in range(windows)]
    moments = [{"start": "2026-01-01T%02d:%02d:00+00:00" % (offset // 60, offset % 60),
                "service": "host-1", "scored": index < scored, "anomaly": False}
               for index, offset in enumerate(minutes)]
    path = root / "data" / "smd" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "dataset": "SMD", "window_s": 300,
                                "overrides": {}, "streams": [{"name": "host-1", "windows": moments}]}),
                    encoding="utf-8")
    return path


def _round(root, name, *, shape, applied=(3,), accepted=("events-8",)):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / name).write_text(json.dumps({
        "round": name, "started_at": "2026-11-17T20:37:43+00:00", "accepted": list(accepted),
        "rejected": [], "applied": list(applied), "rules_hash": "abc",
        "stream": {"source": "SMD", **shape},
    }), encoding="utf-8")
    line = {"round": name, "started_at": "2026-11-17T20:37:43+00:00", "accepted": list(accepted),
            "rejected": [], "applied": list(applied), "rules_hash": "abc",
            "baseline_tokens_per_report": 3356.0}
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_file_that_recorded_this_geometry_is_the_one_proposed():
    assert cli._matching_streams(RECORD, LIBRARY) == ["data/smd/stream.json"]


def test_a_different_window_count_is_not_proposed():
    other = {"stream": {"windows": 1355, "scored": 1235}}
    assert cli._matching_streams(other, LIBRARY) == ["data/smd/stream-check.json"]


def test_the_same_window_count_with_a_different_scored_count_is_not_proposed():
    # Scored windows are what every number is computed on: 948 windows where 900 were scored is another
    # measurement, not the same one.
    other = {"stream": {"windows": 948, "scored": 900}}
    assert cli._matching_streams(other, LIBRARY) == []


def test_a_recorded_cadence_that_disagrees_is_not_proposed():
    other = {"stream": {"windows": 948, "scored": 828, "cadence_s": 2100.0}}
    assert cli._matching_streams(other, LIBRARY) == []


def test_a_record_without_a_cadence_still_matches():
    # Rounds written before round 27 have no cadence; the window count is all they recorded, and refusing to
    # match them would leave the oldest rounds unattributable for a second reason.
    legacy = {"stream": {"windows": 948, "scored": 828}}
    assert cli._matching_streams(legacy, LIBRARY) == ["data/smd/stream.json"]


def test_two_files_with_one_geometry_are_both_listed():
    # Without names the counts cannot separate two files: the two RCAEval files hold the same windows of the
    # same length, so a matcher that stops at the counts lists both. Choosing one would decide which claim is
    # graded (boundary 104).
    library = LIBRARY + [("data/smd/stream-twin.json", {"windows": 948, "scored": 828, "cadence_s": 3000.0})]
    assert cli._matching_streams(RECORD, library) == ["data/smd/stream.json", "data/smd/stream-twin.json"]


def test_the_recording_names_separate_two_files_of_the_same_geometry():
    # What actually separates the RCAEval files is which cases they hold — the same 543 windows, different
    # recordings — and the round recorded their names.
    library = [("data/rcaeval/stream.json",
                {"windows": 543, "scored": 456, "cadence_s": 60.0, "events": 2042,
                 "names": ["case_1", "case_2"]}),
               ("data/rcaeval/stream-holdout.json",
                {"windows": 543, "scored": 456, "cadence_s": 60.0, "events": 2041,
                 "names": ["case_4", "case_5"]})]
    record = {"stream": {"windows": 543, "scored": 456, "cadence_s": 60.0, "events": 2042,
                         "names": ["case_1", "case_2"]}}

    assert cli._matching_streams(record, library) == ["data/rcaeval/stream.json"]


def test_the_event_count_separates_them_when_the_names_were_not_recorded():
    library = [("data/rcaeval/stream.json", {"windows": 543, "scored": 456, "events": 2042}),
               ("data/rcaeval/stream-holdout.json", {"windows": 543, "scored": 456, "events": 2041})]
    record = {"stream": {"windows": 543, "scored": 456, "events": 2041}}

    assert cli._matching_streams(record, library) == ["data/rcaeval/stream-holdout.json"]


def test_a_round_without_a_summary_matches_nothing():
    assert cli._matching_streams({"round": "round-empty.json"}, LIBRARY) == []
    assert cli._matching_streams(RECORD, []) == []


def test_the_label_says_unknown_rather_than_leaving_the_cell_blank():
    assert cli._candidate_label(["data/smd/stream.json"]) == "data/smd/stream.json"
    assert cli._candidate_label(["a.json", "b.json"]) == "2 candidates: a.json, b.json"
    # A blank cell reads as "nobody looked"; `unknown` says the round measured something this checkout cannot
    # name, which is a property of the round.
    assert cli._candidate_label([]) == "unknown"


def test_the_library_reads_stream_files_and_skips_the_rest(tmp_path):
    path = _stream_file(tmp_path, "stream-one.json", windows=3, scored=2)
    (tmp_path / "data" / "smd" / "stream-broken.json").write_text("not json", encoding="utf-8")
    (tmp_path / "data" / "smd" / "notes.json").write_text("{}", encoding="utf-8")

    library = cli._stream_library(tmp_path)

    assert [name for name, _shape in library] == ["data/smd/stream-one.json"]
    shape = dict(library)["data/smd/stream-one.json"]
    assert (shape["windows"], shape["scored"]) == (3, 2)
    assert shape == summarise(load_stream_file(path).streams)


def test_the_matched_file_reaches_the_listing(tmp_path):
    shape = {"windows": 3, "scored": 2, "cadence_s": 300.0}
    _stream_file(tmp_path, "stream-one.json", windows=3, scored=2)
    _round(tmp_path, "round-a.json", shape=shape)

    listed = cli._unrecorded_matters(tmp_path, [("data/smd/stream-one.json", shape)])

    assert [(domain, name, files) for domain, name, _why, files in listed] == [
        ("SMD", "round-a.json", ["data/smd/stream-one.json"])]


def test_a_listing_without_a_library_says_unknown(tmp_path):
    _round(tmp_path, "round-a.json", shape={"windows": 3, "scored": 2, "cadence_s": 300.0})

    listed = cli._unrecorded_matters(tmp_path, [])

    # Nothing under `data/` in this root, so the round cannot be placed on any file this checkout can see.
    assert listed == [("SMD", "round-a.json", "wrote 1 version(s) still on the page", [])]
    assert cli._candidate_label(listed[0][3]) == "unknown"
