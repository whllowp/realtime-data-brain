"""Which rendered line carries the trouble, host by host.

Round 8 of the RSI loop asks the question round 4 left open: on `machine-2-2` the engine ranks
faulted windows *below* healthy ones (AUC 0.347) while a threshold on the loudest rendered ratio
reaches 0.597. Same text, same labels. So either a particular rendered series points the wrong way,
or the engine is reading a line that means something else on that host.

This script answers the first half without spending a single model call: it replays the production
path (`Windower` -> `build_digest(profile="host")` -> `BaselineTracker`), then computes, for every
metric the renderer prints, the AUC of

  * `value`            — the window's own number,
  * `delta_ratio`      — against the 30-window median,
  * `long_delta_ratio` — against the 480-window median,

plus the share of printed ratios that rest on a baseline small enough to make the ratio noise.
A series whose `value` AUC is 0.87 and whose ratio AUC is 0.23 tells you the renderer threw the
signal away; a series where both are below 0.5 tells you the fault moves it the other way.

Usage:
    python scripts/real_data/host_diagnosis.py --machines machine-2-2,machine-1-6
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "scripts" / "real_data") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "real_data"))

from brian.calibration import auc                                       # noqa: E402
from brian.contracts import Event                                       # noqa: E402
from brian.state import (BaselineTracker, Windower, baseline_keys,      # noqa: E402
                         build_digest, digest_specs)

from smd_stream import COLUMNS, EPOCH, load                             # noqa: E402

#: A baseline below this is indistinguishable from "the series sits at zero", so the printed ratio is
#: arithmetic noise. SMD is full of these: `load 1m 0.01 (baseline 0.00, 3.60x)` reads like a
#: dramatic move and is a rounding artefact.
NOISE_FLOOR = 0.02

FIELDS = ("value", "delta_ratio", "long_delta_ratio")


def _entries(comparison: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Flatten `digest.metrics["comparison"]` — nested one level by group — into metric dicts."""
    found: List[Mapping[str, Any]] = []
    for value in comparison.values():
        if isinstance(value, Mapping) and "value" in value:
            found.append(value)
            continue
        if isinstance(value, Mapping):
            for inner in value.values():
                if isinstance(inner, Mapping) and "value" in inner:
                    found.append(inner)
    return found


def replay(machine: str, window_s: int = 300, baseline_windows: int = 30,
           long_windows: int = 480) -> Tuple[List[Any], Dict[int, bool]]:
    """Replay the production path; return the digests and a window-start -> anomalous map."""
    rows, labels = load(machine)
    service = "smd-%s" % machine
    specs = digest_specs("host")
    keys = baseline_keys("host")
    windower = Windower(size_s=window_s, slide_s=window_s, allowed_lateness_s=0, min_events=1,
                        specs=specs)
    tracker = BaselineTracker(window=baseline_windows, long_window=long_windows, keys=keys)
    digests: List[Any] = []
    truth: Dict[int, bool] = {}

    def consume(aggregators) -> None:
        for aggregator in aggregators:
            digest = build_digest(aggregator, tracker, profile="host")
            tracker.observe(digest.service, digest.metrics)
            digests.append(digest)
            start = int((digest.window_start - EPOCH).total_seconds() // 60)
            span = max(1, int(round((digest.window_end - digest.window_start).total_seconds() / 60)))
            truth[int(digest.window_start.timestamp())] = any(labels[start:start + span])

    for index, row in enumerate(rows):
        at = EPOCH + timedelta(seconds=60 * index)
        payload = {key: row[position - 1] for key, position in COLUMNS.items()}
        windower.add(Event(event_time=at, service=service, kind="metric", payload=payload,
                           event_id="%s-%06d" % (machine, index), ingest_time=at))
        consume(windower.ready())
    consume(windower.flush())
    return digests, truth


def metric_auc(digests: Sequence[Any], truth: Dict[int, bool],
               key: str, field: str) -> Optional[float]:
    scores: List[float] = []
    labels: List[int] = []
    for digest in digests:
        label = 1 if truth.get(int(digest.window_start.timestamp())) else 0
        for entry in _entries(digest.metrics.get("comparison") or {}):
            if entry.get("key") != key:
                continue
            value = entry.get(field)
            if value is None:
                continue
            scores.append(float(value))
            labels.append(label)
    if len(set(labels)) < 2 or len(scores) < 2:
        return None
    return auc(scores, labels)


def noise_share(digests: Sequence[Any]) -> Tuple[int, int]:
    """Printed ratios that rest on a baseline too small to divide by, out of all printed ones."""
    small = 0
    total = 0
    for digest in digests:
        for entry in _entries(digest.metrics.get("comparison") or {}):
            long_ratio, long_baseline = entry.get("long_delta_ratio"), entry.get("long_baseline")
            if long_ratio is not None and long_baseline is not None:
                ratio, baseline = long_ratio, long_baseline
            else:
                ratio, baseline = entry.get("delta_ratio"), entry.get("baseline")
            if ratio is None or baseline is None:
                continue
            total += 1
            if abs(baseline) < NOISE_FLOOR and abs(ratio - 1.0) > NOISE_FLOOR:
                small += 1
    return small, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machines", default="machine-2-2,machine-1-6")
    parser.add_argument("--window-s", type=int, default=300)
    args = parser.parse_args()

    for machine in [name.strip() for name in args.machines.split(",") if name.strip()]:
        digests, truth = replay(machine, window_s=args.window_s)
        positives = sum(1 for value in truth.values() if value)
        print("=" * 88)
        print("%s: %d windows (%d anomalous)" % (machine, len(digests), positives))
        print("=" * 88)
        print("%-16s %7s %7s %7s   %s" % ("metric", "value", "ratio", "long", "noise ratios"))
        keys: List[str] = []
        for digest in digests:
            for entry in _entries(digest.metrics.get("comparison") or {}):
                if entry.get("key") not in keys:
                    keys.append(entry.get("key"))
        for key in keys:
            row = []
            for field in FIELDS:
                value = metric_auc(digests, truth, key, field)
                row.append("    n/a" if value is None else "%7.3f" % value)
            small, total = noise_share(digests)
            print("%-16s %s %s %s   %d/%d" % (key, row[0], row[1], row[2], small, total))
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
