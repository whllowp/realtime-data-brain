"""Gate, calibration and catalog tests — the safety properties.

These are the tests that matter most, because they encode the failure modes the design exists to
prevent: a confident model being trusted where trust is not warranted, a monotone transform being
mistaken for a fix, and a calibration that looks good while being useless. The gate is the only place
automation is licensed, so its behaviour is pinned down precisely.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brian.calibration import (
    CalibrationFit,
    apply_calibration,
    apply_platt,
    auc,
    expected_calibration_error,
    fit_calibration,
    log_loss,
)
from brian.config import GateThresholds
from brian.contracts import DecisionAction, Digest, Severity, Verdict
from brian.laya.client import LayaResult
from brian.laya.gate import apply_gate, calibrate_anomaly_probability
from brian.laya.qcatalog import (
    DEFAULT_QUESTIONS,
    catalog_hash,
    questions,
    rubric_word_count,
    validate_questions,
)

UTC = timezone.utc


# --------------------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------------------


def test_the_shipped_catalog_passes_its_own_validator() -> None:
    # The validator checks the architectural limits (option count vs head_max_len) AND that every
    # question's declared evidence is a field the renderer can actually emit. A question that cannot
    # be answered from the digest would produce a confident answer built on nothing.
    assert validate_questions() == []


def test_catalog_options_stay_within_the_token_budget() -> None:
    for question_id, definition in DEFAULT_QUESTIONS.items():
        criteria = definition.get("criteria")
        if isinstance(criteria, dict):
            assert len(criteria) <= 20, question_id
            for label, description in criteria.items():
                assert label and str(description or "").strip(), (question_id, label)


def test_validator_rejects_a_bare_enum_without_descriptions() -> None:
    # Measured upstream: a bare string enum degrades answer quality because the rubric loses its
    # per-label descriptions. The validator must catch that, not merely document it.
    problems = validate_questions(
        {
            "category": {
                "type": "choice",
                "instructions": "x",
                "criteria": {"a": "", "b": None, "c": ""},
            }
        }
    )
    assert any("empty description" in problem for problem in problems)


def test_validator_rejects_a_noul_question_with_criteria() -> None:
    problems = validate_questions({"q": {"type": "noul", "instructions": "x", "criteria": {"true": "y"}}})
    assert any("noul" in problem for problem in problems)


def test_validator_rejects_evidence_the_renderer_cannot_emit() -> None:
    import brian.laya.qcatalog as catalog

    original = catalog.EVIDENCE
    try:
        catalog.EVIDENCE = dict(original)
        catalog.EVIDENCE["is_anomaly"] = ("comparison.metric_that_does_not_exist",)
        problems = validate_questions()
        assert any("never emits" in problem for problem in problems)
    finally:
        catalog.EVIDENCE = original


def test_questions_returns_a_copy_so_a_caller_cannot_mutate_the_detector() -> None:
    first = questions()
    first["is_anomaly"]["instructions"] = "tampered"
    assert questions()["is_anomaly"]["instructions"] != "tampered"


def test_catalog_hash_is_stable_and_key_order_independent() -> None:
    assert catalog_hash() == catalog_hash()
    reordered = {key: DEFAULT_QUESTIONS[key] for key in reversed(list(DEFAULT_QUESTIONS))}
    assert catalog_hash(reordered) == catalog_hash(DEFAULT_QUESTIONS)


def test_rubric_fits_the_declared_word_budget() -> None:
    assert rubric_word_count() < 700


# --------------------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------------------


def _policy(**overrides) -> GateThresholds:
    """A gate policy with explicit numbers, independent of the shipped defaults.

    Deliberately NOT `_policy()`. These tests assert the *relationships* the gate depends on —
    a weak anomaly is observed rather than raised, an unconfident normal is not suppressed — and those
    relationships must hold whatever the measured operating point happens to be. The shipped defaults
    are derived from a threshold sweep on labelled windows and will be re-derived as the checkpoint is
    fine-tuned; a test that borrowed them would change meaning every time they moved.
    """
    values = dict(
        anomaly_probability=0.50,
        notify_probability=0.65,
        confidence_auto=0.75,
        confidence_floor=0.45,
        page_severity_rank=2,
        trust_needs_human=True,
        suppress_confidence=0.70,
        normal_probability=0.35,
        always_analyze_alerts=True,
    )
    values.update(overrides)
    return GateThresholds(**values)  # type: ignore[arg-type]


def _digest() -> Digest:
    return Digest(
        service="checkout-api",
        window_start=datetime(2026, 1, 14, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 1, 14, 10, 1, tzinfo=UTC),
        interpretation="text",
        event_count=60,
    )


def _answer(probability: float, confidence: float, category: str = "dependency_failure",
            category_probability: float = 0.9, severity: float = 2.0, severity_confidence: float = 0.9,
            needs_human: float = 0.1, needs_human_confidence: float = 0.9) -> LayaResult:
    return LayaResult(
        answers={
            "is_anomaly": {"type": "noul", "noul": probability, "confidence": confidence},
            "category": {
                "type": "choice",
                "choice": category,
                "probabilities": {category: category_probability},
                "confidence": category_probability,
            },
            "severity": {"type": "score", "score": severity, "confidence": severity_confidence},
            "needs_human": {"type": "noul", "noul": needs_human, "confidence": needs_human_confidence},
        },
        routing=_routing(),
        latency_ms=42.0,
    )


def _routing():
    from brian.contracts import Routing

    return Routing(model="typed-decisions", reason="explicit model='typed-decisions'")


def test_confident_severe_anomaly_pages_a_human() -> None:
    outcome = apply_gate(_digest(), _answer(0.95, 0.9, severity=3.0), _policy())
    assert outcome.decision.verdict is Verdict.ANOMALY
    assert outcome.decision.action is DecisionAction.PAGE_HUMAN
    assert outcome.rule == "page_human"


def test_confident_but_unsevere_anomaly_auto_triages() -> None:
    outcome = apply_gate(_digest(), _answer(0.95, 0.9, severity=1.0), _policy())
    assert outcome.decision.action is DecisionAction.AUTO_TRIAGE
    assert outcome.decision.severity is Severity.MINOR


def test_anomaly_with_weak_class_confidence_escalates_instead_of_acting() -> None:
    # The model says something is wrong but cannot say what. That is precisely when System 1 must not
    # act: escalate the *question*, not the answer. The severity answer is given a confident rank
    # below the page threshold so that this test isolates the class-confidence rule rather than
    # tripping the paging rule first.
    outcome = apply_gate(
        _digest(),
        _answer(0.95, 0.9, severity=1.0, category_probability=0.2, severity_confidence=0.2),
        _policy(),
    )
    assert outcome.decision.action is DecisionAction.DEEP_ANALYSIS
    assert outcome.rule == "low_confidence_escalation"
    # The existence verdict is still an anomaly: only the *routing* is refused.
    assert outcome.decision.is_anomaly is True


def test_anomaly_below_the_notify_bar_is_observed_not_raised() -> None:
    outcome = apply_gate(_digest(), _answer(0.55, 0.9), _policy())
    assert outcome.decision.action is DecisionAction.OBSERVE
    assert outcome.rule == "anomaly_below_notify"


def test_confident_normal_window_is_suppressed() -> None:
    outcome = apply_gate(_digest(), _answer(0.05, 0.95), _policy())
    assert outcome.decision.verdict is Verdict.NORMAL
    assert outcome.decision.action is DecisionAction.SUPPRESS
    assert outcome.rule == "confident_normal"


def test_uncertain_normal_window_is_recorded_not_declared_healthy() -> None:
    # A false silence is worse than a false alert, so suppression requires real confidence.
    outcome = apply_gate(_digest(), _answer(0.45, 0.55), _policy())
    assert outcome.decision.action is DecisionAction.OBSERVE
    assert outcome.rule == "uncertain_normal"


def test_a_confident_yes_to_needs_human_pages_even_at_low_severity() -> None:
    outcome = apply_gate(
        _digest(), _answer(0.95, 0.9, severity=0.2, needs_human=0.95, needs_human_confidence=0.9),
        _policy(),
    )
    assert outcome.decision.action is DecisionAction.PAGE_HUMAN


def test_an_uncertain_needs_human_does_not_page() -> None:
    # An unknown confidence must never satisfy a confidence requirement.
    outcome = apply_gate(
        _digest(), _answer(0.95, 0.9, severity=0.2, needs_human=0.95, needs_human_confidence=0.3),
        _policy(),
    )
    assert outcome.decision.action is not DecisionAction.PAGE_HUMAN


def test_missing_probability_is_treated_as_not_meeting_the_bar() -> None:
    result = _answer(0.95, 0.9)
    result.answers["is_anomaly"] = {"type": "noul", "noul": None, "confidence": None}
    outcome = apply_gate(_digest(), result, _policy())
    # An unreadable probability is not an anomaly claim; it must not page.
    assert outcome.decision.action in (DecisionAction.OBSERVE, DecisionAction.SUPPRESS)


def test_gate_records_the_calibration_it_applied() -> None:
    fit = CalibrationFit(slope=30.0, intercept=9.0, n=90, auc=0.775)
    outcome = apply_gate(_digest(), _answer(0.43, 0.9), _policy(), calibrator=fit)
    record = outcome.signals["calibration"]
    assert record["method"] == "platt"
    assert record["raw_probability"] == pytest.approx(0.43, abs=1e-6)
    # The raw model output stays in `answers`; only `anomaly_probability` is recalibrated.
    assert outcome.decision.answers["is_anomaly"]["noul"] == 0.43 or outcome.decision.answers[
        "is_anomaly"
    ]["noul"] == pytest.approx(0.43, abs=1e-6)
    assert outcome.decision.anomaly_probability > 0.43


def test_no_calibrator_leaves_the_probability_untouched() -> None:
    calibrated, record = calibrate_anomaly_probability(0.43, None)
    assert calibrated == 0.43 and record is None
    # An identity fit is also a no-op, and reports no record rather than a fake one.
    identity = CalibrationFit(slope=1.0, intercept=0.0, n=100)
    assert calibrate_anomaly_probability(0.43, identity) == (0.43, None)


def test_calibration_never_moves_the_ranking() -> None:
    # Monotonicity is the property that makes calibration safe: it can fix the *meaning* of a
    # probability but can never invent discrimination. If this broke, a calibration could be used to
    # manufacture an AUC that the model does not have.
    fit = CalibrationFit(slope=30.0, intercept=9.0, n=100)
    raw = [0.41, 0.42, 0.43, 0.44, 0.45, 0.46]
    calibrated = [apply_calibration(value, fit) for value in raw]
    assert calibrated == sorted(calibrated)
    assert auc(raw, [False, False, False, True, True, True]) == auc(
        calibrated, [False, False, False, True, True, True]
    )


# --------------------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------------------


def test_auc_handles_perfect_separation_inversion_and_ties() -> None:
    assert auc([0.1, 0.2, 0.9, 0.8], [False, False, True, True]) == 1.0
    assert auc([0.9, 0.8, 0.1, 0.2], [False, False, True, True]) == 0.0
    assert auc([0.4, 0.4, 0.4, 0.4], [False, True, False, True]) == 0.5
    assert auc([0.1, 0.9], [True, True]) is None       # single class: undefined
    assert auc([0.1, 0.9], [False, False]) is None


def test_platt_identity_is_the_identity() -> None:
    for probability in (0.01, 0.3, 0.5, 0.77, 0.99):
        assert apply_platt(probability, 1.0, 0.0) == pytest.approx(probability, abs=1e-9)


def test_platt_intercept_can_move_a_compressed_distribution_across_a_threshold() -> None:
    # This is the exact failure the temperature-only fit could not fix: a distribution compressed into
    # 0.41-0.46, centred below any usable threshold. A pure scale cannot relocate it; an intercept can.
    raw = [0.41, 0.43, 0.45, 0.46]
    scale_only = [apply_platt(value, 1.0, 0.0) for value in raw]
    assert all(value < 0.5 for value in scale_only)
    fit = CalibrationFit(slope=30.0, intercept=9.0, n=100)
    moved = [fit.apply(value) for value in raw]
    assert moved[0] < 0.5 < moved[-1]


def test_fit_refuses_a_sample_too_small_to_mean_anything() -> None:
    with pytest.raises(ValueError):
        fit_calibration([0.4, 0.5, 0.6], [True, False, True])


def test_fit_reports_the_identity_for_a_single_class_sample() -> None:
    # The likelihood is monotone and the optimum is at infinity, so there is no identifiable fit.
    # Returning a plausible-looking number here would be the worst outcome.
    fit = fit_calibration([0.4 + 0.001 * i for i in range(30)], [True] * 30)
    assert fit.slope == 1.0 and fit.intercept == 0.0
    assert any("monotone" in note or "single-class" in note for note in fit.notes)


def test_fit_reports_indiscriminate_probabilities_as_such() -> None:
    # Noise: AUC near 0.5. `discriminates` must be False and the note must point at fine-tuning, since
    # no recalibration can create separation.
    probabilities = [0.4 + 0.001 * (index % 7) for index in range(60)]
    labels = [index % 2 == 0 for index in range(60)]
    fit = fit_calibration(probabilities, labels)
    assert fit.discriminates is False
    assert any("fine-tuning" in note for note in fit.notes)


def test_fit_learns_a_usable_boundary_when_separation_exists() -> None:
    # Two well-separated clusters, both shifted below 0.5 — i.e. the model ranks correctly but is
    # systematically offset, which is the measured situation on this domain.
    probabilities = [0.30, 0.32, 0.34, 0.36, 0.38, 0.40] * 8 + [0.42, 0.44, 0.46, 0.48, 0.50, 0.52] * 8
    labels = [False] * 48 + [True] * 48
    fit = fit_calibration(probabilities, labels)
    assert fit.discriminates is True
    assert fit.slope > 1.0                     # widens the compressed spread
    assert fit.apply(0.36) < 0.5 < fit.apply(0.50)
    assert fit.log_loss_after < fit.log_loss_before


def test_fit_round_trips_through_its_file_format(tmp_path) -> None:
    probabilities = [0.30 + 0.01 * index for index in range(40)] + [0.60 + 0.01 * index for index in range(40)]
    labels = [False] * 40 + [True] * 40
    fit = fit_calibration(probabilities, labels)
    path = fit.save(str(tmp_path / "cal.json"))
    restored = CalibrationFit.load(path)
    assert restored is not None
    assert restored.slope == pytest.approx(fit.slope, rel=1e-6)
    assert restored.intercept == pytest.approx(fit.intercept, rel=1e-6)
    # Reported metrics are rounded to 4 decimals in the file, so compare at that resolution.
    assert restored.auc == pytest.approx(fit.auc, abs=1e-4)
    assert restored.ece_after == pytest.approx(fit.ece_after, abs=1e-4)
    assert len(restored.mapping) == len(fit.mapping)


def test_load_returns_none_for_a_missing_or_corrupt_file(tmp_path) -> None:
    assert CalibrationFit.load(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert CalibrationFit.load(str(bad)) is None


def test_ece_is_none_rather_than_noise_on_a_tiny_sample() -> None:
    assert expected_calibration_error([0.5, 0.6], [True, False]) is None


def test_ece_is_zero_for_perfectly_calibrated_predictions() -> None:
    # Every prediction says 0.9 and 90% of them are true.
    probabilities = [0.9] * 100
    labels = [index < 90 for index in range(100)]
    assert expected_calibration_error(probabilities, labels) == pytest.approx(0.0, abs=0.01)
    assert log_loss(probabilities, labels) < 0.4


def test_operating_point_is_a_stated_policy_not_a_constant() -> None:
    probabilities = [0.30 + 0.01 * index for index in range(40)] + [0.60 + 0.01 * index for index in range(40)]
    labels = [False] * 40 + [True] * 40
    fit = fit_calibration(probabilities, labels)
    loose = fit.operating_point(alert_rate=0.5)
    tight = fit.operating_point(alert_rate=0.02)
    assert loose["threshold"] < tight["threshold"]
    assert "policy choice" in loose["note"]
