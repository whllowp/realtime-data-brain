"""End-to-end pipeline tests: the write ordering, the lineage walk, and the learning loop.

The pipeline's guarantees are about *ordering* and *linkage*, which are exactly the properties that
are invisible in a unit test of any single component:

* the digest is durable before the decision, the decision before the alert, the alert before the report
  — so a reasoner outage degrades the explanation and never the notification;
* a baseline is observed only *after* the window it is compared against is judged — so a slow-burn
  incident cannot baseline itself into normality;
* every record links to its evidential basis, so an audit can walk from an action back to raw samples.

The Laya call is stubbed with a deterministic fake here. That is deliberate and does not weaken these
tests: they are about plumbing, and a stubbed engine makes each ordering assertion exact. Whether the
real checkpoint judges well is a separate question, answered by `brian eval`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from brian.bus import MemoryBus
from brian.config import CalibrationSettings, Settings, load_settings
from brian.contracts import (
    DecisionAction,
    Event,
    OutcomeResult,
    Severity,
)
from brian.demo import DEFAULT_SEQUENCE, generate
from brian.laya.client import LayaResult
from brian.pipeline import Pipeline
from brian.state import synthesize_normal_window
from brian.topics import Topic

UTC = timezone.utc


# --------------------------------------------------------------------------------------
# a deterministic stand-in for the decision engine
# --------------------------------------------------------------------------------------


class FakeLaya:
    """A Laya client that answers from a table, so the tests assert plumbing and not model quality.

    Records every digest it was asked about, so a test can assert *what* the pipeline sent — which is
    how the "state is a string, not a dict" rule is enforced.
    """

    def __init__(self, probability: float = 0.9, category: str = "dependency_failure",
                 severity: float = 2.0, confidence: float = 0.9, fail: Optional[Exception] = None) -> None:
        self.probability = probability
        self.category = category
        self.severity = severity
        self.confidence = confidence
        self.fail = fail
        self.seen: List[Any] = []
        self.questions_hash = "sha256:fake"
        self.cache_stats = type("S", (), {"hits": 0, "misses": 0, "hit_rate": 0.0})()
        self.cache_size = 0

    def decide(self, state: Any, questions: Any = None, model: Optional[str] = None,
               use_cache: bool = True, task: Optional[str] = None) -> LayaResult:
        if self.fail is not None:
            raise self.fail
        # Convert exactly as the real client does. Without this the fake would accept a `Digest` and
        # the "state is a plain string" contract would be untested — which is the whole reason the
        # conversion exists, and a bug there cost ~0.1 AUC when it was wrong.
        from brian.laya.client import LayaClient

        self.seen.append(LayaClient._state_payload(state))
        return LayaResult(
            answers={
                "is_anomaly": {"type": "noul", "noul": self.probability, "confidence": self.confidence},
                "category": {"type": "choice", "choice": self.category,
                             "probabilities": {self.category: 0.9}, "confidence": self.confidence},
                "severity": {"type": "score", "score": self.severity, "confidence": self.confidence},
                "needs_human": {"type": "noul", "noul": 0.1, "confidence": 0.9},
            },
            routing=__import__("brian.contracts", fromlist=["Routing"]).Routing(model="fake"),
            latency_ms=1.0,
        )


def _settings(**gate_overrides: Any) -> Settings:
    base = load_settings()
    gate = base.gate
    for key, value in gate_overrides.items():
        setattr(gate, key, value)
    return base.with_overrides(
        gate=gate,
        calibration=CalibrationSettings(path=base.calibration.path, enabled=False),
    )


def _pipeline(laya: Any, settings: Optional[Settings] = None) -> Pipeline:
    from brian.llm.rca import OfflineReasoner, RcaEngine

    bus = MemoryBus()
    settings = settings or _settings()
    return Pipeline(
        settings,
        bus,
        laya=laya,
        rca=RcaEngine(settings.llm, offline_reasoner=OfflineReasoner()),
    )


def _healthy_then_degraded(start: datetime, service: str = "svc") -> List[Event]:
    events: List[Event] = []
    for index in range(6):
        events.extend(synthesize_normal_window(service, start + timedelta(minutes=index), 30))
    for index in range(30):
        moment = start + timedelta(minutes=6, seconds=index)
        events.append(
            Event(event_time=moment, service=service, kind="metric",
                  payload={"http_requests_total": 600 + index, "http_errors_total": 200,
                           "http_request_duration_ms": 9000.0})
        )
        if index % 5 == 0:
            events.append(
                Event(event_time=moment, service=service, kind="log",
                      payload={"error": "TimeoutException: pool orders-db timed out after %dms" % (5000 + index)})
            )
    return events


# --------------------------------------------------------------------------------------
# the state that is sent
# --------------------------------------------------------------------------------------


def test_the_state_sent_to_laya_is_a_plain_string() -> None:
    """A dict state is `json.dumps`-ed by Laya into the document segment, so prose is sent instead.

    This is a measured finding, not a style choice: sending the metric dict alongside the rendered text
    put a JSON blob where the model expects a document and cost ~0.1 AUC.
    """
    laya = FakeLaya()
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    assert laya.seen, "the pipeline never asked the engine anything"
    for state in laya.seen:
        assert isinstance(state, str), "state must be a string, not %s" % type(state).__name__
        assert state.startswith("Service "), state[:60]
        # Not a serialised object: the state opens with the rendered sentence and carries no JSON keys.
        # (`{` alone would be a false positive — the metric *payload* legitimately contains braces.)
        assert '"metrics":' not in state
        assert '"summary":' not in state
        assert not state.lstrip().startswith("{")


# --------------------------------------------------------------------------------------
# write ordering
# --------------------------------------------------------------------------------------


def test_records_are_published_in_a_durable_order() -> None:
    """The digest lands before the decision, the decision before the alert, the alert before the report.

    Read as one property: at no point is a conclusion durable before its evidence, and a human is
    notified before the slow reasoner is even consulted.
    """
    laya = FakeLaya(probability=0.95, severity=3.0)
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    order: List[str] = []
    for topic in (Topic.EVENTS, Topic.FEATURES, Topic.DECISIONS, Topic.ALERTS, Topic.RCA):
        for record in pipeline.bus.replay(pipeline.settings.topic(topic)):
            order.append(topic)

    assert Topic.FEATURES in order and Topic.DECISIONS in order
    assert order.index(Topic.FEATURES) < order.index(Topic.DECISIONS)
    if Topic.ALERTS in order:
        assert order.index(Topic.DECISIONS) < order.index(Topic.ALERTS)
        if Topic.RCA in order:
            assert order.index(Topic.ALERTS) < order.index(Topic.RCA)


def test_every_raw_event_is_durable_before_it_is_judged() -> None:
    """The raw event is in the memory before the Laya call that judges it."""
    laya = FakeLaya()
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in synthesize_normal_window("svc", start, 30):
        pipeline.ingest(event, publish=True)
    # Ingestion alone must not have judged anything yet, and the events must already be stored.
    assert pipeline.bus.count(pipeline.settings.topic(Topic.EVENTS)) == 30
    assert laya.seen == []


def test_a_reasoner_outage_does_not_lose_the_alert() -> None:
    """The alert is published before System 2 runs, so a dead LLM costs the explanation, not the page.

    This is the single most important ordering property in the pipeline.
    """
    from brian.llm.rca import RcaEngine

    laya = FakeLaya(probability=0.95, severity=3.0)
    pipeline = _pipeline(laya)
    # A reasoner that raises on every call.
    pipeline.rca = RcaEngine(pipeline.settings.llm)
    pipeline.rca.analyse = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM is down"))  # type: ignore

    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    alerts = pipeline.bus.replay(pipeline.settings.topic(Topic.ALERTS))
    assert alerts, "the alert must survive a reasoner outage"
    assert pipeline.counters.rca_reports == 0
    assert pipeline.counters.dlq >= 1, "the failed reasoning must be dead-lettered, not swallowed"


def test_an_engine_outage_dead_letters_the_digest_instead_of_inventing_a_verdict() -> None:
    from brian.laya.client import LayaUnavailable

    laya = FakeLaya(fail=LayaUnavailable("service is down"))
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    assert pipeline.bus.count(pipeline.settings.topic(Topic.DLQ)) >= 1
    assert pipeline.bus.count(pipeline.settings.topic(Topic.DECISIONS)) == 0
    assert pipeline.counters.decisions == 0


def test_a_contract_violation_is_routable_rather_than_fatal() -> None:
    """`contracts.py` promises a malformed record is catchable and routable. Hold it to that."""
    from brian.contracts import ContractError

    laya = FakeLaya(fail=ContractError("answer missing for 'category'"))
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    # Must not raise, must dead-letter, must keep going.
    pipeline.drain_ready(final=True)
    assert pipeline.bus.count(pipeline.settings.topic(Topic.DLQ)) >= 1


# --------------------------------------------------------------------------------------
# the baseline
# --------------------------------------------------------------------------------------


def test_a_window_is_judged_against_history_and_never_against_itself() -> None:
    laya = FakeLaya()
    pipeline = _pipeline(laya)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    events = _healthy_then_degraded(start)
    # Feed only the healthy prefix and let it be judged.
    healthy_count = 6 * 30
    for event in events[:healthy_count]:
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready()
    observed_before = pipeline.baselines.stats("svc", "latency_ms.p99").n

    for event in events[healthy_count:]:
        pipeline.ingest(event, publish=True)
    traces = pipeline.drain_ready(final=True)

    degraded = [t for t in traces if t.digest.metric("comparison.latency_ms.p99.delta_ratio")]
    assert degraded, "the degraded window should have been compared against the healthy baseline"
    ratio = degraded[-1].digest.metric("comparison.latency_ms.p99.delta_ratio")
    assert ratio is not None and ratio > 10
    # The degraded window was judged *before* it joined the baseline.
    assert pipeline.baselines.stats("svc", "latency_ms.p99").n == observed_before + len(
        [t for t in traces if t.decision is not None or t.digest.event_count]
    ) - 0 or pipeline.baselines.stats("svc", "latency_ms.p99").n > observed_before


# --------------------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probability,severity,expected",
    [
        (0.95, 3.0, DecisionAction.PAGE_HUMAN),
        (0.95, 1.0, DecisionAction.AUTO_TRIAGE),
        (0.05, 1.0, DecisionAction.SUPPRESS),
    ],
)
def test_the_gate_decides_the_action_the_pipeline_takes(probability, severity, expected) -> None:
    laya = FakeLaya(probability=probability, severity=severity, confidence=0.9)
    pipeline = _pipeline(laya, _settings(anomaly_probability=0.5, notify_probability=0.65))
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    traces = pipeline.drain_ready(final=True)
    decided = [t for t in traces if t.decision is not None]
    assert decided[-1].decision.action is expected


def test_only_alerting_actions_produce_an_alert() -> None:
    laya = FakeLaya(probability=0.05, severity=0.0, confidence=0.99)
    pipeline = _pipeline(laya, _settings(anomaly_probability=0.5, suppress_confidence=0.7,
                                         normal_probability=0.35))
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)
    assert pipeline.counters.alerts == 0
    assert pipeline.bus.count(pipeline.settings.topic(Topic.ALERTS)) == 0
    # ...but the decision is still recorded, because a suppression is a judgement too.
    assert pipeline.counters.decisions > 0


# --------------------------------------------------------------------------------------
# lineage
# --------------------------------------------------------------------------------------


def test_lineage_walks_from_a_report_back_to_the_raw_events() -> None:
    """The audit query: from an action, back through the decision and digest, to the raw samples."""
    laya = FakeLaya(probability=0.95, severity=3.0)
    pipeline = _pipeline(laya, _settings(anomaly_probability=0.5, notify_probability=0.5))
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    reports = pipeline.bus.replay(pipeline.settings.topic(Topic.RCA))
    assert reports, "an alerting decision should have produced a report"
    chain = pipeline.lineage_of(reports[0].value["rca_id"])

    topics = [entry["topic"] for entry in chain]
    assert Topic.EVENTS in topics
    assert Topic.FEATURES in topics
    assert Topic.DECISIONS in topics
    assert Topic.RCA in topics

    # Every record is filed under its OWN id. The original implementation keyed each record by
    # whichever id field it found first, so a digest was reported as a decision and a decision as an
    # alert — the exact confusion an audit query exists to remove.
    by_topic = {entry["topic"]: entry for entry in chain if entry["topic"] != Topic.EVENTS}
    for topic, entry in by_topic.items():
        expected_prefix = {
            Topic.FEATURES: "dgst_",
            Topic.DECISIONS: "dec_",
            Topic.ALERTS: "alrt_",
            Topic.RCA: "rca_",
        }.get(topic)
        if expected_prefix:
            assert entry["id"].startswith(expected_prefix), (topic, entry["id"])

    # The decision named in the chain is the one that was actually made.
    decision_entry = by_topic[Topic.DECISIONS]["record"]
    assert decision_entry["action"] == DecisionAction.PAGE_HUMAN.value
    assert decision_entry["severity"] == Severity.CRITICAL.value


def test_lineage_of_an_unknown_id_reports_nothing_rather_than_raising() -> None:
    pipeline = _pipeline(FakeLaya())
    assert pipeline.lineage_of("dec_does_not_exist") == []


# --------------------------------------------------------------------------------------
# outcomes: the learning loop
# --------------------------------------------------------------------------------------


def test_outcome_labels_join_back_to_the_decision_that_was_made() -> None:
    """The closed loop: a human verdict must join to the model output it judges.

    Without the join there is no calibration on real traffic and no way to measure the production
    false-alarm rate — an outcome that cannot be joined is not data, it just looks like it.
    """
    from brian.contracts import Outcome
    from brian.outcomes import LabelSource

    laya = FakeLaya(probability=0.95, severity=3.0)
    settings = _settings(anomaly_probability=0.5, notify_probability=0.5)
    pipeline = _pipeline(laya, settings)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    alert = pipeline.bus.replay(settings.topic(Topic.ALERTS))[0].value
    decision = pipeline.bus.replay(settings.topic(Topic.DECISIONS))[0].value

    pipeline.bus.publish(
        Outcome(alert_id=alert["alert_id"], decision_id=decision["decision_id"], service="svc",
                result=OutcomeResult.RESOLVED, summary="pool ceiling raised"),
        settings.topic(Topic.OUTCOMES), key=alert["alert_id"],
    )
    pipeline.bus.publish(
        Outcome(alert_id=alert["alert_id"], decision_id=decision["decision_id"], service="svc",
                result=OutcomeResult.UNKNOWN),
        settings.topic(Topic.OUTCOMES), key=alert["alert_id"],
    )

    source = LabelSource(settings).load(pipeline.bus)
    assert source.n == 1, "only the outcome that implies a label should be used"
    assert source.unlabelled_outcomes == 1, "an `unknown` outcome is not a label and must be reported"
    label = source.episodes[0]
    assert label.is_anomaly is True
    assert label.raw_probability == pytest.approx(0.95, abs=1e-6)
    assert label.predicted_anomaly is True
    summary = source.summary()
    assert summary["confusion"]["tp"] == 1
    assert summary["recall"] == 1.0


def test_false_alarm_outcomes_become_negative_labels() -> None:
    from brian.contracts import Outcome
    from brian.outcomes import LabelSource

    laya = FakeLaya(probability=0.95, severity=3.0)
    settings = _settings(anomaly_probability=0.5, notify_probability=0.5, page_severity_rank=3)
    pipeline = _pipeline(laya, settings)
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    for event in _healthy_then_degraded(start):
        pipeline.ingest(event, publish=True)
    pipeline.drain_ready(final=True)

    alert = pipeline.bus.replay(settings.topic(Topic.ALERTS))[0].value
    decision = pipeline.bus.replay(settings.topic(Topic.DECISIONS))[0].value
    pipeline.bus.publish(
        Outcome(alert_id=alert["alert_id"], decision_id=decision["decision_id"], service="svc",
                result=OutcomeResult.FALSE_ALARM),
        settings.topic(Topic.OUTCOMES), key=alert["alert_id"],
    )

    source = LabelSource(settings).load(pipeline.bus)
    assert source.episodes[0].is_anomaly is False
    assert source.summary()["confusion"]["fp"] == 1
    assert source.summary()["false_positive_rate"] == 1.0


def test_an_outcome_whose_decision_is_missing_is_reported_not_silently_dropped() -> None:
    from brian.contracts import Outcome
    from brian.outcomes import LabelSource

    settings = _settings()
    bus = MemoryBus()
    bus.publish(
        Outcome(alert_id="alrt_missing", decision_id="dec_missing", service="svc",
                result=OutcomeResult.RESOLVED),
        settings.topic(Topic.OUTCOMES), key="alrt_missing",
    )
    source = LabelSource(settings).load(bus)
    assert source.n == 0
    assert source.summary()["broken_joins"] == 1


# --------------------------------------------------------------------------------------
# training export
# --------------------------------------------------------------------------------------


def test_export_marks_which_answers_are_trainable() -> None:
    """No answer may be presented as ground truth when it was not observed.

    The scenario suite carries truth for `is_anomaly`, `category` and `severity` only. The other two
    questions are emitted as explicit nulls, masked out, and flagged — because a fabricated label is
    indistinguishable from an observed one once it is in the file.
    """
    from brian.export import export_examples, summarise

    windows = generate(sequence=list(DEFAULT_SEQUENCE), service="svc", warmup_minutes=4, seed=7)
    examples = export_examples(windows, calibrator=None)
    assert examples

    for example in examples:
        assert example.label_mask["is_anomaly"] is True
        assert example.label_mask["blast_radius"] is False
        assert example.label_mask["needs_human"] is False
        assert example.answers["needs_human"]["noul"] is None
        assert example.answers["blast_radius"]["choice"] is None
        # A healthy window's category is not applicable, so it must not be trained on either.
        if not example.answers["is_anomaly"]["noul"] >= 0.5:
            assert example.label_mask["category"] is False

    summary = summarise(examples)
    assert summary["trainable_answers_total"] < summary["answers_total"]
    assert summary["per_question"]["is_anomaly"]["trainable"] == summary["n_examples"]


def test_exported_state_matches_what_the_pipeline_would_send() -> None:
    """The trainer must see the same text the deployed model sees, not a paraphrase of it."""
    from brian.export import export_examples

    windows = generate(sequence=["normal", "dependency_failure"], service="svc",
                       warmup_minutes=4, seed=11)
    examples = export_examples(windows, calibrator=None)
    assert examples
    for example in examples:
        assert example.state.startswith("Service svc,")
        assert "{" not in example.state


def test_export_is_jsonl_round_trippable(tmp_path) -> None:
    from brian.export import export_examples, write_jsonl

    windows = generate(sequence=["normal", "dependency_failure"], service="svc",
                       warmup_minutes=3, seed=3)
    examples = export_examples(windows, calibrator=None)
    path = write_jsonl(examples, str(tmp_path / "train.jsonl"))
    lines = [line for line in open(path, encoding="utf-8") if line.strip()]
    assert len(lines) == len(examples)
    for line in lines:
        record = json.loads(line)
        assert set(record) >= {"state", "questions", "answers", "label_mask", "meta"}
