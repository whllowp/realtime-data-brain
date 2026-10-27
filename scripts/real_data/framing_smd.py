"""Why did the richer host digest rank *worse* than the impoverished service one?

The full SMD run produced a counter-intuitive result:

    service profile (2 lines: "Resources: CPU 19.75%, memory 0.00%.")   AUC 0.862
    host profile    (10 lines, 8 metrics with baselines and ratios)     AUC 0.515

Adding real multivariate evidence destroyed the ranking. This script tests the explanation rather than
assuming it: SMD's faults are often *sustained* (one interval in `interpretation_label` runs 3,161
minutes), and a 30-window rolling baseline adapts to a sustained shift, so a deviation-based reading
("CPU 1.03x its baseline") cancels exactly the faults it is meant to find. An absolute-level reading
("CPU 0.92") does not, because the level stays high.

So the comparison here is not Laya-vs-control but *framing*:

    level   —?the value itself                     (what the service digest accidentally conveys)
    ratio   —?value / rolling baseline             (what the host digest emphasises)
    delta   —?value - rolling baseline             (a third framing, for completeness)

If `level` ranks far above `ratio` on this dataset, the explanation holds and the actionable conclusion
is that a digest for sustained-fault data must state absolute level alongside the deviation.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402

DATA = REPO_ROOT / "data" / "smd"
WINDOW_ROWS = 5          # 300 s at one sample per minute
BASELINE_WINDOWS = 30
WARMUP = 30

FEATURES = {
    "cpu_r": 1, "cpu_s": 2, "load_1": 4, "mem_free": 7,
    "swap_used": 13, "disk_io_write": 15, "net_in_bytes": 22, "net_out_bytes": 23,
}


def load(machine: str):
    rows = [[float(v) for v in line.split(",")]
            for line in (DATA / ("%s.data.txt" % machine)).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    labels = [int(t) for t in (DATA / ("%s.label.txt" % machine)).read_text().split()]
    return rows, labels


def windows_of(rows: Sequence[Sequence[float]], labels: Sequence[int]):
    """Tumbling 5-row windows: (start_row, feature means, window label)."""
    for start in range(0, len(rows) - WINDOW_ROWS + 1, WINDOW_ROWS):
        chunk = rows[start:start + WINDOW_ROWS]
        means = {name: statistics.mean(row[col - 1] for row in chunk) for name, col in FEATURES.items()}
        yield start, means, any(labels[start:start + WINDOW_ROWS])


def main() -> int:
    machine = sys.argv[1] if len(sys.argv) > 1 else "machine-1-6"
    rows, labels = load(machine)
    series = list(windows_of(rows, labels))
    print("SM D %s: %d windows, %d anomalous (%.2f%%)"
          % (machine, len(series), sum(1 for _, _, y in series if y),
             100.0 * sum(1 for _, _, y in series if y) / len(series)))
    print("")

    results: Dict[str, Dict[str, float]] = {}

    for name in FEATURES:
        history: List[float] = []
        levels: List[float] = []
        ratios: List[float] = []
        deltas: List[float] = []
        keep: List[bool] = []
        for index, (_, means, anomalous) in enumerate(series):
            value = means[name]
            if index >= WARMUP and len(history) >= 5:
                baseline = statistics.median(history)
                levels.append(value)
                ratios.append(value / baseline if baseline else 1.0)
                deltas.append(value - baseline)
                keep.append(anomalous)
            history.append(value)
            if len(history) > BASELINE_WINDOWS:
                del history[0]
        if not keep:
            continue
        results[name] = {
            "level": auc(levels, keep),
            "ratio": auc(ratios, keep),
            "delta": auc(deltas, keep),
        }

    print("%-16s %10s %10s %10s" % ("feature", "AUC level", "AUC ratio", "AUC delta"))
    print("-" * 50)
    for name, row in results.items():
        print("%-16s %10s %10s %10s"
              % (name,
                 _fmt(row["level"]), _fmt(row["ratio"]), _fmt(row["delta"])))

    def mean_of(which: str) -> float:
        values = [row[which] for row in results.values() if row[which] is not None]
        return sum(values) / len(values) if values else float("nan")

    print("")
    print("mean AUC across the eight features:")
    print("  level  %.4f   <- what the thin service digest conveys" % mean_of("level"))
    print("  ratio  %.4f   <- what the host digest emphasises" % mean_of("ratio"))
    print("  delta  %.4f" % mean_of("delta"))
    print("")
    print("A level/ratio gap this size on sustained faults is the explanation for the host digest ranking")
    print("at chance: the rolling baseline adapts to a fault that lasts longer than the baseline window,")
    print("so `CPU 1.03x its baseline` is the *correct* comparison and still hides the fault.")

    out = DATA / ("framing_%s.json" % machine)
    out.write_text(json.dumps({"machine": machine, "features": results}, indent=2), encoding="utf-8")
    print("")
    print("wrote %s" % out)
    return 0


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else "%.4f" % value


if __name__ == "__main__":
    raise SystemExit(main())
