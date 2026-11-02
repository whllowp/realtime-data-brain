"""Turn RCAEval RE1-OB cases into the same state the production service path renders.

This is the point of the whole exercise: feed the pipeline *service-side* telemetry — request rate, error
ratio, latency, CPU — from real fault-injection runs, and let it produce the digest it would produce in
production. Fidelity comes from reusing the shipped catalogue's metric **keys** (`requests.rate`,
`errors.ratio`, `latency_ms.p99`, `cpu.util`), so `render_digest(profile="service")` emits its normal
sentences without any special-casing. Only the *source* columns differ.

Schema reality, measured over all 125 cases rather than assumed:

    5 services x 5 fault types x 5 repetitions = 125 cases
    every case: 12 services x (cpu, mem), plus error columns for frontend / PassthroughCluster
    cpu/mem/disk faults:  `X_load` (QPS) and `X_latency` (seconds)
    delay/loss faults:    `X_latency-50` and `X_latency-90`, and no load column at all

So the adapter degrades honestly: a case without a load column yields a digest without a throughput line,
which is exactly what the renderer does when a metric is absent. It does not invent a request rate.

Label: the file `inject_time.txt` holds a Unix timestamp; every recording runs ~2,100 s before and
~2,100 s after it. A window is anomalous if it starts at or after the injection.

Usage:
    python scripts/service_data/rcaeval_to_events.py --case re1ob_adservice_cpu_1 --window-s 60
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.contracts import Event  # noqa: E402
from brian.state import MetricSpec  # noqa: E402

DATA = REPO_ROOT / "data" / "rcaeval"

#: Specs that reuse the shipped catalogue's keys so the renderer's own clauses fire. Kinds differ from
#: production because the source already publishes rates and levels per second rather than counters.
SERVICE_GAUGES = (
    MetricSpec("requests.rate", "load", "gauge", "req/s", "either", "request rate", "throughput"),
    MetricSpec("errors.ratio", "error_ratio", "gauge", "fraction", "up", "error fraction", "errors"),
    MetricSpec("latency_ms.p99", "latency_ms", "gauge", "ms", "up", "request latency", "latency"),
    MetricSpec("cpu.util", "cpu_util", "gauge", "fraction", "up", "CPU utilisation", "CPU"),
)

#: Which services the RE1-OB parquet describes. Used to name the faulted service from the case name.
SERVICES = (
    "adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice",
    "frontend", "paymentservice", "productcatalogservice", "recommendationservice",
    "redis", "shippingservice", "main",
)


def faulted_service(case: str) -> str:
    """`re1ob_adservice_cpu_1` -> `adservice`; the longest matching service name wins."""
    parts = case.split("_")
    body = "_".join(parts[1:-1])  # drop `re1ob` and the repetition index
    for service in sorted(SERVICES, key=len, reverse=True):
        if body.startswith(service):
            return service
    return body


def fault_type(case: str) -> str:
    parts = case.split("_")
    return parts[-2] if len(parts) >= 3 else "?"


def columns_for(frame, service: str) -> Dict[str, Optional[str]]:
    """Pick the columns that carry each golden signal, tolerating the two schema variants."""
    names = set(frame.columns)
    load = "%s_load" % service if "%s_load" % service in names else None
    if "%s_latency-90" % service in names:
        latency, latency_source = "%s_latency-90" % service, "latency-90"
    elif "%s_latency" % service in names:
        latency, latency_source = "%s_latency" % service, "latency"
    else:
        latency, latency_source = None, None
    error = None
    for candidate in ("frontend_error", "frontend-external_error", "PassthroughCluster_error"):
        if candidate in names:
            error = candidate
            break
    cpu = "%s_cpu" % service if "%s_cpu" % service in names else None
    return {"load": load, "latency": latency, "latency_source": latency_source,
            "error": error, "cpu": cpu}


def window_events(case_dir: Path, window_s: int) -> Tuple[List[Event], int, Dict[str, object]]:
    """One event per (window, service) carrying the golden signals, plus the injection timestamp."""
    import pandas as pd

    frame = pd.read_parquet(case_dir / "metrics.parquet")
    inject = int((case_dir / "inject_time.txt").read_text(encoding="utf-8").strip())
    case = case_dir.name
    service = faulted_service(case)
    columns = columns_for(frame, service)

    start = int(frame["time"].min())
    end = int(frame["time"].max())
    events: List[Event] = []
    index = 0
    while start + index * window_s <= end:
        window_start = start + index * window_s
        chunk = frame[(frame["time"] >= window_start) & (frame["time"] < window_start + window_s)]
        if chunk.empty:
            index += 1
            continue
        payload: Dict[str, float] = {}
        if columns["load"]:
            value = chunk[columns["load"]].mean()
            if value == value:  # not NaN
                payload["load"] = float(value)
        if columns["latency"]:
            value = chunk[columns["latency"]].mean()
            if value == value:
                payload["latency_ms"] = float(value) * 1000.0  # the source is seconds
        if columns["error"]:
            value = chunk[columns["error"]].mean()
            if value == value:
                payload["error_ratio"] = float(value)
        if columns["cpu"]:
            value = chunk[columns["cpu"]].mean()
            if value == value:
                payload["cpu_util"] = float(value) / 100.0  # the source is percent
        moment = datetime.fromtimestamp(window_start, tz=timezone.utc)
        # One event per metric, not one per window: the aggregator counts events and the renderer prints
        # that count ("a 60-second window with N events"). Collapsing four metrics into one event made the
        # digest read "with 1 events", which is true of this adapter and false of any real feed.
        for key, value in payload.items():
            events.append(Event(event_time=moment, service=service, kind="metric",
                                payload={key: value},
                                ingest_time=moment + timedelta(seconds=1), source="rcaeval"))
        index += 1

    meta = {"case": case, "service": service, "fault": fault_type(case), "inject": inject,
            "window_s": window_s, "columns": columns, "windows": len(events)}
    return events, inject, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="re1ob_adservice_cpu_1")
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args()

    case_dir = DATA / args.case
    if not case_dir.is_dir():
        print("case not found: %s" % case_dir)
        return 2

    events, inject, meta = window_events(case_dir, args.window_s)
    print("case        %s" % meta["case"])
    print("service     %s   fault=%s   injection at %d"
          % (meta["service"], meta["fault"], inject))
    print("windows     %d of %ds" % (meta["windows"], args.window_s))
    print("sources     %s" % {k: v for k, v in meta["columns"].items() if v})
    anomalous = sum(1 for e in events if e.event_time.timestamp() >= inject)
    print("labelled    %d pre-fault, %d post-fault" % (len(events) - anomalous, anomalous))
    print("")
    print("first payloads:")
    for event in events[:args.show]:
        print("  %s  %s" % (event.event_time.strftime("%H:%M:%S"),
                            {k: round(v, 4) for k, v in event.payload.items()}))
    print("...")
    for event in events[-args.show:]:
        print("  %s  %s" % (event.event_time.strftime("%H:%M:%S"),
                            {k: round(v, 4) for k, v in event.payload.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
