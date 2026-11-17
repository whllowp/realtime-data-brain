"""Where the deviation reader's own cut sits on a stream, and what the shipped bar does.

The point of this script is reproducibility for one number in `docs/RSI_LOOP.zh.md` section 3.12:
`host-scoring-deviation` ships `gate.anomaly_probability = 0.87`. That constant was chosen by applying
the loop's own 10%-false-positive rule to the reader's scores, so anyone re-running this should be
able to see the sweep it came from instead of taking the doc's word for it.

It makes no Laya calls: the reader is a pure function of the digest, which is exactly its selling point.

    python scripts/real_data/deviation_reader.py --stream data/smd/stream.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brian.config import load_settings  # noqa: E402
from brian.evolve import apply_overrides  # noqa: E402
from brian.scoring import CROSSOVER_DEVIATION, deviation_probability  # noqa: E402
from brian.state import (  # noqa: E402
    BaselineTracker,
    Windower,
    baseline_keys,
    build_digest,
    digest_specs,
)
from brian.streams import load_stream_file  # noqa: E402


def score_stream(stream, settings) -> List[Tuple[float, bool]]:
    labels = {(window.service, int(window.window_start.timestamp())): bool(
        (window.expected or {}).get("is_anomaly"))
        for window in stream.windows if window.scored}
    windower = Windower(size_s=settings.window.size_s, slide_s=settings.window.slide_s,
                        min_events=settings.window.min_events,
                        specs=digest_specs(settings.digest.profile, settings.digest.host_features))
    baselines = BaselineTracker(window=settings.window.baseline_windows,
                                long_window=settings.window.baseline_windows_long,
                                keys=baseline_keys(settings.digest.profile,
                                                   settings.digest.host_features))
    scored: List[Tuple[float, bool]] = []
    for event in stream.events():
        windower.add(event)
        for aggregator in windower.ready():
            digest = build_digest(aggregator, baselines, profile=settings.digest.profile)
            key = (digest.service, int(digest.window_start.timestamp()))
            probability = deviation_probability(digest)
            if key in labels and probability is not None:
                scored.append((probability, labels[key]))
            baselines.observe(digest.service, digest.metrics)
    for aggregator in windower.flush():
        digest = build_digest(aggregator, baselines, profile=settings.digest.profile)
        key = (digest.service, int(digest.window_start.timestamp()))
        probability = deviation_probability(digest)
        if key in labels and probability is not None:
            scored.append((probability, labels[key]))
    return scored


def main() -> int:
    parser = argparse.ArgumentParser(description="deviation reader: shipped bar vs budget sweep")
    parser.add_argument("--stream", default="data/smd/stream.json")
    parser.add_argument("--budget", type=float, default=0.10)
    parser.add_argument("--cuts", default="0.70,0.75,0.80,0.83,0.85,0.87,0.90")
    args = parser.parse_args()

    loaded = load_stream_file(Path(args.stream))
    settings = apply_overrides(load_settings(), loaded.overrides)
    scored: Dict[str, List[Tuple[float, bool]]] = {
        stream.name: score_stream(stream, settings) for stream in loaded.streams}

    print("=" * 78)
    print("deviation reader - %s" % args.stream)
    print("=" * 78)
    print("crossover: dev %.2f -> p %.4f (where the SMD 0.33 bar sits)"
          % (CROSSOVER_DEVIATION, CROSSOVER_DEVIATION / (1.0 + CROSSOVER_DEVIATION)))
    for name, rows in scored.items():
        positives = [p for p, truth in rows if truth]
        negatives = [p for p, truth in rows if not truth]
        print("%-24s windows=%-4d pos=%-4d neg=%-4d" % (name, len(rows), len(positives),
                                                        len(negatives)))

    print("")
    print("cut    pooled recall   pooled fpr   per recording (recall/fpr)")
    cuts = [float(value) for value in args.cuts.split(",") if value.strip()]
    for cut in cuts:
        tp = fp = positives = negatives = 0
        detail = []
        for name, rows in scored.items():
            host_tp = sum(1 for p, truth in rows if truth and p >= cut)
            host_fp = sum(1 for p, truth in rows if not truth and p >= cut)
            host_pos = sum(1 for _p, truth in rows if truth)
            host_neg = sum(1 for _p, truth in rows if not truth)
            tp += host_tp
            fp += host_fp
            positives += host_pos
            negatives += host_neg
            detail.append("%s %.3f/%.3f" % (name.split("-")[-1], host_tp / max(host_pos, 1),
                                            host_fp / max(host_neg, 1)))
        print("%.2f   %.3f           %.3f        %s"
              % (cut, tp / max(positives, 1), fp / max(negatives, 1), "  ".join(detail)))

    best = None
    for cut in cuts:
        tp = sum(1 for rows in scored.values() for p, truth in rows if truth and p >= cut)
        fp = sum(1 for rows in scored.values() for p, truth in rows if not truth and p >= cut)
        positives = sum(1 for rows in scored.values() for _p, truth in rows if truth)
        negatives = sum(1 for rows in scored.values() for _p, truth in rows if not truth)
        if negatives and fp / negatives <= args.budget:
            if best is None or tp / max(positives, 1) > best[1]:
                best = (cut, tp / max(positives, 1), fp / negatives)
    print("")
    print("lowest cut inside the %.0f%% budget: %s"
          % (args.budget * 100, "none" if best is None
             else "p >= %.2f  recall %.3f  fpr %.3f" % best))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
