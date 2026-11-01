"""Offline scoring: is this checkpoint good enough to gate automation on?

The question this module answers is not "how accurate is Laya?" — it is the question the gate
actually turns on:

    *Does this detector's confidence mean anything, on our data, right now?*

That framing decides what gets measured:

**Recall and false-positive rate are reported separately, never averaged.** A single "accuracy"
number hides the trade that matters. Missing a 62x error spike and paging on a healthy window are
both "one wrong answer" to an accuracy metric, and completely different in production. So the
report puts `recall` and `false_positive_rate` side by side.

**Per-scenario breakdown, always.** Aggregate accuracy hides a detector that catches every
resource-exhaustion window and misses every dependency failure. The per-scenario table is where
the confusion between `resource_exhaustion` and `dependency_failure` — the pair this catalog is
built to separate — becomes visible.

**Calibration (ECE) as a first-class metric.** The gate branches on confidence, so confidence has
to be *checkable*. Laya's own docs warn that the shipped temperatures are inherited from the base
checkpoint (ECE 0.213) and that probabilities must be refit on held-out data before automation is
gated on them. ECE is measured here, and `temperature.py` is what acts on it.

**Latency measured through the same boundary production uses.** The number that matters is the
full HTTP round trip the pipeline pays, not an in-process benchmark — the two differ by ~150 ms.

The honest failure mode of this module is a small sample. `n` is printed next to every rate, and
`confidence_interval` is given for recall and FPR, because a 0.9 recall on 10 positive windows is
not evidence and should not be read as if it were.
"""
from __future__ import annotations


import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .calibration import CalibrationFit
from .config import Settings
from .contracts import Severity
from .demo import LabeledWindow
from .laya.client import LayaClient, LayaError
from .laya.gate import apply_gate

from .state import BaselineTracker, Windower, build_digest


#: ECE bin count. 10 is the convention; more bins would make each estimate noisier at the
#: sample sizes this harness runs at.
ECE_BINS = 10


@dataclass
class WindowScore:
    """One window's prediction against its label, with everything needed to explain a miss."""

    scenario: str
    service: str
    window_start: datetime
    expected_anomaly: bool
    expected_category: Optional[str]
    expected_severity: Optional[str]
    predicted_anomaly: Optional[bool] = None
    predicted_probability: Optional[float] = None
    raw_probability: Optional[float] = None
    calibrated: bool = False
    predicted_category: Optional[str] = None
    predicted_severity: Optional[str] = None
    action: Optional[str] = None
    confidence: Optional[float] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    digest_text: str = ""

    @property
    def anomaly_correct(self) -> Optional[bool]:
        if self.predicted_anomaly is None:
            return None
        return self.predicted_anomaly == self.expected_anomaly

    @property
    def category_correct(self) -> Optional[bool]:
        # Only scored on windows that are genuinely anomalous: asking whether a healthy window's
        # category matches is meaningless, and counting it would inflate the number.
        if not self.expected_anomaly or self.predicted_category is None:
            return None
        return self.predicted_category == self.expected_category

    @property
    def severity_rank_error(self) -> Optional[float]:
        if not self.expected_anomaly or self.predicted_severity is None or not self.expected_severity:
            return None
        expected = Severity.coerce(self.expected_severity).rank
        return abs(Severity.coerce(self.predicted_severity).rank - expected)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "service": self.service,
            "window_start": self.window_start.isoformat().replace("+00:00", "Z"),
            "expected": {
                "is_anomaly": self.expected_anomaly,
                "category": self.expected_category,
                "severity": self.expected_severity,
            },
            "predicted": {
                "is_anomaly": self.predicted_anomaly,
                "probability": self.predicted_probability,
                "raw_probability": self.raw_probability,
                "calibrated": self.calibrated,
                "category": self.predicted_category,
                "severity": self.predicted_severity,
                "action": self.action,
                "confidence": self.confidence,
            },
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


