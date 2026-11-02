"""Full service-side evaluation on RCAEval RE1-OB, in the production digest format.

For every case the faulted service's windows are driven through the shipped service path
(`Windower` + `BaselineTracker` + `build_digest(profile="service")`), and each window is judged by the
engine. Labels come from `inject_time.txt`: a window is anomalous once it starts at or after the injection.

Two design decisions that matter for honesty:

* **Per-case baselines.** Each case is its own recording, so the baseline tracker is reset per case.
  Sharing one tracker across cases would compare a service against a different run's history.
* **The control is computed on the same windows.** Every judged window also records the raw `cpu`,
  `latency_ms` and `load` that produced the digest, so a one-line threshold can be evaluated on exactly
  these windows instead of on a parallel dataset.

Output is one row per judged window; the analysis scripts (`operating_curve.py`, `compare_operating.py`,
`incremental_value.py`) consume it unchanged.

Usage:
    python scripts/service_data/bench_rcaeval.py --limit 20 --stride 1
    python scripts/service_data/bench_rcaeval.py            # all 125 cases
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "service_data"))

from brian.calibration import auc  # noqa: E402
from brian.config import CalibrationSettings, load_settings  # noqa: E402
from brian.laya.client import LayaClient, LayaError  # noqa: E402
from brian.state import BaselineTracker, Windower, build_digest  # noqa: E402

from rcaeval_to_events import DATA, SERVICE_GAUGES, fault_type, window_events  # noqa: E402

KEYS = tuple(spec.key for spec in SERVICE_GAUGES)


def run_case(case_dir: Path, args, client: LayaClient, settings) -> List[Dict[str, Any]]:
    events, inject, meta = window_events(case_dir, args.window_s)
    if not events:
        return []
    # Adaptive warm-up. The recordings are not all the same length: cpu/mem cases run ~2,100 s before the
    # fault, delay/disk/loss cases only ~360 s. A fixed 10-window warm-up therefore consumed the entire
    # pre-fault period of the short cases, which is how an earlier run labelled 100% of their windows
    # anomalous and scored them as pure false negatives. The baseline takes at most `--warmup` windows and
    # never more than half of the pre-fault period.
    first = events[0].event_time.timestamp()
    pre_fault_windows = max(1, int((inject - first) // args.window_s))
    warmup = max(1, min(args.warmup, pre_fault_windows // 2))
    windower = Windower(size_s=args.window_s, slide_s=args.window_s, allowed_lateness_s=0,
                        min_events=1, specs=SERVICE_GAUGES)
    baselines = BaselineTracker(window=max(3, min(args.baseline_windows, warmup)), keys=KEYS)

    rows: List[Dict[str, Any]] = []
    index = 0
    for event in events:
        windower.add(event)
        for aggregator in windower.ready():
            digest = build_digest(aggregator, baselines, profile="service")
            metrics = digest.metrics
            comparison = metrics.get("comparison") or {}

            def value(*path: str) -> Optional[float]:
                node: Any = comparison
                for part in path:
                    if not isinstance(node, dict) or part not in node:
                        return None
                    node = node[part]
                return node.get("value") if isinstance(node, dict) else None

            anomalous = aggregator.window_start.timestamp() >= inject
            baselines.observe(digest.service, metrics)
            index += 1
            if index <= warmup:
                continue
            if args.stride > 1 and (index - warmup) % args.stride:
                continue
            row = {
                "case": meta["case"], "service": meta["service"], "fault": meta["fault"],
                "window": aggregator.window_start.isoformat(),
                "label": 1 if anomalous else 0,
                "cpu": value("cpu", "util"),
                "latency_ms": value("latency_ms", "p99"),
                "load": value("requests", "rate"),
                "error_ratio": value("errors", "ratio"),
                "digest": digest.interpretation,
                "p": None, "error": None,
            }
            try:
                result = client.decide(digest, model=args.model or None, use_cache=False)
                answer = (result.answers.get("is_anomaly") or {}).get("noul")
                row["p"] = float(answer) if answer is not None else None
            except LayaError as exc:
                row["error"] = str(exc)
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fault", default="",
                        help="only cases whose fault family matches (cpu,mem,delay,disk,loss)")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--gate-p", type=float, default=0.5,
                        help="neutral decision threshold; calibration is disabled on this domain")
    parser.add_argument("--baseline-windows", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--out", default="data/rcaeval/report.json")
    args = parser.parse_args()

    cases = sorted(p for p in DATA.iterdir() if p.is_dir() and p.name.startswith("re1ob_"))
    if not cases:
        print("no cases under %s" % DATA)
        return 2
    if args.fault:
        wanted = [name.strip() for name in args.fault.split(",") if name.strip()]
        cases = [case for case in cases if fault_type(case.name) in wanted]
    if args.limit:
        cases = cases[:args.limit]
    print("cases     %d   window=%ds stride=%d workers=%d"
          % (len(cases), args.window_s, args.stride, args.workers))

    base = load_settings()
    settings = base.with_overrides(
        calibration=CalibrationSettings(path=base.calibration.path, enabled=False),
        gate=type(base.gate)(**{**vars(base.gate), "anomaly_probability": args.gate_p,
                                "notify_probability": args.gate_p}),
    )
    client = LayaClient(settings.laya)

    started = time.perf_counter()
    all_rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_case, case, args, client, settings) for case in cases]
        for index, future in enumerate(futures, 1):
            try:
                all_rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001
                print("  case failed: %s" % exc)
            if index % 10 == 0:
                print("  %d/%d cases, %d windows" % (index, len(cases), len(all_rows)))
    elapsed = time.perf_counter() - started

    judged = [row for row in all_rows if row["p"] is not None]
    if not judged:
        print("no decisions; is the engine running?")
        return 2
    labels = [row["label"] for row in judged]
    probabilities = [row["p"] for row in judged]
    positives = sum(labels)

    print("")
    print("windows judged  %d  (anomalous %d = %.1f%%)  in %.0fs (%.0f windows/s)"
          % (len(judged), positives, 100 * positives / len(judged), elapsed, len(judged) / elapsed))
    print("AUC             %s" % auc(probabilities, [bool(y) for y in labels]))

    by_fault: Dict[str, List[Dict[str, Any]]] = {}
    for row in judged:
        by_fault.setdefault(row["fault"], []).append(row)
    print("")
    print("%-10s %7s %8s %10s %10s" % ("fault", "windows", "anom%", "mean_p_fault", "mean_p_normal"))
    print("-" * 50)
    for fault, rows in sorted(by_fault.items()):
        fault_rows = [r for r in rows if r["label"] == 1]
        normal_rows = [r for r in rows if r["label"] == 0]
        mean_fault = sum(r["p"] for r in fault_rows) / len(fault_rows) if fault_rows else float("nan")
        mean_normal = sum(r["p"] for r in normal_rows) / len(normal_rows) if normal_rows else float("nan")
        print("%-10s %7d %7.1f%% %10.4f %10.4f"
              % (fault, len(rows), 100 * len(fault_rows) / len(rows), mean_fault, mean_normal))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "dataset": "RCAEval RE1-OB", "window_s": args.window_s, "stride": args.stride,
        "warmup": args.warmup, "cases": len(cases), "model": args.model or "default",
        "windows": len(judged), "anomalous": positives,
        "elapsed_s": round(elapsed, 1),
        "rows": [{k: v for k, v in row.items() if k != "digest"} for row in judged],
        "sample_digest": judged[len(judged) // 2]["digest"],
    }, indent=2), encoding="utf-8")
    print("")
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
