"""The wire contract — the single definition of every record that crosses a boundary.

Four records carry the whole system:

    Event    what happened            (produced by ingest, consumed by Flink)
    Digest   what Flink computed      (produced by Flink, judged by Laya)
    Decision what Laya concluded      (produced by the gate, explained by the LLM)
    Alert    what an operator sees    (an escalation of a Decision)

Two properties are load-bearing:

* **Every record carries its lineage.** `lineage.upstream_ids` means any downstream record
  can be traced back to the raw events and the exact model version that produced it. That
  is what makes Kafka a *memory* rather than a log.
* **Deserialisation is defensive.** Records come off a topic that a different process — or
  a different version of this code — may have written. `from_dict` never raises
  `KeyError`/`TypeError` on a merely-missing optional field; it raises `ContractError`,
  which the pipeline turns into a DLQ record and goes on serving. A malformed record must
  never stop the stream.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

# --------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------


class ContractError(ValueError):
    """A record did not satisfy the wire contract.

    Raised on deserialisation and on construction-time validation. The pipeline's contract
    is: this error is *catchable and routable* (to the DLQ), never fatal to the stream.
    """


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (or pass through a datetime). Tolerant of trailing Z."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("not an ISO-8601 timestamp: %r" % (value,)) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    """A short, sortable-enough unique id, e.g. `dec_9f2c1a4b7d3e`."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ContractError("%s is missing required field %r" % (where, key))
    return mapping[key]


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("%s must be numeric, got %r" % (where, value)) from exc


def _as_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------------------


class Severity(str, Enum):
    """Operator-facing severity. `score` on a Laya `score` question maps onto this."""

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "minor": 1, "major": 2, "critical": 3}[self.value]

    @classmethod
    def from_rank(cls, rank: float) -> "Severity":
        """Map a continuous Laya `score` (expected level 0..3) onto a discrete level."""
        if rank < 0.5:
            return cls.INFO
        if rank < 1.5:
            return cls.MINOR
        if rank < 2.5:
            return cls.MAJOR
        return cls.CRITICAL

    @classmethod
    def coerce(cls, value: Any) -> "Severity":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ContractError("unknown severity %r" % (value,))


class IncidentCategory(str, Enum):
    """Laya's `choice` label space. Keep this under ~20 options (head token budget)."""

    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_FAILURE = "dependency_failure"
    TRAFFIC_SURGE = "traffic_surge"
    DATA_QUALITY = "data_quality"
    CONFIG_OR_DEPLOY = "config_or_deploy"
    SECURITY = "security"
    NETWORK = "network"
    NONE = "none"

    @classmethod
    def coerce(cls, value: Any) -> "IncidentCategory":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ContractError("unknown category %r" % (value,))


class Verdict(str, Enum):
    """Binary outcome of the `noul` question — the alerting decision itself."""

    ANOMALY = "anomaly"
    NORMAL = "normal"

    @classmethod
    def coerce(cls, value: Any) -> "Verdict":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ContractError("unknown verdict %r" % (value,))


class DecisionAction(str, Enum):
    """What the gate does with a decision. This is the *only* place automation is licensed.

    The ordering is deliberate: Laya decides how much the system is allowed to do on its
    own, and the LLM never gates this — it only reasons once a decision is escalated.
    """

    #: Anomaly below the notify threshold: recorded for scoring, not surfaced.
    OBSERVE = "observe"
    #: Anomaly above threshold with confident System 1 judgement: LLM writes the narrative.
    AUTO_TRIAGE = "auto_triage"
    #: Anomaly, but not confident enough to act on the class/severity: LLM must reason harder.
    DEEP_ANALYSIS = "deep_analysis"
    #: Severe: page a human in parallel with the LLM analysis.
    PAGE_HUMAN = "page_human"
    #: Judged normal and confident enough to leave alone.
    SUPPRESS = "suppress"

    @classmethod
    def coerce(cls, value: Any) -> "DecisionAction":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ContractError("unknown decision action %r" % (value,))


#: Actions that must reach a human. Used by the alert router and the metrics.
HUMAN_ACTIONS = frozenset({DecisionAction.PAGE_HUMAN})
#: Actions that carry an alert record at all.
ALERTING_ACTIONS = frozenset(
    {DecisionAction.AUTO_TRIAGE, DecisionAction.DEEP_ANALYSIS, DecisionAction.PAGE_HUMAN}
)


