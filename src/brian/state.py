"""Flink's output: a deterministic window digest, and the digest *text* Laya conditions on.

This module is the most important non-obvious piece of the system.

Laya is an encoder over text. It does not compute; it reads. So a window's fate is decided
by **what the rendered text says**, not by the numbers that went into it. Two consequences
drive the whole design:

1. **The text is a versioned artefact, not string formatting.** `digest_version` is stamped
   on every digest, and the renderer lives here so it can be reviewed, diffed and unit
   tested. Changing a sentence changes model behaviour, and that must be a visible commit —   not an incidental edit inside a Flink operator.

2. **The deterministic part is separated from the interpretive part.** `metrics` holds the
   raw truth (baselines, ratios, robust z-scores) so a judgement is reproducible from the
   record alone; `interpretation` holds the rendered English. The model reads the second and
   the auditor reads the first, and both travel in the same record.

Rendering is also *budgeted*. The `english` checkpoint has `head_max_len=192` and
`max_len=512`, so a long document is truncated and the truncation silently removes evidence.
`MAX_DIGEST_WORDS` exists so the digest cannot grow into that failure mode as fields are
added; `render_digest` reports whether it had to drop anything, and the dropped lines are
listed in `metrics.render.dropped` rather than vanishing.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import Digest, Event, Lineage, iso, parse_iso, utc_now

#: Bump when the rendered text or the metric schema changes. Recorded on every digest, so
#: "why did accuracy move last Tuesday?" is answerable by diffing two renderers.
DIGEST_VERSION = "digest/1"

#: Version tag for the **host** rendering. Separate from `DIGEST_VERSION` because the two profiles are
#: different model inputs that happen to share a schema: a decision made on a host window cannot be
#: compared against one made on a service window, and the version string is what makes that visible in
#: the audit trail instead of silently mixing the two.
DIGEST_HOST_VERSION = "digest-host/1"

#: Hard budget for the rendered text. This is the *entire* state segment of the sequence: the
#: digest is sent to Laya as a plain string (see `LayaClient._state_payload` for the measured
#: reason a dict state is worse), and the question rubric is built into a *separate* head segment
#: (`head_max_len`), so the usable document budget is `max_len - head_max_len`:
#:
#:   english / typed-decisions  512 - 192 = ~320 tokens for the state
#:   multilingual              1024 - 256 = ~768 tokens
#:
#: 240 words is ~310 tokens in this register — close to the ceiling for the English checkpoint
#: and comfortable on `typed-decisions` (1024 context). `render_digest` reports anything it had to
#: drop rather than truncating silently.
MAX_DIGEST_WORDS = 240

#: Log lines are normalised before they are counted: numbers, uuids, hex ids and quoted
#: values become placeholders, so `timeout after 5000ms` and `timeout after 5123ms` are one
#: signature instead of two —which is what makes "one signature dominates the errors"
#: visible at all.
_NORMALISERS: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+(:\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?Z?\b"), "<ts>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|m|h|kb|mb|gb|%)\b", re.IGNORECASE), "<num><unit>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"'[^']{0,40}'"), "'<str>'"),
    (re.compile(r'"[^"]{0,40}"'), '"<str>"'),
    (re.compile(r"\s+"), " "),
)


# --------------------------------------------------------------------------------------
# small numeric helpers (no numpy: this must import on a bare interpreter)
# --------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile. `q` in [0, 1]. Returns None for an empty sample.

    Implemented here rather than pulled in from numpy because the digest must be computable
    identically in the Flink job, in a test, and in a replay script —one implementation,
    no version drift between them.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * min(max(q, 0.0), 1.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[int(position)])
    frac = position - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


def median_absolute_deviation(values: Sequence[float]) -> Optional[float]:
    """Median absolute deviation —the robust spread used for every z-score here.

    Deliberately not standard deviation. Telemetry is spiky, and one 60-second outage in a
    30-window baseline would inflate a standard deviation enough to hide the *next* outage.
    MAD is unmoved by a single wild window, which is exactly the property needed when the
    baseline is drawn from production traffic rather than a lab.
    """
    if not values:
        return None
    med = statistics.median(values)
    return float(statistics.median([abs(v - med) for v in values]))


def robust_z(current: float, baseline_median: float, mad: float) -> Optional[float]:
    """Deviation in robust sigmas (0.6745 scales MAD to a normal sigma). None if unstable.

    Returns None when MAD is zero —a perfectly flat baseline has no meaningful spread —or
    when the deviation is infinite. Callers render `None` as "no baseline", which is honest,
    rather than printing a huge number that would read as an extreme anomaly.
    """
    if mad <= 0:
        return None
    sigma = 1.4826 * mad
    if sigma <= 0 or not math.isfinite(sigma):
        return None
    z = (current - baseline_median) / sigma
    return z if math.isfinite(z) else None


def ratio(current: float, baseline: float) -> Optional[float]:
    """`current / baseline`, or None when the baseline cannot support a ratio."""
    if baseline == 0:
        return None
    value = current / baseline
    return value if math.isfinite(value) else None


def normalise_signature(message: str) -> str:
    """Collapse a log line to a stable template so like errors aggregate."""
    text = (message or "").strip()
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    return text.strip()[:180]


# --------------------------------------------------------------------------------------
# metric specification
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSpec:
    """One thing Flink measures per window.

    `key` is the dotted path inside `Digest.metrics`, `source` names the ingest field, and
    `kind` decides how the window reduces it:

        gauge     last value      (cpu_util, queue_depth, kafka lag)
        counter   rate per second (http_requests_total -> requests/s)
        samples   distribution    (http_request_duration_ms -> p50/p90/p99/max)
        ratio     fraction        (errors / requests, cache miss rate)

    `direction` is what makes a comparison meaningful: for `down` metrics a *fall* is the
    anomaly (throughput collapse, cache hit rate drop). Getting this wrong produces a
    detector that is confidently blind to half its incidents, which is why it is declared
    per metric instead of assumed.
    """

    key: str
    source: str
    kind: str  # gauge | counter | samples | ratio
    unit: str = ""
    direction: str = "up"  # up | down | either
    description: str = ""
    #: Name used in the rendered sentence. Kept separate from `key` so the metric schema can
    #: be refactored without silently changing what the model reads.
    label: str = ""
    #: How a gauge collapses to one number for the window: `last` (default), `max`, `min` or `mean`.
    #:
    #: Left configurable because the right reduction depends on the cadence, but the shipped host
    #: profile uses `last`: `max` was tried and is **worse** on SMD (AUC 0.743 vs 0.758, recall at a 10%
    #: false-positive budget 0.315 vs 0.409). An earlier round believed the opposite because a harness
    #: bug fed every metric the wrong history; the production path disagreed, which is what exposed it.
    #: Treat this as a knob to be measured per deployment, not a free improvement.
    reduce: str = "last"

    def __post_init__(self) -> None:
        if self.kind not in ("gauge", "counter", "samples", "ratio"):
            raise ValueError("MetricSpec.kind must be gauge|counter|samples|ratio, got %r" % self.kind)
        if self.direction not in ("up", "down", "either"):
            raise ValueError("MetricSpec.direction must be up|down|either, got %r" % self.direction)
        if self.reduce not in ("last", "max", "min", "mean"):
            raise ValueError("MetricSpec.reduce must be last|max|min|mean, got %r" % self.reduce)
        if not self.label:
            object.__setattr__(self, "label", self.key.replace(".", " ").replace("_", " "))


#: The default detector profile: a service-level health picture.
#:
#: Chosen so that each `IncidentCategory` has at least one metric that discriminates it —#: without that, the category question is unanswerable and fine-tuning cannot recover it:
#:
#:   resource_exhaustion  cpu_util, memory_util, thread_pool_util, disk_util
#:   dependency_failure   upstream_error_rate, dependency latency, connection pool
#:   traffic_surge        requests/s and concurrency *alongside* latency (surge = high load,
#:                        flat error rate; failure = high load, rising errors)
#:   data_quality         schema_errors, duplicate ratio, null ratio, consumer lag
#:   config_or_deploy     a deploy/flag event in the window correlated with the change
#:   security             auth failures, 401/403 rate, WAF blocks
#:   network              connect timeouts, DNS failures, retransmits
DEFAULT_METRICS: Tuple[MetricSpec, ...] = (
    MetricSpec("requests.rate", "http_requests_total", "counter", "req/s", "down",
               "request throughput", "throughput"),
    MetricSpec("errors.rate", "http_errors_total", "counter", "err/s", "up",
               "error throughput", "error rate"),
    MetricSpec("errors.ratio", "http_errors_total", "ratio", "fraction", "up",
               "fraction of requests that failed", "error fraction"),
    MetricSpec("latency_ms.p50", "http_request_duration_ms", "samples", "ms", "up",
               "median latency", "p50 latency"),
    MetricSpec("latency_ms.p90", "http_request_duration_ms", "samples", "ms", "up",
               "90th percentile latency", "p90 latency"),
    MetricSpec("latency_ms.p99", "http_request_duration_ms", "samples", "ms", "up",
               "99th percentile latency", "p99 latency"),
    MetricSpec("cpu.util", "cpu_util", "gauge", "fraction", "up", "CPU utilisation", "CPU"),
    MetricSpec("memory.util", "memory_util", "gauge", "fraction", "up",
               "memory utilisation", "memory"),
    MetricSpec("thread_pool.util", "thread_pool_util", "gauge", "fraction", "up",
               "worker thread pool utilisation", "thread pool"),
    MetricSpec("db_pool.util", "db_pool_util", "gauge", "fraction", "up",
               "database connection pool utilisation", "DB pool"),
    MetricSpec("upstream.error_ratio", "upstream_errors_total", "ratio", "fraction", "up",
               "fraction of upstream calls that failed", "upstream error fraction"),
    MetricSpec("upstream.latency_ms.p99", "upstream_duration_ms", "samples", "ms", "up",
               "99th percentile upstream call latency", "upstream p99"),
    MetricSpec("auth.failures_rate", "auth_failures_total", "counter", "fail/s", "up",
               "authentication failures", "auth failures"),
    MetricSpec("schema_errors.ratio", "schema_errors_total", "ratio", "fraction", "up",
               "fraction of records failing schema validation", "schema error fraction"),
    MetricSpec("consumer.lag", "consumer_lag", "gauge", "records", "up",
               "consumer group lag", "consumer lag"),
    MetricSpec("timeouts.rate", "connect_timeouts_total", "counter", "timeout/s", "up",
               "connection timeouts", "connect timeouts"),
)

#: Metric keys tracked by the rolling baseline. Quantiles for distributions, plus the
#: rate/ratio derived keys, so a spike is compared against the same quantity it was derived
#: from rather than against a differently-shaped number.
BASELINE_KEYS: Tuple[str, ...] = (
    "requests.rate",
    "errors.rate",
    "errors.ratio",
    "latency_ms.p50",
    "latency_ms.p90",
    "latency_ms.p99",
    "cpu.util",
    "memory.util",
    "thread_pool.util",
    "db_pool.util",
    "upstream.error_ratio",
    "upstream.latency_ms.p99",
    "auth.failures_rate",
    "schema_errors.ratio",
    "consumer.lag",
    "timeouts.rate",
)


#: Comparison keys that `render_digest` already has a dedicated sentence for. Anything outside this
#: set is stated in the `Other signals:` line instead of being dropped.
#:
#: This exists because of a real-data finding: on the SMD server dataset none of the HTTP-service
#: metrics are present, so every populated metric landed outside the hardcoded clauses and the rendered
#: state was two lines long — a header and a bare `Resources:` list. The model was asked "is anything
#: wrong?" with nothing to compare against and answered "normal" for every window. A metric without a
#: clause must still reach the model.
COVERED_COMPARISON_KEYS = frozenset(BASELINE_KEYS)


#: Resource metrics rendered as one `Resources:` line.
RESOURCE_KEYS: Tuple[str, ...] = ("cpu.util", "memory.util", "db_pool.util", "thread_pool.util")


#: An alternative metric profile for **host** telemetry: OS-level resource signals rather than
#: service-level request telemetry. Same pipeline, same gate, same questions — only the set of measured
#: things differs.
#:
#: Values are relative levels within the source series (host datasets are commonly shipped per-column
#: normalised), so they are rendered as plain numbers rather than percentages: calling a normalised 0.98
#: "98.00% CPU" would be a fabricated unit. Keys deliberately avoid the `.util` suffix for that reason.
#:
#: This is not a guess at what a host feed *should* contain; it is the smallest profile that makes a
#: real host dataset judgeable by the existing catalog, verified against SMD.
HOST_METRICS: Tuple[MetricSpec, ...] = (
    # `reduce` is left at `last`. Taking the window's worst sample instead was measured and rejected:
    # on SMD it lowered AUC (0.743 vs 0.758) and recall at a 10% false-positive budget (0.315 vs
    # 0.409). The intuition that a 5-minute window should report its extreme rather than its endpoint
    # is plausible and wrong here — the digest's value and its baseline must be reduced the same way,
    # and the model handles the endpoint series better.
    MetricSpec("host.cpu", "cpu_r", "gauge", "", "up", "CPU utilisation level", "CPU"),
    MetricSpec("host.cpu_system", "cpu_s", "gauge", "", "up", "system CPU share", "CPU system"),
    MetricSpec("host.load_1m", "load_1", "gauge", "", "up", "1-minute load average", "load 1m"),
    MetricSpec("host.mem_free", "mem_free", "gauge", "", "down", "free memory level", "free memory"),
    MetricSpec("host.swap_used", "swap_used", "gauge", "", "up", "swap in use", "swap used"),
    MetricSpec("host.disk_write", "disk_io_write", "gauge", "", "up", "disk write activity", "disk write"),
    MetricSpec("host.net_in", "net_in_bytes", "gauge", "", "either", "inbound network volume", "net in"),
    MetricSpec("host.net_out", "net_out_bytes", "gauge", "", "either", "outbound network volume", "net out"),
)

#: Baseline keys for `HOST_METRICS`.
HOST_BASELINE_KEYS: Tuple[str, ...] = tuple(spec.key for spec in HOST_METRICS)



# --------------------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------------------


@dataclass
class MetricValue:
    """One reduced metric: the current window plus its baseline comparison."""

    key: str
    label: str
    unit: str
    value: Optional[float] = None
    baseline: Optional[float] = None
    delta_ratio: Optional[float] = None
    z: Optional[float] = None
    direction: str = "up"
    n: int = 0
    #: Median over the *long* horizon (`baseline_windows_long`), when one is tracked.
    #:
    #: The short baseline answers "did this just move?", which is the right question for a spike and
    #: the wrong one for a fault longer than the baseline window: the rolling median follows the fault,
    #: so `CPU 0.92 against a baseline of 0.90` reads as normal. Measured on the SMD server dataset,
    #: lengthening the reference from 30 windows (2.5 h) to the full history moved ranking AUC from
    #: 0.562 to 0.772 with disjoint bootstrap intervals. The long median is kept *alongside* the short
    #: one rather than replacing it: a spike is only visible against a short horizon, a sustained shift
    #: only against a long one.
    long_baseline: Optional[float] = None
    long_delta_ratio: Optional[float] = None
    long_n: int = 0

    def to_dict(self) -> Dict[str, Any]:
        # `key` and `unit` are included so a consumer can render the value in its own unit without
        # re-deriving which metric it is looking at. The renderer needs exactly that: `error fraction
        # 0.05% against a baseline of 0.0003` asks the model to compare a percentage with a raw
        # fraction, and the answers measurably compressed when it had to. `label` travels too, so a
        # metric that has no dedicated sentence in `render_digest` can still be named in words rather
        # than as a dotted path.
        payload = {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "value": _round(self.value),
            "baseline": _round(self.baseline),
            "delta_ratio": _round(self.delta_ratio, 3),
            "z": _round(self.z, 2),
            "direction": self.direction,
            "n": self.n,
        }
        if self.long_baseline is not None:
            payload["long_baseline"] = _round(self.long_baseline)
            payload["long_delta_ratio"] = _round(self.long_delta_ratio, 3)
            payload["long_n"] = self.long_n
        return payload


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return round(float(value), digits)


@dataclass
class BaselineStats:
    """What "normal" looks like for one metric: a robust centre and spread."""

    median: Optional[float] = None
    mad: Optional[float] = None
    n: int = 0
    #: Centre over the long horizon, when the tracker was built with one.
    long_median: Optional[float] = None
    long_n: int = 0

    def to_dict(self) -> Dict[str, Any]:
        payload = {"median": _round(self.median), "mad": _round(self.mad), "n": self.n}
        if self.long_median is not None:
            payload["long_median"] = _round(self.long_median)
            payload["long_n"] = self.long_n
        return payload


class BaselineTracker:
    """Rolling per-(service, metric) baseline, keyed for Flink keyed state.

    Deliberately a plain dict of recent values rather than a streaming quantile sketch: the
    baseline is `baseline_windows` long (30 by default), so exact order statistics are both
    cheap and more accurate than an approximation. In the Flink job this dict is what lives
    in `ValueState`; keeping the algorithm identical in both places is worth more than the
    asymptotically better sketch.

    Tracks **two** horizons when `long_window` is positive:

    * `window` (short, default 30) — how a metric normally behaves *lately*. Needed for spikes: a
      sudden 5x jump only looks like a jump against recent history.
    * `long_window` — how it normally behaves *overall*. Needed for sustained faults: a rolling median
      follows a fault that outlasts its window, so the deviation collapses to 1.0x exactly when the
      fault is worst. On the SMD dataset the short-horizon reading had AUC 0.562 and the long-horizon
      reading 0.772.

    Neither replaces the other; `MetricValue` carries both and each renderer uses the horizon its own
    profile was measured with.
    """

    def __init__(self, window: int = 30, keys: Sequence[str] = BASELINE_KEYS,
                 long_window: int = 0) -> None:
        self.window = max(2, window)
        self.long_window = max(0, long_window) if long_window else 0
        self.keys = tuple(keys)
        self._history: Dict[str, Dict[str, List[float]]] = {}
        self._long: Dict[str, Dict[str, List[float]]] = {}

    def observe(self, service: str, metrics: Mapping[str, Any]) -> None:
        """Record one window's values as history for future windows."""
        bucket = self._history.setdefault(service, {k: [] for k in self.keys})
        long_bucket = self._long.setdefault(service, {k: [] for k in self.keys})
        for key in self.keys:
            value = _dig(metrics, key)
            if value is None:
                continue
            bucket[key].append(float(value))
            if len(bucket[key]) > self.window:
                del bucket[key][0]
            if self.long_window:
                long_bucket[key].append(float(value))
                if len(long_bucket[key]) > self.long_window:
                    del long_bucket[key][0]

    def stats(self, service: str, key: str) -> BaselineStats:
        values = self._history.get(service, {}).get(key) or []
        if not values:
            return BaselineStats()
        long_values = (self._long.get(service, {}).get(key) or []) if self.long_window else []
        return BaselineStats(
            median=float(statistics.median(values)),
            mad=median_absolute_deviation(values),
            n=len(values),
            long_median=float(statistics.median(long_values)) if long_values else None,
            long_n=len(long_values),
        )

    def service_history(self, service: str) -> Dict[str, BaselineStats]:
        return {k: self.stats(service, k) for k in self.keys}

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable form, for checkpointing into Flink `ValueState`."""
        payload = {
            "window": self.window,
            "history": {
                service: {k: list(v) for k, v in metrics.items()}
                for service, metrics in self._history.items()
            },
        }
        if self.long_window:
            # Carried in checkpoint state for the same reason as the short horizon: a restart must not
            # have to re-learn what "normal" meant, and the long horizon is the slower of the two to
            # rebuild.
            payload["long_window"] = self.long_window
            payload["long_history"] = {
                service: {k: list(v) for k, v in metrics.items()}
                for service, metrics in self._long.items()
            }
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], keys: Sequence[str] = BASELINE_KEYS) -> "BaselineTracker":
        tracker = cls(window=int(raw.get("window", 30)), keys=keys,
                      long_window=int(raw.get("long_window", 0) or 0))
        for service, metrics in (raw.get("history") or {}).items():
            tracker._history[str(service)] = {str(k): [float(x) for x in v] for k, v in (metrics or {}).items()}
        for service, metrics in (raw.get("long_history") or {}).items():
            tracker._long[str(service)] = {str(k): [float(x) for x in v] for k, v in (metrics or {}).items()}
        return tracker


def _dig(node: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


class ServiceAggregator:
    """Reduces one (service, window)'s events into a `Digest`.

    Stateful only in that it accumulates events; it holds no baseline and makes no
    judgement. Everything it produces is a pure function of the events it was given plus the
    baselines passed in, which is what makes a digest reproducible —and therefore makes an
    old decision re-derivable.
    """

    def __init__(self, service: str, window_start: datetime, window_end: datetime,
                 specs: Sequence[MetricSpec] = DEFAULT_METRICS,
                 min_events: int = 10) -> None:
        self.service = service
        self.window_start = window_start
        self.window_end = window_end
        self.specs = tuple(specs)
        self.min_events = min_events
        self.events: List[Event] = []
        self.event_ids: List[str] = []
        #: Counted separately from `len(self.events)` on purpose. The raw event list is *not* part
        #: of checkpointed state (it would grow without bound), so a resumed operator rebuilds the
        #: aggregate without it. Deriving `event_count` from the list made a restarted job emit
        #: digests that claimed zero events and rendered an empty summary — a silent, total loss of
        #: judgement after every failover. The counter is authoritative; the list is only a
        #: convenience for the in-process path.
        self.event_count: int = 0
        #: Log-line count is also a counter, for the same reason: it must survive checkpointing
        #: even though the log lines themselves do not.
        self.log_lines: int = 0
        self._samples: Dict[str, List[float]] = {}
        self._counters: Dict[str, List[Tuple[float, float]]] = {}   # source -> (t, value)
        self._gauges: Dict[str, List[Tuple[float, float]]] = {}
        self._signatures: Dict[str, int] = {}
        self._signature_example: Dict[str, str] = {}
        self._deploys: List[str] = []
        self._flags: List[str] = []

    # -- ingest ----------------------------------------------------------------
    def add(self, event: Event) -> None:
        self.events.append(event)
        self.event_count += 1
        if event.kind == "log":
            self.log_lines += 1
        if len(self.event_ids) < 5000:
            self.event_ids.append(event.event_id)
        t = event.event_time.timestamp()
        payload = event.payload or {}

        error = payload.get("error") or payload.get("message") or payload.get("log")
        if event.kind == "log" and error:
            signature = normalise_signature(str(error))
            if signature:
                self._signatures[signature] = self._signatures.get(signature, 0) + 1
                self._signature_example.setdefault(signature, str(error)[:240])

        for spec in self.specs:
            if spec.source not in payload:
                continue
            raw = payload.get(spec.source)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if spec.kind == "samples":
                self._samples.setdefault(spec.source, []).append(value)
            elif spec.kind == "counter":
                self._counters.setdefault(spec.source, []).append((t, value))
            else:
                self._gauges.setdefault(spec.source, []).append((t, value))

        for marker in ("deploy", "deployment", "config_change", "feature_flag", "schema_change"):
            if payload.get(marker):
                detail = str(payload[marker])
                target = self._deploys if marker in ("deploy", "deployment") else self._flags
                if detail not in target:
                    target.append(detail)

    # -- reduce ----------------------------------------------------------------
    @property
    def error_signature(self) -> Optional[Tuple[str, int, int]]:
        """The dominant error template, its count, and the total error count."""
        if not self._signatures:
            return None
        signature, count = max(self._signatures.items(), key=lambda kv: kv[1])
        return signature, count, sum(self._signatures.values())

    def reduce(self, baselines: Optional[BaselineTracker] = None) -> Dict[str, Any]:
        """Compute the metric block. Pure: same events in, same metrics out."""
        span_s = max((self.window_end - self.window_start).total_seconds(), 1e-6)
        metrics: Dict[str, Any] = {
            "service": self.service,
            "window_start": self.window_start.isoformat().replace("+00:00", "Z"),
            "window_end": self.window_end.isoformat().replace("+00:00", "Z"),
            "window_seconds": round(span_s, 3),
            "event_count": self.event_count,
            "log_lines": self.log_lines,
        }

        # counters -> per-second rate, using the shape of the counter (monotonic or not)
        for spec in self.specs:
            if spec.kind != "counter":
                continue
            points = sorted(self._counters.get(spec.source, []))
            value = _counter_rate(points, span_s)
            if value is not None:
                _set(metrics, spec.key, value)

        # distributions -> quantiles
        for spec in self.specs:
            if spec.kind != "samples":
                continue
            values = self._samples.get(spec.source, [])
            if not values:
                continue
            if spec.key.endswith(".p50"):
                _set(metrics, spec.key, percentile(values, 0.50))
            elif spec.key.endswith(".p90"):
                _set(metrics, spec.key, percentile(values, 0.90))
            elif spec.key.endswith(".p99"):
                _set(metrics, spec.key, percentile(values, 0.99))
            else:
                _set(metrics, spec.key, percentile(values, 0.95))
            _set(metrics, spec.key.rsplit(".", 1)[0] + ".max", max(values))
            _set(metrics, spec.key.rsplit(".", 1)[0] + ".n", len(values))

        # gauges -> reduced over the window by the spec's own rule
        #
        # `last` is the default because for a service metric sampled throughout a 60 s window the latest
        # reading is the current state, and it is what the service profile was measured with. For host
        # telemetry sampled once a minute into a 5-minute window it is the wrong reduction: it discards
        # four fifths of the window, so a fault that peaks mid-window and has cleared by the end renders
        # as clean. Measured on SMD at a fixed 10% false-positive budget, reducing the window by `max`
        # bought +0.086 recall (paired bootstrap CI [+0.032, +0.167]) over `last`, at a cost of 0.03 AUC.
        # A downgrade of the summary statistic that improves the operating point is the right trade for an
        # alerting system, so the reduction is per-metric rather than global.
        for spec in self.specs:
            if spec.kind != "gauge":
                continue
            points = sorted(self._gauges.get(spec.source, []))
            if not points:
                continue
            values = [value for _, value in points]
            if spec.reduce == "max":
                _set(metrics, spec.key, max(values))
            elif spec.reduce == "min":
                _set(metrics, spec.key, min(values))
            elif spec.reduce == "mean":
                _set(metrics, spec.key, sum(values) / len(values))
            else:
                _set(metrics, spec.key, points[-1][1])

        # ratios -> numerator rate / denominator rate, computed from the same window
        for spec in self.specs:
            if spec.kind != "ratio":
                continue
            num = _counter_rate(sorted(self._counters.get(spec.source.replace("_total", "_total"), [])), span_s)
            if num is None:
                # fall back to a plain count in the window when the emitter sends raw totals
                num = _counter_rate(sorted(self._counters.get(spec.source, [])), span_s)
            if num is None:
                continue
            denominator_source = "http_requests_total" if spec.source != "http_requests_total" else None
            if denominator_source:
                den = _counter_rate(sorted(self._counters.get(denominator_source, [])), span_s)
                if den:
                    _set(metrics, spec.key, min(num / den, 1.0))
            else:
                # ratio of a rate against itself is meaningless; treat as a fraction of
                # *all* recorded error-ish counters in the window rather than inventing one
                total = sum(
                    v for k, v in _counter_rates(metrics).items() if k != spec.key
                ) or num
                _set(metrics, spec.key, min(num / total, 1.0) if total else None)

        # the dominant error signature is evidence, not a metric: store it whole
        sig = self.error_signature
        if sig:
            signature, count, total = sig
            metrics["error_signature"] = {
                "template": signature,
                "count": count,
                "total": total,
                "share": round(count / total, 4) if total else None,
                "example": self._signature_example.get(signature),
                "distinct": len(self._signatures),
            }

        if self._deploys:
            metrics["deploys"] = list(self._deploys)[:5]
        if self._flags:
            metrics["config_changes"] = list(self._flags)[:5]

        # provenance of the window
        #
        # `ingest_lag_s` is the one field that cannot survive a checkpoint, because it derives from
        # the individual events (`ingest_time`) and raw events are deliberately not carried in
        # checkpoint state. On the in-process path it is therefore present, and in a resumed Flink
        # operator it is absent — the digest text and every decision-relevant metric are identical
        # either way (verified by round-trip), and the renderer treats a missing lag as "not
        # measured" rather than as "no lag". It is kept for the local/replay path, where the events
        # are in hand and late-arrival evidence is worth having.
        lags = [e.lag_seconds for e in self.events if e.lag_seconds is not None]
        if lags:
            metrics["ingest_lag_s"] = {
                "max": round(max(lags), 3),
                "p50": round(percentile(lags, 0.5) or 0.0, 3),
            }

        # -- baseline comparison ------------------------------------------------
        comparison: Dict[str, Any] = {}
        for spec in self.specs:
            current = _dig(metrics, spec.key)
            if current is None:
                continue
            stats = baselines.stats(self.service, spec.key) if baselines else BaselineStats()
            entry = MetricValue(
                key=spec.key,
                label=spec.label,
                unit=spec.unit,
                value=float(current),
                baseline=stats.median,
                delta_ratio=ratio(float(current), stats.median) if stats.median is not None else None,
                z=robust_z(float(current), stats.median, stats.mad or 0.0) if stats.median is not None else None,
                direction=spec.direction,
                n=stats.n,
                long_baseline=stats.long_median,
                long_delta_ratio=(ratio(float(current), stats.long_median)
                                  if stats.long_median is not None else None),
                long_n=stats.long_n,
            )
            # Only meaningful comparisons are kept: a metric with no baseline yet stays in
            # `metrics` (flat) but is left out of the comparison block entirely, so the
            # renderer cannot accidentally describe a deviation it has no basis for.
            if entry.baseline is not None and entry.n >= 3:
                comparison[spec.key] = entry.to_dict()

        if comparison:
            metrics["comparison"] = _nest(comparison)
        metrics["baseline_samples"] = (
            max((v.get("n", 0) for v in comparison.values()), default=0) if comparison else 0
        )
        return metrics


def _nest(flat: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn `{"latency_ms.p99": {...}}` into `{"latency_ms": {"p99": {...}}}`.

    A representation detail with real consequences. The rest of `metrics` is nested (`_set` writes
    dotted keys as nested dicts), so `Digest.metric("latency_ms.p99")` reads them by walking the path.
    `comparison` was built with the *literal* key `"latency_ms.p99"`, which looks like the same thing
    when printed and is not: `metric("comparison.latency_ms.p99")` walks `comparison` → `latency_ms`
    → `p99` and finds nothing, returning `None` where a reader (or a script, or a test) expects a
    value. Nesting it removes the trap and makes every dotted path in the digest resolvable through
    one mechanism.
    """
    nested: Dict[str, Any] = {}
    for dotted, value in flat.items():
        node = nested
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return nested


