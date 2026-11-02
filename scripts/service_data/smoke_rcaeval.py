"""End-to-end smoke: does the production service path render and judge real service telemetry?

Not a benchmark — a wiring check. It drives RCAEval events through the real `Windower` + `BaselineTracker`
+ `build_digest(profile="service")` and asks the engine, so that "the service path works on real data" is
demonstrated rather than asserted. The full evaluation (all cases, control comparison, operating curve) is a
separate script; this one exists to fail loudly and early if the plumbing is wrong.

Usage:
    python scripts/service_data/smoke_rcaeval.py --case re1ob_adservice_cpu_1 --windows 6
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "service_data"))

from brian.config import load_settings  # noqa: E402
from brian.laya.client import LayaClient, LayaError  # noqa: E402
from brian.state import BaselineTracker, Windower, build_digest  # noqa: E402

from rcaeval_to_events import DATA, SERVICE_GAUGES, window_events  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="re1ob_adservice_cpu_1")
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--windows", type=int, default=6, help="how many windows after warmup to judge")
    args = parser.parse_args()

    case_dir = DATA / args.case
    if not case_dir.is_dir():
        print("case not found: %s" % case_dir)
        return 2

    events, inject, meta = window_events(case_dir, args.window_s)
    settings = load_settings()
    client = LayaClient(settings.laya)
    windower = Windower(size_s=args.window_s, slide_s=args.window_s, allowed_lateness_s=0,
                        min_events=1, specs=SERVICE_GAUGES)
    baselines = BaselineTracker(window=10, keys=tuple(s.key for s in SERVICE_GAUGES))

    print("case      %s   fault=%s   service=%s" % (meta["case"], meta["fault"], meta["service"]))
    print("windows   %d total, injection at %d" % (meta["windows"], inject))
    print("")

    judged = 0
    index = 0
    for event in events:
        windower.add(event)
        for aggregator in windower.ready():
            digest = build_digest(aggregator, baselines, profile="service")
            baselines.observe(digest.service, digest.metrics)
            index += 1
            if index <= args.warmup or judged >= args.windows:
                continue
            label = "FAULT" if aggregator.window_start.timestamp() >= inject else "normal"
            try:
                result = client.decide(digest, use_cache=False)
                answer = (result.answers.get("is_anomaly") or {}).get("noul")
                print("%s  label=%-6s p=%.4f  latency=%.0fms"
                      % (aggregator.window_start.strftime("%H:%M:%S"), label,
                         float(answer) if answer is not None else float("nan"),
                         result.latency_ms))
                print("    " + digest.interpretation.replace("\n", "\n    "))
            except LayaError as exc:
                print("engine error: %s" % exc)
                return 3
            judged += 1

    if not judged:
        print("no windows judged - check warmup/window counts")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
