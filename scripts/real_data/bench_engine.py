"""Measure the decision engine's throughput: sequential vs concurrent.

Why this matters for the goal
-----------------------------
Speed is half the objective, and the engine call dominates a run: a 4,708-window evaluation takes
minutes at ~70 ms per call. Before optimising anything, measure where the time actually goes and whether
the service can serve requests in parallel at all. A GPU is often the bottleneck and serialises, in which
case threads buy nothing and the only real speed lever is *fewer calls* (a cheaper prefilter), not
faster ones.

Reports:
  * sequential throughput and latency percentiles
  * concurrent throughput at several worker counts
  * the speedup curve, so the knee is visible rather than assumed

Usage:
    python scripts/real_data/bench_engine.py --calls 60 --workers 1,2,4,8
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.config import load_settings  # noqa: E402
from brian.laya.client import LayaClient, LayaError  # noqa: E402

QUESTIONS = {
    "is_anomaly": {
        "type": "noul",
        "instructions": (
            "Do the measurements in this state show a real anomaly that needs attention, as opposed to "
            "normal variation for this host?"
        ),
    }
}


def make_states(count: int) -> List[str]:
    """Distinct state strings, so no answer is served from the client cache.

    The cache is keyed on the request body, so repeating one state would measure cache lookups rather
    than the engine. Each state carries different numbers, which is also closer to production.
    """
    states = []
    for index in range(count):
        cpu = 0.10 + (index % 40) * 0.02
        load = 0.05 + (index % 17) * 0.05
        free = 0.60 - (index % 25) * 0.02
        states.append(
            "Host bench-%d, a 300-second window with 5 samples.\n"
            "CPU %.2f (baseline %.2f, %.2fx).\n"
            "load 1m %.2f (baseline %.2f, %.2fx).\n"
            "free memory %.2f (baseline %.2f, %.2fx)."
            % (index, cpu, cpu * 0.9, 1.11, load, load * 0.95, 1.05, free, free * 1.02, 0.98)
        )
    return states


def percentiles(values: List[float]) -> Tuple[float, float, float]:
    ordered = sorted(values)
    def at(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]
    return statistics.median(ordered), at(0.95), max(ordered)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=60)
    parser.add_argument("--workers", default="1,2,4,8")
    args = parser.parse_args()

    settings = load_settings()
    client = LayaClient(settings.laya)
    try:
        checkpoint = client.verify_model()
        print("checkpoint   %s (%s, max_len=%s)"
              % (checkpoint.get("name"), checkpoint.get("encoder"), checkpoint.get("max_len")))
    except LayaError as exc:
        print("engine unavailable: %s" % exc)
        return 3

    states = make_states(args.calls * max(int(w) for w in args.workers.split(",")))
    print("states       %d distinct" % len(states))
    print("")

    results: Dict[int, Dict[str, float]] = {}
    offset = 0
    for workers in [int(w) for w in args.workers.split(",") if w.strip()]:
        batch = states[offset:offset + args.calls]
        offset += args.calls

        def one(state: str) -> float:
            started = time.perf_counter()
            client.decide(state, QUESTIONS, use_cache=False)
            return (time.perf_counter() - started) * 1000.0

        started = time.perf_counter()
        if workers == 1:
            latencies = [one(state) for state in batch]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                latencies = list(pool.map(one, batch))
        elapsed = time.perf_counter() - started

        throughput = len(batch) / elapsed
        median, p95, worst = percentiles(latencies)
        results[workers] = {"throughput": throughput, "median": median, "p95": p95,
                            "max": worst, "elapsed": elapsed}
        print("workers=%-2d  calls=%-3d  wall=%5.2fs  throughput=%6.2f calls/s  "
              "latency median=%6.1fms p95=%6.1fms" % (workers, len(batch), elapsed, throughput, median, p95))

    base = results[1]["throughput"]
    print("")
    print("speedup vs sequential:")
    for workers, row in results.items():
        print("  workers=%-2d  x%.2f" % (workers, row["throughput"] / base))

    best = max(results.items(), key=lambda kv: kv[1]["throughput"])
    print("")
    print("best: workers=%d at %.2f calls/s (x%.2f)" % (best[0], best[1]["throughput"],
                                                       best[1]["throughput"] / base))
    print("")
    print("For a 4,708-window evaluation at the best setting: %.1f s"
          % (4708 / best[1]["throughput"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
