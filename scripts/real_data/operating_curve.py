"""Operating curve for a saved run, plus the calibration question.

Two separate things get called "the threshold", and conflating them has already cost this project once:

  * **Where to cut.** Given a ranking, the cut decides how many alerts are raised. It is a product
    decision constrained by analyst capacity, not by the model.
  * **Whether the number means anything.** A probability of 0.55 should mean "55% of such windows are
    anomalous". On this checkpoint it does not: the shipped temperatures were inherited from the base
    checkpoint and the model's probabilities here occupy a 0.20-0.50 band.

So this script reports the full curve (recall/precision at every false-positive budget) and then asks
what Platt scaling fitted *on this domain* would change. Platt is a monotone transform, so it cannot move
AUC; it can only relocate the cut and improve the probability's meaning. Any table claiming calibration
improved "accuracy" is wrong by construction — this script prints AUC before and after to make that
visible.

Usage:
    python scripts/real_data/operating_curve.py data/smd/bench_product_prod_full.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc, expected_calibration_error, fit_calibration  # noqa: E402


def curve(probabilities: Sequence[float], labels: Sequence[int]) -> List[Tuple[float, float, float, float]]:
    """(threshold, recall, fpr, precision) for every distinct score, highest first."""
    positives = sum(labels)
    negatives = len(labels) - positives
    rows = []
    for threshold in sorted(set(probabilities), reverse=True):
        tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= threshold)
        fp = sum(1 for p, y in zip(probabilities, labels) if not y and p >= threshold)
        if not tp:
            continue
        rows.append((threshold, tp / positives, fp / negatives if negatives else 0.0,
                     tp / (tp + fp)))
    return rows


def at_budget(rows, max_fpr: float):
    eligible = [row for row in rows if row[2] <= max_fpr]
    if not eligible:
        return None
    return max(eligible, key=lambda row: row[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("--budgets", default="0.01,0.02,0.05,0.10,0.20")
    args = parser.parse_args()

    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    probabilities: List[float] = run["probabilities"]
    labels: List[int] = run["labels"]
    positives = sum(labels)
    negatives = len(labels) - positives

    print("run             %s" % Path(args.run).name)
    print("config          digest=%s features=%s model=%s long=%s"
          % (run.get("digest"), run.get("n_features"), run.get("model", "-"),
             run.get("long_windows", "-")))
    print("windows         %d  (anomalous %d = %.1f%%)" % (len(labels), positives,
                                                           100 * positives / len(labels)))
    print("p range         %.4f .. %.4f" % (min(probabilities), max(probabilities)))
    print("AUC             %.4f" % auc(probabilities, labels))
    print("")

    rows = curve(probabilities, labels)
    budget_list = [float(b) for b in args.budgets.split(",") if b.strip()]
    print("%-10s %-10s %-10s %-10s %s" % ("FPR<=%", "threshold", "recall", "precision", "alerts/1000"))
    print("-" * 64)
    for budget in budget_list:
        best = at_budget(rows, budget)
        if best is None:
            print("%-10.2f %-10s %-10s %-10s %s" % (budget * 100, "n/a", "n/a", "n/a", "n/a"))
            continue
        threshold, recall, fpr, precision = best
        alerts_per_1000 = (recall * positives + fpr * negatives) / len(labels) * 1000
        print("%-10.2f %-10.4f %-10.4f %-10.4f %.0f"
              % (budget * 100, threshold, recall, precision, alerts_per_1000))

    print("")
    print("what a 0.5 cut would do (the default an unconfigured deployment gets):")
    at_half = [row for row in rows if row[0] >= 0.5]
    if not at_half:
        print("  no window scores at or above 0.5 — every window would be reported normal.")
        print("  This is the silent-no-fire case: the gate looks healthy because it never speaks.")
    else:
        threshold, recall, fpr, precision = at_half[-1]
        print("  recall %.4f at FPR %.4f (%.0f alerts per 1000 windows)"
              % (recall, fpr, (recall * positives + fpr * negatives) / len(labels) * 1000))

    # -- what calibration can and cannot do ----------------------------------
    print("")
    print("calibration fitted on this domain:")
    try:
        fit = fit_calibration(probabilities, [bool(y) for y in labels], domain="smd-host")
    except ValueError as exc:
        print("  refused: %s" % exc)
        return 0
    calibrated = [fit.apply(p) for p in probabilities]
    ece_raw = expected_calibration_error(probabilities, [bool(y) for y in labels])
    ece_cal = expected_calibration_error(calibrated, [bool(y) for y in labels])
    print("  slope %.4f  intercept %+.4f" % (fit.slope, fit.intercept))
    print("  ECE   raw %.4f -> calibrated %.4f" % (ece_raw, ece_cal))
    print("  AUC   raw %.4f -> calibrated %.4f   (must be identical: Platt is monotone)"
          % (auc(probabilities, labels), auc(calibrated, labels)))
    print("  log loss %.4f -> %.4f"
          % (log_loss(probabilities, labels), log_loss(calibrated, labels)))
    print("")
    print("  the cut moves, the ranking does not:")
    raw_best = at_budget(rows, 0.10)
    cal_rows = curve(calibrated, labels)
    cal_best = at_budget(cal_rows, 0.10)
    if raw_best and cal_best:
        print("    raw         P>=%.4f  recall %.4f  precision %.4f"
              % (raw_best[0], raw_best[1], raw_best[3]))
        print("    calibrated  P>=%.4f  recall %.4f  precision %.4f"
              % (cal_best[0], cal_best[1], cal_best[3]))
        print("    same windows, same recall — only the number on the dial changed.")
    return 0


def log_loss(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    epsilon = 1e-12
    total = 0.0
    for p, y in zip(probabilities, labels):
        q = min(max(p, epsilon), 1 - epsilon)
        total += -math.log(q if y else 1 - q)
    return total / max(len(labels), 1)


if __name__ == "__main__":
    raise SystemExit(main())