# --------------------------------------------------------------------------------------
# lineage
# --------------------------------------------------------------------------------------


@dataclass
class Lineage:
    """Provenance of one record: what it came from, and who produced it.

    `upstream_ids` is the load-bearing field. Walking it backwards from an `outcome` is how
    an operator answers "why did the system restart this service at 10:08?" with the exact
    raw events and model version that justified it.
    """

    upstream_ids: List[str] = field(default_factory=list)
    producer: str = "unknown"
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    code_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upstream_ids": list(self.upstream_ids),
            "producer": self.producer,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "code_version": self.code_version,
        }

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]], where: str = "lineage") -> "Lineage":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ContractError("%s must be an object" % where)
        upstream = raw.get("upstream_ids") or []
        if not isinstance(upstream, Sequence) or isinstance(upstream, (str, bytes)):
            raise ContractError("%s.upstream_ids must be a list" % where)
        return cls(
            upstream_ids=[str(u) for u in upstream],
            producer=str(raw.get("producer") or "unknown"),
            model_id=raw.get("model_id"),
            prompt_version=raw.get("prompt_version"),
            code_version=raw.get("code_version"),
        )


# --------------------------------------------------------------------------------------
# Event — the raw layer
# --------------------------------------------------------------------------------------


@dataclass
class Event:
    """One raw telemetry sample, exactly as observed. Never rewritten.

    `event_time` is the *observation* time and is what Flink windows on; `ingest_time` is
    when the pipeline saw it. Their difference is the end-to-end lag, and a large gap is
    itself a cause of misjudgement (late data), so both are kept.
    """

    event_time: datetime
    service: str
    kind: str = "metric"
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: new_id("evt"))
    ingest_time: Optional[datetime] = None
    source: str = "unknown"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.service:
            raise ContractError("Event.service must be non-empty")
        if not self.kind:
            raise ContractError("Event.kind must be non-empty")
        self.event_time = parse_iso(self.event_time)  # type: ignore[assignment]
        if self.ingest_time is not None:
            self.ingest_time = parse_iso(self.ingest_time)  # type: ignore[assignment]
        if not isinstance(self.payload, dict):
            raise ContractError("Event.payload must be an object")

    @property
    def lag_seconds(self) -> Optional[float]:
        if self.ingest_time is None:
            return None
        return (self.ingest_time - self.event_time).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_time": iso(self.event_time),
            "ingest_time": iso(self.ingest_time),
            "service": self.service,
            "kind": self.kind,
            "source": self.source,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Event":
        if not isinstance(raw, Mapping):
            raise ContractError("Event must be a JSON object")
        return cls(
            event_time=parse_iso(_require(raw, "event_time", "Event")),  # type: ignore[arg-type]
            service=str(_require(raw, "service", "Event")),
            kind=str(raw.get("kind") or "metric"),
            payload=dict(raw.get("payload") or {}),
            event_id=str(raw.get("event_id") or new_id("evt")),
            ingest_time=parse_iso(raw.get("ingest_time")),
            source=str(raw.get("source") or "unknown"),
            schema_version=int(raw.get("schema_version") or 1),
        )


# --------------------------------------------------------------------------------------
# Digest — what Flink computed
# --------------------------------------------------------------------------------------


