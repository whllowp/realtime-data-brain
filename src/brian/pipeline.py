"""Pipeline orchestration: the whole chain, runnable locally and mirrored by the Flink job.

    events ─► windower ─► digest ─► Laya ─► gate ─► alert ─► System 2 ─► report
      │           │         │        │       │       │          │          │
      └───────────┴─────────┴────────┴───────┴───────┴──────────┴──────────┘
                        every hop is appended to Kafka (the memory)

This class is the **local twin** of `flink/job.py`. The reason there are two implementations
of one pipeline is not duplication for its own sake:

* Flink owns the production path — parallelism, checkpointing, exactly-once sinks, backpressure.
* This owns the *addressable* path — one process, deterministic ordering, no cluster. It is how
  a window is replayed to answer "why did you page me at 10:08?", how the detector is scored
  offline, and how the whole chain is tested in CI.

They stay honest by sharing everything that carries meaning: the windower algorithm, the digest
renderer, the question catalog, the gate, and this class's `process_digest` — which the Flink
job imports rather than reimplementing. Only the *transport* and the *timeout* differ.

Two behaviours here are easy to get wrong and are therefore explicit:

**The baseline excludes the window being judged.** `BaselineTracker.observe` is called *after*
the decision, so a window cannot contribute to the baseline it is compared against. Getting this
backwards makes a slow-burn incident baseline itself into normality — a detector that adapts to
the outage it is supposed to catch.

**A reasoner outage never loses an alert.** The decision and the alert are published before
System 2 runs, so a dead LLM degrades the *explanation*, not the *notification*. Ordering the
writes this way is what makes "the LLM was down for an hour" a footnote instead of a gap in the
incident record.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .bus import Bus, Record
from .calibration import CalibrationFit
from .config import Settings
from .contracts import (
    ALERTING_ACTIONS,
    HUMAN_ACTIONS,
    Alert,
    ContractError,
    Decision,
    Digest,
    Event,
    Lineage,
    RcaReport,
    Severity,
)
from .laya.gate import GateOutcome, apply_gate
from .laya.client import LayaClient, LayaError
from .llm.rca import RcaEngine
from .state import BaselineTracker, Windower, build_digest
from .topics import Topic


@dataclass
class Counters:
    """Everything the pipeline did, in one place — the numbers the CLI and tests assert on."""

    events_in: int = 0
    events_late: int = 0
    windows_closed: int = 0
    windows_skipped: int = 0           # closed but below `min_events`
    digests: int = 0
    decisions: int = 0
    anomalies: int = 0
    suppressed: int = 0
    observed: int = 0
    alerts: int = 0
    pages: int = 0
    rca_reports: int = 0
    rca_degraded: int = 0
    dlq: int = 0
    laya_cache_hits: int = 0
    laya_latency_ms_total: float = 0.0
    rca_latency_ms_total: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        payload = dict(vars(self))
        decisions = self.decisions or 1
        payload["mean_laya_latency_ms"] = round(self.laya_latency_ms_total / decisions, 2)
        payload["laya_cache_hit_rate"] = round(self.laya_cache_hits / decisions, 4)
        if self.rca_reports:
            payload["mean_rca_latency_ms"] = round(self.rca_latency_ms_total / self.rca_reports, 2)
        return payload


@dataclass
class WindowTrace:
    """One window's full reasoning chain, in memory, for tests and for the CLI's summary."""

    digest: Digest
    decision: Optional[Decision] = None
    gate: Optional[GateOutcome] = None
    alert: Optional[Alert] = None
    report: Optional[RcaReport] = None
    history: List[Event] = field(default_factory=list)

    @property
    def action(self) -> Optional[str]:
        return self.decision.action.value if self.decision else None


class Pipeline:
    """The chain, wired to a bus, a Laya client and a reasoner."""

    def __init__(self, settings: Settings, bus: Bus, laya: Optional[LayaClient] = None,
                 rca: Optional[RcaEngine] = None,
                 baselines: Optional[BaselineTracker] = None,
                 calibrator: Optional[CalibrationFit] = None,
                 keep_events: int = 200) -> None:
        self.settings = settings
        self.bus = bus
        self.laya = laya if laya is not None else LayaClient(settings.laya)
        self.rca = rca if rca is not None else RcaEngine(settings.llm)
        # Loaded once at construction, not per decision: the fit is a deployment artefact, and a
        # pipeline that picked up a new temperature mid-run would produce decisions that cannot be
        # attributed to a single calibration.
        self.calibrator = calibrator if calibrator is not None else settings.load_calibration()
        self.baselines = baselines if baselines is not None else BaselineTracker(
            window=settings.window.baseline_windows
        )
        self.windower = Windower(
            size_s=settings.window.size_s,
            slide_s=settings.window.slide_s,
            allowed_lateness_s=settings.window.allowed_lateness_s,
            min_events=settings.window.min_events,
        )
        self.counters = Counters()
        #: Bounded per-service event buffer, handed to System 2 as raw context. Bounded on
        #: purpose: this is a stream, and an unbounded buffer is a slow memory leak that only
        #: shows up in production.
        self._recent: Dict[str, List[Event]] = {}
        self._keep_events = max(0, keep_events)
        self.traces: List[WindowTrace] = []
        self._started_at = time.time()

    # -- ingest ----------------------------------------------------------------
    def ingest(self, event: Event, publish: bool = True) -> Record:
        """Append one raw event to the memory and feed it to the windower.

        Publishing the raw event *before* judging it is the ordering guarantee that makes the
        trail complete: if the process dies mid-judgement, the window is still re-derivable
        from the topic, because the events are already durable.
        """
        record = Record(topic="", key=None, value={}, offset=-1, timestamp=0.0)
        if publish:
            record = self.bus.publish(event, self.settings.topic(Topic.EVENTS), key=event.service,
                                      settings=self.settings)
        self.counters.events_in += 1
        self.windower.add(event)
        if self._keep_events:
            buffer = self._recent.setdefault(event.service, [])
            buffer.append(event)
            if len(buffer) > self._keep_events:
                del buffer[: len(buffer) - self._keep_events]
        return record

    def ingest_many(self, events: Iterable[Event], publish: bool = True) -> None:
        for event in events:
            self.ingest(event, publish=publish)

    def recent_events(self, service: str, limit: int) -> List[Event]:
        buffer = self._recent.get(service) or []
        return buffer[-limit:] if limit else []

    # -- windows ---------------------------------------------------------------
    def drain_ready(self, final: bool = False) -> List[WindowTrace]:
        """Close and process every window the watermark has released (or all, if `final`)."""
        aggregators = self.windower.flush() if final else self.windower.ready()
        traces: List[WindowTrace] = []
        for aggregator in aggregators:
            digest = build_digest(aggregator, self.baselines)
            trace = self.process_digest(digest, events=self.recent_events(digest.service, self.settings.llm.max_context_events))
            # Only now does this window become baseline material for *later* windows.
            self.baselines.observe(digest.service, digest.metrics)
            traces.append(trace)
        self.counters.events_late = self.windower.stats["late_events"]
        return traces

    # -- the judgement itself --------------------------------------------------
    def process_digest(self, digest: Digest, events: Optional[Sequence[Event]] = None,
                       publish: bool = True) -> WindowTrace:
        """Judge one digest end to end. Imported by the Flink job so both paths share it.

        Steps, in order, and the order carries the durability argument:

          1. publish the digest          — the feature snapshot becomes replayable
          2. Laya decides                — the only inference in the hot path
          3. gate                        — policy turns probabilities into permission
          4. publish the decision        — the judgement is durable before anything acts on it
          5. publish the alert           — a human is notified even if System 2 is down
          6. System 2 reasons            — the slow, optional half
          7. publish the report          — the explanation joins the same trail
        """
        self.counters.windows_closed += 1
        trace = WindowTrace(digest=digest, history=list(events or []))

        if digest.event_count < self.settings.window.min_events:
            # Too little data to accuse anyone of anything. Recorded, not judged — and the
            # digest is still published so the gap is visible in the memory rather than absent.
            self.counters.windows_skipped += 1
            if publish:
                self.bus.publish(digest, self.settings.topic(Topic.FEATURES), key=digest.service,
                                 settings=self.settings)
            return trace

        if publish:
            self.bus.publish(digest, self.settings.topic(Topic.FEATURES), key=digest.service,
                             settings=self.settings)
        self.counters.digests += 1

        try:
            result = self.laya.decide(digest)
        except LayaError as exc:
            # The engine is unreachable. Do not invent a verdict: dead-letter the digest with the
            # reason so it can be re-judged from the events once Laya is back. The memory makes
            # this recoverable, which is the whole point of storing the digest as well as the
            # events.
            self.counters.dlq += 1
            if publish:
                self._publish_dlq(Topic.FEATURES, digest.digest_id, digest.service, exc, digest.to_dict())
            return trace
        except ContractError as exc:
            # The engine answered with something that violates the contract (a missing answer, an
            # unknown label, a probability outside [0, 1]). `contracts.py` promises this is
            # *catchable and routable* rather than fatal to the stream, and this is where that
            # promise is kept: the digest is dead-lettered for a human, and the next window is
            # judged normally. A malformed response is an integration bug, and losing one window to
            # it is strictly better than losing the stream.
            self.counters.dlq += 1
            if publish:
                self._publish_dlq(Topic.FEATURES, digest.digest_id, digest.service, exc, digest.to_dict())
            return trace

        self.counters.laya_latency_ms_total += result.latency_ms
        if result.cache_hit:
            self.counters.laya_cache_hits += 1

        outcome = apply_gate(digest, result, self.settings.gate, calibrator=self.calibrator)
        decision = outcome.decision
        trace.decision = decision
        trace.gate = outcome
        self.counters.decisions += 1
        if decision.is_anomaly:
            self.counters.anomalies += 1
        if decision.action.value == "suppress":
            self.counters.suppressed += 1
        if decision.action.value == "observe":
            self.counters.observed += 1

        if publish:
            self.bus.publish(decision, self.settings.topic(Topic.DECISIONS), key=decision.service,
                             settings=self.settings)

        if decision.action not in ALERTING_ACTIONS:
            return trace

        # -- notify before reasoning -------------------------------------------
        alert = self._alert_for(digest, decision)
        trace.alert = alert
        self.counters.alerts += 1
        if decision.action in HUMAN_ACTIONS:
            self.counters.pages += 1
        if publish:
            self.bus.publish(alert, self.settings.topic(Topic.ALERTS), key=alert.service,
                             settings=self.settings)

        # -- System 2 ----------------------------------------------------------
        if self.settings.gate.always_analyze_alerts:
            try:
                report = self.rca.analyse(digest, decision, alert.alert_id, events=events)
            except Exception as exc:  # noqa: BLE001 - reasoning must never kill the stream
                # Broad on purpose. System 2 is an external service plus a parser plus a schema; any
                # failure in that chain means "no explanation right now", never "stop judging
                # windows". The alert has already been published above, so the notification survives
                # an explanation failure — which is the ordering this whole method is arranged around.
                self.counters.dlq += 1
                if publish:
                    self._publish_dlq(Topic.RCA, alert.alert_id, alert.service, exc, decision.to_dict())
                return trace
            trace.report = report
            self.counters.rca_reports += 1
            self.counters.rca_latency_ms_total += report.latency_ms or 0.0
            if report.degraded:
                self.counters.rca_degraded += 1
            if publish:
                self.bus.publish(report, self.settings.topic(Topic.RCA), key=report.alert_id,
                                 settings=self.settings)
        return trace

    def _alert_for(self, digest: Digest, decision: Decision) -> Alert:
        """Build the operator-facing record.

        `dedup_key` is `service|category` rather than `service` alone: two different failure
        classes on one service in one minute are two incidents, and collapsing them would hide
        the second one. Window start is deliberately *not* in the key — the same incident
        across consecutive windows should be recognisable as one thing.
        """
        category = decision.category.value if decision.category else "unknown"
        severity = decision.severity.value if decision.severity else "unknown"
        title = "%s: %s (%s)" % (digest.service, category.replace("_", " "), severity)
        return Alert(
            decision_id=decision.decision_id,
            service=digest.service,
            severity=decision.severity or Severity_for(decision),
            action=decision.action,
            title=title,
            digest_id=digest.digest_id,
            category=decision.category,
            confidence=decision.confidence,
            dedup_key="%s|%s" % (digest.service, category),
            lineage=Lineage(
                upstream_ids=[decision.decision_id, digest.digest_id],
                producer="alert-router",
                model_id=decision.routing.model,
            ),
        )

    def _publish_dlq(self, topic: str, source_id: str, service: str, error: BaseException,
                     payload: Mapping[str, Any]) -> None:
        """Dead-letter a record with enough context to replay it after the cause is fixed."""
        self.bus.append(
            self.settings.topic(Topic.DLQ),
            service,
            {
                "source_topic": self.settings.topic(topic),
                "source_id": source_id,
                "service": service,
                "error": "%s: %s" % (type(error).__name__, error),
                "payload": dict(payload),
                "at": time.time(),
            },
            headers={"record_type": "Dlq"},
        )

    # -- evidence and reporting ------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        return {
            "counters": self.counters.as_dict(),
            "laya": {
                "model": self.settings.laya.model,
                "cache_size": self.laya.cache_size,
                "cache_stats": {
                    "hits": self.laya.cache_stats.hits,
                    "misses": self.laya.cache_stats.misses,
                    "hit_rate": round(self.laya.cache_stats.hit_rate, 4),
                },
                "questions_hash": self.laya.questions_hash,
            },
            "rca": self.rca.stats(),
            "windower": self.windower.stats,
            "bus": self.bus.describe(),
            "elapsed_s": round(time.time() - self._started_at, 3),
        }

    #: Which id field identifies a record *of each topic*. Used to index the trail by the record's own
    #: id. A single "try every id field" loop is wrong and was the original bug here: a `Digest` carries
    #: `digest_id` only, but a `Decision` carries both `decision_id` and `digest_id`, so keying by
    #: whichever field matched first filed every decision under its *digest's* id and every alert under
    #: its decision's id. The walk then reported a digest as a decision and a decision as an alert —
    #: exactly the confusion an audit query exists to remove.
    _ID_FIELD = {
        Topic.EVENTS: "event_id",
        Topic.FEATURES: "digest_id",
        Topic.DECISIONS: "decision_id",
        Topic.ALERTS: "alert_id",
        Topic.RCA: "rca_id",
        Topic.OUTCOMES: "outcome_id",
    }

    def lineage_of(self, record_id: str) -> List[Dict[str, Any]]:
        """Walk the trail backwards from any record id: the audit query, implemented.

        Returns every record in the memory that this id depends on, transitively, in causal order.
        This answers "why did the system do that?" with evidence rather than a narrative, and it works
        because every record carries `lineage.upstream_ids`.

        `upstream_ids` is authoritative for edge direction; the id index is only used to *resolve* the
        ids it names. That ordering matters, because a record's `upstream_ids` sometimes names its
        dependency rather than its parent (a `Decision` points at the digest it judged, an `Alert` at
        both the decision and the digest), so the walk visits parents and dependencies alike — which is
        what an auditor wants: the full evidential basis for a decision, not just its parent chain.
        """
        index: Dict[str, Dict[str, Any]] = {}
        for topic, id_field in self._ID_FIELD.items():
            for record in self.bus.replay(self.settings.topic(topic)):
                value = record.value or {}
                identifier = value.get(id_field)
                if identifier:
                    index[str(identifier)] = {"topic": topic, "id": str(identifier), "record": value}

        chain: List[Dict[str, Any]] = []
        seen = set()
        queue = [str(record_id)]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            entry = index.get(current)
            if entry is None:
                continue
            chain.append(entry)
            lineage = entry["record"].get("lineage") or {}
            for upstream in lineage.get("upstream_ids") or []:
                if str(upstream) not in seen:
                    queue.append(str(upstream))
        # Oldest first: `queue` walks newest-to-oldest, and reversing puts the raw evidence before the
        # conclusion drawn from it, which is how the chain reads.
        chain.reverse()
        return chain


def Severity_for(decision: Decision) -> Severity:
    """Fallback severity when Laya's `score` answer was unusable.

    Ranking *up* from the verdict rather than defaulting to `info`: an anomaly with an
    unreadable severity is not a low-severity anomaly, it is an unknown one, and the safe
    default for an unknown anomaly is the middle of the scale where a human looks at it.
    """
    if not decision.is_anomaly:
        return Severity.INFO
    return Severity.MAJOR


__all__ = ["Pipeline", "Counters", "WindowTrace", "Severity_for"]
