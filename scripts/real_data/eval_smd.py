"""Independent validation on a REAL ops dataset (SMD / ServerMachineDataset).

Why this exists
---------------
Every number measured before this file used `brian.demo`'s synthetic scenarios, which I wrote myself.
That is a closed loop: the scenarios encode the assumptions the catalog and the renderer were built
around, so doing well on them proves little. SMD is an external check —?38 real server metrics sampled
every minute on 28 machines, with human-verified point labels and per-fault root-cause dimensions.

Known limits of this test, stated up front
-------------------------------------------
* SMD is **host** telemetry: CPU, load, memory, disk, network. It has no request rate, error count or
  latency, so most of the HTTP-service catalog cannot be populated. That is a real coverage gap, not a
  detail —?it is reported by `coverage_report()` and measured by the `service`-profile arm below.
* Values are per-column min-max normalised by the dataset's authors, so they are relative levels, not
  physical units. A monotone per-column transform leaves threshold-free ranking (AUC) intact, but the
  numbers must not be read as percentages.
* Column semantics follow the SMD documentation. This machine's column 8 is constant zero, so the
  layout is assumed rather than verified; the detector only uses values and their baselines, so a
  misnamed column changes the label on the line, not the measurement.

The control that makes the result interpretable
-------------------------------------------
"Laya vs SMD labels" alone says nothing about usefulness. The same windows are therefore also
classified by a few-line robust z-score detector over identical features. If a rule of thumb matches or
beats a 421M-parameter model, that is the finding worth reporting.

Usage
-----
    python _real_smd_eval.py --limit-calls 400          # quick A/B
    python _real_smd_eval.py                            # full series, both profiles
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import CalibrationSettings, load_settings  # noqa: E402
from brian.contracts import Event  # noqa: E402
from brian.laya.client import LayaClient, LayaError  # noqa: E402
from brian.laya.gate import apply_gate  # noqa: E402
from brian.state import (  # noqa: E402
    BASELINE_KEYS,
    DEFAULT_METRICS,
    HOST_BASELINE_KEYS,
    HOST_METRICS,
    BaselineTracker,
    Windower,
    build_digest,
)

DATA = REPO_ROOT / "data" / "smd"
EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)

#: SMD documented column layout (1-indexed, matching `interpretation_label`). Column 8 is constant zero
#: on this machine and is excluded.
COLUMN_NAMES = {
    1: "cpu_r", 2: "cpu_s", 3: "cpu_i", 4: "load_1", 5: "load_5", 6: "load_15",
    7: "mem_free", 8: "mem_used", 9: "mem_shared", 10: "mem_buff", 11: "mem_available",
    12: "swap_free", 13: "swap_used", 14: "disk_io_read", 15: "disk_io_write",
    16: "disk_space", 17: "disk_space_free", 18: "disk_inode", 19: "disk_inode_free",
    20: "net_in_packets", 21: "net_out_packets", 22: "net_in_bytes", 23: "net_out_bytes",
}

#: Which SMD column feeds which `HOST_METRICS` source field.
HOST_MAPPING = {
    "cpu_r": 1,
    "cpu_s": 2,
    "load_1": 4,
    "mem_free": 7,
    "swap_used": 13,
    "disk_io_write": 15,
    "net_in_bytes": 22,
    "net_out_bytes": 23,
}

#: A two-feature mapping that matches the *shipped* HTTP catalog (`cpu_util`, `memory_util`), used by
#: the `service` arm to demonstrate what happens when a host feed is pushed through a service-shaped
#: catalog. Column 8 is constant zero here, which is itself part of the finding.
SERVICE_MAPPING = {"cpu_util": 1, "memory_util": 8}

#: Baseline keys for the service arm: exactly the shipped HTTP profile's set. The host keys must NOT be
#: used here —?with the host key set the tracker records `host.*` statistics while the service renderer
#: looks up `cpu.util`, so every window reports "no baseline" and the arm measures a configuration bug
#: instead of the renderer.
SERVICE_BASELINE_KEYS = BASELINE_KEYS


@dataclass
class Window:
    index: int
    start_row: int
    end_row: int
    anomalous: bool
    probability: Optional[float] = None
    action: Optional[str] = None
    category: Optional[str] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    digest_text: str = ""
    z_max: Optional[float] = None
    z_flag: Optional[bool] = None


def load_series(machine: str):
    rows: List[List[float]] = []
    for line in (DATA / ("%s.data.txt" % machine)).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append([float(v) for v in line.split(",")])
    labels = [int(tok) for tok in (DATA / ("%s.label.txt" % machine)).read_text().split()]
    if len(rows) != len(labels):
        raise SystemExit("length mismatch %d vs %d" % (len(rows), len(labels)))
    return rows, labels


def load_interpretations(machine: str) -> Dict[int, List[int]]:
    path = DATA / ("%s.interpret.txt" % machine)
    out: Dict[int, List[int]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        span, dims = line.split(":", 1)
        try:
            start, end = [int(x) for x in span.split("-")]
        except ValueError:
            continue
        for row in range(start, end + 1):
            out[row] = [int(d) for d in dims.split(",") if d.strip()]
    return out


def build_events(rows: Sequence[Sequence[float]], machine: str, mapping: Dict[str, int]) -> List[Event]:
    events = []
    for index, row in enumerate(rows):
        payload = {}
        for field_name, column in mapping.items():
            if column <= len(row):
                payload[field_name] = row[column - 1]
        moment = EPOCH + timedelta(seconds=index * 60)
        events.append(Event(event_time=moment, service=machine, kind="metric", payload=payload,
                            ingest_time=moment + timedelta(seconds=1), source="smd"))
    return events


def sweep(probabilities: Sequence[float], labels: Sequence[bool]) -> List[Dict[str, Any]]:
    positives = sum(1 for y in labels if y)
    negatives = len(labels) - positives
    rows = []
    for candidate in sorted(set(probabilities), reverse=True):
        tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= candidate)
        fp = sum(1 for p, y in zip(probabilities, labels) if not y and p >= candidate)
        if not tp:
            continue
        rows.append({
            "threshold": round(candidate, 6),
            "recall": round(tp / positives, 4) if positives else None,
            "fpr": round(fp / negatives, 4) if negatives else None,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "tp": tp, "fp": fp,
        })
    return rows


def coverage_report() -> Dict[str, Any]:
    covered = {spec.source for spec in DEFAULT_METRICS}
    host = {spec.source for spec in HOST_METRICS}
    return {
        "http_sources": sorted(covered),
        "host_sources": sorted(host),
        "host_can_populate": sorted(host & set(HOST_MAPPING)),
        "http_sources_populatable_by_smd": sorted(covered & set(SERVICE_MAPPING)),
        "http_sources_uncovered": sorted(covered - set(SERVICE_MAPPING)),
    }


def run_arm(name: str, machine: str, rows, labels, args, profile: str, mapping: Dict[str, int],
            specs, baseline_keys) -> Dict[str, Any]:
    # A neutral gate for both arms. The shipped thresholds live on the *calibrated* scale, and
    # calibration is deliberately disabled here (the fit belongs to another domain), so reusing them
    # would report a scale mismatch as a detection result. AUC and the swept thresholds carry the
    # comparison; the action counts are reported only as an "did it ever fire at 0.5" sanity check.
    base = load_settings()
    settings = base.with_overrides(
        calibration=CalibrationSettings(path=base.calibration.path, enabled=False),
        gate=type(base.gate)(**{**vars(base.gate),
                                "anomaly_probability": args.gate_p,
                                "notify_probability": args.gate_p}),
    )
    laya = LayaClient(settings.laya)
    events = build_events(rows, machine, mapping)

    windower = Windower(size_s=args.window_s, slide_s=args.window_s, allowed_lateness_s=0,
                        min_events=1, specs=specs)
    baselines = BaselineTracker(window=args.baseline_windows, keys=baseline_keys)
    history: Dict[str, List[float]] = {k: [] for k in mapping}

    windows: List[Window] = []
    digests: List[str] = []
    calls = 0
    started = time.time()
    scored = 0

    def judge(aggregator) -> Optional[Window]:
        nonlocal calls, scored
        first_row = int((aggregator.window_start - EPOCH).total_seconds() // 60)
        last_row = first_row + max(1, args.window_s // 60)
        sub = labels[first_row:last_row]
        digest = build_digest(aggregator, baselines, profile=profile)

        z_max = 0.0
        for field_name in mapping:
            points = aggregator._gauges.get(field_name)
            if not points:
                continue
            current = float(points[-1][1])
            past = history[field_name]
            if len(past) >= 5:
                med = statistics.median(past)
                mad = statistics.median([abs(v - med) for v in past])
                if mad > 0:
                    z_max = max(z_max, abs(current - med) / (1.4826 * mad))
        for field_name in mapping:
            points = aggregator._gauges.get(field_name)
            if points:
                history[field_name].append(float(points[-1][1]))
                if len(history[field_name]) > args.baseline_windows:
                    del history[field_name][0]

        window = Window(index=len(windows), start_row=first_row, end_row=last_row,
                        anomalous=any(sub), digest_text=digest.interpretation,
                        z_max=round(z_max, 3), z_flag=z_max >= args.z_threshold)

        should_call = window.index >= args.warmup and (not args.limit_calls or calls < args.limit_calls)
        if should_call:
            if len(digests) < 2:
                digests.append(digest.interpretation)
            try:
                result = laya.decide(digest)
                outcome = apply_gate(digest, result, settings.gate)
                window.probability = outcome.decision.anomaly_probability
                window.action = outcome.decision.action.value
                window.category = (outcome.decision.category.value
                                   if outcome.decision.category else None)
                window.latency_ms = result.latency_ms
                calls += 1
            except LayaError as exc:
                window.error = str(exc)
        windows.append(window)
        baselines.observe(digest.service, digest.metrics)
        if window.probability is not None:
            scored += 1
        return window

    for event in events:
        windower.add(event)
        for aggregator in windower.ready():
            judge(aggregator)
    for aggregator in windower.flush():
        judge(aggregator)

    answered = [w for w in windows if w.probability is not None]
    if not answered:
        return {"arm": name, "error": "no answers", "digests": digests}

    probabilities = [w.probability for w in answered]  # type: ignore[misc]
    truth = [w.anomalous for w in answered]
    pos = sum(1 for y in truth if y)
    neg = len(truth) - pos
    z_tp = sum(1 for w in answered if w.anomalous and w.z_flag)
    z_fp = sum(1 for w in answered if not w.anomalous and w.z_flag)
    window_auc = auc(probabilities, truth)
    rows_sweep = sweep(probabilities, truth)
    eligible = [r for r in rows_sweep if r["fpr"] is not None and r["fpr"] <= args.max_fpr]
    best = max(eligible, key=lambda r: r["recall"] or 0) if eligible else None

    print("%-8s windows=%-5d anomalous=%-4d  AUC=%-7s  actions=%s"
          % (name, len(answered), pos,
             "n/a" if window_auc is None else "%.4f" % window_auc,
             json.dumps({a: sum(1 for w in answered if w.action == a)
                         for a in sorted({w.action for w in answered if w.action})})))
    print("         digest: %.1f lines / %.1f words"
          % (sum(len(w.digest_text.splitlines()) for w in answered) / len(answered),
             sum(len(w.digest_text.split()) for w in answered) / len(answered)))
    print("         z-control: recall=%s fpr=%s" % (_r(z_tp, pos), _r(z_fp, neg)))
    if best:
        print("         best @fpr<=%.2f: P>=%s recall=%s precision=%s"
              % (args.max_fpr, best["threshold"], best["recall"], best["precision"]))

    return {
        "arm": name,
        "profile": profile,
        "mapping": {k: COLUMN_NAMES.get(v, v) for k, v in mapping.items()},
        "windows": len(answered),
        "anomalous_windows": pos,
        "auc": window_auc,
        "actions": {a: sum(1 for w in answered if w.action == a)
                    for a in sorted({w.action for w in answered if w.action})},
        "control": {"threshold": args.z_threshold, "recall": _r(z_tp, pos), "fpr": _r(z_fp, neg)},
        "sweep": rows_sweep,
        "best_operating_point": best,
        "digest_lines": round(sum(len(w.digest_text.splitlines()) for w in answered) / len(answered), 2),
        "digest_words": round(sum(len(w.digest_text.split()) for w in answered) / len(answered), 2),
        "digest_samples": digests,
        "errors": sum(1 for w in answered if w.error),
        "elapsed_s": round(time.time() - started, 1),
    }


def _r(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="machine-1-6")
    parser.add_argument("--window-s", type=int, default=300)
    parser.add_argument("--baseline-windows", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--max-fpr", type=float, default=0.10)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--limit-calls", type=int, default=0)
    parser.add_argument("--profiles", default="host,service")
    parser.add_argument("--gate-p", type=float, default=0.5,
                        help="neutral decision threshold for both arms (calibration is off here)")
    parser.add_argument("--host-features", default="",
                        help="comma list of host field names to restrict the host arm to, e.g. cpu_r,load_1")
    parser.add_argument("--out", default="data/smd/report.json")
    args = parser.parse_args()

    rows, labels = load_series(args.machine)
    print("dataset   SMD %s: %d rows, %d anomalous (%.2f%%), %s cadence"
          % (args.machine, len(rows), sum(labels), 100.0 * sum(labels) / len(labels), "60s"))
    print("window    %ds tumble, baseline %d, warmup %d"
          % (args.window_s, args.baseline_windows, args.warmup))
    cov = coverage_report()
    print("coverage  HTTP catalog sources populatable by SMD: %s"
          % (cov["http_sources_populatable_by_smd"] or "NONE"))
    print("          uncovered: %d of %d" % (len(cov["http_sources_uncovered"]), len(cov["http_sources"])))
    print("")

    arms: List[Dict[str, Any]] = []
    for profile in [p.strip() for p in args.profiles.split(",") if p.strip()]:
        if profile == "host":
            host_mapping = HOST_MAPPING
            if args.host_features:
                wanted = [f.strip() for f in args.host_features.split(",") if f.strip()]
                host_mapping = {k: v for k, v in HOST_MAPPING.items() if k in wanted}
                specs = tuple(s for s in HOST_METRICS if s.source in host_mapping)
                keys = tuple(s.key for s in specs)
            else:
                specs, keys = HOST_METRICS, HOST_BASELINE_KEYS
            arms.append(run_arm("host", args.machine, rows, labels, args, "host",
                                host_mapping, specs, keys))
        elif profile == "service":
            # A host feed pushed through the shipped HTTP-shaped catalog: what a user would hit if they
            # connected this pipeline to a host-metrics source without adapting the metric profile.
            arms.append(run_arm("service", args.machine, rows, labels, args, "service",
                                SERVICE_MAPPING, DEFAULT_METRICS, SERVICE_BASELINE_KEYS))
        else:
            raise SystemExit("unknown profile %r" % profile)
        print("")

    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for arm in arms:
        print("%-8s AUC=%-7s control_recall=%-7s digest_words=%-6s"
              % (arm["arm"], arm.get("auc"), arm.get("control", {}).get("recall"),
                 arm.get("digest_words")))
        for sample in arm.get("digest_samples", [])[:1]:
            print("    state the model read:")
            for line in sample.splitlines():
                print("      | " + line)

    report = {"dataset": "SMD/ServerMachineDataset",
              "source": "https://github.com/NetManAIOps/OmniAnomaly",
              "machine": args.machine, "rounds": args.rounds if hasattr(args, "rounds") else 1,
              "window_s": args.window_s, "coverage": cov, "arms": arms}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("")
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