@dataclass
class Digest:
    """A deterministic, windowed feature snapshot — the state Laya reasons over.

    The split between `metrics` and `interpretation` is a deliberate design choice:

    * `metrics` is the machine-readable truth, computed by Flink. Baselines, ratios and
      z-scores live here so the judgement is reproducible from the record alone.
    * `interpretation` is the rendered English sentence Laya actually conditions on.
      Laya is an encoder over text, so *what the text says* decides the answer. Rendering
      is therefore a versioned, reviewable artefact (`digest_version`), not ad-hoc
      string building — see `engine/state.py`.

    Laya is English-only on the `english`/`typed-decisions` checkpoints, so the digest is
    rendered in English regardless of the locale of the underlying log lines.
    """

    service: str
    window_start: datetime
    window_end: datetime
    metrics: Dict[str, Any] = field(default_factory=dict)
    interpretation: str = ""
    digest_id: str = field(default_factory=lambda: new_id("dgst"))
    event_count: int = 0
    digest_version: str = "digest/1"
    lineage: Lineage = field(default_factory=Lineage)

    def __post_init__(self) -> None:
        if not self.service:
            raise ContractError("Digest.service must be non-empty")
        self.window_start = parse_iso(self.window_start)  # type: ignore[assignment]
        self.window_end = parse_iso(self.window_end)  # type: ignore[assignment]
        if self.window_end < self.window_start:
            raise ContractError(
                "Digest window ends before it starts (%s -> %s)" % (self.window_start, self.window_end)
            )
        if not isinstance(self.metrics, dict):
            raise ContractError("Digest.metrics must be an object")
        if isinstance(self.lineage, Mapping):
            self.lineage = Lineage.from_dict(self.lineage)

    @property
    def window_key(self) -> str:
        """Stable identity of (service, window) — also the Kafka message key context."""
        return "%s|%s" % (self.service, iso(self.window_start))

    def metric(self, path: str, default: Any = None) -> Any:
        """Read a dotted metric path, e.g. `metric("error_rate.delta_ratio")`.

        Returns `default` rather than raising: a missing metric is a reason to judge more
        cautiously, never a reason to drop the window.
        """
        node: Any = self.metrics
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest_id": self.digest_id,
            "digest_version": self.digest_version,
            "service": self.service,
            "window_start": iso(self.window_start),
            "window_end": iso(self.window_end),
            "event_count": self.event_count,
            "metrics": self.metrics,
            "interpretation": self.interpretation,
            "lineage": self.lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Digest":
        if not isinstance(raw, Mapping):
            raise ContractError("Digest must be a JSON object")
        return cls(
            service=str(_require(raw, "service", "Digest")),
            window_start=parse_iso(_require(raw, "window_start", "Digest")),  # type: ignore[arg-type]
            window_end=parse_iso(_require(raw, "window_end", "Digest")),  # type: ignore[arg-type]
            metrics=dict(raw.get("metrics") or {}),
            interpretation=str(raw.get("interpretation") or ""),
            digest_id=str(raw.get("digest_id") or new_id("dgst")),
            event_count=int(raw.get("event_count") or 0),
            digest_version=str(raw.get("digest_version") or "digest/1"),
            lineage=Lineage.from_dict(raw.get("lineage"), "Digest.lineage"),
        )


# --------------------------------------------------------------------------------------
# Decision — what Laya concluded
# --------------------------------------------------------------------------------------


@dataclass
class Routing:
    """Which checkpoint answered, and why. Copied verbatim from Laya's response.

    Kept because it is the difference between a debuggable and an opaque decision: it
    records the *reason* the router picked a checkpoint (script detection, explicit model,
    workflow match) without re-running anything.
    """

    model: Optional[str] = None
    reason: Optional[str] = None
    repo: Optional[str] = None
    detection: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "reason": self.reason,
            "repo": self.repo,
            "detection": self.detection,
        }

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "Routing":
        if not isinstance(raw, Mapping):
            return cls()
        detection = raw.get("detection")
        return cls(
            model=raw.get("model"),
            reason=raw.get("reason"),
            repo=raw.get("repo"),
            detection=dict(detection) if isinstance(detection, Mapping) else None,
        )


