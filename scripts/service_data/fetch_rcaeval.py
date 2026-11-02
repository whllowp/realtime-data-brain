"""Fetch the RCAEval RE1-OB cases from Hugging Face, one case at a time.

Why this dataset
----------------
The service half of the architecture — request rate, error ratio, latency — has never been checked against
real telemetry. SMD is host-only and AnoMod turned out to have no service metrics and only one normal run.
RE1-OB has, per case:

    inject_time.txt    the Unix time the fault was injected at  -> per-window labels become possible
    metrics.parquet    one row per second with, per service, `_load` (requests), `_latency` and `_cpu`;
                       plus `PassthroughCluster_error` / `frontend_error` for failures

That is exactly the catalogue's vocabulary, with a fault timestamp rather than a run-level label.

The download is selective: only `re1ob_*` cases (the metric-only suite, ~360 KB each), not the 4 GB RE2
archive. Files land under `data/rcaeval/`, which is gitignored.

Usage:
    python scripts/service_data/fetch_rcaeval.py                # all re1ob cases
    python scripts/service_data/fetch_rcaeval.py --limit 5      # first five, for a smoke test
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "data" / "rcaeval"
CACHE = OUT / "cases.json"
API = "https://huggingface.co/api/datasets/phamquiluan/RCAEval"
RAW = "https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/main"


def fetch(url: str, timeout: float = 120.0, attempts: int = 5) -> bytes:
    """GET with backoff. Hugging Face answers 429 when a sweep is too eager; a blind retry loop would
    either hammer it harder or give up, so the wait doubles each time and is retried a bounded number of
    times."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "brian-service-validation/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == attempts:
                raise
            print("    429 on attempt %d, waiting %.0fs" % (attempt, delay))
            time.sleep(delay)
            delay *= 2
        except urllib.error.URLError:
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def main() -> int:
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    OUT.mkdir(parents=True, exist_ok=True)
    if CACHE.is_file():
        listing = json.loads(CACHE.read_text(encoding="utf-8"))
        print("using cached listing")
    else:
        listing = json.loads(fetch(API, timeout=60).decode("utf-8"))
        CACHE.write_text(json.dumps(listing), encoding="utf-8")
    names = [entry["rfilename"] for entry in listing.get("siblings", [])]
    cases = sorted({name.split("/")[0] for name in names if name.startswith("re1ob_")})
    print("RE1-OB cases in the repository: %d" % len(cases))
    if limit:
        cases = cases[:limit]
        print("limiting to %d for this run" % len(cases))

    OUT.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped = 0
    for index, case in enumerate(cases, 1):
        target = OUT / case
        target.mkdir(parents=True, exist_ok=True)
        for file_name in ("inject_time.txt", "metrics.parquet"):
            path = target / file_name
            if path.is_file() and path.stat().st_size > 0:
                skipped += 1
                continue
            try:
                path.write_bytes(fetch("%s/%s/%s" % (RAW, case, file_name)))
                downloaded += 1
            except Exception as exc:  # noqa: BLE001 - one bad case must not stop the sweep
                print("  FAILED %s/%s: %s" % (case, file_name, exc))
        if index % 10 == 0:
            print("  %d/%d cases" % (index, len(cases)))

    print("downloaded %d files, skipped %d already present" % (downloaded, skipped))
    total = sum(path.stat().st_size for path in OUT.rglob("*") if path.is_file())
    print("on disk: %.1f MB under %s" % (total / 1e6, OUT.relative_to(REPO_ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
