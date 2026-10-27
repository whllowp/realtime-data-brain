"""Threshold selection: what does each escalation rate actually cost?

The calibration fit answers "what do these probabilities mean?". This script answers the question
that follows, and that no calibration can answer for you: **where should the line be?**

Raw probabilities here are compressed into a ~0.05-wide band, so the threshold that separates
anomalous from healthy windows is a *policy* choice with a visible price: every point of recall it
buys costs some number of false pages. Reporting that curve is the only honest way to pick a
number, because "0.5" is not a meaningful default when 0.5 might correspond to a 30% false-positive
rate.

Reads the raw probabilities the evaluator already collected (no new Laya calls), replays every
candidate threshold on the raw scale, and prints the reachable operating points. The recommended
row is the highest threshold that still meets a recall floor — i.e. the *quietest* configuration
that does not miss more than the stated fraction of incidents.

    python scripts/threshold_sweep.py --repeat 6 --recall-floor 0.85
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import CalibrationSettings, load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, LabeledWindow, generate  # noqa: E402
from brian.laya.client import LayaClient  # noqa: E402


def repeat_windows(sequence: List[str], service: str, warmup: int, repeat: int, seed: int) -> List[LabeledWindow]:
    windows: List[LabeledWindow] = []
    cursor = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    for index in range(max(1, repeat)):
        created = generate(sequence=sequence, service=service, start=cursor,
                           warmup_minutes=warmup if index == 0 else 0, seed=seed + index)
        windows.extend(created)
        cursor = cursor + timedelta(minutes=len(created) + 1)
    return windows


def sweep(probabilities: List[float], labels: List[bool]) -> List[Dict[str, Any]]:
    positives = sum(1 for y in labels if y)
    negatives = len(labels) - positives
    rows: List[Dict[str, Any]] = []
    for candidate in sorted(set(probabilities), reverse=True):
        tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= candidate)
        fp = sum(1 for p, y in zip(probabilities, labels) if not y and p >= candidate)
        fn = positives - tp
        tn = negatives - fp
        recall = tp / positives if positives else None
        fpr = fp / negatives if negatives else None
        precision = tp / (tp + fp) if (tp + fp) else None
        rows.append({
            "threshold": round(candidate, 6),
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "recall": None if recall is None else round(recall, 4),
            "fpr": None if fpr is None else round(fpr, 4),
            "precision": None if precision is None else round(precision, 4),
            "alerts_per_1000_windows": round((tp + fp) / len(labels) * 1000, 1),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=6)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--service", default="checkout-api")
    parser.add_argument("--seed", type=int, default=20260114)
    parser.add_argument("--recall-floor", type=float, default=0.85,
                        help="the quietest threshold that still reaches this recall")
    parser.add_argument("--max-fpr", type=float, default=0.10,
                        help="the false-positive rate an operator can absorb; bounds the recommendation")
    parser.add_argument("--renderer", default="current",
                        help="digest renderer to sweep: current | prose | ledger | ledger_labeled | "
                             "verdict_ledger | prose_plus_deviations")
    parser.add_argument("--out", default="var/threshold_sweep.json")
    args = parser.parse_args()

    settings = load_settings()
    windows = repeat_windows(list(DEFAULT_SEQUENCE), args.service, args.warmup, args.repeat, args.seed)
    laya = LayaClient(settings.laya)

    # Calibration off: the sweep operates on the raw scale, which is where the thresholds live
    # until a fit is accepted.
    raw_settings = settings.with_overrides(
        calibration=CalibrationSettings(path=settings.calibration.path, enabled=False)
    )
    # Reuse the renderer-comparison harness so the sweep measures the *same* pipeline the renderer
    # choice was made on. Running the plain `Evaluator` here while varying --renderer silently swept
    # the production renderer three times and reported identical numbers, which is how a
    # parameter that is not wired through looks exactly like a parameter that has no effect.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from compare_renderers import RENDERERS, RenderingEvaluator  # noqa: E402

    renderer = RENDERERS.get(args.renderer)
    if renderer is None:
        print("unknown --renderer %r; have: %s" % (args.renderer, ", ".join(sorted(RENDERERS))))
        return 2
    report = RenderingEvaluator(
        raw_settings, laya=laya, calibrator=None, renderer=renderer
    ).run(windows, warmup=args.warmup)
    print("renderer=%s" % args.renderer)
    probabilities = [s.raw_probability for s in report.scores if s.raw_probability is not None]
    labels = [s.expected_anomaly for s in report.scores if s.raw_probability is not None]
    print("windows=%d anomalous=%d healthy=%d"
          % (len(labels), sum(1 for y in labels if y), sum(1 for y in labels if not y)))
    print("AUC=%.4f  raw p in [%.4f, %.4f]"
          % (auc(probabilities, labels) or 0.0, min(probabilities), max(probabilities)))
    print()

    rows = sweep(probabilities, labels)
    print("%-10s %5s %5s %8s %8s %10s %22s" % (
        "threshold", "tp", "fp", "recall", "fpr", "precision", "alerts/1000 windows"))
    print("-" * 78)
    for row in rows:
        print("%-10.4f %5d %5d %8s %8s %10s %22.1f" % (
            row["threshold"], row["tp"], row["fp"],
            "n/a" if row["recall"] is None else "%.3f" % row["recall"],
            "n/a" if row["fpr"] is None else "%.3f" % row["fpr"],
            "n/a" if row["precision"] is None else "%.3f" % row["precision"],
            row["alerts_per_1000_windows"],
        ))

    meeting = [r for r in rows if (r["recall"] or 0) >= args.recall_floor]
    print()
    if not meeting:
        print("no threshold reaches a %.2f recall floor: the ranking is too weak at this sample size."
              % args.recall_floor)
        print("AUC=%.4f means the ceiling is real; more labelled windows or a fine-tuned checkpoint "
              "is required, not a different threshold." % (auc(probabilities, labels) or 0.0))
    else:
        # Highest threshold meeting the floor = quietest acceptable configuration.
        best = max(meeting, key=lambda r: r["threshold"])
        print("Quietest point meeting recall >= %.2f:" % args.recall_floor)
        print("  threshold=%.4f  recall=%s  fpr=%s  precision=%s  alerts/1000 windows=%.1f"
              % (best["threshold"], best["recall"], best["fpr"], best["precision"],
                 best["alerts_per_1000_windows"]))
        print("  price: %s false pages per real incident." % (
            "n/a" if not best["precision"] else "%.2f" % (1.0 / best["precision"] - 1.0)))
        print()

    # The configuration an operator would actually run: bound the noise, and report what recall
    # that buys. Precision bound rather than recall bound, because alert fatigue is the failure
    # mode that kills real-time detection systems in practice — a detector nobody reads catches
    # nothing, however good its recall looks on paper.
    affordable = [r for r in rows if (r["fpr"] or 1.0) <= args.max_fpr and r["tp"] > 0]
    if affordable:
        best_fpr = max(affordable, key=lambda r: r["recall"] or 0.0)
        print("Best point with fpr <= %.2f (the noise an operator can realistically absorb):" % args.max_fpr)
        print("  BRIAN_GATE_ANOMALY_P=%.4f" % best_fpr["threshold"])
        print("  BRIAN_GATE_NOTIFY_P=%.4f" % best_fpr["threshold"])
        print("  recall=%s  fpr=%s  precision=%s  alerts/1000 windows=%.1f"
              % (best_fpr["recall"], best_fpr["fpr"], best_fpr["precision"],
                 best_fpr["alerts_per_1000_windows"]))
        print()
        print("Set these deliberately. They are the false-page versus missed-incident trade for this")
        print("domain, and they belong in a config file with this table next to them — not in a")
        print("default inherited from a sigmoid's midpoint.")
    else:
        print("No threshold reaches fpr <= %.2f while catching anything: at this sample the model"
              % args.max_fpr)
        print("cannot be operated quietly. Keep the gate escalating to System 2 and fine-tune.")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"sweep": rows}, indent=2), encoding="utf-8")
    print()
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