def _set(metrics: Dict[str, Any], dotted: str, value: Any) -> None:
    node = metrics
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _counter_rate(points: Sequence[Tuple[float, float]], span_s: float) -> Optional[float]:
    """Turn a window's counter samples into a per-second rate.

    Handles both conventions an emitter may use, because a rate computed the wrong way is a
    silently wrong anomaly signal:

    * **monotonic counters** (Prometheus style): rate = (last - first) / elapsed. A reset
      (the value drops) is treated as a restart and the last value is used as the delta.
    * **pre-aggregated counts**: samples that decrease or repeat are treated as counts per
      scrape, and the mean is divided by the scrape interval.

    Distinguishing them matters: treating a pre-aggregated count as a monotonic counter
    yields a negative rate on a drop and a rate of zero on a flat-but-busy service.
    """
    if not points:
        return None
    if len(points) == 1:
        return float(points[0][1]) / span_s
    values = [v for _t, v in points]
    monotonic = all(b >= a for a, b in zip(values, values[1:])) or all(b <= a for a, b in zip(values, values[1:]))
    increasing = values[-1] > values[0]
    if monotonic and increasing:
        elapsed = points[-1][0] - points[0][0]
        delta = values[-1] - values[0]
        if elapsed > 0:
            return delta / elapsed
    # pre-aggregated count per scrape
    return (sum(values) / len(values)) / span_s


