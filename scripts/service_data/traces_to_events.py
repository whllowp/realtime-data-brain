"""Turn AnoMod traces into the event stream `brian` already knows how to judge.

This is the adapter the whole exercise needed: until now the pipeline had only ever been fed host
metrics or synthetic service events. `all_traces.csv` carries one row per span with `service`,
`start_time`, `duration_us` and `http_status_code`, which is exactly enough to reconstruct the service
signals the catalogue is built around:

    requests.rate   spans per second for the service in the window
    errors.ratio    share of spans whose HTTP status is 5xx
    latency_ms.p*   duration percentiles from the span durations

Deliberate choices, each of which would silently change the result if made differently:

* **5xx only counts as an error.** The workload is fuzzer-generated and 100% of the client-side
  `api_responses` are 4xx ("Incomplete arguments"), so counting 4xx would measure the fuzzer rather than
  the service. In traces the same trap appears as a handful of 4xx on entry spans.
* **The root span is the request.** A service's rate is counted from spans where it is the *server*
  (operation ends in `_server`) so a single user request is not multiplied by its internal calls.
* **Only 200/500 appear in this dataset**, so the ratio is well defined; a missing status is treated as
  "no error observation" rather than as success.

Usage:
    python scripts/service_data/traces_to_events.py --case Normal_Baseline --window-s 60
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACES = REPO_ROOT / "data" / "anomod" / "SN_data" / "trace_data"


def parse(stamp: str) -> datetime:
    return datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S.%f")


def load_case(case: str) -> Dict[str, List[dict]]:
    """Spans grouped by service, ordered by time."""
    path = TRACES / case / "all_traces.csv"
    if not path.is_file():
        raise SystemExit("case not found: %s" % path)
    by_service: Dict[str, List[dict]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                moment = parse(row["start_time"])
            except (KeyError, ValueError):
                continue
            by_service[row.get("service") or "?"].append({
                "t": moment,
                "duration_ms": float(row.get("duration_us") or 0.0) / 1000.0,
                "status": (row.get("http_status_code") or "").strip(),
                "operation": row.get("operation") or "",
            })
    for spans in by_service.values():
        spans.sort(key=lambda span: span["t"])
    return by_service


def is_server_span(span: dict) -> bool:
    """Entry spans are named `*_server`; client/internal spans are not, and would double-count requests."""
    return span["operation"].endswith("_server")


def windows(case: str, window_s: int) -> Dict[str, List[dict]]:
    """Per service, per tumbling window: the golden-signal block for that window."""
    by_service = load_case(case)
    all_times = [span["t"] for spans in by_service.values() for span in spans]
    if not all_times:
        return {}
    start = min(all_times).replace(microsecond=0)
    end = max(all_times)
    buckets: Dict[str, List[dict]] = defaultdict(list)

    for service, spans in by_service.items():
        for span in spans:
            index = int((span["t"] - start).total_seconds() // window_s)
            buckets["%s|%d" % (service, index)].append(span)

    out: Dict[str, List[dict]] = defaultdict(list)
    offset = 0
    while start + timedelta(seconds=offset) <= end:
        window_start = start + timedelta(seconds=offset)
        for service in by_service:
            spans = buckets.get("%s|%d" % (service, offset // window_s), [])
            entry = [span for span in spans if is_server_span(span)]
            observed = [span for span in spans if span["status"]]
            statuses = [span["status"] for span in observed]
            errors = sum(1 for code in statuses if code.isdigit() and int(code) >= 500)
            durations = sorted(span["duration_ms"] for span in spans)
            out[service].append({
                "window_start": window_start,
                "requests": len(entry),
                "requests_rate": len(entry) / float(window_s),
                "error_ratio": (errors / len(statuses)) if statuses else None,
                "latency_p50": durations[len(durations) // 2] if durations else None,
                "latency_p99": (durations[min(len(durations) - 1, int(0.99 * len(durations)))]
                                if durations else None),
                "spans": len(spans),
            })
        offset += window_s
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="Normal_Baseline_20251103_220228_traces_2025-11-03_22-02-59")
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--service", default="")
    args = parser.parse_args()

    cases = [p.name for p in TRACES.iterdir() if p.is_dir()]
    case = args.case
    if case not in cases:
        match = [c for c in cases if args.case in c]
        if not match:
            print("available cases:")
            for name in sorted(cases):
                print("  " + name)
            return 2
        case = match[0]

    data = windows(case, args.window_s)
    print("case      %s" % case)
    print("windows   %d per service, %ds each" % (len(next(iter(data.values()))), args.window_s))
    print("")
    service = args.service or "user-service"
    rows = data.get(service) or next(iter(data.values()))
    print("service   %s" % (service if service in data else next(iter(data))))
    print("%-21s %8s %9s %11s %10s %10s" % ("window_start", "req", "req/s", "error_ratio",
                                            "p50_ms", "p99_ms"))
    print("-" * 74)
    for row in rows[:12]:
        print("%-21s %8d %9.3f %11s %10s %10s"
              % (row["window_start"].strftime("%H:%M:%S"), row["requests"], row["requests_rate"],
                 ("%.3f" % row["error_ratio"]) if row["error_ratio"] is not None else "-",
                 ("%.2f" % row["latency_p50"]) if row["latency_p50"] is not None else "-",
                 ("%.2f" % row["latency_p99"]) if row["latency_p99"] is not None else "-"))

    print("")
    print("services in this case: %d" % len(data))
    busy = sorted(((name, sum(r["requests"] for r in rows)) for name, rows in data.items()),
                  key=lambda item: item[1], reverse=True)[:6]
    print("busiest (total entry spans): %s" % busy)
    print("")
    print("This is the block the catalogue consumes: requests.rate, errors.ratio, latency_ms.p50/p99.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
