"""Throwaway experiment 2: find the best digest text with AUC as the objective.

Experiment 1 established two things: the metric JSON must never be sent (it is dumped verbatim
into the document budget and costs ~0.1 AUC), and a compact deviation-explicit ledger beats
narrative prose. This experiment tunes the *rendering* against AUC on a fixed set of labelled
windows, then checks that the winning format still drives the full question catalog.

Variants (all plain text, all built from the same `Digest.metrics`):

    ledger          one line per metric: name value (baseline B, Rx, z=Z)
    ledger_labeled  as above, with a human label per metric instead of the dotted key
    ledger_explicit as above, plus an explicit "X times its normal level" phrasing
    narrative       the production `render_digest` output
    narrative_plus  production output with a z-score ledger appended

The objective is AUC, never accuracy: AUC is threshold-free, so it measures ordering rather than
the accident of where a threshold lands. Accuracy is reported too, as context.

    python scripts/experiment_render.py --repeat 4
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brian.calibration import auc  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, LabeledWindow, generate  # noqa: E402
from brian.laya.qcatalog import DEFAULT_QUESTIONS  # noqa: E402
from brian.state import BaselineTracker, Windower, build_digest  # noqa: E402

URL = "http://127.0.0.1:8077"

#: Human labels, so the ledger reads like a sentence rather than a JSON path.
LABELS = {
    "requests.rate": "throughput (req/s)",
    "errors.rate": "errors per second",
    "errors.ratio": "error fraction",
    "latency_ms.p50": "p50 latency (ms)",
    "latency_ms.p90": "p90 latency (ms)",
    "latency_ms.p99": "p99 latency (ms)",
    "cpu.util": "CPU utilisation",
    "memory.util": "memory utilisation",
    "thread_pool.util": "thread pool utilisation",
    "db_pool.util": "DB pool utilisation",
    "upstream.error_ratio": "upstream error fraction",
    "upstream.latency_ms.p99": "upstream p99 (ms)",
    "auth.failures_rate": "auth failures per second",
    "schema_errors.ratio": "schema error fraction",
    "consumer.lag": "consumer lag (records)",
    "timeouts.rate": "connect timeouts per second",
}

#: Order matters to an encoder that may truncate: the signals that separate the scenarios first.
ORDER = [
    "errors.ratio",
    "latency_ms.p99",
    "latency_ms.p50",
    "cpu.util",
    "db_pool.util",
    "thread_pool.util",
    "memory.util",
    "upstream.error_ratio",
    "upstream.latency_ms.p99",
    "requests.rate",
    "auth.failures_rate",
    "schema_errors.ratio",
    "consumer.lag",
    "timeouts.rate",
]


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 1000:
        return "%d" % round(number)
    if abs(number) < 0.01 and number != 0:
        return "%.4f" % number
    return ("%%.%df" % digits) % number


def _rows(digest: Any) -> List[Tuple[str, Dict[str, Any]]]:
    comparison = digest.metrics.get("comparison") or {}
    rows = []
    for key in ORDER:
        entry = comparison.get(key)
        if isinstance(entry, dict) and entry.get("value") is not None:
            rows.append((key, entry))
    return rows


def render_ledger(digest: Any, labeled: bool = False, explicit: bool = False) -> str:
    lines = ["Telemetry window for service %s (60 seconds)." % digest.service]
    for key, entry in _rows(digest):
        name = LABELS.get(key, key) if labeled else key
        value = _fmt(entry["value"])
        baseline = entry.get("baseline")
        ratio = entry.get("delta_ratio")
        z = entry.get("z")
        if baseline is None:
            lines.append("%s: %s" % (name, value))
            continue
        if explicit and ratio is not None and abs(float(ratio) - 1.0) >= 0.25:
            lines.append(
                "%s: %s, which is %s times its normal level of %s" % (name, value, _fmt(ratio, 1), _fmt(baseline))
            )
        else:
            detail = "baseline %s" % _fmt(baseline)
            if ratio is not None:
                detail += ", %sx" % _fmt(ratio, 2)
            if z is not None:
                detail += ", z=%s%s" % ("+" if float(z) >= 0 else "", _fmt(z, 1))
            lines.append("%s: %s (%s)" % (name, value, detail))
    signature = digest.metrics.get("error_signature") or {}
    if signature.get("template"):
        lines.append(
            "Most frequent error (%s%% of errors): %s"
            % (_fmt((signature.get("share") or 0) * 100, 0), str(signature["template"])[:150])
        )
    if digest.metrics.get("deploys"):
        lines.append("Deployment recorded in this window: %s" % "; ".join(digest.metrics["deploys"])[:120])
    if digest.metrics.get("config_changes"):
        lines.append("Config change recorded: %s" % "; ".join(digest.metrics["config_changes"])[:120])
    return "\n".join(lines)


def variants(digest: Any) -> Dict[str, str]:
    return {
        "ledger": render_ledger(digest),
        "ledger_labeled": render_ledger(digest, labeled=True),
        "ledger_explicit": render_ledger(digest, labeled=True, explicit=True),
        "narrative": digest.interpretation,
        "narrative_plus": digest.interpretation + "\n" + render_ledger(digest, labeled=True),
    }


def _decide(state: Any, questions: Dict[str, Any], model: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        URL + "/v1/decide",
        data=json.dumps({"state": state, "questions": questions, "model": model},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--service", default="checkout-api")
    parser.add_argument("--seed", type=int, default=20260114)
    parser.add_argument("--model", default="typed-decisions")
    parser.add_argument("--full-catalog", action="store_true",
                        help="also run the full question catalog on the winning format")
    parser.add_argument("--out", default="var/experiment_render.json")
    args = parser.parse_args()

    windows: List[LabeledWindow] = []
    cursor = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    for index in range(max(1, args.repeat)):
        created = generate(sequence=list(DEFAULT_SEQUENCE), service=args.service, start=cursor,
                           warmup_minutes=args.warmup if index == 0 else 0, seed=args.seed + index)
        windows.extend(created)
        cursor = cursor + timedelta(minutes=len(created) + 1)

    baselines = BaselineTracker(window=30)
    prepared: List[Tuple[LabeledWindow, Any]] = []
    for window in windows:
        windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
        for event in window.events:
            windower.add(event)
        for aggregator in windower.flush():
            digest = build_digest(aggregator, baselines)
            prepared.append((window, digest))
            baselines.observe(digest.service, digest.metrics)
    scored = [(w, d) for w, d in prepared if w.scored]
    print("scored windows: %d (%d anomalous)\n"
          % (len(scored), sum(1 for w, _ in scored if w.expected["is_anomaly"])))

    nuol = {"is_anomaly": DEFAULT_QUESTIONS["is_anomaly"]}
    results: List[Dict[str, Any]] = []
    best_name = None
    best_auc = 0.0
    for name in ("ledger", "ledger_labeled", "ledger_explicit", "narrative", "narrative_plus"):
        probabilities: List[float] = []
        labels: List[bool] = []
        correct = 0
        for window, digest in scored:
            text = variants(digest)[name]
            response = _decide(text, nuol, args.model)
            probability = (response.get("answers", {}).get("is_anomaly") or {}).get("noul")
            if probability is None:
                continue
            probability = float(probability)
            expected = bool(window.expected["is_anomaly"])
            probabilities.append(probability)
            labels.append(expected)
            correct += 1 if (probability >= 0.5) == expected else 0
        if not probabilities:
            continue
        window_auc = auc(probabilities, labels)
        row = {
            "variant": name,
            "n": len(probabilities),
            "auc": None if window_auc is None else round(window_auc, 4),
            "accuracy_at_0.5": round(correct / len(probabilities), 4),
            "p_min": round(min(probabilities), 4),
            "p_max": round(max(probabilities), 4),
            "words": len(variants(scored[0][1])[name].split()),
        }
        results.append(row)
        print("%-16s AUC=%s  acc@0.5=%.3f  p in [%.3f, %.3f]  words=%d"
              % (name, row["auc"], row["accuracy_at_0.5"], row["p_min"], row["p_max"], row["words"]))
        if (window_auc or 0) > best_auc:
            best_auc, best_name = window_auc or 0, name

    print()
    print("winner: %s (AUC %.4f)" % (best_name, best_auc))

    if args.full_catalog and best_name:
        print()
        print("full catalog on the winning format:")
        probabilities, labels = [], []
        for window, digest in scored:
            response = _decide(variants(digest)[best_name], DEFAULT_QUESTIONS, args.model)
            probability = (response.get("answers", {}).get("is_anomaly") or {}).get("noul")
            if probability is None:
                continue
            probabilities.append(float(probability))
            labels.append(bool(window.expected["is_anomaly"]))
        full_auc = auc(probabilities, labels)
        print("  AUC=%s  p in [%.3f, %.3f]"
              % (None if full_auc is None else round(full_auc, 4), min(probabilities), max(probabilities)))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"results": results, "winner": best_name}, indent=2), encoding="utf-8")
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
