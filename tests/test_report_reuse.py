"""Report reuse must save tokens without ever replaying a stale explanation.

Round 7 measured 37% of the analysis bill going to re-alerts of an incident already analysed, and a 24%
saving from reusing the previous report when the basis is unchanged. The guard matters more than the saving:
reusing on the dedup key alone would replay yesterday's explanation for today's different failure.
"""
from __future__ import annotations

from brian.config import GateThresholds
from brian.contracts import Decision, DecisionAction, Digest, Severity, Verdict
from brian.pipeline import evidence_signature


def _digest(signature: str | None = None) -> Digest:
    metrics = {"service": "svc", "event_count": 10}
    if signature is not None:
        metrics["error_signature"] = {"template": signature, "count": 3, "total": 10}
    return Digest(service="svc", window_start="2026-01-01T00:00:00Z",
                  window_end="2026-01-01T00:01:00Z", metrics=metrics, interpretation="x",
                  event_count=10)


def _decision(action=DecisionAction.DEEP_ANALYSIS, severity=Severity.MAJOR) -> Decision:
    return Decision(digest_id="d", service="svc", verdict=Verdict.ANOMALY, action=action,
                    confidence=0.9, severity=severity)


def test_the_same_incident_with_the_same_evidence_has_the_same_signature() -> None:
    assert evidence_signature(_digest("timeout on pool"), _decision()) == \
        evidence_signature(_digest("timeout on pool"), _decision())


def test_a_different_error_template_breaks_reuse() -> None:
    """Same service and category, different failure: the old explanation no longer applies."""
    assert evidence_signature(_digest("timeout on pool"), _decision()) != \
        evidence_signature(_digest("schema mismatch"), _decision())


def test_a_severity_change_breaks_reuse() -> None:
    assert evidence_signature(_digest("x"), _decision(severity=Severity.MAJOR)) != \
        evidence_signature(_digest("x"), _decision(severity=Severity.CRITICAL))


def test_an_action_change_breaks_reuse() -> None:
    """Escalating from deep_analysis to page_human is a different decision, not a repeat."""
    assert evidence_signature(_digest("x"), _decision(action=DecisionAction.DEEP_ANALYSIS)) != \
        evidence_signature(_digest("x"), _decision(action=DecisionAction.PAGE_HUMAN))


def test_reuse_is_off_by_default() -> None:
    """It changes what a report means, so it cannot arrive switched on with an upgrade."""
    assert GateThresholds().reuse_report_within_s == 0
