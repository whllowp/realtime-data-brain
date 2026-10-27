"""Synthetic telemetry scenarios — the ground truth the detector is scored against.

Seven scenarios, each one built to be *discriminated from the others* rather than merely to
look anomalous. That is the whole design constraint: a detector that can only say "something is
wrong" is not useful, and a benchmark made of obviously-broken windows would not reveal the
difference. So each scenario pairs a primary signal with a deliberate confuser:

    normal                weekly traffic variation (rises 40%) with a flat error fraction
    resource_exhaustion   CPU/worker saturation, errors rising, errors *local*
    dependency_failure    DB pool saturated + one dominant timeout template, low local CPU
    traffic_surge         throughput up 12x with error fraction and p50 latency flat
    data_quality          schema-validation failures and consumer lag, service otherwise healthy
    security             auth-failure flood with the service itself healthy
    config_or_deploy      a deploy recorded inside the window, errors up modestly afterwards

The confusers are the point. `normal` and `traffic_surge` both show throughput up sharply;
`resource_exhaustion` and `dependency_failure` both show latency and errors up. A detector that
scored well without distinguishing them would be fitting the label rather than the evidence.

Each window carries `expected`: the label a human reviewer would give. `brian eval` scores the
live System 1 against it, which is how "is this checkpoint good enough to gate automation?"
becomes a measurement instead of an opinion.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import Event, Severity

#: Per-scenario (category, severity, is_anomaly) ground truth.
EXPECTED: Dict[str, Dict[str, Any]] = {
    "normal": {"is_anomaly": False, "category": None, "severity": None},
    "traffic_surge": {"is_anomaly": True, "category": "traffic_surge", "severity": "minor"},
    "resource_exhaustion": {"is_anomaly": True, "category": "resource_exhaustion", "severity": "critical"},
    "dependency_failure": {"is_anomaly": True, "category": "dependency_failure", "severity": "major"},
    "data_quality": {"is_anomaly": True, "category": "data_quality", "severity": "major"},
    "security": {"is_anomaly": True, "category": "security", "severity": "major"},
    "config_or_deploy": {"is_anomaly": True, "category": "config_or_deploy", "severity": "minor"},
}

#: The scenario sequence a demo/replay run uses. Interleaved deliberately: a detector evaluated
#: only on long runs of one scenario would never be tested on the transition it must handle.
DEFAULT_SEQUENCE: Tuple[str, ...] = (
    "normal",
    "normal",
    "dependency_failure",
    "dependency_failure",
    "normal",
    "traffic_surge",
    "normal",
    "resource_exhaustion",
    "resource_exhaustion",
    "normal",
    "data_quality",
    "normal",
    "security",
    "config_or_deploy",
    "normal",
)


@dataclass
class LabeledWindow:
    """One generated window: its events, and what a human would have said about it.

    `scored=False` marks a warm-up window: it is fed through the pipeline so the baseline and the
    windowing machinery see it, but it is excluded from scoring. Warm-up windows exist because a
    baseline needs history before a comparison means anything, and scoring them would penalise the
    detector for having no history yet.
    """

    service: str
    window_start: datetime
    scenario: str
    events: List[Event] = field(default_factory=list)
    expected: Dict[str, Any] = field(default_factory=dict)
    scored: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "window_start": self.window_start.isoformat().replace("+00:00", "Z"),
            "scenario": self.scenario,
            "expected": self.expected,
            "scored": self.scored,
            "event_ids": [e.event_id for e in self.events],
            "event_count": len(self.events),
        }


def _healthy_payload(rng: random.Random, mild_variation: float = 0.0) -> Dict[str, Any]:
    """A plausible healthy minute, with an optional mild upward traffic drift.

    `mild_variation` exists so the `normal` scenario is not a flat line: real healthy traffic
    has a daily shape, and a detector tuned on a perfectly flat baseline is being tested on
    something easier than production.
    """
    load = 640 * (1.0 + mild_variation)
    return {
        "http_requests_total": load + rng.uniform(-40, 40),
        "http_errors_total": rng.choice([0, 0, 0, 1]),
        "http_request_duration_ms": max(1.0, rng.gauss(120, 25)),
        "cpu_util": min(0.95, max(0.05, rng.gauss(0.42, 0.04))),
        "memory_util": min(0.95, max(0.05, rng.gauss(0.61, 0.03))),
        "thread_pool_util": min(0.99, max(0.01, rng.gauss(0.35, 0.05))),
        "db_pool_util": min(0.99, max(0.01, rng.gauss(0.33, 0.05))),
        "upstream_errors_total": 0 if rng.random() > 0.05 else 1,
        "upstream_duration_ms": max(5.0, rng.gauss(90, 20)),
        "auth_failures_total": 0 if rng.random() > 0.08 else 1,
    }


def _window_events(scenario: str, service: str, start: datetime, rng: random.Random,
                   samples: int = 60) -> List[Event]:
    """Generate one window of a scenario. One sample per second by default."""
    events: List[Event] = []
    step = 60.0 / max(samples, 1)

    def metric(t: datetime, payload: Mapping[str, Any]) -> None:
        events.append(Event(event_time=t, service=service, kind="metric", payload=dict(payload),
                            ingest_time=t + timedelta(seconds=rng.uniform(0.2, 2.5)), source="demo"))

    def log(t: datetime, message: str) -> None:
        events.append(Event(event_time=t, service=service, kind="log", payload={"error": message},
                            ingest_time=t + timedelta(seconds=rng.uniform(0.1, 1.0)), source="demo"))

    for i in range(samples):
        t = start + timedelta(seconds=i * step)
        drift = (i / max(samples - 1, 1)) * 0.40 if scenario == "normal" else 0.0

        if scenario == "normal":
            metric(t, _healthy_payload(rng, mild_variation=drift))
            if rng.random() < 0.25:
                log(t, "INFO request completed status=200 duration=%dms path=/checkout" % rng.randint(60, 400))

        elif scenario == "traffic_surge":
            # Load up 12x, service healthy: error fraction and p50 latency stay flat. This is
            # the confuser for "high load == incident".
            payload = _healthy_payload(rng)
            payload["http_requests_total"] = 640 * 12 + rng.uniform(-500, 500)
            payload["http_errors_total"] = rng.choice([0, 1, 2])
            payload["http_request_duration_ms"] = max(1.0, rng.gauss(135, 30))
            payload["cpu_util"] = min(0.99, max(0.05, rng.gauss(0.74, 0.05)))
            metric(t, payload)

        elif scenario == "resource_exhaustion":
            # Local saturation: CPU pegged, worker pool exhausted, errors rising but *locally*
            # generated (no dominant upstream template).
            payload = _healthy_payload(rng)
            payload["http_requests_total"] = 900 + rng.uniform(-80, 80)
            payload["http_errors_total"] = 260 + rng.uniform(-40, 40)
            payload["http_request_duration_ms"] = max(50.0, rng.gauss(3100, 900))
            payload["cpu_util"] = min(0.999, 0.97 + rng.uniform(-0.01, 0.02))
            payload["thread_pool_util"] = min(0.999, 0.96 + rng.uniform(-0.02, 0.03))
            payload["memory_util"] = min(0.99, 0.88 + rng.uniform(-0.03, 0.05))
            payload["upstream_errors_total"] = 2
            metric(t, payload)
            if i % 6 == 0:
                log(t, "RejectedExecutionException: worker queue full, task dropped at ThreadPoolExecutor")
            if i % 11 == 0:
                log(t, "GC pause 1840ms exceeded the 500ms budget on heap generation=old")

        elif scenario == "dependency_failure":
            # The signature case: DB pool saturated while local CPU stays *low* (the service is
            # waiting, not working) and one timeout template dominates.
            payload = _healthy_payload(rng)
            payload["http_requests_total"] = 520 + rng.uniform(-60, 60)
            payload["http_errors_total"] = 210 + rng.uniform(-30, 30)
            payload["http_request_duration_ms"] = max(50.0, rng.gauss(8200, 2400))
            payload["cpu_util"] = min(0.6, max(0.2, rng.gauss(0.31, 0.05)))
            payload["db_pool_util"] = min(0.999, 0.99 + rng.uniform(-0.01, 0.005))
            payload["upstream_errors_total"] = 190 + rng.uniform(-20, 20)
            payload["upstream_duration_ms"] = max(50.0, rng.gauss(6500, 1200))
            metric(t, payload)
            if i % 4 == 0:
                log(t, "TimeoutException: connection acquisition from pool orders-db timed out after %dms"
                    % (5000 + rng.randint(-50, 400)))

        elif scenario == "data_quality":
            # The service is fine; the data is not. Errors come from validation, and lag builds.
            payload = _healthy_payload(rng)
            payload["schema_errors_total"] = 140 + rng.uniform(-20, 20)
            payload["consumer_lag"] = 42000 + rng.uniform(-3000, 9000)
            payload["http_errors_total"] = 40 + rng.uniform(-8, 8)
            metric(t, payload)
            if i % 8 == 0:
                log(t, "SchemaValidationError: field 'order_total' expected decimal, received string 'N/A'")

        elif scenario == "security":
            # Auth-failure flood: the service is healthy, the traffic is not legitimate.
            payload = _healthy_payload(rng)
            payload["auth_failures_total"] = 220 + rng.uniform(-30, 30)
            payload["http_errors_total"] = 150 + rng.uniform(-20, 20)
            metric(t, payload)
            if i % 7 == 0:
                log(t, "401 Unauthorized: invalid credentials for user=<str> from <ip>")

        elif scenario == "config_or_deploy":
            # A change recorded inside the window, with a modest post-change degradation. Modest
            # on purpose: this is the scenario that tests whether a small movement *plus* a
            # corroborating event is enough, rather than requiring a spike.
            payload = _healthy_payload(rng)
            if i >= samples // 4:
                payload["http_errors_total"] = 26 + rng.uniform(-5, 5)
                payload["http_request_duration_ms"] = max(1.0, rng.gauss(260, 60))
            if i == samples // 4:
                payload["deploy"] = "checkout-api v2.14.0 (rollout started)"
                payload["feature_flag"] = "new_pricing_engine=enabled"
            metric(t, payload)
            if i >= samples // 4 and i % 9 == 0:
                log(t, "NullPointerException: pricingEngine.calculate returned null for plan=<str>")
        else:
            raise ValueError("unknown scenario %r" % scenario)

    events.sort(key=lambda e: e.event_time)
    return events


def generate(sequence: Sequence[str] = DEFAULT_SEQUENCE, service: str = "checkout-api",
             start: Optional[datetime] = None, warmup_minutes: int = 0,
             seed: int = 20260114) -> List[LabeledWindow]:
    """Build a labelled window sequence.

    `warmup_minutes` prepends healthy windows that are *not* labelled and *not* returned for
    scoring: the baseline tracker needs history before a comparison means anything, and scoring
    the warm-up would penalise the detector for having no baseline yet.
    """
    rng = random.Random(seed)
    base = start or datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    windows: List[LabeledWindow] = []
    cursor = base

    for _ in range(warmup_minutes):
        windows.append(
            LabeledWindow(
                service=service,
                window_start=cursor,
                scenario="normal",
                events=_window_events("normal", service, cursor, rng),
                expected=dict(EXPECTED["normal"]),
                scored=False,
            )
        )
        cursor += timedelta(minutes=1)

    for scenario in sequence:
        windows.append(
            LabeledWindow(
                service=service,
                window_start=cursor,
                scenario=scenario,
                events=_window_events(scenario, service, cursor, rng),
                expected=dict(EXPECTED.get(scenario, EXPECTED["normal"])),
            )
        )
        cursor += timedelta(minutes=1)
    return windows


def flatten(windows: Sequence[LabeledWindow]) -> List[Event]:
    """All events across windows, in event-time order — what a replay produces."""
    events = [e for w in windows for e in w.events]
    events.sort(key=lambda e: e.event_time)
    return events


def expected_for(scenario: str) -> Dict[str, Any]:
    return dict(EXPECTED.get(scenario, EXPECTED["normal"]))


def severity_rank(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    return Severity.coerce(name).rank


__all__ = [
    "EXPECTED",
    "DEFAULT_SEQUENCE",
    "LabeledWindow",
    "generate",
    "flatten",
    "expected_for",
    "severity_rank",
]