@dataclass
class Decision:
    """One Laya verdict over one digest, with everything needed to audit or replay it.

    `questions` is stored alongside the answers on purpose. A model is only interpretable
    against the question it was asked: if the rubric changes, old decisions must still be
    readable in their original rubric, and `questions_hash` makes the change visible.
    """

    digest_id: str
    service: str
    verdict: Verdict
    action: DecisionAction
    confidence: float
    questions: Dict[str, Any] = field(default_factory=dict)
    answers: Dict[str, Any] = field(default_factory=dict)
    decision_id: str = field(default_factory=lambda: new_id("dec"))
    category: Optional[IncidentCategory] = None
    severity: Optional[Severity] = None
    severity_score: Optional[float] = None
    needs_human: Optional[bool] = None
    anomaly_probability: Optional[float] = None
    routing: Routing = field(default_factory=Routing)
    gate_reason: str = ""
    latency_ms: Optional[float] = None
    decided_at: datetime = field(default_factory=utc_now)
    cache_hit: bool = False
    questions_hash: Optional[str] = None
    lineage: Lineage = field(default_factory=Lineage)

    def __post_init__(self) -> None:
        if not self.digest_id:
            raise ContractError("Decision.digest_id must be non-empty")
        self.verdict = Verdict.coerce(self.verdict)
        self.action = DecisionAction.coerce(self.action)
        self.confidence = _as_float(self.confidence, "Decision.confidence")
        if not 0.0 <= self.confidence <= 1.0:
            raise ContractError("Decision.confidence must be within [0, 1], got %r" % self.confidence)
        if self.category is not None:
            self.category = IncidentCategory.coerce(self.category)
        if self.severity is not None:
            self.severity = Severity.coerce(self.severity)
        if self.severity_score is not None:
            self.severity_score = _as_float(self.severity_score, "Decision.severity_score")
        self.decided_at = parse_iso(self.decided_at)  # type: ignore[assignment]
        if isinstance(self.routing, Mapping):
            self.routing = Routing.from_dict(self.routing)
        if isinstance(self.lineage, Mapping):
            self.lineage = Lineage.from_dict(self.lineage)

    @property
    def is_anomaly(self) -> bool:
        return self.verdict is Verdict.ANOMALY

    @property
    def severity_rank(self) -> int:
        return self.severity.rank if self.severity is not None else -1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "digest_id": self.digest_id,
            "service": self.service,
            "verdict": self.verdict.value,
            "action": self.action.value,
            "category": self.category.value if self.category else None,
            "severity": self.severity.value if self.severity else None,
            "severity_score": self.severity_score,
            "confidence": self.confidence,
            "anomaly_probability": self.anomaly_probability,
            "needs_human": self.needs_human,
            "gate_reason": self.gate_reason,
            "latency_ms": self.latency_ms,
            "decided_at": iso(self.decided_at),
            "cache_hit": self.cache_hit,
            "questions_hash": self.questions_hash,
            "questions": self.questions,
            "answers": self.answers,
            "routing": self.routing.to_dict(),
            "lineage": self.lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Decision":
        if not isinstance(raw, Mapping):
            raise ContractError("Decision must be a JSON object")
        category = raw.get("category")
        severity = raw.get("severity")
        needs_human = raw.get("needs_human")
        return cls(
            digest_id=str(_require(raw, "digest_id", "Decision")),
            service=str(_require(raw, "service", "Decision")),
            verdict=_require(raw, "verdict", "Decision"),
            action=_require(raw, "action", "Decision"),
            confidence=_require(raw, "confidence", "Decision"),
            questions=dict(raw.get("questions") or {}),
            answers=dict(raw.get("answers") or {}),
            decision_id=str(raw.get("decision_id") or new_id("dec")),
            category=IncidentCategory.coerce(category) if category else None,
            severity=Severity.coerce(severity) if severity else None,
            severity_score=_as_float_or_none(raw.get("severity_score")),
            needs_human=bool(needs_human) if needs_human is not None else None,
            anomaly_probability=_as_float_or_none(raw.get("anomaly_probability")),
            routing=Routing.from_dict(raw.get("routing")),
            gate_reason=str(raw.get("gate_reason") or ""),
            latency_ms=_as_float_or_none(raw.get("latency_ms")),
            decided_at=parse_iso(raw.get("decided_at")) or utc_now(),
            cache_hit=bool(raw.get("cache_hit") or False),
            questions_hash=raw.get("questions_hash"),
            lineage=Lineage.from_dict(raw.get("lineage"), "Decision.lineage"),
        )


# --------------------------------------------------------------------------------------
# Alert — what an operator sees
# --------------------------------------------------------------------------------------


