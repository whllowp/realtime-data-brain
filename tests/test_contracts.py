"""Contract tests: the wire format must be stable, and malformed input must never be fatal.

The pipeline's stated guarantee is that a bad record is *catchable and routable* (to the DLQ), never
fatal to the stream. These tests hold that guarantee to account, because it is the kind of promise
that quietly stops being true.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from brian.contracts import (
    Alert,
    ContractError,
    Decision,
    DecisionAction,
    Digest,
    Event,
    Lineage,
    RcaReport,
    Severity,
    Verdict,
    as_jsonable,
    dumps,
    loads,
    parse_iso,
)

UTC = timezone.utc


# --------------------------------------------------------------------------------------
# Event
# --------------------------------------------------------------------------------------


def test_event_round_trip_preserves_every_field() -> None:
    event = Event(
        event_time=datetime(2026, 1, 14, 10, 0, tzinfo=UTC),
        service="checkout-api",
        kind="metric",
        payload={"http_requests_total": 640.0},
        ingest_time=datetime(2026, 1, 14, 10, 0, 1, tzinfo=UTC),
        source="test",
    )
    restored = loads(dumps(event), Event)
    assert restored.to_dict() == event.to_dict()
    assert restored.lag_seconds == pytest.approx(1.0)


def test_event_rejects_empty_service() -> None:
    with pytest.raises(ContractError):
        Event(event_time=datetime(2026, 1, 14, tzinfo=UTC), service="")


def test_event_from_dict_reports_missing_field_as_contract_error() -> None:
    # A missing required field must raise ContractError (routable to the DLQ), not KeyError (fatal).
    with pytest.raises(ContractError):
        Event.from_dict({"service": "svc"})


def test_loads_rejects_invalid_json() -> None:
    with pytest.raises(ContractError):
        loads("{not json", Event)


def test_loads_accepts_a_json_string_and_bytes() -> None:
    raw = json.dumps(
        {"event_time": "2026-01-14T10:00:00Z", "service": "svc", "kind": "metric", "payload": {}}
    )
    assert loads(raw, Event).service == "svc"
    assert loads(raw.encode(), Event).service == "svc"


def test_parse_iso_tolerates_trailing_z_and_naive_input() -> None:
    assert parse_iso("2026-01-14T10:00:00Z").tzinfo is not None
    assert parse_iso("2026-01-14T10:00:00").tzinfo is not None
    with pytest.raises(ContractError):
        parse_iso("not-a-time")


# --------------------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------------------


def test_digest_rejects_a_window_that_ends_before_it_starts() -> None:
    with pytest.raises(ContractError):
        Digest(
            service="svc",
            window_start=datetime(2026, 1, 14, 10, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 14, 10, 0, tzinfo=UTC),
        )


def test_digest_metric_reads_dotted_paths_and_returns_default() -> None:
    digest = Digest(
        service="svc",
        window_start=datetime(2026, 1, 14, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 1, 14, 10, 1, tzinfo=UTC),
        metrics={"errors": {"ratio": {"value": 0.31}}},
    )
    assert digest.metric("errors.ratio.value") == 0.31
    # A missing metric is a reason to judge cautiously, never a reason to raise.
    assert digest.metric("errors.ratio.missing", default="x") == "x"
    assert digest.metric("nothing.here") is None


def test_digest_round_trip_includes_lineage() -> None:
    digest = Digest(
        service="svc",
        window_start=datetime(2026, 1, 14, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 1, 14, 10, 1, tzinfo=UTC),
        interpretation="text",
        lineage=Lineage(upstream_ids=["evt_1", "evt_2"], producer="flink-digest"),
    )
    restored = loads(dumps(digest), Digest)
    assert restored.lineage.upstream_ids == ["evt_1", "evt_2"]
    assert restored.lineage.producer == "flink-digest"


# --------------------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------------------


def _decision(**overrides) -> Decision:
    payload = dict(
        digest_id="dgst_1",
        service="svc",
        verdict=Verdict.ANOMALY,
        action=DecisionAction.PAGE_HUMAN,
        confidence=0.81,
    )
    payload.update(overrides)
    return Decision(**payload)  # type: ignore[arg-type]


def test_decision_coerces_string_enums() -> None:
    decision = _decision(verdict="anomaly", action="page_human", severity="major", category="dependency_failure")
    assert decision.verdict is Verdict.ANOMALY
    assert decision.action is DecisionAction.PAGE_HUMAN
    assert decision.severity is Severity.MAJOR
    assert decision.is_anomaly is True
    assert decision.severity_rank == 2


def test_decision_rejects_confidence_outside_unit_interval() -> None:
    with pytest.raises(ContractError):
        _decision(confidence=1.4)


def test_decision_rejects_unknown_action() -> None:
    with pytest.raises(ContractError):
        _decision(action="restart_everything")


def test_decision_round_trip_preserves_the_questions_it_was_asked() -> None:
    # The rubric must travel with the answer: a model is only interpretable against its question.
    questions = {"is_anomaly": {"type": "noul", "instructions": "…"}}
    decision = _decision(questions=questions, answers={"is_anomaly": {"noul": 0.9}}, questions_hash="sha256:x")
    restored = loads(dumps(decision), Decision)
    assert restored.questions == questions
    assert restored.questions_hash == "sha256:x"
    assert restored.to_dict() == decision.to_dict()


def test_decision_from_dict_tolerates_absent_optional_fields() -> None:
    restored = Decision.from_dict(
        {
            "digest_id": "dgst_1",
            "service": "svc",
            "verdict": "normal",
            "action": "suppress",
            "confidence": 0.9,
        }
    )
    assert restored.category is None and restored.severity is None
    assert restored.routing.model is None


# --------------------------------------------------------------------------------------
# Alert and RcaReport
# --------------------------------------------------------------------------------------


def test_alert_requires_a_decision_id() -> None:
    with pytest.raises(ContractError):
        Alert(decision_id="", service="svc", severity="major", action="page_human", title="t")


def test_rca_report_requires_both_ids() -> None:
    with pytest.raises(ContractError):
        RcaReport(alert_id="a", decision_id="", service="svc", summary="s", root_cause="r")


def test_rca_report_round_trip_keeps_hypotheses_and_actions() -> None:
    report = RcaReport(
        alert_id="alrt_1",
        decision_id="dec_1",
        service="svc",
        summary="summary",
        root_cause="cause",
        hypotheses=[
            {"cause": "a", "confidence": 0.6, "evidence_for": ["x"], "evidence_against": ["y"]},
            {"cause": "b", "confidence": 0.3, "evidence_for": [], "evidence_against": []},
        ],
        actions=[{"step": "check", "rationale": "r", "risk": "low", "autonomous": False, "verify": "v"}],
        confidence=0.55,
        degraded=True,
    )
    restored = loads(dumps(report), RcaReport)
    assert len(restored.hypotheses) == 2
    assert restored.hypotheses[0]["confidence"] == 0.6
    assert restored.actions[0]["autonomous"] is False
    assert restored.degraded is True


def test_severity_rank_mapping_is_monotonic() -> None:
    ranks = [Severity.INFO.rank, Severity.MINOR.rank, Severity.MAJOR.rank, Severity.CRITICAL.rank]
    assert ranks == sorted(ranks) == [0, 1, 2, 3]
    assert Severity.from_rank(0.4) is Severity.INFO
    assert Severity.from_rank(1.6) is Severity.MAJOR
    assert Severity.from_rank(2.9) is Severity.CRITICAL


def test_as_jsonable_produces_pure_json_types() -> None:
    decision = _decision()
    # Round-tripping through json.dumps with no `default=` proves nothing exotic leaked through.
    assert json.loads(json.dumps(as_jsonable(decision)))["decision_id"] == decision.decision_id
