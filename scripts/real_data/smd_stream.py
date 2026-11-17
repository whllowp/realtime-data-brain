"""Turn SMD (Server Machine Dataset) host metrics into a brian stream file.

Why this exists: the improvement loop needs a real domain whose label column can *reject* a candidate.
The synthetic demo stream scores 0.875 recall / 0.000 FPR, so every cost change is accepted for free;
RCAEval carries real service metrics but the shipped gate silences it entirely (0 alerts in 456 scored
windows), so the token column is undefined there. SMD sits between the two: the engine ranks it at
AUC ~0.745 rather than at chance, so recall and false positives can actually move.

Mapping decisions:
  * one sample per 60 s, one window per `--window-s` seconds (default 300, the usual SMD practice), so a
    window holds five samples and the digest's `last` reduce reports the window's final sample;
  * payload keys are the SMD column names, which is what `brian.state.HOST_METRICS` reads, so the
    shipped digest path runs unchanged;
  * a window is anomalous when any of its samples is labelled (`any(...)`, as the SMD bench does);
  * windows are sampled by position with a fixed stride over the whole file rather than a contiguous
    prefix — an earlier round measured a 400-window prefix and got a 6-anomaly slice that made AUC noise.

The base overrides travel inside the stream file because they are part of "what it takes to run this
domain": SMD windows carry five events, while the shipped `window.min_events` is ten.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.contracts import Event  # noqa: E402
from brian.demo import LabeledWindow  # noqa: E402
from brian.state import HOST_METRICS  # noqa: E402
from brian.streams import Stream, dump_stream_file  # noqa: E402

DATA = REPO_ROOT / "data" / "smd"
EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)

#: SMD column layout (1-indexed). Only the columns the digest reads are used.
COLUMNS: Dict[str, int] = {
    "cpu_r": 1, "cpu_s": 2, "load_1": 4, "mem_free": 7,
    "swap_used": 13, "disk_io_write": 15, "net_in_bytes": 22, "net_out_bytes": 23,
}

#: See the module docstring: five events per window, so the shipped minimum of ten would drop them all.
#: What this dataset needs before the pipeline can read it at all.
#:
#: `min_events=1` because a 300-second host window carries five samples, not the ten events the
#: service window requires. `anomaly_probability=0.33` because the engine's answers on this domain all
#: land between 0.30 and 0.47 — a cut anywhere above that range silences the detector no matter how
#: good its ranking is, and 0.33 is the operating point the archived bench runs found at a 10%
#: false-positive budget. The loop still compares configurations at that budget rather than at this
#: cut, so the constant decides whether alerts exist, not who wins.
#: `notify_probability` must not sit above that same range: with the service default (0.5) every window
#: is "anomalous but not worth raising", so the gate returns `OBSERVE`, no alert exists, System 2 never
#: runs, and the cost column of every round is empty — a measurement artefact that reads exactly like
#: "this domain never alerts". Detection, notification and billing are three separate settings.
BASE_OVERRIDES = {
    "calibration": {"enabled": False},
    "digest": {"profile": "host"},
    "gate": {"anomaly_probability": 0.33, "notify_probability": 0.33},
    "window": {"size_s": 300, "slide_s": 300, "min_events": 1},
}


def load(machine: str):
    rows = [
        [float(value) for value in line.split(",")]
        for line in (DATA / ("%s.data.txt" % machine)).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    labels = [int(token) for token in (DATA / ("%s.label.txt" % machine)).read_text().split()]
    if len(rows) != len(labels):
        raise SystemExit("data/label length mismatch for %s" % machine)
    return rows, labels


def machine_stream(machine: str, window_s: int, stride: int, warmup: int) -> Stream:
    rows, labels = load(machine)
    service = "smd-%s" % machine
    keys = [spec.source for spec in HOST_METRICS]
    samples_per_window = max(1, window_s // 60)

    windows: List[LabeledWindow] = []
    index = 0
    while index + samples_per_window <= len(rows):
        if (index // samples_per_window) % stride == 0 and len(windows) >= 0:
            events = []
            anomalous = False
            for offset in range(samples_per_window):
                row = rows[index + offset]
                payload = {key: row[COLUMNS[key] - 1] for key in keys}
                stamp = EPOCH + timedelta(minutes=index + offset)
                events.append(Event(
                    event_time=stamp,
                    service=service,
                    kind="metric",
                    payload=payload,
                    event_id="%s-%06d" % (machine, index + offset),
                    ingest_time=stamp,
                    source="smd",
                ))
                anomalous = anomalous or bool(labels[index + offset])
            windows.append(LabeledWindow(
                service=service,
                window_start=EPOCH + timedelta(minutes=index),
                scenario="smd",
                events=events,
                expected={"is_anomaly": anomalous},
                scored=len(windows) >= warmup,
            ))
        index += samples_per_window

    return Stream(name=service, windows=windows,
                  meta={"machine": machine, "rows": len(rows), "stride": stride,
                        "warmup_windows": warmup})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--machines", default="machine-1-6,machine-2-2")
    parser.add_argument("--window-s", type=int, default=300)
    parser.add_argument("--stride", type=int, default=10,
                        help="keep every Nth window; position sampling covers the whole file")
    parser.add_argument("--warmup", type=int, default=60, help="leading windows left unscored")
    parser.add_argument("--out", default="data/smd/stream.json")
    args = parser.parse_args()

    streams = [
        machine_stream(name.strip(), args.window_s, args.stride, args.warmup)
        for name in args.machines.split(",")
        if name.strip()
    ]
    path = dump_stream_file(REPO_ROOT / args.out, streams, dataset="SMD",
                            window_s=args.window_s, overrides=BASE_OVERRIDES)

    scored = sum(stream.stats()["scored"] for stream in streams)
    anomalous = sum(stream.stats()["anomalous"] for stream in streams)
    events = sum(stream.stats()["events"] for stream in streams)
    print("streams   %d  (%s)" % (len(streams), ", ".join(s.name for s in streams)))
    for stream in streams:
        stats = stream.stats()
        print("  %-20s windows %5d scored %5d anomalous %4d (%.1f%%) events %5d"
              % (stream.name, stats["windows"], stats["scored"], stats["anomalous"],
                 100.0 * stats["anomalous"] / max(1, stats["scored"]), stats["events"]))
    print("total     %d scored (%d anomalous = %.1f%%), %d events"
          % (scored, anomalous, 100.0 * anomalous / max(1, scored), events))
    print("baseline  " + ", ".join("%s=%s" % (section, values) for section, values in BASE_OVERRIDES.items()))
    print("wrote     %s  (%.1f KB)" % (args.out, path.stat().st_size / 1024))
    print("\nnext:     python -m brian.cli evolve --stream-file %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