@dataclass
class Alert:
    """A promoted Decision, routed to an owner.

    Deliberately a *thin* record that references its decision rather than duplicating it.
    The alert can then be re-routed, re-worded or closed without rewriting the judgement
    that justified it — the decision stays exactly as Laya made it.
    """

    decision_id: str
    service: str
    severity: Severity
    action: DecisionAction
    title: str
    alert_id: str = field(default_factory=lambda: new_id("alrt"))
    digest_id: Optional[str] = None
    category: Optional[IncidentCategory] = None
    confidence: Optional[float] = None
    owner: Optional[str] = None
    raised_at: datetime = field(default_factory=utc_now)
    dedup_key: Optional[str] = None
    lineage: Lineage = field(default_factory=Lineage)

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ContractError("Alert.decision_id must be non-empty")
        self.severity = Severity.coerce(self.severity)
        self.action = DecisionAction.coerce(self.action)
        if self.category is not None:
            self.category = IncidentCategory.coerce(self.category)
        if self.confidence is not None:
            self.confidence = _as_float(self.confidence, "Alert.confidence")
        self.raised_at = parse_iso(self.raised_at)  # type: ignore[assignment]
        if isinstance(self.lineage, Mapping):
            self.lineage = Lineage.from_dict(self.lineage)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "decision_id": self.decision_id,
            "digest_id": self.digest_id,
            "service": self.service,
            "severity": self.severity.value,
            "action": self.action.value,
            "category": self.category.value if self.category else None,
            "confidence": self.confidence,
            "title": self.title,
            "owner": self.owner,
            "raised_at": iso(self.raised_at),
            "dedup_key": self.dedup_key,
            "lineage": self.lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Alert":
        if not isinstance(raw, Mapping):
            raise ContractError("Alert must be a JSON object")
        category = raw.get("category")
        return cls(
            decision_id=str(_require(raw, "decision_id", "Alert")),
            service=str(_require(raw, "service", "Alert")),
            severity=_require(raw, "severity", "Alert"),
            action=_require(raw, "action", "Alert"),
            title=str(raw.get("title") or ""),
            alert_id=str(raw.get("alert_id") or new_id("alrt")),
            digest_id=raw.get("digest_id"),
            category=IncidentCategory.coerce(category) if category else None,
            confidence=_as_float_or_none(raw.get("confidence")),
            owner=raw.get("owner"),
            raised_at=parse_iso(raw.get("raised_at")) or utc_now(),
            dedup_key=raw.get("dedup_key"),
            lineage=Lineage.from_dict(raw.get("lineage"), "Alert.lineage"),
        )


# --------------------------------------------------------------------------------------
# RcaReport — what the LLM concluded, and what to do about it
# --------------------------------------------------------------------------------------


@dataclass
class RcaReport:
    """The System 2 output: root cause, evidence, and a recovery plan.

    This is the half of the system that is *allowed to be slow*. It is produced only for
    promoted alerts, and its job is to turn a Laya verdict ("dependency_failure, major,
    p=0.91") into something an operator can act on.

    `hypotheses` is ordered most-likely-first and each carries its own confidence, because
    a root cause asserted without alternatives is the failure mode this whole design exists
    to avoid. `actions` are *proposals*: `autonomous` marks the subset that is safe to
    execute without a human, and it is empty unless the operator opted in.
    """

    alert_id: str
    decision_id: str
    service: str
    summary: str
    root_cause: str
    rca_id: str = field(default_factory=lambda: new_id("rca"))
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    latency_ms: Optional[float] = None
    degraded: bool = False
    created_at: datetime = field(default_factory=utc_now)
    lineage: Lineage = field(default_factory=Lineage)

    def __post_init__(self) -> None:
        if not self.alert_id or not self.decision_id:
            raise ContractError("RcaReport requires both alert_id and decision_id")
        self.created_at = parse_iso(self.created_at)  # type: ignore[assignment]
        self.confidence = _as_float(self.confidence, "RcaReport.confidence")
        if isinstance(self.lineage, Mapping):
            self.lineage = Lineage.from_dict(self.lineage)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rca_id": self.rca_id,
            "alert_id": self.alert_id,
            "decision_id": self.decision_id,
            "service": self.service,
            "summary": self.summary,
            "root_cause": self.root_cause,
            "hypotheses": self.hypotheses,
            "evidence": self.evidence,
            "actions": self.actions,
            "confidence": self.confidence,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "latency_ms": self.latency_ms,
            "degraded": self.degraded,
            "created_at": iso(self.created_at),
            "lineage": self.lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RcaReport":
        if not isinstance(raw, Mapping):
            raise ContractError("RcaReport must be a JSON object")
        return cls(
            alert_id=str(_require(raw, "alert_id", "RcaReport")),
            decision_id=str(_require(raw, "decision_id", "RcaReport")),
            service=str(_require(raw, "service", "RcaReport")),
            summary=str(raw.get("summary") or ""),
            root_cause=str(raw.get("root_cause") or ""),
            rca_id=str(raw.get("rca_id") or new_id("rca")),
            hypotheses=list(raw.get("hypotheses") or []),
            evidence=[str(e) for e in (raw.get("evidence") or [])],
            actions=list(raw.get("actions") or []),
            confidence=_as_float_or_none(raw.get("confidence")) or 0.0,
            model_id=raw.get("model_id"),
            prompt_version=raw.get("prompt_version"),
            latency_ms=_as_float_or_none(raw.get("latency_ms")),
            degraded=bool(raw.get("degraded") or False),
            created_at=parse_iso(raw.get("created_at")) or utc_now(),
            lineage=Lineage.from_dict(raw.get("lineage"), "RcaReport.lineage"),
        )


