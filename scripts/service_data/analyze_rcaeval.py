"""Analyse the RCAEval RE1-OB run: does the system beat a threshold on the same service windows?

The bench records, for every judged window, the engine's probability and the raw `cpu`, `latency_ms` and
`load` that produced the digest. That makes the comparison that matters possible on *identical* windows:

    the engine                       p                    (one call per window)
    a one-line threshold on cpu      cpu >= t
    a one-line threshold on latency  latency_ms >= t
    a one-line threshold on load     |load - median| (traffic anomalies)

Reported per fault family, because the dataset covers five injection types and pooling them hides the
structure: a CPU-stress fault should be trivial for a cpu threshold and may be invisible to a model that
was fine-tuned on error/latency patterns; a delay fault is the opposite.

Also reports, for each fault family, the fraction of *fault* windows whose p exceeded the best normal-window
p — a detection rate that does not depend on choosing a threshold globally.

Usage:
    python scripts/service_data/analyze_rcaeval.py data/rcaeval/report_full.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402


def at_fpr(scores: Sequence[float], labels: Sequence[int], max_fpr: float) -> Tuple[float, float]:
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


def summarise(rows: List[dict], name: str) -> Dict[str, Optional[float]]:
    labels = [row["label"] for row in rows]
    if len(set(labels)) < 2:
        return {"name": name, "auc": None, "recall": None, "fpr": None}
    out: Dict[str, Optional[float]] = {"name": name}
    p = [row["p"] for row in rows]
    out["auc"] = auc(p, [bool(y) for y in labels])
    recall, fpr = at_fpr(p, labels, 0.10)
    out["recall"], out["fpr"] = recall, fpr
    return out


def control(rows: List[dict], field: str) -> Dict[str, Optional[float]]:
    usable = [row for row in rows if row.get(field) is not None]
    if len(usable) < len(rows) * 0.5 or len({row["label"] for row in usable}) < 2:
        return {"name": field, "auc": None, "recall": None, "fpr": None}
    labels = [row["label"] for row in usable]
    scores = [float(row[field]) for row in usable]
    result: Dict[str, Optional[float]] = {"name": field}
    result["auc"] = auc(scores, [bool(y) for y in labels])
    recall, fpr = at_fpr(scores, labels, 0.10)
    result["recall"], result["fpr"] = recall, fpr
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    args = parser.parse_args()

    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = data["rows"]
    print("dataset   %s   windows=%d  anomalous=%d  cases=%d  model=%s"
          % (data["dataset"], data["windows"], data["anomalous"], data["cases"], data["model"]))
    print("")

    overall = summarise(rows, "engine")
    print("overall AUC        %s" % ("%.4f" % overall["auc"] if overall["auc"] else "n/a"))
    print("")

    families: Dict[str, List[dict]] = {}
    for row in rows:
        families.setdefault(row["fault"], []).append(row)

    print("%-8s %6s %9s %9s | %8s %8s %8s | %8s"
          % ("fault", "win", "engine", "eng@10", "cpu", "latency", "load", "cpu@10"))
    print("-" * 88)
    for fault, group in sorted(families.items()):
        engine = summarise(group, "engine")
        cpu = control(group, "cpu")
        latency = control(group, "latency_ms")
        load = control(group, "load")
        print("%-8s %6d %9s %9s | %8s %8s %8s | %8s"
              % (fault, len(group),
                 "%.4f" % engine["auc"] if engine["auc"] else "-",
                 "%.3f" % engine["recall"] if engine["recall"] is not None else "-",
                 "%.4f" % cpu["auc"] if cpu["auc"] else "-",
                 "%.4f" % latency["auc"] if latency["auc"] else "-",
                 "%.4f" % load["auc"] if load["auc"] else "-",
                 "%.3f" % cpu["recall"] if cpu["recall"] is not None else "-"))

    print("")
    print("best achievable on each signal (AUC), and what the engine got: engine vs max(control)")
    for fault, group in sorted(families.items()):
        engine_auc = summarise(group, "engine")["auc"]
        controls = [control(group, field)["auc"] for field in ("cpu", "latency_ms", "load")]
        best = max([value for value in controls if value is not None], default=None)
        if engine_auc is None or best is None:
            continue
        verdict = "engine ahead" if engine_auc > best else "control ahead"
        print("  %-8s engine %.4f   best control %.4f   (%s)" % (fault, engine_auc, best, verdict))

    print("")
    print("per-case detection (fraction of cases where some fault window scored above every normal window):")
    by_case: Dict[str, List[dict]] = {}
    for row in rows:
        by_case.setdefault(row["case"], []).append(row)
    detected = 0
    total = 0
    for case, group in by_case.items():
        fault_rows = [r for r in group if r["label"] == 1]
        normal_rows = [r for r in group if r["label"] == 0]
        if not fault_rows or not normal_rows:
            continue
        total += 1
        if max(r["p"] for r in fault_rows) > max(r["p"] for r in normal_rows):
            detected += 1
    print("  %d/%d cases (%.1f%%) had a fault window above every normal window"
          % (detected, total, 100 * detected / total if total else 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
