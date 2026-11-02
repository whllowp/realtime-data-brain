"""Profile the trace modality: the only place this dataset carries service-level golden signals.

`all_traces.csv` has one row per span with `service`, `start_time`, `duration_us` and `http_status_code`,
which is exactly the shape the catalogue wants: request rate, latency percentiles and error ratio, per
service, per window.

What decides whether an evaluation is possible is not the columns but the density: how many spans, over how
long, and how many distinct *normal* runs there are. A detector cannot be validated against a single normal
case, however many spans it contains, so those numbers are measured before anything is built.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "anomod" / "SN_data" / "trace_data"


def parse(stamp: str) -> datetime:
    return datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S.%f")


def main() -> int:
    if not DATA.is_dir():
        print("trace_data not found under %s" % DATA)
        return 2
    cases = sorted(p for p in DATA.iterdir() if p.is_dir())
    print("trace cases: %d" % len(cases))
    print("")
    print("%-50s %7s %8s %8s %8s %s" % ("case", "spans", "span_s", "services", "5xx", "code mix"))
    print("-" * 104)

    totals: Counter = Counter()
    per_case_services: List[set] = []
    for case in cases:
        path = case / "all_traces.csv"
        if not path.is_file():
            continue
        stamps: List[datetime] = []
        services: Counter = Counter()
        codes: Counter = Counter()
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    stamps.append(parse(row["start_time"]))
                except (KeyError, ValueError):
                    continue
                services[row.get("service") or "?"] += 1
                code = (row.get("http_status_code") or "").strip()
                if code:
                    codes[code] += 1
        if not stamps:
            continue
        span = (max(stamps) - min(stamps)).total_seconds()
        five = sum(v for k, v in codes.items() if k.isdigit() and int(k) >= 500)
        totals.update(codes)
        per_case_services.append(set(services))
        print("%-50s %7d %8.0f %8d %8d %s"
              % (case.name[:50], len(stamps), span, len(services), five,
                 dict(codes.most_common(4))))

    print("")
    print("status codes overall: %s" % dict(totals.most_common(8)))
    print("")
    if per_case_services:
        common = set.intersection(*per_case_services)
        print("services present in every case (%d): %s" % (len(common), sorted(common)))
        print("services in at least one case: %d" % len(set.union(*per_case_services)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
