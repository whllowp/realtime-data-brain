# -*- coding: utf-8 -*-
"""Round 28 tests: a refusal that names the comparable shape.

Round 27 printed the window count that would cover the same wall-clock, and round 28 measured the version
there instead of trusting the sentence: `v020` recovers from 0.297 to 0.427 against a promised 0.507 on the
differently-sampled stream. These tests pin the pieces that produced those numbers: the rescaled candidate,
the window-count fallback for records that predate `cadence_s`, and the line that reports the shape.
"""
import json

from brian.promote import (
    UNMEASURABLE,
    Claim,
    _geometry_mismatch,
    format_promotions,
    load_promotions,
    rescale_hint,
    rescaled_candidate,
    shape_note,
    write_verification,
)

RECORDED = {"streams": 2, "windows": 948, "scored": 828, "anomalous": 134, "events": 4740}
OTHER_SAMPLING = {"streams": 2, "windows": 1355, "scored": 1235, "anomalous": 192, "events": 6775}
OTHER_WITH_CADENCE = dict(OTHER_SAMPLING, cadence_s=2100.0)
RECORDED_WITH_CADENCE = dict(RECORDED, cadence_s=3000.0)

CANDIDATE = {
    "name": "host-rolling-rank-0-85-200",
    "hypothesis": "a longer window keeps the rank calibrated",
    "changes": [["digest", "rolling_window", 200], ["digest", "scoring", "auto"],
                ["gate", "rank_probability", 0.85]],
    "veto": None,
}


def _version_file(tmp_path, *, env="BRIAN_DIGEST_ROLLING_WINDOW=200", stream=None, candidate=None):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 20,
        "candidate": CANDIDATE if candidate is None else candidate,
        "change": "digest.scoring = 'auto', digest.rolling_window = 200, gate.rank_probability = 0.85",
        "env": env,
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
        "claim": {"recall_at_budget": 0.507, "fpr_at_budget": 0.09, "tokens_per_report": 3358.0},
        "created_at": "2026-11-18T00:03:10+00:00",
        "stream": stream or dict(RECORDED, source="SMD"),
    }
    path = directory / "v020.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_hint_falls_back_to_the_window_count_when_the_record_predates_cadence(tmp_path):
    # v020 was written in round 22, before streams recorded a cadence, so the cadence-only hint printed
    # nothing for the one version that needed it. The ratio of window counts is the same quantity the
    # refusal already quoted, and it yields the same number: 200 / (948/1355) = 286.
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    hint = rescale_hint(promotion, RECORDED, OTHER_SAMPLING)

    assert hint is not None
    assert "BRIAN_DIGEST_ROLLING_WINDOW=286" in hint
    assert "same share of the recording" in hint


def test_the_hint_uses_the_cadence_when_both_sides_record_one(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    hint = rescale_hint(promotion, RECORDED_WITH_CADENCE, OTHER_WITH_CADENCE)

    assert hint is not None
    assert "BRIAN_DIGEST_ROLLING_WINDOW=286" in hint
    assert "600000" in hint


def test_a_stream_with_the_same_shape_gets_no_hint(tmp_path):
    # Nothing was refused, so there is nothing to rescale: printing a "comparable" number here would imply
    # the deployment should change when nothing is wrong.
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    assert rescale_hint(promotion, RECORDED, dict(RECORDED, windows=940, scored=820)) is None


def test_the_rescaled_candidate_changes_only_the_window(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    shape = rescaled_candidate(promotion, RECORDED, OTHER_SAMPLING)

    assert shape is not None
    assert tuple(shape.changes()) == (("digest", "rolling_window", 286), ("digest", "scoring", "auto"),
                                      ("gate", "rank_probability", 0.85))
    assert shape.name.endswith("@hint286")
    # Round 30: the shape is a proposal now, so it states the promise it is trying to restore and where
    # it came from -- `decide()` judges it against that promise, not against the baseline.
    assert shape.promise == promotion.claim.recall_at_budget
    assert "v020" in shape.provenance


def test_the_rescaled_candidate_needs_a_window_knob(tmp_path):
    _version_file(tmp_path, env="BRIAN_GATE_RANK_P=0.85")
    promotion = load_promotions(tmp_path)[0]

    assert rescaled_candidate(promotion, RECORDED, OTHER_SAMPLING) is None


def test_the_rescaled_candidate_needs_a_comparable_shape(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    assert rescaled_candidate(promotion, RECORDED, dict(RECORDED, windows=940, scored=820)) is None
    assert _geometry_mismatch(RECORDED, OTHER_SAMPLING) is not None


def test_shape_note_reports_the_share_of_the_promise():
    note = shape_note({"shape": {"window": 286, "recall_at_budget": 0.4271, "claimed_recall": 0.5075}})

    assert "rolling_window=286" in note
    assert "0.427" in note
    assert "84%" in note


def test_shape_note_is_empty_without_a_shape():
    assert shape_note(None) == ""
    assert shape_note({}) == ""
    assert shape_note({"shape": {"window": 286}}) == ""


def test_the_table_shows_the_shape_a_refusal_recorded(tmp_path):
    # A refusal whose number never reaches the table is a dead end (round 28): the shape is the answer.
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    payload = {"verdict": UNMEASURABLE, "reason": "a different measurement",
               "shape": {"window": 286, "recall_at_budget": 0.4271, "claimed_recall": 0.5075}}

    write_verification(tmp_path, promotion, payload)
    table = format_promotions(load_promotions(tmp_path), domain="SMD")

    assert "shape" in table
    assert "rolling_window=286" in table and "84%" in table


def test_a_repeated_identical_check_is_not_piled_up(tmp_path):
    # Re-running one refusal while debugging printed "4 earlier checks" for three copies of it.
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    payload = {"verdict": UNMEASURABLE, "reason": "a different measurement"}

    write_verification(tmp_path, promotion, dict(payload, checked_at="1"))
    write_verification(tmp_path, promotion, dict(payload, checked_at="2"))

    document = json.loads(promotion.path.read_text(encoding="utf-8"))
    assert not document.get("checks")


def test_a_check_that_records_a_new_shape_is_kept(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, {"verdict": UNMEASURABLE, "reason": "a different measurement"})
    write_verification(tmp_path, promotion, {"verdict": UNMEASURABLE,
                                             "reason": "a different measurement",
                                             "shape": {"window": 286}})

    document = json.loads(promotion.path.read_text(encoding="utf-8"))
    assert len(document["checks"]) == 1
    # The earlier check carried no shape, so it is kept as the record of when the shape was first measured.
    assert "shape" not in document["checks"][0]


def test_the_claim_is_untouched_by_a_shape(tmp_path):
    _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, {"verdict": UNMEASURABLE, "reason": "r",
                                             "shape": {"window": 286, "recall_at_budget": 0.4271}})

    document = json.loads(promotion.path.read_text(encoding="utf-8"))
    assert Claim.from_dict(document["claim"]).recall_at_budget == 0.507
    assert "reverted_at" not in document
