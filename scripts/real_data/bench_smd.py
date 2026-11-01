"""One harness for iterating on accuracy and speed against real ops data (SMD).

Everything measured so far used ad-hoc scripts, which made rounds hard to compare. This one run defines
the metric set for the whole optimisation sequence:

    accuracy   AUC (threshold-free), recall at FPR <= budget, precision, FPR
    speed      engine calls/s, wall time, latency percentiles

The two levers it exposes, both of which the earlier validation identified as dominant:

    --digest     how the window is *described* to the model
                 level        absolute values only
                 ratio        deviation from the rolling baseline only
                 current      value + baseline + ratio (the host profile shipped today)
                 level_range  absolute value + long-horizon range + rolling median
    --features   which source columns reach the digest (dilution is measurable)

Sampling: digests for every window are built (cheap, no engine call), but only a strided subset is
judged, so a run covers the whole *time range* rather than its first few hours. A contiguous prefix was
misleading in an earlier round: the first 400 windows held only 6 anomalies, which made AUC noise.

Usage:
    python scripts/real_data/bench_smd.py --digest level_range --features cpu_r,load_1,mem_free --stride 3
    python scripts/real_data/bench_smd.py --digest current --workers 4          # full, ~3 min
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.contracts import Event  # noqa: E402
from brian.laya.client import LayaClient, LayaError  # noqa: E402
from brian.state import (  # noqa: E402
    HOST_METRICS,
    BaselineTracker,
    Windower,
    build_digest,
)

DATA = REPO_ROOT / "data" / "smd"
EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)

#: SMD column layout (1-indexed), restricted to columns that vary on machine-1-6. Column 8 is constant
#: zero there, which is why `mem_used` is absent from the default feature set.
COLUMNS: Dict[str, int] = {
    "cpu_r": 1, "cpu_s": 2, "cpu_i": 3, "load_1": 4, "load_5": 5, "load_15": 6,
    "mem_free": 7, "mem_shared": 9, "swap_used": 13, "disk_io_read": 14, "disk_io_write": 15,
    "disk_space": 16, "net_in_packets": 20, "net_out_packets": 21,
    "net_in_bytes": 22, "net_out_bytes": 23,
}

#: Human labels for the digest. Short, because the document budget is shared with the rubric.
LABELS: Dict[str, str] = {
    "cpu_r": "CPU", "cpu_s": "CPU system", "cpu_i": "CPU idle",
    "load_1": "load 1m", "load_5": "load 5m", "load_15": "load 15m",
    "mem_free": "free memory", "mem_shared": "shared memory", "swap_used": "swap used",
    "disk_io_read": "disk read", "disk_io_write": "disk write", "disk_space": "disk space",
    "net_in_packets": "net in pkts", "net_out_packets": "net out pkts",
    "net_in_bytes": "net in", "net_out_bytes": "net out",
}

#: The rubric is a separate channel from the state: it may say how to *read* the numbers, while the state
#: must not say what to *conclude* (measured in round 2: a conclusion in the state cost 0.175 AUC).
#:
#: The baseline wording gives no interpretation of the scale, and the model has no calibration for what
#: "0.92" means on this host. Two variants test whether naming the semantics recovers that.
QUESTION_VARIANTS: Dict[str, Dict[str, Any]] = {
    "base": {
        "is_anomaly": {
            "type": "noul",
            "instructions": (
                "Do the measurements in this state show a real anomaly that needs attention, as opposed "
                "to normal variation for this host? Answer yes if any measurement is clearly abnormal "
                "for this host, given the reference values stated."
            ),
        }
    },
    "semantics": {
        "is_anomaly": {
            "type": "noul",
            "instructions": (
                "Each line reports one measurement of this host for the window, as `value (baseline B, "
                "Nx)`: B is what that measurement typically is on this host and Nx is value divided by "
                "B. The values are scaled so that higher means more of that resource being used. Decide "
                "whether this window shows a real anomaly that needs attention, as opposed to normal "
                "variation. A measurement far from its baseline, in either direction, is evidence of one."
            ),
        }
    },
    "bothdirections": {
        "is_anomaly": {
            "type": "noul",
            "instructions": (
                "Each line reports one measurement of this host as `value (baseline B, Nx)`, where B is "
                "the typical value and Nx is value divided by B. An anomaly is a measurement that is far "
                "from its typical value: much higher, or much lower. Judge whether this window needs "
                "attention. Values near 1.0x are the host behaving normally."
            ),
        }
    },
}

QUESTION = QUESTION_VARIANTS["base"]


@dataclass
class Row:
    index: int
    anomalous: bool
    digest: str
    probability: Optional[float] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    screen: float = 0.0
    skipped: bool = False


def load(machine: str) -> Tuple[List[List[float]], List[int]]:
    rows = [[float(v) for v in line.split(",")]
            for line in (DATA / ("%s.data.txt" % machine)).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    labels = [int(t) for t in (DATA / ("%s.label.txt" % machine)).read_text().split()]
    if len(rows) != len(labels):
        raise SystemExit("data/label length mismatch")
    return rows, labels


def render(digest_variant: str, machine: str, event_count: int, window_s: int,
           stats: Sequence[Dict[str, Any]]) -> str:
    """Build the state text for one window under the requested variant.

    `stats` carries, per feature: label, current value, rolling median, rolling ratio, and the
    long-horizon (p05, p95) range. Everything is precomputed by the caller so the variants differ only in
    what they *say*, never in what they measure.
    """
    lines = ["Host %s, a %d-second window with %d samples." % (machine, window_s, event_count)]
    if digest_variant == "level_scale":
        # Stated once, as a property of the feed rather than of this window: the model has no way to know
        # the scale otherwise, and repeating it per line is what made earlier variants too wordy.
        lines.append("Each measurement is scaled from 0.00 (idle) to 1.00 (saturated) for this host.")
    outside = 0
    for item in stats:
        label, value = item["label"], item["value"]
        median, ratio = item["median"], item["ratio"]
        lo, hi = item["lo"], item["hi"]
        if hi is not None and (value > hi or value < lo):
            outside += 1
        if digest_variant in ("level", "level_scale"):
            lines.append("%s %.2f." % (label, value))
        elif digest_variant == "ratio":
            lines.append("%s %.2fx its 30-window median." % (label, ratio))
        elif digest_variant in ("current", "current_summary"):
            lines.append("%s %.2f (baseline %.2f, %.2fx)." % (label, value, median, ratio))
        elif digest_variant == "percentile":
            # The level, expressed as a rank against the host's own long history. A raw number does not
            # tell the model whether 0.92 is high (it scored 0.508 on level alone); a percentile is a
            # monotone transform of the level that also says how unusual it is.
            lines.append("%s %.2f, higher than %d%% of the last %d windows."
                         % (label, value, item["pct"], item["history"]))
        elif digest_variant == "current_percentile":
            lines.append("%s %.2f (baseline %.2f, %.2fx; higher than %d%% of the last %d windows)."
                         % (label, value, median, ratio, item["pct"], item["history"]))
        elif digest_variant == "level_range":
            if lo is None:
                lines.append("%s %.2f (30-window median %.2f, no long history yet)." % (label, value, median))
            else:
                lines.append("%s %.2f, usual range %.2f to %.2f over the last %d windows "
                             "(30-window median %.2f)."
                             % (label, value, lo, hi, item["history"], median))
        else:
            raise SystemExit("unknown digest variant %r" % digest_variant)
    if stats:
        if digest_variant == "current_summary":
            lines.append("%d of %d measurements are outside +/-25%% of their baseline."
                         % (outside, len(stats)))
        if digest_variant in ("level_range", "level"):
            lines.append("%d of %d measurements are outside their usual range."
                         % (outside, len(stats)))
    return "\n".join(lines)


def _p(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]


def run(args: argparse.Namespace) -> Dict[str, Any]:
    rows, labels = load(args.machine)
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    unknown = [f for f in features if f not in COLUMNS]
    if unknown:
        raise SystemExit("unknown features %s (known: %s)" % (unknown, ", ".join(COLUMNS)))

    sample_seconds = 60
    window_rows = max(1, args.window_s // sample_seconds)

    events: List[Event] = []
    for index, row in enumerate(rows):
        payload = {name: row[COLUMNS[name] - 1] for name in features}
        moment = EPOCH + timedelta(seconds=index * sample_seconds)
        events.append(Event(event_time=moment, service=args.machine, kind="metric", payload=payload,
                            ingest_time=moment + timedelta(seconds=1), source="smd"))

    # Only the host profile's own metrics that we actually feed are needed; the digest text is replaced
    # below, so specs exist purely to get the aggregator to record gauges.
    specs = tuple(spec for spec in HOST_METRICS if spec.source in features)
    keys = tuple(spec.key for spec in specs)
    windower = Windower(size_s=args.window_s, slide_s=args.window_s, allowed_lateness_s=0,
                        min_events=1, specs=specs)
    baselines = BaselineTracker(window=args.baseline_windows, keys=keys,
                                long_window=args.long_windows)

    history: Dict[str, List[float]] = {name: [] for name in features}
    rows_out: List[Row] = []
    judge_jobs: List[int] = []
    digests: Dict[int, str] = {}
    screens_skipped: List[int] = []
    started = time.perf_counter()

    def build(aggregator) -> None:
        digest = build_digest(aggregator, baselines, profile="host")
        first_row = int((aggregator.window_start - EPOCH).total_seconds() // sample_seconds)
        label_slice = labels[first_row:first_row + window_rows]
        stats = []
        current_values: Dict[str, float] = {}
        for name in features:
            points = aggregator._gauges.get(name)
            if not points:
                continue
            values = [float(p[1]) for p in points]
            if args.window_stat == "mean":
                value = sum(values) / len(values)
            elif args.window_stat == "max":
                value = max(values)
            elif args.window_stat == "min":
                value = min(values)
            else:
                value = values[-1]
            # Recorded per feature. An earlier version re-read a loop variable here, so every feature's
            # history received the value of whichever feature happened to be last — which silently
            # corrupted the baseline and made `--window-stat` look like a 0.09 recall improvement.
            current_values[name] = value
            past = history[name]
            median = statistics.median(past) if past else value
            lo, hi = _p(past, 0.05), _p(past, 0.95)
            stats.append({
                "label": LABELS[name], "value": value, "median": median,
                "ratio": (value / median) if median else 1.0,
                "lo": lo, "hi": hi, "history": len(past),
                "pct": int(round(100.0 * sum(1 for v in past if v <= value) / len(past))) if past else 50,
            })
        if args.digest == "product":
            text = digest.interpretation
        else:
            text = render(args.digest, args.machine, aggregator.event_count, args.window_s, stats)
        for name, value in current_values.items():
            history[name].append(value)
            if len(history[name]) > args.history_windows:
                del history[name][0]

        index = len(rows_out)
        screen = 0.0
        for item in stats:
            median = item["median"]
            if args.screen_mode == "level":
                # Percentile of this level within the host's history: monotone in the level, which is
                # what actually separates faults here, and immune to the baseline being dragged along.
                screen = max(screen, float(item["pct"]))
            elif median:
                screen = max(screen, abs(item["value"] / median - 1.0))
        rows_out.append(Row(index=index, anomalous=any(label_slice), digest=text))
        baselines.observe(digest.service, digest.metrics)
        if index >= args.warmup and (args.stride <= 1 or (index - args.warmup) % args.stride == 0):
            # Stage one. Quiet windows are answered without the engine: their score is a small deviation
            # from baseline on every measured dimension, and the model has never been able to rank those
            # apart from one another anyway (they occupy the bottom of its probability band).
            if args.prefilter > 0 and screen < args.prefilter:
                rows_out[index].probability = args.skip_prob
                rows_out[index].skipped = True
                screens_skipped.append(index)
            else:
                digests[index] = text
                judge_jobs.append(index)
        rows_out[index].screen = screen

    for event in events:
        windower.add(event)
        for aggregator in windower.ready():
            build(aggregator)
    for aggregator in windower.flush():
        build(aggregator)

    build_seconds = time.perf_counter() - started
    print("windows built   %d in %.1fs (%.0f/s)" % (len(rows_out), build_seconds,
                                                    len(rows_out) / max(build_seconds, 1e-6)))
    print("windows judged  %d (stride %d, warmup %d)" % (len(judge_jobs), args.stride, args.warmup))

    if args.question == "permetric":
        question = {
            "%s_abnormal" % name: {
                "type": "noul",
                "instructions": (
                    "On this host, is the %s measurement in this window abnormal, given the value and "
                    "the baseline stated for it?" % LABELS[name]
                ),
            }
            for name in features
        }
    else:
        question = QUESTION_VARIANTS[args.question]
    client = LayaClient(settings_laya())
    latencies: List[float] = []
    started = time.perf_counter()

    def judge(index: int) -> None:
        text = digests[index]
        wall = time.perf_counter()
        try:
            result = client.decide(text, question, model=args.model or None, use_cache=False)
            rows_out[index].probability = None
            if args.question == "permetric":
                values = []
                for name in features:
                    answer = result.answers.get("%s_abnormal" % name) or {}
                    if answer.get("noul") is not None:
                        values.append(float(answer["noul"]))
                rows_out[index].probability = max(values) if values else None
            else:
                answers = result.answers.get("is_anomaly") or {}
                rows_out[index].probability = float(answers.get("noul"))
            rows_out[index].latency_ms = result.latency_ms
        except LayaError as exc:
            rows_out[index].error = str(exc)
            rows_out[index].latency_ms = (time.perf_counter() - wall) * 1000.0
        latencies.append((time.perf_counter() - wall) * 1000.0)

    if args.workers <= 1:
        for index in judge_jobs:
            judge(index)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(judge, judge_jobs))
    elapsed = time.perf_counter() - started

    judged = [row for row in rows_out if row.probability is not None]
    if not judged:
        errors = [row.error for row in rows_out if row.error][:2]
        print("no answers; first errors: %s" % errors)
        return {"error": "no answers"}

    probabilities = [row.probability for row in judged]  # type: ignore[misc]
    truth = [row.anomalous for row in judged]
    positives = sum(1 for y in truth if y)
    negatives = len(truth) - positives

    def sweep_rows() -> List[Dict[str, Any]]:
        out = []
        for threshold in sorted(set(probabilities), reverse=True):
            tp = sum(1 for p, y in zip(probabilities, truth) if y and p >= threshold)
            fp = sum(1 for p, y in zip(probabilities, truth) if not y and p >= threshold)
            if not tp:
                continue
            out.append({"threshold": round(threshold, 6),
                        "recall": round(tp / positives, 4),
                        "fpr": round(fp / negatives, 4),
                        "precision": round(tp / (tp + fp), 4)})
        return out

    swept = sweep_rows()
    eligible = [row for row in swept if row["fpr"] <= args.max_fpr]
    best = max(eligible, key=lambda row: row["recall"]) if eligible else None
    window_auc = auc(probabilities, truth)

    ordered_lat = sorted(latencies) if latencies else [0.0]
    result = {
        "machine": args.machine,
        "digest": args.digest,
        "model": args.model or "default",
        "question": args.question,
        "features": features,
        "n_features": len(features),
        "window_s": args.window_s,
        "baseline_windows": args.baseline_windows,
        "history_windows": args.history_windows,
        "warmup": args.warmup,
        "stride": args.stride,
        "workers": args.workers,
        "judged": len(judged),
        "anomalous": positives,
        "label_rate": round(positives / len(judged), 4),
        "auc": None if window_auc is None else round(window_auc, 4),
        "recall_at_fpr": None if best is None else best["recall"],
        "precision_at_fpr": None if best is None else best["precision"],
        "fpr_at_fpr": None if best is None else best["fpr"],
        "threshold_at_fpr": None if best is None else best["threshold"],
        "sweep": swept,
        # Per-window answers, kept so a later analysis can bootstrap a confidence interval without
        # paying for the engine again. Without these, a 0.09 AUC gap cannot be separated from noise, and
        # the previous round's feature sweep produced exactly the kind of lone spike that invites a
        # wrong conclusion.
        "probabilities": [round(p, 5) for p in probabilities],
        "labels": [1 if y else 0 for y in truth],
        "screened_out": len(screens_skipped),
        "screened_fraction": round(len(screens_skipped) / max(len(judged), 1), 4),
        "engine_calls_per_s": round(len(judge_jobs) / max(elapsed, 1e-6), 2),
        "judge_seconds": round(elapsed, 2),
        "build_seconds": round(build_seconds, 2),
        "latency_p50_ms": round(ordered_lat[len(ordered_lat) // 2], 1),
        "latency_p95_ms": round(ordered_lat[min(len(ordered_lat) - 1, int(0.95 * len(ordered_lat)))], 1),
        "errors": sum(1 for row in rows_out if row.error),
        "sample_digest": digests.get(judge_jobs[len(judge_jobs) // 2]) if judge_jobs else None,
    }
    return result


def settings_laya():
    return load_settings().laya


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="machine-1-6")
    parser.add_argument("--screen-mode", default="deviation", choices=["deviation", "level"],
                        help="deviation: max|ratio-1|; level: max percentile of the value in history")
    parser.add_argument("--prefilter", type=float, default=0.0,
                        help="stage-one screen: skip the engine when max|ratio-1| is below this")
    parser.add_argument("--skip-prob", type=float, default=0.20,
                        help="probability assigned to a screened-out window")
    parser.add_argument("--window-stat", default="last", choices=["last", "mean", "max", "min"],
                        help="how a gauge is reduced over the window (harness variants only)")
    parser.add_argument("--model", default="",
                        help="checkpoint to route to (english | multilingual | typed-decisions)")
    parser.add_argument("--question", default="base", choices=sorted(list(QUESTION_VARIANTS) + ["permetric"]),
                        help="rubric variant; the state is unchanged, only the question differs")
    parser.add_argument("--digest", default="current",
                        choices=["level", "ratio", "current", "level_range", "percentile",
                                  "current_percentile", "product", "current_summary", "level_scale"])
    parser.add_argument("--features", default="cpu_r,cpu_s,load_1,mem_free,swap_used,disk_io_write,net_in_bytes,net_out_bytes")
    parser.add_argument("--window-s", type=int, default=300)
    parser.add_argument("--baseline-windows", type=int, default=30)
    parser.add_argument("--history-windows", type=int, default=240)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--long-windows", type=int, default=480,
                        help="long baseline horizon in windows; 0 disables it")
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-fpr", type=float, default=0.10)
    parser.add_argument("--out", default="")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    print("config          machine=%s digest=%s features=%d workers=%d stride=%d"
          % (args.machine, args.digest, len(args.features.split(",")), args.workers, args.stride))
    result = run(args)
    if "error" in result:
        return 2

    print("")
    print("label rate      %d/%d = %.1f%%" % (result["anomalous"], result["judged"],
                                              100 * result["label_rate"]))
    print("AUC             %s" % result["auc"])
    print("recall @ FPR<=%.2f  %s (precision %s, at P>=%s)"
          % (args.max_fpr, result["recall_at_fpr"], result["precision_at_fpr"],
             result["threshold_at_fpr"]))
    print("speed           %.2f calls/s, judge %.1fs, build %.1fs, p50 %sms p95 %sms"
          % (result["engine_calls_per_s"], result["judge_seconds"], result["build_seconds"],
             result["latency_p50_ms"], result["latency_p95_ms"]))
    print("")
    print("sample state the model read:")
    for line in (result["sample_digest"] or "").splitlines()[:6]:
        print("  | " + line)

    if args.tag:
        result["tag"] = args.tag
    out = args.out or ("data/smd/bench_%s_%s.json" % (result["digest"], args.tag or "run"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("")
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
