"""Convert RCAEval RE1-OB cases into a labelled stream file that `brian evolve` can measure.

The improvement loop needs a verification signal that can *reject* something. Its first stream was
`brian demo generate`, a suite of scenarios written to be detectable: on it the detector scores
recall 0.875 / FPR 0.000, so the label column is saturated and every candidate passes it. This
adapter produces the opposite kind of stream — 125 recordings of real fault injection, labelled
from `inject_time.txt` — and the loop's verdicts on it are a different, harder claim.

Two conventions are inherited from `bench_rcaeval.py` because they are correctness questions, not
style:

* **A window is anomalous if it starts at or after the injection.** Partial overlap is not counted
  as a detection, so the labels never credit a window that is mostly healthy.
* **One recording is one stream, with its own baseline.** Concatenating cases into one stream would
  score the first window of case B against case A's history — measuring the seam between two
  recordings instead of the detector.

Warm-up is adaptive (`min(10, pre_fault // 2)`): cpu/mem recordings run ~2,100 s before the fault
but delay/disk/loss ones only ~360 s, and a fixed 10-window warm-up would consume the entire
healthy period of the short recordings.

Usage:
    python scripts/service_data/rcaeval_stream.py --fault cpu --limit 2
    python scripts/service_data/rcaeval_stream.py                 # all 125 cases
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "service_data"))

from brian.contracts import Event  # noqa: E402
from brian.demo import LabeledWindow  # noqa: E402
from brian.streams import Stream, dump_stream_file  # noqa: E402

from rcaeval_to_events import DATA, fault_type, window_events  # noqa: E402

#: Base configuration this domain needs. The shipped thresholds were calibrated on the synthetic
#: domain; on RCAEval the engine puts healthy and faulted windows at nearly the same probability, so
#: an inherited threshold does not mean "strict", it means "silent". Calibration is disabled for the
#: same reason: the Platt fit was made on other data.
BASE_OVERRIDES = {
    "calibration": {"enabled": False},
    "gate": {"anomaly_probability": 0.5, "notify_probability": 0.5},
    # RCAEval publishes one event per metric per window (four events); the shipped minimum of ten
    # assumes a production feed with thousands. Without this the pipeline emits no window at all,
    # the evaluator scores fine (it uses min_events=1), and the round would compare token costs of
    # zero reports against zero reports.
    "window": {"min_events": 1},
}


def case_stream(case_dir: Path, window_s: int, warmup_cap: int) -> Optional[Stream]:
    """One case as one stream of labelled windows, in the production service digest format."""
    events, inject, meta = window_events(case_dir, window_s)
    if not events:
        return None
    grouped: Dict[datetime, List[Event]] = {}
    for event in events:
        grouped.setdefault(event.event_time, []).append(event)
    starts = sorted(grouped)
    pre_fault = max(1, int((inject - starts[0].timestamp()) // window_s))
    warmup = max(1, min(warmup_cap, pre_fault // 2))
    windows = [
        LabeledWindow(
            service=meta["service"],
            window_start=start,
            scenario=meta["case"],
            events=grouped[start],
            expected={"is_anomaly": start.timestamp() >= inject},
            scored=index >= warmup,
        )
        for index, start in enumerate(starts)
    ]
    return Stream(
        name=meta["case"],
        windows=windows,
        meta={"fault": meta["fault"], "service": meta["service"],
              "inject": inject, "warmup": warmup},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-fault", type=int, default=0,
                        help="take at most N cases per fault family (balance beats size here: "
                             "sorted order is all one service, so --limit alone samples one family)")
    parser.add_argument("--fault", default="", help="comma list: cpu,mem,delay,disk,loss")
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=10, help="cap on warm-up windows")
    parser.add_argument("--out", default="data/rcaeval/stream.json")
    args = parser.parse_args()

    cases = sorted(p for p in DATA.iterdir() if p.is_dir() and p.name.startswith("re1ob_"))
    if not cases:
        print("no cases under %s" % DATA)
        return 2
    if args.fault:
        wanted = [name.strip() for name in args.fault.split(",") if name.strip()]
        cases = [case for case in cases if fault_type(case.name) in wanted]
    if args.per_fault:
        taken: Dict[str, int] = {}
        balanced = []
        for case in cases:
            family = fault_type(case.name)
            if taken.get(family, 0) >= args.per_fault:
                continue
            taken[family] = taken.get(family, 0) + 1
            balanced.append(case)
        cases = balanced
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("no cases matched the filter")
        return 2

    streams = [stream for stream in (case_stream(case, args.window_s, args.warmup)
                                     for case in cases) if stream is not None]
    if not streams:
        print("cases produced no windows")
        return 2

    path = dump_stream_file(Path(args.out), streams, dataset="RCAEval RE1-OB",
                            window_s=args.window_s, overrides=BASE_OVERRIDES)
    scored = sum(stream.stats()["scored"] for stream in streams)
    anomalous = sum(stream.stats()["anomalous"] for stream in streams)
    events = sum(stream.stats()["events"] for stream in streams)
    by_fault: Dict[str, int] = {}
    for stream in streams:
        by_fault[stream.meta["fault"]] = by_fault.get(stream.meta["fault"], 0) + 1

    print("streams   %d  (%s)" % (len(streams), ", ".join(
        "%s=%d" % (fault, count) for fault, count in sorted(by_fault.items()))))
    print("windows   %d scored (%d anomalous = %.1f%%), %d events"
          % (scored, anomalous, 100 * anomalous / scored, events))
    print("baseline  %s" % ", ".join("%s=%r" % (section, values)
                                     for section, values in BASE_OVERRIDES.items()))
    print("wrote     %s  (%.1f KB)" % (path, path.stat().st_size / 1024))
    print("")
    print("next:     python -m brian.cli evolve --stream-file %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
