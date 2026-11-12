"""Laya+LLM versus a plain threshold, at matched false-positive rates, with the bill attached.

The comparison the paper asks for, adapted to an architecture where the two halves do different jobs:

    detection   Laya alone. A threshold is a real competitor here.
    explanation System 2 alone. **No threshold has an equivalent**, and System 2 does not influence the
                detection metric at all — it runs after the decision is made and published.

That asymmetry decides how the cost is read. Tokens spent on System 2 cannot be justified by detection
accuracy, because detection does not use them. They can only be justified by explanation quality, which is
not measurable without a model configured (and arguably not without humans). Reporting "tokens per true
positive" would quietly imply the tokens bought the true positives; they did not.

So this script reports three things separately:
  1. detection: recall/FPR of Laya versus the best single-signal threshold at the same FPR budget;
  2. cost: Laya calls and System 2 tokens per 1,000 windows, kept in their own units;
  3. the marginal cost of the LLM layer per window that reaches an alert — the only place it spends.

Usage:
    python scripts/compare_cost_to_threshold.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, EXPECTED, generate  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402


def _metric(digest, *path):
    node = digest.metrics
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node.get("value") if isinstance(node, dict) else None


def rates(scores, labels, budget):
    """Recall at the highest threshold whose FPR stays inside the budget."""
    positives = sum(labels)
    negatives = len(labels) - positives
    best = (0.0, 1.0)
    for cut in sorted(set(s for s in scores if s is not None), reverse=True):
        tp = sum(1 for s, y in zip(scores, labels) if y and s is not None and s >= cut)
        fp = sum(1 for s, y in zip(scores, labels) if not y and s is not None and s >= cut)
        if not tp:
            continue
        fpr = fp / negatives if negatives else 0.0
        if fpr <= budget and tp / positives > best[0]:
            best = (tp / positives, fpr)
    # "no cut keeps the false-positive rate inside the budget" is not "recall 0 at FPR 1.0"; returning a
    # sentinel that renders as a number would misreport a signal as catastrophic rather than unusable.
    return best if best[0] > 0 else (None, None)


def main() -> int:
    settings = load_settings()
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    expected = {}
    pipeline = Pipeline(settings, bus=MemoryBus())
    traces = []
    for window in windows:
        label = bool((EXPECTED.get(window.scenario) or {}).get("is_anomaly", False))
        expected[(window.service, int(window.window_start.timestamp()))] = label
        for event in window.events:
            pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)
    traces.extend(pipeline.drain_ready(final=True))

    scored = []
    for trace in traces:
        if trace.decision is None:
            continue
        label = expected.get((trace.digest.service, int(trace.digest.window_start.timestamp())))
        if label is None:
            continue
        scored.append((trace, bool(label)))
    if not scored:
        print("no labelled windows matched; nothing to compare")
        return 2

    labels = [label for _, label in scored]
    probabilities = [t.decision.anomaly_probability or 0.0 for t, _ in scored]
    reported = [1 if (t.decision.anomaly_probability or 0.0) >= 0.5 else 0 for t, _ in scored]

    candidates = {
        "error fraction": [_metric(t.digest, "errors", "ratio") or 0.0 for t, _ in scored],
        "latency p99": [_metric(t.digest, "latency_ms", "p99") or 0.0 for t, _ in scored],
        "cpu util": [_metric(t.digest, "cpu", "util") or 0.0 for t, _ in scored],
    }

    positives = sum(labels)
    print("windows %d  (anomalous %d = %.0f%%)" % (len(labels), positives, 100 * positives / len(labels)))
    print("")
    print("%-18s %10s %10s" % ("detector", "recall", "fpr"))
    print("-" * 42)
    def show(name, recall, fpr):
        if recall is None:
            print("%-18s %10s %10s" % (name, "no cut", "at <=10%"))
        else:
            print("%-18s %10.3f %10.3f" % (name, recall, fpr))

    for name, scores in candidates.items():
        show("threshold: " + name, *rates(scores, labels, 0.10))
    show("Laya (swept)", *rates(probabilities, labels, 0.10))

    tp = sum(1 for r, y in zip(reported, labels) if r and y)
    fp = sum(1 for r, y in zip(reported, labels) if r and not y)
    print("%-18s %10.3f %10.3f" % ("Laya @0.5 cut", tp / positives if positives else 0,
                                    fp / (len(labels) - positives) if len(labels) > positives else 0))

    alerts = [t for t, _ in scored if t.report is not None]
    tokens = sum((t.report.prompt_tokens + t.report.completion_tokens
                  + t.report.estimated_prompt_tokens) for t in alerts
                 if t.report.prompt_tokens == 0)
    tokens += sum((t.report.prompt_tokens + t.report.completion_tokens) for t in alerts
                  if t.report.prompt_tokens > 0)
    print("")
    print("cost, in its own units (never blended):")
    print("  Laya decisions          %d  (%.0f per 1,000 windows)" % (len(scored), 1000.0))
    print("  System 2 reports        %d  (%.0f per 1,000 windows)"
          % (len(alerts), 1000.0 * len(alerts) / len(scored)))
    print("  System 2 tokens         %d  (%.0f per 1,000 windows)"
          % (tokens, 1000.0 * tokens / len(scored)))
    print("  threshold detectors     0 tokens, 0 model calls")
    print("")
    print("The asymmetry that decides how to read this: System 2 runs *after* the decision is published,")
    print("so no token it spends can change recall or FPR. Its cost buys explanation, and any claim that")
    print("it 'pays for itself' in detection accuracy would be attributing Laya's work to the LLM.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
