"""Does the model add information a threshold does not already have?

The rounds so far compared AUCs. That answers "which ranks better", not the question that decides whether
this architecture is worth its cost: **is the model's signal redundant with the threshold's, or does it
carry something extra?**

The test is a blend. Rank the windows by the model and by the control, average the two rankings, and score
the result:

  * blend ≈ best single  -> the two says the same thing; the model is an expensive threshold.
  * blend > best single  -> they are looking at different things; the model contributes.
  * blend ≈ average     -> they disagree in a way that adds noise, which is also informative.

Rank averaging is used rather than probability averaging because the two scores live on incomparable scales
(a probability in 0.25-0.47, a normalised resource level in 0.00-1.00).

Alignment matters: the control is recomputed from the raw series for exactly the windows the model judged,
and the label sequence is compared before anything is scored. A silent off-by-one here would produce a
respectable-looking number that means nothing.

Usage:
    python scripts/real_data/incremental_value.py data/smd/bench_product_r6_m22.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402

DATA = REPO_ROOT / "data" / "smd"
COLUMNS = {"cpu_r": 1, "load_1": 4, "mem_free": 7}


def ranked(values: Sequence[float]) -> List[float]:
    """Average-rank transform: comparable between scores with different units."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks


def recall_at(scores: Sequence[float], labels: Sequence[int], max_fpr: float) -> Tuple[float, float]:
    positives = sum(labels)
    negatives = len(labels) - positives
    best = (0.0, 1.0)
    for threshold in sorted(set(scores), reverse=True):
        tp = sum(1 for s, y in zip(scores, labels) if y and s >= threshold)
        fp = sum(1 for s, y in zip(scores, labels) if not y and s >= threshold)
        if not tp:
            continue
        fpr = fp / negatives if negatives else 0.0
        if fpr <= max_fpr and tp / positives > best[0]:
            best = (tp / positives, fpr)
    return best


def control_scores(machine: str, window_s: int, warmup: int, feature: str) -> Tuple[List[float], List[int]]:
    """Recompute the control on exactly the windows the bench judged, in the same order."""
    rows = [[float(v) for v in line.split(",")]
            for line in (DATA / ("%s.data.txt" % machine)).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    labels = [int(t) for t in (DATA / ("%s.label.txt" % machine)).read_text().split()]
    window_rows = max(1, window_s // 60)
    scores: List[float] = []
    truth: List[int] = []
    index = 0
    # `range(0, len(rows), window_rows)` rather than stopping at the last full window: the windower flushes
    # a final partial window, and that window is judged like any other. Omitting it produced 2,338
    # recomputed windows against the run's 2,339, which the alignment check caught instead of quietly
    # scoring a shifted series.
    for start in range(0, len(rows), window_rows):
        if index >= warmup:
            chunk = rows[start:start + window_rows]
            if chunk:
                scores.append(chunk[-1][COLUMNS[feature] - 1])  # `last`, matching the product reduction
                truth.append(1 if any(labels[start:start + window_rows]) else 0)
        index += 1
    return scores, truth


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("--feature", default="load_1", help="control metric (load_1 was best on 1-6)")
    parser.add_argument("--max-fpr", type=float, default=0.10)
    args = parser.parse_args()

    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    model = list(run["probabilities"])
    truth = list(run["labels"])
    window_s = run.get("window_s", 600)
    warmup = run.get("warmup", 30)

    control, control_truth = control_scores(run["machine"], window_s, warmup, args.feature)
    if truth != control_truth:
        raise SystemExit(
            "alignment failure: the recomputed labels do not match the run (%d vs %d windows). "
            "Refusing to report a number computed on misaligned windows."
            % (len(truth), len(control_truth))
        )
    print("alignment     %d windows, labels verified against the run" % len(truth))
    print("machine       %s   window %ds   control=%s" % (run["machine"], window_s, args.feature))
    print("")

    model_ranks = ranked(model)
    control_ranks = ranked(control)
    blend = [(a + b) / 2.0 for a, b in zip(model_ranks, control_ranks)]

    rows = [("model only", model, model_ranks),
            ("control only", control, control_ranks),
            ("blend", blend, blend)]
    print("%-14s %8s %10s %8s" % ("ranker", "AUC", "recall", "fpr"))
    print("-" * 44)
    results = {}
    for name, scores, _ in rows:
        recall, fpr = recall_at(scores, truth, args.max_fpr)
        results[name] = auc(scores, truth)
        print("%-14s %8.4f %10.4f %8.4f" % (name, results[name], recall, fpr))

    best_single = max(results["model only"], results["control only"])
    print("")
    print("blend - best single = %+.4f" % (results["blend"] - best_single))

    # Paired bootstrap over windows: does the blend beat the better single ranker by more than noise?
    rng = random.Random(5)
    n = len(truth)
    diffs: List[float] = []
    for _ in range(300):
        sample = [rng.randrange(n) for _ in range(n)]
        if sum(truth[i] for i in sample) < 2:
            continue
        s_truth = [truth[i] for i in sample]
        blend_score = auc([blend[i] for i in sample], s_truth)
        model_score = auc([model[i] for i in sample], s_truth)
        control_score = auc([control[i] for i in sample], s_truth)
        if blend_score is None or model_score is None or control_score is None:
            continue
        diffs.append(blend_score - max(model_score, control_score))
    if diffs:
        diffs.sort()
        print("paired bootstrap 95%% CI on that difference: [%+.4f, %+.4f]"
              % (diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]))
        if diffs[int(0.025 * len(diffs))] > 0:
            print("-> the model contributes information the threshold does not have.")
        elif diffs[int(0.975 * len(diffs))] < 0:
            print("-> the blend is worse than the best single ranker; the model adds noise here.")
        else:
            print("-> indistinguishable: the model's signal is redundant with this threshold on this data.")
    print("")
    print("A blend that merely matches the best single ranker is the expensive answer: it means the")
    print("model is reproducing a threshold that needs no GPU, no calibration and no explanation layer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
