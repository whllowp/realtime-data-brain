"""How much of an evaluation is repeated state, and what a cache can save?

A cache is the only speed lever that is free: a repeated state has, by construction, the same answer, so
serving it from memory cannot change any metric. The question is only how much repetition a real feed
produces.

This matters more than it looks. The digest rounds values to two decimals and states only a handful of
metrics, so two windows that a human would call "the same quiet window" render identically — especially
in the quiet tail, which is most of the traffic. If a meaningful share repeats, an evaluation gets faster
with no accuracy trade at all, unlike the prefilter, whose 13% saving cost 0.02 recall.

Measures, for a given configuration:
  * distinct digests out of N windows,
  * the repeat rate, and therefore the calls saved,
  * how the repetition is distributed (quiet vs anomalous), because a cache that only ever hits on quiet
    windows is still a win, but it says something about which windows carry information.

Usage:
    python scripts/real_data/cache_potential.py --features cpu_r,load_1,mem_free
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.contracts import Event  # noqa: E402
from brian.state import (  # noqa: E402
    HOST_BASELINE_KEYS,
    HOST_METRICS,
    BaselineTracker,
    Windower,
    build_digest,
)

DATA = REPO_ROOT / "data" / "smd"
EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)
COLUMNS = {"cpu_r": 1, "cpu_s": 2, "load_1": 4, "mem_free": 7, "swap_used": 13,
           "disk_io_write": 15, "net_in_bytes": 22, "net_out_bytes": 23}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="machine-1-6")
    parser.add_argument("--features", default="cpu_r,load_1,mem_free")
    parser.add_argument("--window-s", type=int, default=300)
    parser.add_argument("--long-windows", type=int, default=480)
    parser.add_argument("--warmup", type=int, default=30)
    args = parser.parse_args()

    rows = [[float(v) for v in line.split(",")]
            for line in (DATA / ("%s.data.txt" % args.machine)).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    labels = [int(t) for t in (DATA / ("%s.label.txt" % args.machine)).read_text().split()]
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    window_rows = max(1, args.window_s // 60)

    specs = tuple(spec for spec in HOST_METRICS if spec.source in features)
    windower = Windower(size_s=args.window_s, slide_s=args.window_s, allowed_lateness_s=0,
                        min_events=1, specs=specs)
    baselines = BaselineTracker(window=30, keys=HOST_BASELINE_KEYS, long_window=args.long_windows)

    texts = []
    truth = []
    seen = 0

    def build(aggregator) -> None:
        nonlocal seen
        digest = build_digest(aggregator, baselines, profile="host")
        first_row = int((aggregator.window_start - EPOCH).total_seconds() // 60)
        label_slice = labels[first_row:first_row + window_rows]
        baselines.observe(digest.service, digest.metrics)
        seen += 1
        if seen > args.warmup:
            texts.append(digest.interpretation)
            truth.append(1 if any(label_slice) else 0)

    for index, row in enumerate(rows):
        moment = EPOCH + timedelta(seconds=index * 60)
        windower.add(Event(event_time=moment, service=args.machine, kind="metric",
                           payload={name: row[COLUMNS[name] - 1] for name in features},
                           ingest_time=moment + timedelta(seconds=1), source="smd"))
        for aggregator in windower.ready():
            build(aggregator)
    for aggregator in windower.flush():
        build(aggregator)

    counts = Counter(texts)
    distinct = len(counts)
    repeats = len(texts) - distinct
    print("windows            %d" % len(texts))
    print("distinct digests   %d  (%.1f%%)" % (distinct, 100 * distinct / len(texts)))
    print("repeat rate        %.1f%%  -> engine calls saved: %d of %d (%.1f%%)"
          % (100 * repeats / len(texts), repeats, len(texts), 100 * repeats / len(texts)))
    print("")
    print("most repeated states:")
    for text, count in counts.most_common(3):
        first_line = text.splitlines()[1] if len(text.splitlines()) > 1 else text
        print("  %4d x  %s" % (count, first_line[:88]))

    anomalous_repeats = sum(count - 1 for text, count in counts.items()
                            if any(t == text and y == 1 for t, y in zip(texts, truth)))
    anomalous_total = sum(truth)
    print("")
    print("anomalous windows  %d; of those, repeats %d (%.1f%%)"
          % (anomalous_total, anomalous_repeats, 100 * anomalous_repeats / max(anomalous_total, 1)))
    print("")
    if repeats / len(texts) > 0.25:
        print("-> a cache pays: more than a quarter of the calls would be served from memory.")
    elif repeats / len(texts) > 0.10:
        print("-> a cache helps modestly, and costs nothing in accuracy.")
    else:
        print("-> little repetition at this precision; caching is not the lever here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
