# -*- coding: utf-8 -*-
"""Round 26 tests: a re-check is only a check when it measures the same quantity.

`v020` promised recall 0.507 at a 10% budget on a stream with 828 scored windows; re-checking it on the
same recordings sampled at a different stride (1235 scored windows) printed FAILED and reverted a healthy
configuration. The guard is about geometry, not about outcomes: same shape and worse numbers is still a
failed promise.
"""
import json

from brian.config import load_settings
from brian.evolve import EvolveRules
from brian.promote import (
    FAILED,
    GEOMETRY_TOLERANCE,
    UNMEASURABLE,
    VERIFIED,
    Claim,
    _geometry_mismatch,
    claim_check,
    format_promotions,
    load_promotions,
    write_verification,
)
from brian.evolve import Measurement


def _measurement(*, recall: float, tokens: float = 3358.0, fpr: float = 0.09) -> Measurement:
    return Measurement(
        candidate="host-rolling-rank-0-85-200", recall=0.4, fpr=0.1, precision=0.3, reports=100,
        tokens_per_report=tokens, tokens_per_window=100.0, latency_p95_ms=10.0,
        coverage={"log_retention": 1.0, "signature_retention": 1.0,
                  "first_error_kept": 1.0, "context_retention": 1.0},
        trial_s=1.0, reasoner="offline", budget=0.10,
        recall_at_budget=recall, fpr_at_budget=fpr,
    )


RECORDED = {"streams": 2, "windows": 948, "scored": 828, "anomalous": 134, "events": 4740}
SAME_SHAPE = {"streams": 2, "windows": 940, "scored": 820, "anomalous": 130, "events": 4700}
OTHER_SAMPLING = {"streams": 2, "windows": 1355, "scored": 1235, "anomalous": 192, "events": 6775}


def test_a_geometry_that_matches_is_not_a_mismatch():
    assert _geometry_mismatch(RECORDED, SAME_SHAPE) is None
    assert _geometry_mismatch(RECORDED, RECORDED) is None


def test_a_stream_sampled_differently_is_named_rather_than_judged():
    mismatch = _geometry_mismatch(RECORDED, OTHER_SAMPLING)

    assert mismatch is not None
    assert "1235" in mismatch and "828" in mismatch


def test_a_missing_stream_record_is_not_a_mismatch():
    # Rounds before the stream field existed carry no geometry; refusing to judge them would make every
    # early version permanently uncheckable.
    assert _geometry_mismatch(None, OTHER_SAMPLING) is None
    assert _geometry_mismatch(RECORDED, None) is None


def test_a_worse_number_on_the_same_sampling_is_still_a_failed_promise():
    claim = Claim(recall_at_budget=0.507, fpr_at_budget=0.09, tokens_per_report=3358.0)
    verdict, reason = claim_check(claim, _measurement(recall=0.297), EvolveRules(),
                                  recorded_stream=RECORDED, checked_stream=SAME_SHAPE)

    assert verdict == FAILED
    assert "0.507" in reason


def test_the_same_worse_number_on_another_sampling_is_unmeasurable_not_failed():
    claim = Claim(recall_at_budget=0.507, fpr_at_budget=0.09, tokens_per_report=3358.0)
    verdict, reason = claim_check(claim, _measurement(recall=0.297), EvolveRules(),
                                  recorded_stream=RECORDED, checked_stream=OTHER_SAMPLING)

    assert verdict == UNMEASURABLE
    assert "different measurement" in reason


def test_a_comparable_stream_that_pays_out_verifies():
    claim = Claim(recall_at_budget=0.507, fpr_at_budget=0.09, tokens_per_report=3358.0)
    verdict, _reason = claim_check(claim, _measurement(recall=0.52), EvolveRules(),
                                   recorded_stream=RECORDED, checked_stream=SAME_SHAPE)

    assert verdict == VERIFIED


def test_the_tolerance_is_a_named_constant_not_a_magic_number_in_the_comparison():
    assert 0.0 < GEOMETRY_TOLERANCE <= 0.5
    tight = dict(SAME_SHAPE, scored=int(828 * (1.0 - GEOMETRY_TOLERANCE) - 1))
    assert _geometry_mismatch(RECORDED, tight) is not None


def test_the_table_lists_one_domains_versions_and_marks_the_ones_without_a_stream(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True)
    for number, stream in ((1, None), (2, {"source": "SMD"}), (3, {"source": "RCAEval RE1-OB"})):
        payload = {
            "version": number, "candidate": "events-8", "change": "llm.max_context_events = 8",
            "env": "BRIAN_LLM_MAX_CONTEXT_EVENTS=8", "rollback": "unset BRIAN_LLM_MAX_CONTEXT_EVENTS",
            "claim": {"recall_at_budget": 0.5}, "created_at": "2026-11-18T00:00:00+00:00",
        }
        if stream is not None:
            payload["stream"] = stream
        (directory / ("v%03d.json" % number)).write_text(json.dumps(payload), encoding="utf-8")

    table = format_promotions(load_promotions(tmp_path), domain="SMD")

    assert "v002" in table
    assert "v001" in table and "no stream recorded" in table
    assert "v003" not in table.split("v001")[0]
    assert "1 version(s) recorded on other streams" in table
    assert load_settings().window.size_s > 0


def _version_file(tmp_path, number=20, *, stream=None):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True)
    payload = {
        "version": number, "candidate": "host-rolling-rank-0-85-200",
        "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200", "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
        "claim": {"recall_at_budget": 0.507}, "created_at": "2026-11-18T00:00:00+00:00",
        "stream": stream or {"source": "SMD", "scored": 828, "events": 4740},
    }
    path = directory / ("v%03d.json" % number)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_failed_check_is_kept_after_the_claim_is_re_established(tmp_path):
    # The bug this guards: the good check wrote `holds` but left the earlier `reverted_at` in place, so the
    # table described a configuration that was in force as though it had been taken out of force.
    first = {"verdict": FAILED, "reason": "re-measured 0.297 (below 0.457)", "stream": "SMD"}
    second = {"verdict": VERIFIED, "reason": "recall 0.507 against a promised 0.507", "stream": "SMD"}
    path = _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, first, reverted_at="2026-11-18T00:27:07+00:00")
    write_verification(tmp_path, promotion, second)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["verification"]["verdict"] == VERIFIED
    assert "reverted_at" not in document
    assert [check["verdict"] for check in document["checks"]] == [FAILED]
    assert document["rollback"] == "unset BRIAN_DIGEST_ROLLING_WINDOW"


def test_the_table_counts_the_checks_a_version_has_already_failed(tmp_path):
    path = _version_file(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion, {"verdict": FAILED, "reason": "re-measured 0.297"})
    write_verification(tmp_path, promotion, {"verdict": VERIFIED, "reason": "recall 0.507"})

    table = format_promotions(load_promotions(tmp_path), domain="SMD")

    assert "(1 earlier check)" in table
    assert path.exists()