@dataclass
class EvalReport:
    """The scored run, plus the metadata that makes the numbers interpretable."""

    model: str
    catalog_hash: str
    scores: List[WindowScore] = field(default_factory=list)
    warmup_windows: int = 0
    started_at: Optional[datetime] = None
    elapsed_s: float = 0.0
    settings_snapshot: Dict[str, Any] = field(default_factory=dict)

    # -- rates ---------------------------------------------------------------
    def _confusion(self) -> Dict[str, int]:
        tp = fp = tn = fn = 0
        for score in self.scores:
            if score.predicted_anomaly is None:
                continue
            if score.expected_anomaly and score.predicted_anomaly:
                tp += 1
            elif score.expected_anomaly and not score.predicted_anomaly:
                fn += 1
            elif not score.expected_anomaly and score.predicted_anomaly:
                fp += 1
            else:
                tn += 1
        return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}

    @property
    def recall(self) -> Optional[float]:
        c = self._confusion()
        return c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else None

    @property
    def false_positive_rate(self) -> Optional[float]:
        c = self._confusion()
        return c["fp"] / (c["fp"] + c["tn"]) if (c["fp"] + c["tn"]) else None

    @property
    def precision(self) -> Optional[float]:
        c = self._confusion()
        return c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else None

    @property
    def category_accuracy(self) -> Optional[float]:
        scored = [s for s in self.scores if s.category_correct is not None]
        return sum(1 for s in scored if s.category_correct) / len(scored) if scored else None

    @property
    def severity_mae(self) -> Optional[float]:
        errors = [s.severity_rank_error for s in self.scores if s.severity_rank_error is not None]
        return sum(errors) / len(errors) if errors else None

    @property
    def ece(self) -> Optional[float]:
        """Expected calibration error on the anomaly decision, after calibration.

        Bins windows by the model's own P(anomaly), and measures the gap between what it claimed
        and what happened. A well-calibrated model that says 0.8 is anomalous about 80% of the
        time. This is the number that decides whether the gate's thresholds can be trusted.
        """
        return _ece([(s.predicted_probability, s.expected_anomaly) for s in self.scores])

    @property
    def ece_raw(self) -> Optional[float]:
        """ECE on the *uncalibrated* probabilities.

        Reported alongside `ece` on purpose: it is the evidence for whether the temperature fit
        is doing anything. If raw and calibrated ECE are the same, the fit is not earning its
        place and should be dropped rather than kept as cargo.
        """
        return _ece([(s.raw_probability, s.expected_anomaly) for s in self.scores])

    @property
    def latency_p50(self) -> Optional[float]:
        values = sorted(s.latency_ms for s in self.scores if s.latency_ms)
        return values[len(values) // 2] if values else None

    @property
    def latency_p95(self) -> Optional[float]:
        values = sorted(s.latency_ms for s in self.scores if s.latency_ms)
        if not values:
            return None
        return values[min(len(values) - 1, int(math.ceil(0.95 * len(values))) - 1)]

    def per_scenario(self) -> Dict[str, Dict[str, Any]]:
        table: Dict[str, Dict[str, Any]] = {}
        for score in self.scores:
            row = table.setdefault(
                score.scenario,
                {"n": 0, "anomaly_correct": 0, "scored": 0, "category_correct": 0,
                 "category_scored": 0, "probabilities": [], "misses": []},
            )
            row["n"] += 1
            if score.anomaly_correct is not None:
                row["scored"] += 1
                row["anomaly_correct"] += 1 if score.anomaly_correct else 0
            if score.category_correct is not None:
                row["category_scored"] += 1
                row["category_correct"] += 1 if score.category_correct else 0
            if score.predicted_probability is not None:
                row["probabilities"].append(score.predicted_probability)
            if score.anomaly_correct is False:
                row["misses"].append(
                    {
                        "window": score.window_start.isoformat().replace("+00:00", "Z"),
                        "expected_anomaly": score.expected_anomaly,
                        "predicted_anomaly": score.predicted_anomaly,
                        "p": score.predicted_probability,
                        "predicted_category": score.predicted_category,
                        "expected_category": score.expected_category,
                        "action": score.action,
                    }
                )
        for scenario, row in table.items():
            row["anomaly_accuracy"] = (
                row["anomaly_correct"] / row["scored"] if row["scored"] else None
            )
            row["category_accuracy"] = (
                row["category_correct"] / row["category_scored"] if row["category_scored"] else None
            )
            row["mean_probability"] = (
                sum(row["probabilities"]) / len(row["probabilities"]) if row["probabilities"] else None
            )
        return table

    def to_dict(self) -> Dict[str, Any]:
        confusion = self._confusion()
        return {
            "model": self.model,
            "catalog_hash": self.catalog_hash,
            "n_windows": len(self.scores),
            "warmup_windows": self.warmup_windows,
            "confusion": confusion,
            "recall": _r(self.recall),
            "false_positive_rate": _r(self.false_positive_rate),
            "precision": _r(self.precision),
            "category_accuracy": _r(self.category_accuracy),
            "severity_mae_levels": _r(self.severity_mae),
            "ece_anomaly": _r(self.ece),
            "ece_anomaly_raw": _r(self.ece_raw),
            "calibrated": any(s.calibrated for s in self.scores),
            "latency_ms": {"p50": _r(self.latency_p50), "p95": _r(self.latency_p95)},
            "recall_ci95": _wilson(confusion["tp"], confusion["tp"] + confusion["fn"]),
            "fpr_ci95": _wilson(confusion["fp"], confusion["fp"] + confusion["tn"]),
            "elapsed_s": round(self.elapsed_s, 1),
            "scores": [s.to_dict() for s in self.scores],
        }


def _r(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _ece(pairs: Sequence[Tuple[Optional[float], bool]], bins: int = ECE_BINS) -> Optional[float]:
    """Binned expected calibration error over (probability, is_anomaly) pairs."""
    usable = [(float(p), 1.0 if y else 0.0) for p, y in pairs if p is not None]
    if len(usable) < 5:
        return None
    width = 1.0 / bins
    total = len(usable)
    ece = 0.0
    for index in range(bins):
        low, high = index * width, (index + 1) * width
        members = [
            (p, y)
            for p, y in usable
            if (low <= p < high) or (index == bins - 1 and p >= high)
        ]
        if not members:
            continue
        mean_confidence = sum(p for p, _y in members) / len(members)
        mean_accuracy = sum(y for _p, y in members) / len(members)
        ece += (len(members) / total) * abs(mean_confidence - mean_accuracy)
    return ece


def _wilson(successes: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    """Wilson score interval — the right interval for a rate at small n.

    The normal approximation reports a zero-width interval for 0/n and 1/n, which is exactly the
    case this harness hits most (a clean run, or a total miss). Wilson does not, so a 1.0 recall
    on 8 windows comes back as "somewhere between 0.68 and 1.0" instead of a false certainty.
    """
    if total == 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)) / denominator
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


class Evaluator:
    """Run the detector over labelled windows, through the real client and gate.

    Deliberately reuses the production path (`build_digest` → `LayaClient.decide` → `apply_gate`)
    rather than calling the model directly. A benchmark that takes a shortcut measures the
    shortcut.
    """

    def __init__(self, settings: Settings, laya: Optional[LayaClient] = None,
                 calibrator: Optional[CalibrationFit] = None,
                 apply_calibration_from_settings: bool = True,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        """`calibrator` defaults to the *configured* fit, so `brian eval` measures production.

        This default is load-bearing and was originally a trap. Passing `None` meant "no calibration",
        so an evaluation run with a calibrated threshold compared that threshold against *raw*
        probabilities — recall collapsed to 0.000 while every other number looked plausible, because a
        threshold on the wrong scale simply never fires. The only evidence was ECE reading identically
        for the raw and calibrated columns, which is now also surfaced explicitly in the report.

        `brian calibrate` passes `apply_calibration_from_settings=False` on purpose: it needs the raw
        model output to fit against.
        """
        self.settings = settings
        self.laya = laya if laya is not None else LayaClient(settings.laya)
        if calibrator is not None:
            self.calibrator = calibrator
        elif apply_calibration_from_settings:
            self.calibrator = settings.load_calibration()
        else:
            self.calibrator = None
        self._clock = clock

    def run(self, windows: Sequence[LabeledWindow], warmup: int = 0,
            on_window: Optional[Callable[[WindowScore], None]] = None) -> EvalReport:
        baselines = BaselineTracker(
            window=self.settings.window.baseline_windows,
            long_window=self.settings.window.baseline_windows_long,
        )
        windower = Windower(
            size_s=self.settings.window.size_s,
            slide_s=self.settings.window.slide_s,
            allowed_lateness_s=0,
            min_events=1,          # every labelled window must be judged, even a thin one
        )
        report = EvalReport(
            model=self.settings.laya.model,
            catalog_hash=self.laya.questions_hash,
            started_at=datetime.now(tz=windows[0].window_start.tzinfo) if windows else None,
            settings_snapshot=self.settings.masked(),
        )
        started = self._clock()

        # Map each window to its label by (service, window start) so the digest can be tied back
        # to the scenario it came from without threading state through the windower.
        labels: Dict[Tuple[str, int], LabeledWindow] = {
            (w.service, int(w.window_start.timestamp())): w for w in windows
        }

        for window in windows:
            for event in window.events:
                windower.add(event)

        for aggregator in sorted(windower.flush(), key=lambda a: (a.window_start, a.service)):
            digest = build_digest(aggregator, baselines)
            key = (digest.service, int(digest.window_start.timestamp()))
            label = labels.get(key)
            if label is None or not label.scored:
                # Warm-up windows still feed the baseline below, but they are not scored: the
                # detector has no history to compare against yet, so scoring them would measure
                # the absence of a baseline rather than the quality of the judgement.
                baselines.observe(digest.service, digest.metrics)
                continue
            score = WindowScore(
                scenario=label.scenario,
                service=digest.service,
                window_start=digest.window_start,
                expected_anomaly=bool(label.expected.get("is_anomaly")),
                expected_category=label.expected.get("category"),
                expected_severity=label.expected.get("severity"),
                digest_text=digest.interpretation,
            )
            try:
                result = self.laya.decide(digest)
                outcome = apply_gate(digest, result, self.settings.gate, calibrator=self.calibrator)
                decision = outcome.decision
                score.predicted_anomaly = decision.is_anomaly
                score.predicted_probability = decision.anomaly_probability
                score.raw_probability = outcome.signals.get("raw_anomaly_probability")
                score.calibrated = outcome.signals.get("calibration") is not None
                score.predicted_category = decision.category.value if decision.category else None
                score.predicted_severity = decision.severity.value if decision.severity else None
                score.action = decision.action.value
                score.confidence = decision.confidence
                score.latency_ms = result.latency_ms
            except LayaError as exc:
                score.error = "%s: %s" % (type(exc).__name__, exc)
            report.scores.append(score)
            if on_window is not None:
                on_window(score)
            # Only after judging: a window must not inform the baseline it is compared against.
            baselines.observe(digest.service, digest.metrics)

        report.warmup_windows = warmup
        report.elapsed_s = self._clock() - started
        return report


# --------------------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------------------


def format_report(report: EvalReport, verbose: bool = True) -> str:
    lines: List[str] = []
    payload = report.to_dict()
    lines.append("")
    lines.append("=" * 78)
    lines.append("System 1 evaluation — model=%s catalog=%s windows=%d" % (
        report.model, report.catalog_hash, len(report.scores)))
    lines.append("=" * 78)
    confusion = payload["confusion"]
    lines.append("confusion           tp=%(tp)d  fp=%(fp)d  tn=%(tn)d  fn=%(fn)d" % confusion)
    lines.append("recall              %s  (95%% CI %s)" % (
        _pct(payload["recall"]), payload["recall_ci95"]))
    lines.append("false positive rate %s  (95%% CI %s)" % (
        _pct(payload["false_positive_rate"]), payload["fpr_ci95"]))
    lines.append("precision           %s" % _pct(payload["precision"]))
    lines.append("category accuracy   %s  (over anomalous windows only)" % _pct(payload["category_accuracy"]))
    lines.append("severity MAE        %s levels (0-3 scale)" % (
        "n/a" if payload["severity_mae_levels"] is None else payload["severity_mae_levels"]))
    lines.append("calibration ECE     %s  (raw %s; lower is better — the gate branches on confidence)" % (
        "n/a" if payload["ece_anomaly"] is None else payload["ece_anomaly"],
        "n/a" if payload["ece_anomaly_raw"] is None else payload["ece_anomaly_raw"]))
    if payload["calibrated"]:
        lines.append("                    (a fitted Platt correction is applied before the thresholds)")
    elif payload["ece_anomaly"] == payload["ece_anomaly_raw"] and payload["ece_anomaly"] is not None:
        # Identical columns mean no correction is in play. Worth saying out loud, because a threshold
        # chosen on the *calibrated* scale but compared against *raw* probabilities never fires, and
        # every other number in this report still looks perfectly reasonable.
        lines.append("                    WARNING: no calibration is applied, yet the two columns match.")
        lines.append("                    If your gate thresholds were picked on the calibrated scale,")
        lines.append("                    they are being compared against raw probabilities and will")
        lines.append("                    not fire. Run `brian calibrate`, or set")
        lines.append("                    BRIAN_CALIBRATION_ENABLED=0 to silence this deliberately.")
    lines.append("latency             p50=%s ms  p95=%s ms  (full HTTP round trip)" % (
        payload["latency_ms"]["p50"], payload["latency_ms"]["p95"]))
    lines.append("elapsed             %ss" % payload["elapsed_s"])
    lines.append("")

    table = report.per_scenario()
    lines.append("%-22s %4s %8s %8s %10s %10s" % (
        "scenario", "n", "anom acc", "cat acc", "mean P(an)", "misses"))
    lines.append("-" * 78)
    for scenario in sorted(table):
        row = table[scenario]
        lines.append("%-22s %4d %8s %8s %10s %10d" % (
            scenario,
            row["n"],
            _pct(row["anomaly_accuracy"]),
            _pct(row["category_accuracy"]),
            "n/a" if row["mean_probability"] is None else "%.3f" % row["mean_probability"],
            len(row["misses"]),
        ))
    lines.append("")

    if verbose:
        misses = [s for s in report.scores if s.anomaly_correct is False]
        if misses:
            lines.append("Misses (each one is a window the detector got wrong):")
            for score in misses:
                lines.append("  %s %-20s expected anomaly=%-5s got anomaly=%-5s p=%-6s category=%s action=%s" % (
                    score.window_start.strftime("%H:%M"),
                    score.scenario,
                    score.expected_anomaly,
                    score.predicted_anomaly,
                    "n/a" if score.predicted_probability is None else "%.3f" % score.predicted_probability,
                    score.predicted_category,
                    score.action,
                ))
            lines.append("")
            lines.append("For each miss, the digest the model actually read:")
            for score in misses[:3]:
                lines.append("  --- %s (%s) ---" % (score.scenario, score.window_start.strftime("%H:%M")))
                for line in score.digest_text.splitlines():
                    lines.append("    " + line)
                lines.append("")

        errors = [s for s in report.scores if s.error]
        if errors:
            lines.append("Transport errors: %d (first: %s)" % (len(errors), errors[0].error))
            lines.append("")
    return "\n".join(lines)


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.3f" % value


def gate_recommendation(report: EvalReport, min_recall: float = 0.85, max_fpr: float = 0.05,
                        max_ece: float = 0.10) -> Dict[str, Any]:
    """Turn the numbers into the only decision that matters: may automation act on this?

    The three gates are deliberately conjunctive and deliberately strict. `max_ece` is the one
    that is usually skipped and should not be: a detector with 0.95 recall and 0.30 ECE has
    confident answers that do not mean what they say, and the gate acts on exactly that
    confidence.
    """
    recall = report.recall
    fpr = report.false_positive_rate
    ece = report.ece
    checks = {
        "recall": {"value": _r(recall), "min": min_recall, "pass": recall is not None and recall >= min_recall},
        "false_positive_rate": {"value": _r(fpr), "max": max_fpr, "pass": fpr is not None and fpr <= max_fpr},
        "calibration_ece": {"value": _r(ece), "max": max_ece, "pass": ece is not None and ece <= max_ece},
    }
    verdict = "automation_ready" if all(c["pass"] for c in checks.values()) else "not_ready"
    reasons = []
    for name, check in checks.items():
        if not check["pass"]:
            reasons.append(
                "%s=%s does not meet the bar (%s)"
                % (name, check["value"], check.get("min", check.get("max")))
            )
    if len(report.scores) < 30:
        reasons.append("sample is only %d windows; increase it before trusting these rates" % len(report.scores))
        verdict = "not_ready"
    return {
        "verdict": verdict,
        "checks": checks,
        "reasons": reasons,
        "guidance": (
            "A 'not_ready' verdict means the gate should keep escalating to System 2 rather than "
            "acting on System 1 alone. That is the shipped configuration: it is not a failure, it "
            "is the reason the confidence floor exists."
        ),
    }


__all__ = [
    "Evaluator",
    "EvalReport",
    "WindowScore",
    "format_report",
    "gate_recommendation",
    "ECE_BINS",
]
