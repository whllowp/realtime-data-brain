"""Compare two runs at a fixed false-positive budget, with a paired bootstrap.

Why not just AUC
----------------
AUC is threshold-free, which is why it was the right metric for comparing *framings*. It is not the right
metric for comparing *operating points*: a change can leave the overall ordering slightly worse while
making the top of the ranking much more informative, which is exactly what the window-statistic
experiment did. At a false-positive budget of 10%, "last sample" gave recall 0.409 and "max over the
window" 0.495, while AUC moved the other way (0.758 -> 0.728).

Both numbers are real, so the decision needs a test that speaks to the operational question: **with the
threshold set to spend 10% of the false-positive budget, how much recall does each configuration buy, and
is the difference larger than resampling noise?** The bootstrap resamples *windows* and re-picks each
configuration's threshold on the resample, so it accounts for the threshold being fitted rather than
fixed.

Usage:
    python scripts/real_data/compare_operating.py A.json B.json --max-fpr 0.10
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import List, Sequence


def pick_threshold(probabilities: Sequence[float], labels: Sequence[int], max_fpr: float) -> float:
    """Highest-recall threshold whose false-positive rate stays inside the budget."""
    positives = sum(labels)
    negatives = len(labels) - positives
    best_threshold = 1.1
    best_recall = -1.0
    for threshold in sorted(set(probabilities), reverse=True):
        tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= threshold)
        fp = sum(1 for p, y in zip(probabilities, labels) if not y and p >= threshold)
        if not tp:
            continue
        if negatives and fp / negatives <= max_fpr and tp / positives > best_recall:
            best_recall = tp / positives
            best_threshold = threshold
    return best_threshold


def recall_at(probabilities: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    positives = sum(labels)
    if not positives:
        return 0.0
    tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= threshold)
    return tp / positives


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("a")
    parser.add_argument("b")
    parser.add_argument("--max-fpr", type=float, default=0.10)
    parser.add_argument("--rounds", type=int, default=400)
    args = parser.parse_args()

    run_a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    run_b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    if run_a["labels"] != run_b["labels"]:
        raise SystemExit("the two runs judged different windows; not comparable")

    pa, pb, labels = run_a["probabilities"], run_b["probabilities"], run_a["labels"]
    ta = pick_threshold(pa, labels, args.max_fpr)
    tb = pick_threshold(pb, labels, args.max_fpr)
    ra = recall_at(pa, labels, ta)
    rb = recall_at(pb, labels, tb)

    print("A %-40s recall %.4f at P>=%.4f" % (Path(args.a).name, ra, ta))
    print("B %-40s recall %.4f at P>=%.4f" % (Path(args.b).name, rb, tb))
    print("difference A - B = %+.4f" % (ra - rb))

    rng = random.Random(11)
    n = len(labels)
    diffs: List[float] = []
    for _ in range(args.rounds):
        # Paired resample: the same window indices for both configurations, so the comparison is not
        # diluted by their different sampling noise.
        sample = [rng.randrange(n) for _ in range(n)]
        sa = [pa[i] for i in sample]
        sb = [pb[i] for i in sample]
        sl = [labels[i] for i in sample]
        if sum(sl) < 2:
            continue
        sa_t = pick_threshold(sa, sl, args.max_fpr)
        sb_t = pick_threshold(sb, sl, args.max_fpr)
        diffs.append(recall_at(sa, sl, sa_t) - recall_at(sb, sl, sb_t))

    if not diffs:
        print("not enough positive windows to bootstrap")
        return 2
    diffs.sort()
    low = diffs[int(0.025 * len(diffs))]
    high = diffs[int(0.975 * len(diffs)) - 1]
    print("paired bootstrap 95%% CI on the difference: [%+.4f, %+.4f]" % (low, high))
    if low > 0:
        print("-> A buys more recall at the same false-positive budget, beyond resampling noise.")
    elif high < 0:
        print("-> B buys more recall at the same false-positive budget, beyond resampling noise.")
    else:
        print("-> the difference is inside resampling noise; no winner at this sample size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
