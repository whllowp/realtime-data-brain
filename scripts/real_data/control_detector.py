"""The honest control: how far does a one-line threshold get on the same windows?

Why this file exists
--------------------
Every round reports the system's AUC, but an AUC means nothing without a reference. The reference that
matters is not random guessing — it is the thing an operator would reach for if the model were not there:
"page me if CPU is above X". On this dataset that baseline is strong, because most labelled faults show
up as a level excursion in one or two metrics.

The control must be evaluated on **exactly the same windows** as the system, with the same label rule and
the same window statistic, or the comparison is rigged. That is why this is a script and not a comment: an
earlier round compared a 5-sample-mean threshold against a digest built from the last sample and drew the
wrong conclusion about which was ahead.

Reports, for each candidate feature: level AUC, and recall at a 10% false-positive budget.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402

DATA = REPO_ROOT / "data" / "smd"
COLUMNS = {"cpu_r": 1, "cpu_s": 2, "load_1": 4, "mem_free": 7, "swap_used": 13,
           "disk_io_write": 15, "net_in_bytes": 22, "net_out_bytes": 23}


def windows(rows: List[List[float]], labels: List[int], window_rows: int, reduce: str):
    """Yield (start_row, {feature: value}, label) for tumbling windows."""
    for start in range(0, len(rows) - window_rows + 1, window_rows):
        chunk = rows[start:start + window_rows]
        values: Dict[str, float] = {}
        for name, column in COLUMNS.items():
            series = [row[column - 1] for row in chunk]
            if reduce == "max":
                values[name] = max(series)
            elif reduce == "min":
                values[name] = min(series)
            elif reduce == "mean":
                values[name] = statistics.mean(series)
            else:
                values[name] = series[-1]
        yield start, values, 1 if any(labels[start:start + window_rows]) else 0


def recall_at_fpr(scores: List[float], truth: List[int], max_fpr: float
                  ) -> Tuple[float, float, float]:
    positives = sum(truth)
    negatives = len(truth) - positives
    best = (0.0, 0.0, 0.0)
    for threshold in sorted(set(scores), reverse=True):
        tp = sum(1 for s, y in zip(scores, truth) if y and s >= threshold)
        fp = sum(1 for s, y in zip(scores, truth) if not y and s >= threshold)
        if not tp:
            continue
        fpr = fp / negatives if negatives else 0.0
        if fpr <= max_fpr:
            recall = tp / positives
            if recall > best[0]:
                best = (recall, fpr, threshold)
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="machine-1-6")
    parser.add_argument("--window-s", type=int, default=600)
    parser.add_argument("--max-fpr", type=float, default=0.10)
    parser.add_argument("--reduce", default="last", choices=["last", "max", "min", "mean"])
    args = parser.parse_args()

    rows = [[float(v) for v in line.split(",")]
            for line in (DATA / ("%s.data.txt" % args.machine)).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    labels = [int(t) for t in (DATA / ("%s.label.txt" % args.machine)).read_text().split()]
    window_rows = max(1, args.window_s // 60)

    data = list(windows(rows, labels, window_rows, args.reduce))
    truth = [label for _, _, label in data]
    positives = sum(truth)
    print("machine      %s   window %ds (%d samples)  reduction=%s"
          % (args.machine, args.window_s, window_rows, args.reduce))
    print("windows      %d  (anomalous %d = %.1f%%)" % (len(truth), positives,
                                                        100 * positives / len(truth)))
    print("")
    print("%-16s %10s %10s %10s %12s" % ("feature", "AUC", "recall", "fpr", "threshold"))
    print("-" * 64)
    best = None
    for name in COLUMNS:
        scores = [values[name] for _, values, _ in data]
        recall, fpr, threshold = recall_at_fpr(scores, truth, args.max_fpr)
        feature_auc = auc(scores, truth)
        print("%-16s %10.4f %10.4f %10.4f %12.4f"
              % (name, feature_auc, recall, fpr, threshold))
        if best is None or recall > best[1]:
            best = (name, recall, fpr, threshold, feature_auc)

    print("")
    print("best single-feature rule: %s  recall %.4f at FPR %.4f (AUC %.4f, cut %.4f)"
          % (best[0], best[1], best[2], best[4], best[3]))
    print("")
    print("This is the number the system has to beat. A model that cannot is not adding value over")
    print("a threshold alert on this data, however good its AUC looks in isolation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