# --------------------------------------------------------------------------------------
# Outcome — what was actually done, and whether it worked
# --------------------------------------------------------------------------------------


class OutcomeResult(str, Enum):
    """Whether the response worked. This is the label the learning loop is built on."""

    #: The incident was real and the action resolved it.
    RESOLVED = "resolved"
    #: The incident was real; the action did not resolve it (escalated or reverted).
    UNRESOLVED = "unresolved"
    #: The alert was a false positive — nothing was actually wrong.
    FALSE_ALARM = "false_alarm"
    #: A human confirmed the diagnosis but chose a different remedy.
    DIFFERENT_REMEDY = "different_remedy"
    #: Not yet determined.
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: Any) -> "OutcomeResult":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower().replace("-", "_")
        for member in cls:
            if member.value == text:
                return member
        raise ContractError("unknown outcome result %r" % (value,))

    @property
    def label(self) -> Optional[bool]:
        """The anomaly label this outcome implies, or None when it implies nothing.

        `FALSE_ALARM` is the only outcome that says the window was healthy; `RESOLVED` and
        `DIFFERENT_REMEDY` confirm it was anomalous. `UNRESOLVED` and `UNKNOWN` are deliberately
        excluded: an unresolved incident is still evidence that *something* was wrong, but a quiet
        one is evidence of nothing, and counting it as positive would inject noise into every
        calibration and evaluation that reads outcomes as labels.
        """
        if self is OutcomeResult.FALSE_ALARM:
            return False
        if self in (OutcomeResult.RESOLVED, OutcomeResult.DIFFERENT_REMEDY):
            return True
        return None


