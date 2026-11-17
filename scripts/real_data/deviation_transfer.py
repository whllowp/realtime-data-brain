"""Does the deviation reader's 10%-budget cut survive a holdout?

`deviation_reader.py` fits one cut on all 474 windows of a recording and reports 0.142 recall at a
9.8% false-positive rate. That number shares windows with the cut it produced, so it is a consistency
result, not a generalisation result — boundary 12 in `docs/RSI_LOOP.zh.md` says exactly that and this
script is what closes it.

Two ways to turn scores into an alert, fitted on one set and evaluated on another:

* **pooled** — the highest-recall cut whose false-positive rate sits inside the budget. This uses the
  labels of the fitting set, which a deployment does not have; it is the optimistic bound.
* **quantile** — keep the top `budget` share of scoring windows. This needs no labels, so it is the
  only version that could ship.

And three splits, in increasing order of difficulty: in time within a recording, across hosts, and
leave-one-host-out.

Everything here is a pure function of the digests, so no Laya call is involved and the numbers are
reproducible from `data/smd/stream.json` alone.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deviation_reader import score_stream  # noqa: E402

from brian.calibration import auc  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.evolve import apply_overrides  # noqa: E402
from brian.streams import load_stream_file  # noqa: E402

Rows = Sequence[Tuple[float, bool]]
Fitter = Callable[[Rows, float], Optional[float]]


def fit_cut(rows: Rows, budget: float) -> Optional[float]:
    """The highest-recall cut whose false-positive rate stays inside `budget` (needs labels)."""
    positives = sum(1 for _p, truth in rows if truth)
    negatives = len(rows) - positives
    if not positives or not negatives:
        return None
    best: Optional[Tuple[float, float]] = None           # (recall, cut)
    tp = fp = 0
    ordered = sorted(rows, key=lambda row: -row[0])
    index = 0
    while index < len(ordered):
        cut = ordered[index][0]
        while index < len(ordered) and ordered[index][0] == cut:
            truth = ordered[index][1]
            tp += 1 if truth else 0
            fp += 0 if truth else 1
            index += 1
        recall = tp / positives
        if fp / negatives <= budget + 1e-12 and (best is None or recall > best[0]):
            best = (recall, cut)
    return None if best is None else best[1]


def quantile_cut(rows: Rows, budget: float) -> Optional[float]:
    """The cut that would alert on `budget` share of windows if the same mix recurred.

    A deployment does not know which windows are faults, so this is the only cut it can compute:
    keep the top share of scores. If the ranking is stable, this rule is stable even when the
    absolute level of the score is not.
    """
    if not rows:
        return None
    ordered = sorted((p for p, _truth in rows), reverse=True)
    index = min(len(ordered) - 1, max(0, int(round(budget * len(ordered)))))
    return ordered[index]


def evaluate(rows: Rows, cut: Optional[float]) -> Dict[str, Optional[float]]:
    if cut is None:
        return {"recall": None, "fpr": None, "alerts": None}
    positives = sum(1 for _p, truth in rows if truth)
    negatives = len(rows) - positives
    tp = sum(1 for p, truth in rows if truth and p >= cut)
    fp = sum(1 for p, truth in rows if not truth and p >= cut)
    return {
        "recall": tp / positives if positives else None,
        "fpr": fp / negatives if negatives else None,
        "alerts": tp + fp,
    }


def _fmt(value: Optional[float], digits: int = 3) -> str:
    return "n/a" if value is None else ("%.*f" % (digits, value))


def _line(label: str, fit: Rows, test: Rows, budget: float, fitter: Fitter) -> List[str]:
    cut = fitter(fit, budget)
    inside = evaluate(fit, cut)
    outside = evaluate(test, cut)
    return [label,
            "n=%d" % len(fit), "n=%d" % len(test),
            _fmt(cut, 4), _fmt(inside["recall"]), _fmt(inside["fpr"]),
            _fmt(outside["recall"]), _fmt(outside["fpr"])]


def rolling_rule(rows: Rows, budget: float, window: int) -> Dict[str, Optional[float]]:
    """Alert when the score is in the top `budget` share of the previous `window` scores.

    No labels, no future, no constant: only the ordering of the last `window` windows is used, so a
    drift in the absolute level cannot silently turn the detector off. The first `window` windows are
    a warm-up and are not scored, which is why the sample here is smaller than the table above.
    """
    tp = fp = positives = negatives = 0
    for index in range(window, len(rows)):
        trail = sorted((p for p, _truth in rows[index - window:index]), reverse=True)
        position = min(len(trail) - 1, max(0, int(round(budget * len(trail)))))
        cut = trail[position]
        score, truth = rows[index]
        if truth:
            positives += 1
            tp += 1 if score >= cut else 0
        else:
            negatives += 1
            fp += 1 if score >= cut else 0
    return {"recall": tp / positives if positives else None,
            "fpr": fp / negatives if negatives else None,
            "alerts": tp + fp,
            "scored": positives + negatives}


def table(title: str, lines: List[List[str]], header: Sequence[str]) -> None:
    width = [max(len(h), 7) for h in header]
    print("")
    print(title)
    print("  ".join(h.ljust(w) for h, w in zip(header, width)))
    for line in lines:
        print("  ".join(value.ljust(w) for value, w in zip(line, width)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", default="data/smd/stream.json")
    parser.add_argument("--budget", type=float, default=0.10)
    args = parser.parse_args()

    loaded = load_stream_file(ROOT / args.stream)
    settings = apply_overrides(load_settings(), loaded.overrides)
    scored: Dict[str, List[Tuple[float, bool]]] = {
        stream.name: score_stream(stream, settings) for stream in loaded.streams}
    names = list(scored)

    print("=" * 96)
    print("deviation reader - does the %.0f%% operating point survive a holdout?  (%s)"
          % (args.budget * 100, args.stream))
    print("=" * 96)
    for name in names:
        rows = scored[name]
        print("  %s: %d windows, %d fault, auc %.3f"
              % (name, len(rows), sum(1 for _p, t in rows if t),
                 auc([p for p, _t in rows], [t for _p, t in rows]) or 0.0))

    transfer_header = ["split", "fit", "test", "cut", "recall(in)", "fpr(in)", "recall(out)", "fpr(out)"]
    for label, fitter in (("pooled (labels of the fitting set)", fit_cut),
                          ("quantile (no labels)", quantile_cut)):
        lines: List[List[str]] = []
        for name in names:
            rows = scored[name]
            half = len(rows) // 2
            lines.append(_line("%s first->second" % name, rows[:half], rows[half:], args.budget, fitter))
            lines.append(_line("%s second->first" % name, rows[half:], rows[:half], args.budget, fitter))
        for fit_name in names:
            for test_name in names:
                if fit_name != test_name:
                    lines.append(_line("%s -> %s" % (fit_name, test_name),
                                       scored[fit_name], scored[test_name], args.budget, fitter))
        if len(names) > 2:
            for held in names:
                others = [row for name in names if name != held for row in scored[name]]
                lines.append(_line("all but %s -> %s" % (held, held), others, scored[held],
                                   args.budget, fitter))
        table("cut fitted by %s" % label, lines, transfer_header)

    drift: List[List[str]] = []
    for name in names:
        rows = scored[name]
        cut = fit_cut(rows, args.budget)
        quarters = [rows[i * len(rows) // 4:(i + 1) * len(rows) // 4] for i in range(4)]
        for index, quarter in enumerate(quarters, start=1):
            if not quarter:
                continue
            scores = sorted(p for p, _t in quarter)
            result = evaluate(quarter, cut)
            drift.append(["%s q%d" % (name.split("-")[-1], index), "n=%d" % len(quarter),
                          "%.4f" % scores[len(scores) // 2],
                          "%.4f" % scores[min(len(scores) - 1, int(0.9 * len(scores)))],
                          _fmt(result["recall"]), _fmt(result["fpr"])])
    table("level drift inside a recording, measured at the whole-recording %.0f%% cut"
          % (args.budget * 100), drift,
          ["recording", "windows", "median", "p90", "recall@cut", "fpr@cut"])

    # The rule a deployment can actually run: no labels, no fitted constant, only the ordering of the
    # last K scores. It is causal by construction, so the numbers below are out-of-sample by
    # definition — every window is scored using scores that existed before it.
    rolling: List[List[str]] = []
    for name in names:
        rows = scored[name]
        fixed = evaluate(rows, fit_cut(rows, args.budget))
        rolling.append(["%s fixed cut" % name, "n=%d" % len(rows), "-",
                        _fmt(fixed["recall"]), _fmt(fixed["fpr"]), "in-sample"])
        for window in (25, 50, 100):
            result = rolling_rule(rows, args.budget, window)
            rolling.append(["%s rolling K=%d" % (name, window), "n=%d" % (result["scored"] or 0),
                            "%d" % (result["alerts"] or 0), _fmt(result["recall"]),
                            _fmt(result["fpr"]), "causal"])
    table("top-%.0f%% of the last K scores, against the fitted cut" % (args.budget * 100), rolling,
          ["rule", "scored", "alerts", "recall", "fpr", "kind"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
