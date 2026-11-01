"""Inspect saved bench runs: is a difference real, or is it noise?

Why this exists
---------------
A feature-count sweep produced AUC 0.391 / 0.727 / 0.633 / 0.412 / 0.381 for 2 / 3 / 4 / 5 / 8 features.
A lone spike with worse results either side admits two readings:

  1. exactly three features happen to help, or
  2. the differences are noise and no configuration was better.

They demand opposite responses, so the gap has to be measured against its own uncertainty before it is
acted on. With 242 positives out of 1,570 windows the standard error on AUC is roughly 0.035, so a 0.09
gap is barely resolvable and a 0.3 gap is not noise — but that is a calculation, not an impression.

This script reports, per run: the AUC, a bootstrap 95% confidence interval over windows, how many
*distinct* probabilities the model emitted (a degenerate model would show almost none), and then a
paired comparison between runs where both judged the same windows.

Usage:
    python scripts/real_data/inspect_run.py data/smd/bench_*.json
    python scripts/real_data/inspect_run.py --compare a.json b.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


def auc(probabilities: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Rank-based AUC with ties worth half, matching `brian.calibration.auc`."""
    positives = [p for p, y in zip(probabilities, labels) if y]
    negatives = [p for p, y in zip(probabilities, labels) if not y]
    if not positives or not negatives:
        return None
    # Counting via ranks is O(n log n) instead of O(n*m); needed because bootstrap repeats it.
    combined = sorted(
        [(p, 1) for p in positives] + [(p, 0) for p in negatives], key=lambda item: item[0]
    )
    rank_sum = 0.0
    index = 0
    position = 0
    while index < len(combined):
        end = index
        while end + 1 < len(combined) and combined[end + 1][0] == combined[index][0]:
            end += 1
        average_rank = (position + 1 + position + (end - index + 1)) / 2.0
        for i in range(index, end + 1):
            if combined[i][1] == 1:
                rank_sum += average_rank
        position += end - index + 1
        index = end + 1
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def bootstrap(probabilities: Sequence[float], labels: Sequence[int],
              rounds: int = 400, seed: int = 7) -> Tuple[float, float]:
    rng = random.Random(seed)
    pairs = list(zip(probabilities, labels))
    values: List[float] = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        value = auc([p for p, _ in sample], [y for _, y in sample])
        if value is not None:
            values.append(value)
    if not values:
        return (float("nan"), float("nan"))
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def tie_profile(probabilities: Sequence[float]) -> Tuple[int, int]:
    counts: Dict[float, int] = {}
    for p in probabilities:
        counts[p] = counts.get(p, 0) + 1
    return len(counts), max(counts.values())


def describe(path: Path) -> Dict[str, object]:
    run = json.loads(path.read_text(encoding="utf-8"))
    probabilities = run.get("probabilities")
    labels = run.get("labels")
    if not probabilities or not labels:
        raise SystemExit("%s has no per-window probabilities; re-run bench_smd.py to record them" % path)
    distinct, largest = tie_profile(probabilities)
    point = auc(probabilities, labels)
    low, high = bootstrap(probabilities, labels)
    positives = sum(labels)
    print("=" * 78)
    print("%s" % path.name)
    print("=" * 78)
    print("digest / features   %s / %d (%s)"
          % (run.get("digest"), run.get("n_features"), ",".join(run.get("features", []))))
    print("windows             %d  (anomalous %d, %.1f%%)"
          % (len(labels), positives, 100 * positives / len(labels)))
    print("AUC                 %.4f   bootstrap 95%% CI [%.4f, %.4f]"
          % (point, low, high))
    print("p range             %.4f .. %.4f" % (min(probabilities), max(probabilities)))
    print("distinct p          %d (%.1f%% of windows), largest tie %d (%.1f%%)"
          % (distinct, 100 * distinct / len(probabilities), largest, 100 * largest / len(probabilities)))
    print("recall @ FPR<=0.10  %s (precision %s)"
          % (run.get("recall_at_fpr"), run.get("precision_at_fpr")))
    return {"file": path.name, "auc": point, "low": low, "high": high, "n": len(labels),
            "distinct": distinct, "largest": largest, "probabilities": probabilities,
            "labels": labels}


def compare(left: Dict[str, object], right: Dict[str, object]) -> None:
    """Paired comparison. Both runs must have judged the same windows in the same order."""
    left_labels = left["labels"]
    right_labels = right["labels"]
    if left_labels != right_labels:
        print("cannot compare %s with %s: different windows were judged"
              % (left["file"], right["file"]))
        return
    a, b = left["probabilities"], right["probabilities"]
    assert isinstance(a, list) and isinstance(b, list)
    difference = (left["auc"] or 0) - (right["auc"] or 0)
    overlap = (left["low"] or 0) <= (right["high"] or 0) and (right["low"] or 0) <= (left["high"] or 0)
    print("")
    print("paired comparison")
    print("  A %-34s AUC %.4f" % (left["file"], left["auc"]))
    print("  B %-34s AUC %.4f" % (right["file"], right["auc"]))
    print("  A - B = %+.4f" % difference)
    print("  95%% CI overlap: %s" % ("YES" if overlap else "no"))
    if overlap:
        print("  -> the difference is inside what resampling alone produces; do not claim a winner.")
    else:
        print("  -> the difference survives resampling; it is worth acting on.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = parser.parse_args()

    if args.compare:
        left = describe(Path(args.compare[0]))
        right = describe(Path(args.compare[1]))
        compare(left, right)
        return 0

    paths = [Path(p) for p in args.files] or sorted(Path("data/smd").glob("bench_*.json"))
    rows = [describe(path) for path in paths if path.is_file()]
    print("")
    print("%-36s %7s %19s %8s %8s" % ("file", "AUC", "95% CI", "distinct", "largest"))
    print("-" * 84)
    for row in rows:
        print("%-36s %7.4f  [%.4f, %.4f] %8d %8d"
              % (row["file"], row["auc"], row["low"], row["high"], row["distinct"], row["largest"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