@dataclass
class Outcome:
    """The closing record of an incident: what was done, and whether it worked.

    This is the most valuable record the system produces, because it is the only one that carries a
    *human* judgement of whether the machine was right. Everything else is the system's opinion of
    itself. Specifically:

    * it is the label source for `brian calibrate` and `brian eval` (`OutcomeResult.label`),
    * it is the training data `brian export-training` needs for the questions the synthetic scenarios
      cannot answer (`blast_radius`, `needs_human`),
    * and it is the only place a false alarm is recorded as such, which is what makes the
      false-positive rate measurable in production rather than only in a synthetic benchmark.

    Referenced by `alert_id` and `decision_id` rather than duplicating them, so the outcome can be
    attached to a judgement that has already been written and never rewritten.
    """

    alert_id: str
    decision_id: str
    service: str
    result: OutcomeResult
    summary: str = ""
    outcome_id: str = field(default_factory=lambda: new_id("outc"))
    action_taken: Optional[str] = None
    operator: Optional[str] = None
    #: Which root cause turned out to be right, if the RCA proposed several. `0` is the top
    #: hypothesis; `None` means it was not recorded or none fitted.
    confirmed_hypothesis_index: Optional[int] = None
    #: Wall-clock duration of the incident, when the operator measured it.
    duration_s: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    recorded_at: datetime = field(default_factory=utc_now)
    lineage: Lineage = field(default_factory=Lineage)

    def __post_init__(self) -> None:
        if not self.alert_id or not self.decision_id:
            raise ContractError("Outcome requires both alert_id and decision_id")
        self.result = OutcomeResult.coerce(self.result)
        self.recorded_at = parse_iso(self.recorded_at)  # type: ignore[assignment]
        if self.duration_s is not None:
            self.duration_s = _as_float(self.duration_s, "Outcome.duration_s")
        if self.confirmed_hypothesis_index is not None:
            self.confirmed_hypothesis_index = int(self.confirmed_hypothesis_index)
        if isinstance(self.lineage, Mapping):
            self.lineage = Lineage.from_dict(self.lineage)

    @property
    def anomaly_label(self) -> Optional[bool]:
        """Whether this outcome may be used as a ground-truth anomaly label."""
        return self.result.label

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "alert_id": self.alert_id,
            "decision_id": self.decision_id,
            "service": self.service,
            "result": self.result.value,
            "anomaly_label": self.anomaly_label,
            "summary": self.summary,
            "action_taken": self.action_taken,
            "operator": self.operator,
            "confirmed_hypothesis_index": self.confirmed_hypothesis_index,
            "duration_s": self.duration_s,
            "notes": list(self.notes),
            "recorded_at": iso(self.recorded_at),
            "lineage": self.lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Outcome":
        if not isinstance(raw, Mapping):
            raise ContractError("Outcome must be a JSON object")
        return cls(
            alert_id=str(_require(raw, "alert_id", "Outcome")),
            decision_id=str(_require(raw, "decision_id", "Outcome")),
            service=str(_require(raw, "service", "Outcome")),
            result=_require(raw, "result", "Outcome"),
            summary=str(raw.get("summary") or ""),
            outcome_id=str(raw.get("outcome_id") or new_id("outc")),
            action_taken=raw.get("action_taken"),
            operator=raw.get("operator"),
            confirmed_hypothesis_index=raw.get("confirmed_hypothesis_index"),
            duration_s=_as_float_or_none(raw.get("duration_s")),
            notes=[str(n) for n in (raw.get("notes") or [])],
            recorded_at=parse_iso(raw.get("recorded_at")) or utc_now(),
            lineage=Lineage.from_dict(raw.get("lineage"), "Outcome.lineage"),
        )


# --------------------------------------------------------------------------------------
# serialisation entry points
# --------------------------------------------------------------------------------------

#: Contract name -> class, so a consumer can dispatch on a topic's record type.
RECORD_TYPES: Dict[str, Any] = {
    "Event": Event,
    "Digest": Digest,
    "Decision": Decision,
    "Alert": Alert,
    "RcaReport": RcaReport,
    "Outcome": Outcome,
}


def dumps(record: Any) -> str:
    """Serialise any contract record (or plain dict) to a compact JSON line."""
    payload = record.to_dict() if hasattr(record, "to_dict") else record
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def loads(raw: Any, record_type: Any) -> Any:
    """Parse a topic payload into `record_type`, raising ContractError on malformed input."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("payload is not valid JSON: %s" % exc) from exc
    if not isinstance(raw, Mapping):
        raise ContractError("payload must be a JSON object, got %s" % type(raw).__name__)
    kind = record_type if not isinstance(record_type, str) else RECORD_TYPES[record_type]
    return kind.from_dict(raw)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


def as_jsonable(record: Any) -> Dict[str, Any]:
    """to_dict() output with every nested value reduced to JSON-safe primitives."""
    return json.loads(dumps(record))


# Keep `asdict` importable from here for callers that want the raw dataclass view.
__all__ = [
    "ContractError",
    "utc_now",
    "iso",
    "parse_iso",
    "new_id",
    "Severity",
    "IncidentCategory",
    "Verdict",
    "DecisionAction",
    "HUMAN_ACTIONS",
    "ALERTING_ACTIONS",
    "Lineage",
    "Event",
    "Digest",
    "Routing",
    "Decision",
    "Alert",
    "RcaReport",
    "Outcome",
    "OutcomeResult",
    "RECORD_TYPES",
    "dumps",
    "loads",
    "as_jsonable",
    "asdict",
]
