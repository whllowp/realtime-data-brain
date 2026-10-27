"""The gate: turning calibrated probabilities into *permission to act*.

This is the piece that decides how much the system is allowed to do on its own, and it is
deliberately the most conservative code in the repository, for one reason: **Laya's shipped
probabilities are not yet trustworthy enough for unattended automation.** The upstream
service says so itself (`limits.probability_calibration`: "the shipped temperatures are
inherited from the base checkpoint (ECE 0.213); refit on your own held-out data before gating
automation on confidence"), and this project's own probe reproduced the consequence — the
zero-shot checkpoint called a healthy window anomalous at p=0.763 and missed a 62x error-rate
spike.

So the gate is built to fail safely in two independent ways:

**1. Two scales, never one.** `anomaly_probability` answers *whether* something happened.
Model `confidence` answers *how well the model knows what it is*. A window can be a confident
anomaly (act), a hesitant anomaly (ask System 2), or a confident normal (stay silent) — and
those are genuinely different states that a single threshold would collapse into one. The
classic production failure is gating on confidence alone, which acts most boldly exactly when
the model is most confidently wrong.

**2. Silence costs more than noise.** `suppress_confidence` is deliberately higher than
`anomaly_probability`, so a false silence (a real incident nobody heard about) is harder to
reach than a false alert (a page somebody dismisses). The system is designed to be *cheap to
ignore*, not *quiet*: suppression is a claim of confidence, and it is held to a higher bar.

The gate never decides the *root cause* or the *remedy*. It only decides how much reasoning
the situation deserves. System 1 triages; System 2 explains; humans authorise — that ordering
is what keeps a 35 ms classifier from being able to restart production by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..calibration import CalibrationFit, apply_calibration
from ..config import GateThresholds
from ..contracts import (
    Decision,
    DecisionAction,
    Digest,
    IncidentCategory,
    Lineage,
    Severity,
    Verdict,
    utc_now,
)
from ..laya.qcatalog import DEFAULT_QUESTIONS, catalog_hash
from .client import LayaResult


@dataclass
class GateOutcome:
    """The decision, plus why the gate chose that action.

    `rule` and `reasons` exist so the gate's behaviour is auditable without re-running it.
    A threshold change is the single most likely cause of a behaviour shift in this system,
    and "which rule fired" is the only way to attribute that shift from the records alone.
    """

    decision: Decision
    rule: str
    reasons: List[str] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)


def _score_to_severity(score: Optional[float], levels: int = 4) -> Optional[Severity]:
    """Map the expected level of a `score` answer onto a discrete severity."""
    if score is None:
        return None
    return Severity.from_rank(float(score))


def calibrate_anomaly_probability(raw: Optional[float], fit: Optional[CalibrationFit]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """Apply the fitted recalibration to P(anomaly).

    Returns the calibrated value and a record of the transform. The raw value is never
    overwritten anywhere — `Decision.answers` keeps the model's own output verbatim and this
    record explains the difference, so a reviewer can always see both and judge the correction
    independently of it.
    """
    if raw is None:
        return None, None
    if fit is None or (abs(fit.slope - 1.0) < 1e-9 and abs(fit.intercept) < 1e-9):
        return float(raw), None
    calibrated = apply_calibration(raw, fit)
    return calibrated, {
        "method": "platt",
        "slope": round(fit.slope, 6),
        "intercept": round(fit.intercept, 6),
        "equivalent_temperature": round(fit.temperature, 6) if fit.slope else None,
        "raw_probability": round(float(raw), 6),
        "calibrated_probability": round(float(calibrated), 6) if calibrated is not None else None,
        "fitted_at": fit.fitted_at,
        "fit_n": fit.n,
        "fit_auc": fit.auc,
        "fit_ece_after": fit.ece_after,
        "source": fit.source,
    }


def _answer_confidence(answers: Mapping[str, Any], qid: str) -> Optional[float]:
    answer = answers.get(qid)
    if not isinstance(answer, Mapping):
        return None
    confidence = answer.get("confidence")
    try:
        return float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        return None


def _joint_confidence(answers: Mapping[str, Any], qids: Tuple[str, ...]) -> Optional[float]:
    """The weakest confidence among the answers an action depends on.

    Minimum, not mean. If the category is confident but the severity is a coin flip, the
    system does not in fact know how bad this is, and averaging would hide that behind the
    stronger answer. The judgement is only as reliable as its least reliable component.
    """
    values = [c for c in (_answer_confidence(answers, q) for q in qids) if c is not None]
    return min(values) if values else None


def apply_gate(digest: Digest, result: LayaResult, thresholds: GateThresholds,
               questions: Optional[Mapping[str, Any]] = None,
               service_owner: Optional[str] = None,
               calibrator: Optional[CalibrationFit] = None) -> GateOutcome:
    """Convert one Laya result into a `Decision` with an authorised action.

    Every branch below is a policy statement. They are written in the order the severity of
    their consequences increases — observe, suppress, triage, analyse, page — so that reading
    the function top to bottom reads as "what this system is allowed to do".

    `calibrator` is applied to P(anomaly) *before* any threshold sees it. Calibration is
    measurement and thresholds are policy; keeping them in that order is what stops the
    thresholds from becoming magic numbers tuned around a miscalibrated model.
    """
    answers = result.answers
    catalog = dict(questions or DEFAULT_QUESTIONS)
    reasons: List[str] = []

    anomaly_answer = answers.get("is_anomaly") or {}
    try:
        raw_probability = float(anomaly_answer.get("noul"))
    except (TypeError, ValueError):
        raw_probability = None
    anomaly_probability, calibration_record = calibrate_anomaly_probability(raw_probability, calibrator)
    if calibration_record is not None:
        reasons.append(
            "P(anomaly) recalibrated from raw %.3f to %.3f (slope=%.2f, intercept=%+.2f, "
            "fitted on %d windows)"
            % (raw_probability, anomaly_probability or 0.0, calibration_record["slope"],
               calibration_record["intercept"], calibration_record["fit_n"])
        )
    anomaly_confidence = _answer_confidence(answers, "is_anomaly")

    category: Optional[IncidentCategory] = None
    raw_category = (answers.get("category") or {}).get("choice")
    if raw_category:
        try:
            category = IncidentCategory.coerce(raw_category)
        except Exception:
            category = None

    severity_answer = answers.get("severity") or {}
    try:
        severity_score = float(severity_answer.get("score"))
    except (TypeError, ValueError):
        severity_score = None
    severity = _score_to_severity(severity_score)

    needs_human_answer = answers.get("needs_human") or {}
    needs_human_raw = needs_human_answer.get("noul")
    try:
        needs_human = bool(float(needs_human_raw) >= 0.5)
    except (TypeError, ValueError):
        needs_human = None
    needs_human_confidence = _answer_confidence(answers, "needs_human")

    blast_answer = answers.get("blast_radius") or {}
    blast_radius = blast_answer.get("choice")

    # -- 1. is there an incident at all? --------------------------------------
    is_anomaly = (
        anomaly_probability is not None and anomaly_probability >= thresholds.anomaly_probability
    )

    # Confidence in the parts a *routing* decision rests on: the class and the severity. The
    # existence probability is deliberately not part of this — it has its own scale above.
    routing_confidence = _joint_confidence(answers, ("category", "severity"))

    action: DecisionAction
    rule: str
    verdict = Verdict.ANOMALY if is_anomaly else Verdict.NORMAL

    if is_anomaly:
        if anomaly_probability is not None and anomaly_probability < thresholds.notify_probability:
            # Above the existence threshold but below the notify bar: a real but weak signal.
            # Recorded (so the threshold itself can be evaluated offline) and not surfaced.
            action = DecisionAction.OBSERVE
            rule = "anomaly_below_notify"
            reasons.append(
                "P(anomaly)=%.3f is at or above %.2f but below the notify bar of %.2f; "
                "recorded for scoring, not raised"
                % (anomaly_probability, thresholds.anomaly_probability, thresholds.notify_probability)
            )
        else:
            severe_enough_to_page = (
                severity is not None and severity.rank >= thresholds.page_severity_rank
            )
            confident_human_request = (
                thresholds.trust_needs_human
                and needs_human is True
                and (needs_human_confidence or 0.0) >= thresholds.confidence_auto
            )
            if severe_enough_to_page or confident_human_request:
                action = DecisionAction.PAGE_HUMAN
                rule = "page_human"
                if severe_enough_to_page:
                    reasons.append("severity=%s meets the page threshold" % severity.value)
                if confident_human_request:
                    reasons.append(
                        "the model asked for a human (P=%.3f, confidence=%.3f)"
                        % (float(needs_human_raw or 0.0), needs_human_confidence or 0.0)
                    )
            elif routing_confidence is None or routing_confidence < thresholds.confidence_floor:
                # The model says something is wrong but cannot say what or how bad. That is
                # precisely the case System 1 must not act on: escalate the *question*, not
                # the answer.
                action = DecisionAction.DEEP_ANALYSIS
                rule = "low_confidence_escalation"
                reasons.append(
                    "class/severity confidence is %s, below the floor of %.2f; System 1 cannot "
                    "say what this is, so System 2 reasons instead of the decision being trusted"
                    % (
                        "unknown" if routing_confidence is None else "%.3f" % routing_confidence,
                        thresholds.confidence_floor,
                    )
                )
            elif routing_confidence >= thresholds.confidence_auto:
                action = DecisionAction.AUTO_TRIAGE
                rule = "confident_anomaly"
                reasons.append(
                    "class/severity confidence %.3f clears the auto threshold of %.2f"
                    % (routing_confidence, thresholds.confidence_auto)
                )
            else:
                action = DecisionAction.DEEP_ANALYSIS
                rule = "mid_confidence_escalation"
                reasons.append(
                    "class/severity confidence %.3f is between the floor and the auto threshold; "
                    "escalating for corroboration" % routing_confidence
                )
    else:
        # Not an anomaly. Two very different situations hide behind one probability, and
        # conflating them is how a monitor ends up either noisy or deaf.
        confident_normal = (
            threshold_ok(anomaly_confidence, thresholds.suppress_confidence)
            and anomaly_probability is not None
            and anomaly_probability <= thresholds.normal_probability
        )
        if confident_normal:
            action = DecisionAction.SUPPRESS
            rule = "confident_normal"
            reasons.append(
                "P(anomaly)=%.3f is at or below %.2f with confidence %.3f, so this window is "
                "left alone"
                % (anomaly_probability, thresholds.normal_probability, anomaly_confidence or 0.0)
            )
        else:
            action = DecisionAction.OBSERVE
            rule = "uncertain_normal"
            reasons.append(
                "P(anomaly)=%.3f is below %.2f but confidence is %s, so the window is recorded "
                "rather than declared healthy"
                % (
                    anomaly_probability if anomaly_probability is not None else -1.0,
                    thresholds.anomaly_probability,
                    "unknown" if anomaly_confidence is None else "%.3f" % anomaly_confidence,
                )
            )

    # The reported confidence is the one that drove the action, so a reader never has to guess
    # which number to compare against the thresholds.
    reported_confidence = (
        routing_confidence if is_anomaly else anomaly_confidence
    ) or anomaly_confidence or 0.0

    decision = Decision(
        digest_id=digest.digest_id,
        service=digest.service,
        verdict=verdict,
        action=action,
        confidence=round(float(reported_confidence), 4),
        questions=catalog,
        answers=dict(answers),
        category=category,
        severity=severity,
        severity_score=severity_score,
        needs_human=needs_human,
        anomaly_probability=anomaly_probability,
        routing=result.routing,
        gate_reason="; ".join(reasons),
        latency_ms=result.latency_ms,
        decided_at=utc_now(),
        cache_hit=result.cache_hit,
        questions_hash=catalog_hash(catalog),
        lineage=Lineage(
            upstream_ids=[digest.digest_id],
            producer="gate",
            model_id=result.model or result.routing.model,
            prompt_version=digest.digest_version,
        ),
    )

    signals = {
        "anomaly_probability": anomaly_probability,
        "raw_anomaly_probability": raw_probability,
        "calibration": calibration_record,
        "anomaly_confidence": anomaly_confidence,
        "routing_confidence": routing_confidence,
        "severity_score": severity_score,
        "needs_human_probability": _safe_float(needs_human_raw),
        "needs_human_confidence": needs_human_confidence,
        "category_probability": _probability_of(answers, "category", raw_category),
        "blast_radius": blast_radius,
        "blast_radius_probability": _probability_of(answers, "blast_radius", blast_radius),
        "cache_hit": result.cache_hit,
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "service_owner": service_owner,
    }
    return GateOutcome(decision=decision, rule=rule, reasons=reasons, signals=signals)


def threshold_ok(value: Optional[float], threshold: float) -> bool:
    """`value >= threshold`, treating a missing value as *not* meeting the bar.

    Written as a named function because the direction of this comparison is a safety
    property: an unknown confidence must never satisfy a confidence requirement.
    """
    return value is not None and value >= threshold


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probability_of(answers: Mapping[str, Any], qid: str, label: Any) -> Optional[float]:
    """The model's own probability for the label it chose (not just its confidence)."""
    answer = answers.get(qid)
    if not isinstance(answer, Mapping) or label is None:
        return None
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, Mapping) and label in probabilities:
        return _safe_float(probabilities[label])
    # `score` answers use stringified indices.
    if isinstance(probabilities, Mapping):
        return None
    return None


def explain_thresholds(thresholds: GateThresholds) -> Dict[str, Any]:
    """Human-readable description of the policy — used by `brian config` and the docs."""
    return {
        "exists": {
            "anomaly_probability": thresholds.anomaly_probability,
            "notify_probability": thresholds.notify_probability,
        },
        "acts": {
            "confidence_auto": thresholds.confidence_auto,
            "confidence_floor": thresholds.confidence_floor,
        },
        "silence": {"suppress_confidence": thresholds.suppress_confidence,
                    "normal_probability": thresholds.normal_probability},
        "pages": {"page_severity_rank": thresholds.page_severity_rank,
                  "trust_needs_human": thresholds.trust_needs_human},
    }


__all__ = ["GateOutcome", "apply_gate", "explain_thresholds", "threshold_ok",
           "calibrate_anomaly_probability"]