def _counter_rates(metrics: Mapping[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key in ("errors.rate", "auth.failures_rate", "timeouts.rate", "schema_errors.rate"):
        value = _dig(metrics, key)
        if isinstance(value, (int, float)):
            out[key] = float(value)
    return out


# --------------------------------------------------------------------------------------
# rendering: the text Laya actually reads
# --------------------------------------------------------------------------------------

#: Line order is fixed and meaningful: what the service is, then the two signals that
#: establish "is anything wrong" (errors, latency), then the resources that explain *why*,
#: then the single strongest piece of local evidence (the dominant error), then context that
#: changes the interpretation (deploys, load shape, late data).
#:
#: The order is not cosmetic. An encoder given a truncated document keeps the head, so the
#: most decision-relevant sentences are rendered first and the contextual ones last.



#: Metric key -> unit suffix, derived from `DEFAULT_METRICS` so the renderer cannot disagree with the
#: spec about what a number means. Built once at import: a metric whose unit changed in the spec must
#: change here too, and deriving it is how that stays true.
METRIC_UNITS: Dict[str, str] = {spec.key: spec.unit for spec in DEFAULT_METRICS}


def render_digest(metrics: Mapping[str, Any], max_words: int = MAX_DIGEST_WORDS,
                  profile: str = "service") -> Tuple[str, List[str]]:
    """Render the digest text: the state Laya actually reads. Returns (text, dropped_lines).

    **This function is a model parameter, not formatting.** Laya is an encoder over text, so these
    sentences *are* the decision input. Measured through the production evaluation path over six seeds,
    renderings of byte-identical metrics produced:

        this form (value + ratio, percentage baselines)   AUC 0.89   p in [0.34, 0.47]
        the same text with z-scores and sample counts      AUC 0.78   p in [0.41, 0.45]
        dotted-key ledger                                  AUC 0.00-0.78 depending on the run
        narrative plus restated numbers                    AUC 0.43

    Four rules follow from that, and each is a rule rather than a preference because violating it
    measurably changed the answers:

    1. **State the comparison, and only the comparison.** `against a baseline of 0.03% (2.00x)`.
       Adding the robust z-score and the sample count compressed the answers into a band where every
       window looked the same to the threshold.
    2. **Baselines are rendered in the same unit as their value.** A fraction against a fraction
       (`0.05% against 0.03%`), not against a raw `0.0003`.
    3. **One line per signal, plainly punctuated.** No semicolon-joined secondary statistics.
    4. **Say when no baseline exists.** A bare list of numbers reads like a healthy window; with no
       baseline it means nothing is known, which is the opposite.

    `dropped_lines` is returned rather than logged: a truncated digest means the model judged on
    partial evidence, and that must be visible in the record rather than inferred from a word count.
    """
    if profile == "host":
        return _render_host_digest(metrics, max_words)
    if profile != "service":
        raise ValueError("unknown digest profile %r (service|host)" % (profile,))
    lines: List[str] = []
    service = str(metrics.get("service") or "unknown-service")
    lines.append(
        "Service %s, a %s-second window with %s events."
        % (service, _num(metrics.get("window_seconds"), 0), metrics.get("event_count", 0))
    )

    comparison = metrics.get("comparison") or {}
    baseline_n = metrics.get("baseline_samples") or 0

    def cmp_entry(key: str) -> Optional[Mapping[str, Any]]:
        """Read one comparison entry by its dotted metric key.

        `comparison` is nested, so this is a path walk. A flat `.get("latency_ms.p99")` returns `None`
        and turns every line below into silence while looking like it checked — which is how this
        renderer once emitted a single sentence and the model scored AUC 0.02.
        """
        return _dig(comparison, key)

    # -- error signal: the primary discriminator ------------------------------
    error_ratio = cmp_entry("errors.ratio")
    if error_ratio and error_ratio.get("value") is not None:
        lines.append("Error fraction %s%s." % (_pct(error_ratio["value"]), _deviation(error_ratio)))

    # -- latency signal -------------------------------------------------------
    # Median is dropped rather than appended: it added a figure the model has to weigh against the
    # same window's p99, and the measured rendering that scored best stated one latency per line.
    p99 = cmp_entry("latency_ms.p99")
    if p99 and p99.get("value") is not None:
        lines.append("Latency %s ms%s." % (_num(p99["value"], 1), _deviation(p99)))

    # -- resources: what would explain *why* ---------------------------------
    # Bare values, no per-resource baseline, for the *service* profile. That is measured, not stylistic:
    # adding `(baseline 43.00%, 2.28x)` to each resource on the HTTP scenario suite raised the
    # false-positive rate from 0.000 to 1.000 — every healthy window started firing — because the
    # extra percentages shifted the model's whole probability band without adding discrimination.
    # Host telemetry has the opposite requirement (no error/latency lines exist, so resources are the
    # only evidence and must carry their baselines), which is why it gets its own profile below
    # instead of compromising this one.
    resources = []
    for key in RESOURCE_KEYS:
        entry = cmp_entry(key)
        if entry and entry.get("value") is not None:
            resources.append("%s %s" % (entry_label(key), _pct(entry["value"])))
    if resources:
        lines.append("Resources: %s." % ", ".join(resources))

    # -- anything else the catalog does not have a sentence for --------------
    # A metric with no dedicated clause used to be dropped silently, which is how a host-metric feed
    # rendered as a single unjudgeable line. Whatever is populated gets stated, with its comparison.
    # For the service profile this never fires (every HTTP metric has a clause); it exists so a feed
    # that adds a metric is not silently ignored.
    covered = COVERED_COMPARISON_KEYS
    extras = []
    for key in _comparison_keys(comparison):
        if key in covered:
            continue
        entry = cmp_entry(key)
        if not isinstance(entry, Mapping) or entry.get("value") is None:
            continue
        extras.append(_signal_fragment(str(entry.get("label") or key), entry))
    if extras:
        lines.append("Other signals: %s." % "; ".join(extras))

    # -- dependency and network signals --------------------------------------
    upstream = cmp_entry("upstream.error_ratio")
    if upstream and upstream.get("value") is not None:
        lines.append("Upstream calls failed at %s%s." % (_pct(upstream["value"]), _deviation(upstream)))
    timeouts = cmp_entry("timeouts.rate")
    if timeouts and timeouts.get("value") is not None and (timeouts["value"] or 0) > 0:
        lines.append("Connection timeouts at %s per second%s."
                     % (_num(timeouts["value"], 3), _deviation(timeouts)))

    # -- load shape: surge vs failure, in one sentence -----------------------
    # The pair is the discriminator: high load with flat errors is a surge, high load with rising
    # errors is a failure. Neither number alone can say which, so they share a sentence.
    load = cmp_entry("requests.rate")
    if load and load.get("value") is not None:
        sentence = "Throughput %s requests per second%s" % (_num(load["value"], 1), _deviation(load))
        if error_ratio and (error_ratio.get("delta_ratio") or 1.0) > 2 and (load.get("delta_ratio") or 1.0) > 1.2:
            sentence += ", with errors rising at the same time"
        elif load.get("delta_ratio") and load["delta_ratio"] > 1.5 and not error_ratio:
            sentence += ", with no matching rise in errors"
        lines.append(sentence + ".")

    # -- the strongest single piece of local evidence ------------------------
    signature = metrics.get("error_signature")
    if isinstance(signature, Mapping) and signature.get("template"):
        # Only the template, no share or distinct-count. The measured rendering that scored best
        # stated it bare; the extra statistics belong in `metrics`, where the RCA reasoner reads them.
        lines.append("The most frequent error is: %s" % str(signature.get("template"))[:180])

    # -- context that changes the interpretation -----------------------------
    if metrics.get("deploys"):
        lines.append("A deployment was recorded: %s." % "; ".join(metrics["deploys"])[:140])
    if metrics.get("config_changes"):
        lines.append("A configuration change was recorded: %s." % "; ".join(metrics["config_changes"])[:140])
    schema = cmp_entry("schema_errors.ratio")
    if schema and schema.get("value") is not None and (schema["value"] or 0) > 0:
        lines.append("Schema validation failed for %s of records." % _pct(schema["value"]))
    lag = cmp_entry("consumer.lag")
    if lag and lag.get("value") is not None and (lag["value"] or 0) > 0:
        lines.append("Consumer lag is %s records." % _num(lag["value"], 0))
    ingest = metrics.get("ingest_lag_s")
    if isinstance(ingest, Mapping) and (ingest.get("max") or 0) > 30:
        lines.append("Caution: telemetry arrived up to %s seconds late, so this window may be incomplete."
                     % _num(ingest.get("max"), 0))

    # -- do not imply a judgement that was never made ------------------------
    if not baseline_n or baseline_n < 3:
        lines.append(
            "No baseline is available yet for this service, so the values above are raw and no "
            "deviation can be stated."
        )

    # -- budget ---------------------------------------------------------------
    kept: List[str] = []
    dropped: List[str] = []
    used = 0
    for line in lines:
        words = len(line.split())
        if used + words > max_words:
            dropped.append(line)
            continue
        kept.append(line)
        used += words
    if dropped:
        kept.append("Note: %d further signals were omitted for length." % len(dropped))
    return "\n".join(kept), dropped


def _deviation(entry: Mapping[str, Any]) -> str:
    """`, against a baseline of X (N.NNx)` — the comparison, stated once, in the value's own unit.

    Both halves matter and both were arrived at by measurement:

    * **the unit**, because `Error fraction 0.05% against a baseline of 0.0003` asks the model to
      compare a percentage with a raw fraction, and the answers compressed when it had to;
    * **nothing else**, because appending z-scores and sample counts compressed them further, to the
      point where every window in the labelled set scored inside a 0.04-wide band and the detector's
      false-positive rate went to 1.000.
    """
    baseline = entry.get("baseline")
    if baseline is None:
        return ""
    key = str(entry.get("key") or "")
    text = " against a baseline of %s" % _same_unit(baseline, key, str(entry.get("unit") or ""))
    if entry.get("delta_ratio") is not None:
        text += " (%sx)" % _num(entry["delta_ratio"], 2)
    return text


def _render_host_digest(metrics: Mapping[str, Any], max_words: int = MAX_DIGEST_WORDS) -> Tuple[str, List[str]]:
    """Render a window of **host** telemetry: one line per measured signal, each with its baseline.

    This is a second profile rather than a tweak to the service one, because the two have incompatible
    requirements and both were measured:

    * **Service profile.** Error/latency/throughput lines carry the signal; resources are supporting
      detail. Adding per-resource baselines there raised the false-positive rate from 0.000 to 1.000 on
      the labelled HTTP suite — every healthy window started firing.
    * **Host profile.** There is no request rate, no error count and no latency, so the resource signals
      *are* the evidence. Without their baselines the model receives a header and a bare list of
      numbers: on the real SMD server dataset it read `Resources: CPU 19.75%, memory 0.00%.` and
      suppressed every window as confidently normal, including fault-injected ones. The state was not
      cautious, it was empty.

    Every line states `value (baseline B, Nx)` so nothing is left without a scale, and nothing else is
    stated. An earlier version closed with a count of how many signals had moved; that count is a
    verdict, and it cost 0.175 AUC (0.758 -> 0.583) on SMD. The model is asked for the verdict, so the
    state must not contain one.
    """
    lines: List[str] = []
    service = str(metrics.get("service") or "unknown-host")
    lines.append(
        "Host %s, a %s-second window with %s samples."
        % (service, _num(metrics.get("window_seconds"), 0), metrics.get("event_count", 0))
    )

    comparison = metrics.get("comparison") or {}
    baseline_n = metrics.get("baseline_samples") or 0

    rendered = 0
    ordered = [key for key in HOST_BASELINE_KEYS if _dig(comparison, key)]
    ordered += [key for key in _comparison_keys(comparison) if key not in set(HOST_BASELINE_KEYS)]
    for key in ordered:
        entry = _dig(comparison, key)
        if not isinstance(entry, Mapping) or entry.get("value") is None:
            continue
        label = str(entry.get("label") or key)
        # Prefer the long horizon when one was tracked. A host fault typically lasts far longer than a
        # 30-window baseline, which the rolling median simply follows; the long median keeps it visible.
        # When no long horizon exists this falls back to the short one, so the profile still works.
        reference = entry.get("long_baseline")
        reference_ratio = entry.get("long_delta_ratio")
        if reference is None:
            reference = entry.get("baseline")
            reference_ratio = entry.get("delta_ratio")
        lines.append("%s %s (baseline %s%s)."
                     % (label,
                        _num(entry["value"], 2),
                        _num(reference, 2),
                        ", %sx" % _num(reference_ratio, 2)
                        if reference_ratio is not None else ""))
        rendered += 1

    # No summarising sentence about how many signals moved. That was tried and measured: appending
    # "N of M measurements are outside +/-25% of their baseline" to an otherwise identical digest
    # dropped ranking AUC from 0.758 to 0.583 on SMD, and stating the converse ("all within range")
    # is the same kind of claim in the other direction. A verdict in the state pre-empts the verdict the
    # model is asked for — the same lesson as the `Resources:` line, which also lost accuracy the moment
    # it carried a pre-digested conclusion.
    if not baseline_n or baseline_n < 3:
        lines.append(
            "No baseline is available yet for this host (fewer than 3 prior windows), so the values "
            "above are raw and no deviation can be stated."
        )

    kept: List[str] = []
    dropped: List[str] = []
    used = 0
    for line in lines:
        words = len(line.split())
        if used + words > max_words:
            dropped.append(line)
            continue
        kept.append(line)
        used += words
    if dropped:
        kept.append("Note: %d further signals were omitted for length." % len(dropped))
    return "\n".join(kept), dropped


def _comparison_keys(node: Any, prefix: str = "") -> List[str]:
    """Flatten a nested `comparison` block into dotted metric keys.

    `comparison` is nested (`comparison.latency_ms.p99.value`), so the renderer's "anything not covered
    by a dedicated clause" pass needs the leaf paths, not the group names.
    """
    keys: List[str] = []
    if not isinstance(node, Mapping):
        return keys
    for name, value in node.items():
        path = "%s.%s" % (prefix, name) if prefix else str(name)
        if isinstance(value, Mapping) and "value" not in value:
            keys.extend(_comparison_keys(value, path))
        elif isinstance(value, Mapping):
            keys.append(path)
    return keys


def _signal_fragment(label: str, entry: Mapping[str, Any], percentage: bool = False) -> str:
    """`CPU 98.00% (baseline 43.00%, 2.28x)` — one metric with its comparison, in its own unit.

    The comparison is not optional decoration. A bare value gives the model no scale: on real host
    telemetry the shipped renderer emitted `Resources: CPU 19.75%, memory 0.00%.` and the model
    suppressed every window as confidently normal, because nothing in that sentence could be abnormal.
    """
    unit = str(entry.get("unit") or "")
    as_percent = percentage or unit == "fraction"
    value_text = _pct(entry.get("value")) if as_percent else _num(entry.get("value"), 2)
    text = "%s %s" % (label, value_text)
    baseline = entry.get("baseline")
    if baseline is None:
        return text
    baseline_text = _pct(baseline) if as_percent else _num(baseline, 2)
    details = ["baseline %s" % baseline_text]
    if entry.get("delta_ratio") is not None:
        details.append("%sx" % _num(entry["delta_ratio"], 2))
    return "%s (%s)" % (text, ", ".join(details))


def _same_unit(value: Any, key: str, unit: str = "") -> str:
    """Render a baseline in the same unit *and phrasing* as the value it is compared with.

    Reads the metric's declared `unit` rather than inferring the format from the key name. Inferring
    was wrong in a way that is hard to spot: `key.endswith(".ratio")` is true for `upstream.error_ratio`,
    so an upstream *fraction* was fine, but a bare-metric key like `latency_ms.p99` carries no unit in
    its name at all, and `_plain` then printed a duration as a bare number. The unit is declared once,
    in `DEFAULT_METRICS`, and travels in the comparison entry.
    """
    if unit == "fraction" or key.endswith(".util") or key.endswith(".ratio") or key.startswith("schema_errors"):
        return _pct(value)
    text = _plain(value)
    # A unit on a zero baseline would read as noise ("0 timeout/s"), but a *fraction* keeps its
    # percent sign even at zero: `0` and `0.00%` are different claims about the same number.
    if unit == "fraction":
        return _pct(value)
    if not unit or _is_zero(value):
        return text
    return "%s %s" % (text, unit)


def _is_zero(value: Any) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _deviation_note(comparison: Mapping[str, Any], keys: Sequence[str]) -> str:
    """Unused by design; kept so the intent is documented rather than rediscovered.

    An earlier renderer appended "with <metric> standing out (0.9x baseline, z=-3.1)" to the resource
    line. It was removed because it made the answers worse — counter-intuitive enough to be worth
    recording: naming the standout metric invites the model to treat whichever metric was named as *the*
    cause, regardless of which signal the window is actually about.
    """
    return ""


def entry_label(key: str) -> str:
    return {
        "cpu.util": "CPU",
        "memory.util": "memory",
        "thread_pool.util": "thread pool",
        "db_pool.util": "DB pool",
    }.get(key, key)


def _num(value: Any, digits: int) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        return "%d" % round(number)
    # Large values read better as plain words than as float noise in a sentence the model
    # has to parse: "8900.0 ms" invites a misreading that "8900 ms" does not.
    if abs(number) >= 1000:
        return "%d" % round(number)
    return ("%%.%df" % digits) % number


def _pct(value: Any) -> str:
    try:
        return "%.2f%%" % (float(value) * 100.0)
    except (TypeError, ValueError):
        return "n/a"






def _plain(value: Any) -> str:
    """A baseline rendered to one decimal place, matching how its own value is rendered.

    One decimal, not full float precision. `175.1689` printed next to a value shown as `180.4` invites
    the model to read the extra digits as significant and treat a 1.03x ratio as a large number; both
    sides of a comparison should look like the same kind of quantity.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return "%d" % int(number)
    if abs(number) >= 1000:
        return "%d" % round(number)
    return "%.1f" % number


# --------------------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------------------


def build_digest(aggregator: ServiceAggregator, baselines: Optional[BaselineTracker] = None,
                 version: Optional[str] = None, profile: str = "service") -> Digest:
    """Turn an aggregated window into the contract record Laya will judge.

    `profile` selects the rendering: `"service"` for HTTP-service telemetry (the shipped catalog) or
    `"host"` for OS-level resource telemetry. They are two measured artefacts, not one text bent to fit
    both — see `render_digest` for the numbers that forced the split.
    """
    metrics = aggregator.reduce(baselines)
    text, dropped = render_digest(metrics, profile=profile)
    version = version or (DIGEST_HOST_VERSION if profile == "host" else DIGEST_VERSION)
    render_meta: Dict[str, Any] = {"words": len(text.split()), "digest_version": version,
                                   "profile": profile}
    if dropped:
        # Recorded, not logged: a truncated digest means the model judged on partial
        # evidence, and that must be visible in the record rather than inferred later.
        render_meta["dropped"] = dropped
        render_meta["dropped_count"] = len(dropped)
    metrics["render"] = render_meta
    return Digest(
        service=aggregator.service,
        window_start=aggregator.window_start,
        window_end=aggregator.window_end,
        metrics=metrics,
        interpretation=text,
        # The aggregator's counter, not `len(aggregator.events)`: raw events are not carried in
        # checkpoint state, so a resumed operator has the count and not the list. Reading the list
        # here reintroduced exactly the bug the counter exists to prevent.
        event_count=aggregator.event_count,
        digest_version=version,
        lineage=Lineage(
            upstream_ids=list(aggregator.event_ids),
            producer="flink-digest",
            code_version=version,
        ),
    )


# --------------------------------------------------------------------------------------
# event-time windowing (the local twin of the Flink job)
# --------------------------------------------------------------------------------------


@dataclass
class Windower:
    """Tumbling/sliding event-time windows with watermarks, driven only by event time.

    This is the local implementation of what the Flink job does in `flink/job.py`, and it
    exists for two reasons: it makes the digest logic testable without a cluster, and it
    makes `brian replay` able to re-derive exactly the windows Flink produced from the same
    raw events. If these two ever disagreed, replay would be worthless —so the algorithm
    (assign by event time, close when `watermark > end + allowed_lateness`, drop anything
    older than that) is deliberately identical in both.

    Late events are counted, not silently discarded. A window that lost 40% of its events to
    lateness is a window whose judgement should be distrusted, and that is only knowable if
    the loss is recorded.
    """

    size_s: int = 60
    slide_s: int = 60
    allowed_lateness_s: int = 30
    specs: Sequence[MetricSpec] = DEFAULT_METRICS
    min_events: int = 10

    _open: Dict[Tuple[str, int], ServiceAggregator] = field(default_factory=dict)
    _max_event_time: Optional[datetime] = None
    events_seen: int = 0
    late_events: int = 0
    late_by_service: Dict[str, int] = field(default_factory=dict)
    min_events: int = 10

    # -- time ------------------------------------------------------------------
    def watermark(self) -> Optional[datetime]:
        if self._max_event_time is None:
            return None
        return self._max_event_time - timedelta(seconds=self.allowed_lateness_s)

    def _window_start(self, event_time: datetime) -> int:
        """Epoch second of the window this event belongs to —the newest window it fits."""
        epoch = int(event_time.timestamp())
        size = max(1, int(self.size_s))
        slide = max(1, int(self.slide_s))
        return (epoch // slide) * slide - max(0, size - slide)

    # -- ingest ----------------------------------------------------------------
    def add(self, event: Event) -> None:
        start = self._window_start(event.event_time)
        key = (event.service, start)
        watermark = self.watermark()
        if watermark is not None and event.event_time < watermark:
            self.late_events += 1
            self.late_by_service[event.service] = self.late_by_service.get(event.service, 0) + 1
        self.events_seen += 1
        if self._max_event_time is None or event.event_time > self._max_event_time:
            self._max_event_time = event.event_time
        aggregator = self._open.get(key)
        if aggregator is None:
            begin = datetime.fromtimestamp(start, tz=event.event_time.tzinfo)
            aggregator = ServiceAggregator(
                service=event.service,
                window_start=begin,
                window_end=begin + timedelta(seconds=self.size_s),
                specs=self.specs,
                min_events=self.min_events,
            )
            self._open[key] = aggregator
        aggregator.add(event)

    # -- close -----------------------------------------------------------------
    def ready(self) -> List[ServiceAggregator]:
        """Windows whose watermark has passed, oldest first.

        Oldest-first is a requirement, not a preference: baselines are built in event-time
        order, so closing out of order would compare a window against its own future.
        """
        watermark = self.watermark()
        if watermark is None:
            return []
        closed: List[Tuple[Tuple[str, int], ServiceAggregator]] = []
        for key, aggregator in list(self._open.items()):
            if aggregator.window_end <= watermark:
                closed.append((key, aggregator))
        closed.sort(key=lambda kv: (kv[1].window_end, kv[0][0]))
        for key, aggregator in closed:
            self._open.pop(key, None)
        return [aggregator for _key, aggregator in closed]

    def flush(self) -> List[ServiceAggregator]:
        """Close everything still open. Used at end-of-stream/shutdown.

        Partial windows are included on purpose: dropping the last window of a replay would
        hide exactly the incident the replay was run to explain. The digest carries
        `window_seconds` and `event_count` so a partial window is visible downstream.
        """
        closed = sorted(self._open.values(), key=lambda a: (a.window_end, a.service))
        self._open.clear()
        return closed

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "events_seen": self.events_seen,
            "late_events": self.late_events,
            "late_by_service": dict(self.late_by_service),
            "open_windows": len(self._open),
            "watermark": self.watermark().isoformat().replace("+00:00", "Z") if self.watermark() else None,
        }

    # -- checkpointing ---------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Serialise the open window into the JSON that lives in Flink `ValueState`.

        The open window's *raw events* are not serialised: what a Flink operator needs in state is
        the aggregate being accumulated (plus the timestamps that decide when it closes), and
        re-inserting whole events into checkpoint state would grow it without bound. So the state
        carries the aggregate as a serialised `ServiceAggregator`.
        """
        return {
            "size_s": self.size_s,
            "slide_s": self.slide_s,
            "allowed_lateness_s": self.allowed_lateness_s,
            "max_event_time": iso(self._max_event_time),
            "events_seen": self.events_seen,
            "late_events": self.late_events,
            "late_by_service": dict(self.late_by_service),
            "open": [
                {
                    "key": list(key),
                    "aggregator": _aggregator_to_dict(aggregator),
                }
                for key, aggregator in self._open.items()
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], specs: Sequence[MetricSpec] = DEFAULT_METRICS) -> "Windower":
        windower = cls(
            size_s=int(raw.get("size_s", 60)),
            slide_s=int(raw.get("slide_s", 60)),
            allowed_lateness_s=int(raw.get("allowed_lateness_s", 30)),
            specs=specs,
            min_events=int(raw.get("min_events", 10)),
        )
        windower._max_event_time = parse_iso(raw.get("max_event_time"))
        windower.events_seen = int(raw.get("events_seen", 0) or 0)
        windower.late_events = int(raw.get("late_events", 0) or 0)
        windower.late_by_service = {str(k): int(v) for k, v in (raw.get("late_by_service") or {}).items()}
        for entry in raw.get("open") or []:
            key = entry.get("key") or []
            if len(key) != 2:
                continue
            windower._open[(str(key[0]), int(key[1]))] = _aggregator_from_dict(entry["aggregator"], specs)
        return windower

    def pop(self, service: str, window_start: datetime) -> Optional[ServiceAggregator]:
        """Remove and return one in-flight window, so it is not emitted twice.

        Needed by the Flink job: it rebuilds the windower from checkpoint state on each invoke, so
        an already-emitted window must be removed explicitly or it would be published again on
        every subsequent event of that key.
        """
        key = (service, int(window_start.timestamp()))
        return self._open.pop(key, None)


def _aggregator_to_dict(aggregator: ServiceAggregator) -> Dict[str, Any]:
    """Serialise a partial aggregate for checkpointed state."""
    return {
        "service": aggregator.service,
        "window_start": iso(aggregator.window_start),
        "window_end": iso(aggregator.window_end),
        "event_count": aggregator.event_count,
        "log_lines": aggregator.log_lines,
        "event_ids": list(aggregator.event_ids),
        "samples": {k: list(v) for k, v in aggregator._samples.items()},
        "counters": {k: [list(p) for p in v] for k, v in aggregator._counters.items()},
        "gauges": {k: [list(p) for p in v] for k, v in aggregator._gauges.items()},
        "signatures": dict(aggregator._signatures),
        "signature_example": dict(aggregator._signature_example),
        "deploys": list(aggregator._deploys),
        "flags": list(aggregator._flags),
    }


def _aggregator_from_dict(raw: Mapping[str, Any], specs: Sequence[MetricSpec]) -> ServiceAggregator:
    aggregator = ServiceAggregator(
        service=str(raw["service"]),
        window_start=parse_iso(raw["window_start"]),  # type: ignore[arg-type]
        window_end=parse_iso(raw["window_end"]),  # type: ignore[arg-type]
        specs=specs,
    )
    aggregator.event_ids = [str(e) for e in (raw.get("event_ids") or [])]
    aggregator.event_count = int(raw.get("event_count", 0) or 0)
    aggregator.log_lines = int(raw.get("log_lines", 0) or 0)
    aggregator._samples = {str(k): [float(x) for x in v] for k, v in (raw.get("samples") or {}).items()}
    aggregator._counters = {
        str(k): [(float(a), float(b)) for a, b in v] for k, v in (raw.get("counters") or {}).items()
    }
    aggregator._gauges = {
        str(k): [(float(a), float(b)) for a, b in v] for k, v in (raw.get("gauges") or {}).items()
    }
    aggregator._signatures = {str(k): int(v) for k, v in (raw.get("signatures") or {}).items()}
    aggregator._signature_example = {str(k): str(v) for k, v in (raw.get("signature_example") or {}).items()}
    aggregator._deploys = [str(x) for x in (raw.get("deploys") or [])]
    aggregator._flags = [str(x) for x in (raw.get("flags") or [])]
    # `events` stays empty on purpose: the aggregate is what the digest needs, and carrying the raw
    # events through checkpoint state would grow without bound. The raw events remain in the
    # `events` topic, which is the point of storing them separately.
    return aggregator


def synthesize_normal_window(service: str, window_start: datetime, count: int = 120,
                             rng: Optional[Any] = None) -> List[Event]:
    """Helper for tests and demos: a plausible healthy window of events.

    Lives here rather than in the demo module so the *shape* of healthy traffic is defined
    next to the metric specs it must satisfy —if a metric is added, this is where a
    realistic normal distribution for it belongs.
    """
    import random

    rng = rng or random.Random(0)
    events: List[Event] = []
    for i in range(count):
        t = window_start + timedelta(seconds=i * (60.0 / max(count, 1)))
        payload = {
            "http_requests_total": 640 + rng.uniform(-40, 40),
            "http_errors_total": rng.choice([0, 0, 1]),
            "http_request_duration_ms": max(1.0, rng.gauss(120, 25)),
            "cpu_util": min(0.95, max(0.05, rng.gauss(0.42, 0.04))),
            "memory_util": min(0.95, max(0.05, rng.gauss(0.61, 0.03))),
            "db_pool_util": min(0.99, max(0.01, rng.gauss(0.35, 0.05))),
            "upstream_errors_total": 0 if rng.random() > 0.05 else 1,
            "upstream_duration_ms": max(5.0, rng.gauss(90, 20)),
        }
        kind = "metric"
        if rng.random() < 0.15:
            kind = "log"
            payload = {"error": "INFO request completed status=200 duration=%dms" % rng.randint(60, 400)}
        events.append(Event(event_time=t, service=service, kind=kind, payload=payload,
                            ingest_time=t + timedelta(seconds=rng.uniform(0.2, 2.0)), source="demo"))
    return events


def digest_age_seconds(digest: Digest, now: Optional[datetime] = None) -> float:
    """How stale a digest is at judgement time —surfaced in metrics, not used to judge."""
    reference = now or utc_now()
    return (reference - digest.window_end).total_seconds()
