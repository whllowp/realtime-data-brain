"""Compare digest renderers through the *real* evaluation path, not a bespoke one.

Why this script exists, and what it replaced
--------------------------------------------
The first renderer comparison produced a confident, wrong answer. It built each window's digest with
its own windower and fed a differently-ordered baseline sequence than the production `Evaluator` does,
so the renderers were never scored on comparable inputs — the ledger variant "won" with AUC 0.875 and,
wired into the pipeline, immediately produced 7 false positives and a *higher* ECE.

That is the failure this script is written to prevent: **a benchmark that takes a shortcut measures the
shortcut.** So it now reuses `Evaluator` verbatim — the same windower, the same compare-then-observe
baseline ordering, the same warm-up handling, the same `apply_gate` — and varies exactly one thing: the
function that turns `Digest.metrics` into the text the model reads.

Candidates are supplied by `--renderer`, and the comparison is reported on AUC (threshold-free, so it
measures ordering rather than the accident of a threshold), plus recall/FPR at the shipped threshold
and the probability spread, which is what a threshold has to work with.

    python scripts/compare_renderers.py --repeat 4 --renderer current,ledger,prose

Adding a candidate: write a function `(metrics) -> str` and register it in `RENDERERS`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import CalibrationSettings, load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, LabeledWindow, generate  # noqa: E402
from brian.eval import Evaluator  # noqa: E402
from brian.laya.client import LayaClient  # noqa: E402
from brian.state import _dig, _num, render_digest  # noqa: E402

#: Metric order for the ledger variants. Defined *here* rather than imported from `state`, because the
#: ledger is not the production renderer any more — it is a candidate under measurement, and a
#: candidate's constants belong next to the candidate. If it ever wins, it moves back into `state.py`
#: along with the evidence for the change.
LEDGER_ORDER: Tuple[str, ...] = (
    "errors.ratio",
    "errors.rate",
    "latency_ms.p99",
    "latency_ms.p90",
    "latency_ms.p50",
    "cpu.util",
    "memory.util",
    "thread_pool.util",
    "db_pool.util",
    "upstream.error_ratio",
    "upstream.latency_ms.p99",
    "timeouts.rate",
    "requests.rate",
    "auth.failures_rate",
    "schema_errors.ratio",
    "consumer.lag",
)


def _ledger_value(entry: Mapping[str, Any], key: str) -> str:
    """The `value (baseline B, Rx, z=Z)` fragment used by the ledger candidates."""
    text = _quantity(entry.get("value"), key)
    baseline = entry.get("baseline")
    if baseline is None:
        return text
    parts = ["baseline %s" % _quantity(baseline, key)]
    if entry.get("delta_ratio") is not None:
        parts.append("%sx" % _num(entry["delta_ratio"], 2))
    if entry.get("z") is not None:
        z = float(entry["z"])
        parts.append("z=%s%s" % ("+" if z >= 0 else "", _num(z, 1)))
    if entry.get("n"):
        parts.append("n=%s" % entry["n"])
    return "%s (%s)" % (text, ", ".join(parts))


def _quantity(value: Any, key: str) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if key.endswith(".util") or key.endswith(".ratio") or key.startswith("schema_errors"):
        return "%.2f%%" % (number * 100.0)
    if abs(number) >= 1000:
        return "%d" % round(number)
    return _num(number, 2)

# --------------------------------------------------------------------------------------
# candidate renderers
# --------------------------------------------------------------------------------------


def render_current(metrics: Mapping[str, Any]) -> str:
    """Whatever `state.render_digest` does today — the production renderer."""
    return render_digest(metrics)[0]


def render_prose(metrics: Mapping[str, Any]) -> str:
    """The narrative renderer, byte-identical to what `state.render_digest` produces.

    It is *reconstructed* here rather than imported, so that a proposed change to the production
    renderer can be measured against the shipped one. `tests/test_digest_renderer.py` asserts the two
    agree on a fixed window: when they drifted apart — production gained z-scores and sample counts
    that this function did not have — a threshold swept against *this* text was applied to the
    *other* text, and the detector scored recall 0.98 at a false-positive rate of 1.00. The lesson is
    that a benchmark which renders its own input is measuring something else.
    """
    return _production_prose(metrics)


def _production_prose(metrics: Mapping[str, Any]) -> str:
    """Call the production renderer, so this file cannot silently diverge from it again."""
    from brian.state import render_digest as production_render_digest

    return production_render_digest(metrics)[0]



def render_ledger(metrics: Mapping[str, Any]) -> str:
    """The compact ledger, as implemented during the experiment."""
    lines: List[str] = []
    lines.append(
        "Telemetry window for service %s: %s seconds, %s events."
        % (metrics.get("service"), _num(metrics.get("window_seconds"), 0), metrics.get("event_count", 0))
    )
    comparison = metrics.get("comparison") or {}
    for key in LEDGER_ORDER:
        entry = _dig(comparison, key)
        if isinstance(entry, Mapping) and entry.get("value") is not None:
            lines.append("%s: %s" % (key, _ledger_value(entry, key)))
    signature = metrics.get("error_signature")
    if isinstance(signature, Mapping) and signature.get("template"):
        lines.append("dominant_error: %s" % str(signature["template"])[:150])
    return "\n".join(lines)


def render_ledger_labeled(metrics: Mapping[str, Any]) -> str:
    """The ledger with human labels instead of dotted keys."""
    labels = {
        "errors.ratio": "error fraction",
        "errors.rate": "errors per second",
        "latency_ms.p99": "p99 latency (ms)",
        "latency_ms.p90": "p90 latency (ms)",
        "latency_ms.p50": "p50 latency (ms)",
        "cpu.util": "CPU utilisation",
        "memory.util": "memory utilisation",
        "thread_pool.util": "thread pool utilisation",
        "db_pool.util": "DB pool utilisation",
        "upstream.error_ratio": "upstream error fraction",
        "upstream.latency_ms.p99": "upstream p99 (ms)",
        "timeouts.rate": "connect timeouts per second",
        "requests.rate": "throughput (req/s)",
        "auth.failures_rate": "auth failures per second",
        "schema_errors.ratio": "schema error fraction",
        "consumer.lag": "consumer lag (records)",
    }
    lines: List[str] = []
    lines.append("Telemetry window for service %s." % metrics.get("service"))
    comparison = metrics.get("comparison") or {}
    for key in LEDGER_ORDER:
        entry = _dig(comparison, key)
        if isinstance(entry, Mapping) and entry.get("value") is not None:
            lines.append("%s is %s" % (labels.get(key, key), _ledger_value(entry, key)))
    signature = metrics.get("error_signature")
    if isinstance(signature, Mapping) and signature.get("template"):
        lines.append("Most frequent error: %s" % str(signature["template"])[:150])
    return "\n".join(lines)


def render_verdict_ledger(metrics: Mapping[str, Any]) -> str:
    """Only what discriminates: error, latency, resources, load — no trace metrics.

    A hypothesis worth testing explicitly: the earlier per-window probe found that a *shorter* state
    scored better, and this is the shortest state that still carries the discriminating metrics.
    """
    lines: List[str] = []
    lines.append("Service %s, %s-second window." % (metrics.get("service"), _num(metrics.get("window_seconds"), 0)))
    comparison = metrics.get("comparison") or {}
    for key in ("errors.ratio", "latency_ms.p99", "latency_ms.p50", "cpu.util",
                "db_pool.util", "thread_pool.util", "requests.rate"):
        entry = _dig(comparison, key)
        if isinstance(entry, Mapping) and entry.get("value") is not None:
            lines.append("%s: %s" % (key, _ledger_value(entry, key)))
    signature = metrics.get("error_signature")
    if isinstance(signature, Mapping) and signature.get("template"):
        lines.append("dominant_error: %s" % str(signature["template"])[:150])
    return "\n".join(lines)


def render_prose_plus_deviations(metrics: Mapping[str, Any]) -> str:
    """The narrative sentences, with each comparison also stated numerically.

    Chases the best of both measured forms: `prose` had the highest and most *stable* AUC (0.875-0.897
    across six seeds) but a useless ECE (~0.40); `verdict_ledger` had a much better ECE (~0.11) and a
    slightly lower AUC (~0.81). The hypothesis is that the model reads a *sentence* for the decision
    and a *number* for the confidence, so stating both should raise the AUC without losing the
    calibration — which is exactly the combination a gate needs.
    """
    lines = render_prose(metrics).splitlines()
    comparison = metrics.get("comparison") or {}
    short = {
        "errors.ratio": "error fraction",
        "latency_ms.p99": "p99 latency",
        "cpu.util": "CPU",
        "db_pool.util": "DB pool",
        "thread_pool.util": "thread pool",
        "requests.rate": "throughput",
    }
    for key, label in short.items():
        entry = _dig(comparison, key)
        if isinstance(entry, Mapping) and entry.get("value") is not None:
            lines.append("%s: %s" % (label, _ledger_value(entry, key)))
    return "\n".join(lines)


RENDERERS: Dict[str, Callable[[Mapping[str, Any]], str]] = {
    "current": render_current,
    "prose": render_prose,
    "prose_plus_deviations": render_prose_plus_deviations,
    "ledger": render_ledger,
    "ledger_labeled": render_ledger_labeled,
    "verdict_ledger": render_verdict_ledger,
}


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


class RenderingEvaluator(Evaluator):
    """`Evaluator` with one method changed: how the digest text is produced.

    Subclassing rather than copying is the whole point — the windower, the compare-then-observe
    baseline ordering, the warm-up rule and the gate all stay production-identical, so a difference
    between two runs can only come from the renderer.
    """

    def __init__(self, *args: Any, renderer: Callable[[Mapping[str, Any]], str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.renderer = renderer

    def run(self, windows: Sequence[LabeledWindow], warmup: int = 0,
            on_window: Optional[Callable[[Any], None]] = None):
        from brian.state import BaselineTracker, Windower, build_digest

        baselines = BaselineTracker(window=self.settings.window.baseline_windows)
        windower = Windower(
            size_s=self.settings.window.size_s,
            slide_s=self.settings.window.slide_s,
            allowed_lateness_s=0,
            min_events=1,
        )
        from brian.eval import EvalReport, WindowScore
        from brian.laya.gate import apply_gate

        report = EvalReport(model=self.settings.laya.model, catalog_hash=self.laya.questions_hash)
        labels = {(w.service, int(w.window_start.timestamp())): w for w in windows}
        for window in windows:
            for event in window.events:
                windower.add(event)

        from datetime import datetime as _dt

        started = _dt.now()
        for aggregator in sorted(windower.flush(), key=lambda a: (a.window_start, a.service)):
            digest = build_digest(aggregator, baselines)
            # The single substitution this whole script exists for.
            digest.interpretation = self.renderer(digest.metrics)
            key = (digest.service, int(digest.window_start.timestamp()))
            label = labels.get(key)
            if label is None or not label.scored:
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
                score.predicted_category = decision.category.value if decision.category else None
                score.predicted_severity = decision.severity.value if decision.severity else None
                score.action = decision.action.value
                score.confidence = decision.confidence
                score.latency_ms = result.latency_ms
            except Exception as exc:  # noqa: BLE001
                score.error = str(exc)
            report.scores.append(score)
            if on_window is not None:
                on_window(score)
            baselines.observe(digest.service, digest.metrics)
        report.elapsed_s = (_dt.now() - started).total_seconds()
        return report


def repeat_windows(sequence: List[str], service: str, warmup: int, repeat: int, seed: int) -> List[LabeledWindow]:
    windows: List[LabeledWindow] = []
    cursor = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    for index in range(max(1, repeat)):
        created = generate(sequence=sequence, service=service, start=cursor,
                           warmup_minutes=warmup if index == 0 else 0, seed=seed + index)
        windows.extend(created)
        cursor = cursor + timedelta(minutes=len(created) + 1)
    return windows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--service", default="checkout-api")
    parser.add_argument("--seed", type=int, default=20260114)
    parser.add_argument("--renderer", default="current,prose,ledger,ledger_labeled,verdict_ledger")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="reporting threshold for recall/FPR alongside AUC")
    parser.add_argument("--out", default="var/compare_renderers.json")
    args = parser.parse_args()

    settings = load_settings().with_overrides(
        calibration=CalibrationSettings(path=load_settings().calibration.path, enabled=False)
    )
    settings = settings.with_overrides(
        gate=type(settings.gate)(**{**vars(settings.gate), "anomaly_probability": args.threshold})
    )
    windows = repeat_windows(list(DEFAULT_SEQUENCE), args.service, args.warmup, args.repeat, args.seed)
    laya = LayaClient(settings.laya)

    results: List[Dict[str, Any]] = []
    for name in [n.strip() for n in args.renderer.split(",") if n.strip()]:
        renderer = RENDERERS.get(name)
        if renderer is None:
            print("unknown renderer %r (have: %s)" % (name, ", ".join(RENDERERS)))
            return 2
        report = RenderingEvaluator(settings, laya=laya, calibrator=None, renderer=renderer).run(
            windows, warmup=args.warmup
        )
        probabilities = [s.raw_probability for s in report.scores if s.raw_probability is not None]
        labels = [s.expected_anomaly for s in report.scores if s.raw_probability is not None]
        row = {
            "renderer": name,
            "n": len(labels),
            "auc": None if auc(probabilities, labels) is None else round(auc(probabilities, labels), 4),
            "recall": None if report.recall is None else round(report.recall, 4),
            "fpr": None if report.false_positive_rate is None else round(report.false_positive_rate, 4),
            "precision": None if report.precision is None else round(report.precision, 4),
            "category_accuracy": None if report.category_accuracy is None else round(report.category_accuracy, 4),
            "ece": None if report.ece is None else round(report.ece, 4),
            "p_min": round(min(probabilities), 4) if probabilities else None,
            "p_max": round(max(probabilities), 4) if probabilities else None,
            "p_spread": round(max(probabilities) - min(probabilities), 4) if probabilities else None,
            "mean_latency_ms": round(sum(s.latency_ms for s in report.scores) / max(len(report.scores), 1), 1),
            "state_words": round(
                sum(len(s.digest_text.split()) for s in report.scores) / max(len(report.scores), 1), 1
            ),
        }
        results.append(row)
        print("%-16s AUC=%-7s recall=%-7s fpr=%-7s spread=%-7s ECE=%-7s words=%s"
              % (name, row["auc"], row["recall"], row["fpr"], row["p_spread"], row["ece"], row["state_words"]))

    usable = [r for r in results if r["recall"] is not None]
    if usable:
        # Ranked by F-measure-like usefulness under the constraint that a detector nobody reads is
        # worthless: require the FPR to be plausible before comparing recall.
        quiet = [r for r in usable if (r["fpr"] or 1.0) <= 0.15]
        pool = quiet or usable
        best = max(pool, key=lambda r: ((r["recall"] or 0) - (r["fpr"] or 0), r["auc"] or 0))
        print()
        print("best under fpr <= 0.15: %s (recall=%s fpr=%s AUC=%s)"
              % (best["renderer"], best["recall"], best["fpr"], best["auc"]))
        if best["renderer"] != "current":
            print("consider promoting %r into state.render_digest" % best["renderer"])

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
