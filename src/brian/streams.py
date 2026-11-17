"""Labelled window streams from a file, so the loop can be pointed at data nobody wrote by hand.

`brian evolve` first ran on `brian demo generate`, which is a suite of scenarios written to be
detectable: on it the detector scores recall 0.875 and FPR 0.000, so the label column of the
evaluator is saturated and *cannot* reject a candidate. A verification signal that never fires
is not a signal — it is decoration that makes the loop look supervised.

This module is the escape hatch. Adapter scripts (see `scripts/service_data/rcaeval_stream.py`)
convert a real dataset into this file format once, in whatever environment has the readers for
it; `brian` itself only needs the standard library to read it back. The loop then measures the
same mechanism on fault-injection recordings instead of on scenarios designed to be caught.

File format (`brian.stream/1`)::

    {
      "format": "brian.stream/1",
      "dataset": "RCAEval RE1-OB",
      "window_s": 60,
      "overrides": {"calibration": {"enabled": false},
                    "gate": {"anomaly_probability": 0.5, "notify_probability": 0.5}},
      "streams": [
        {"name": "re1ob_adservice_cpu_1",
         "meta": {"fault": "cpu", "service": "adservice"},
         "windows": [
           {"start": "2026-01-01T00:00:00Z", "anomaly": false, "scored": true,
            "events": [{"time": "2026-01-01T00:00:00Z", "service": "adservice",
                        "kind": "metric", "payload": {"cpu_util": 0.31}}]}
         ]}
      ]
    }

`overrides` is not a convenience. The shipped gate thresholds were calibrated on this repo's
synthetic domain; applied unchanged to a different domain they can silence the detector entirely,
and the resulting round would report "no candidate helps" about a configuration mistake. Putting
the base configuration in the file means the choice travels with the data, is visible in the record,
and cannot be forgotten by whoever re-runs the loop next.

Why one file per dataset rather than a reader per dataset: the format is the boundary where the
verification signal enters the loop, so it should be inspectable, diffable and cacheable, and the
loop should not gain a dependency on a parquet stack to be runnable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .contracts import ContractError, Event, iso, parse_iso
from .demo import LabeledWindow

#: Bumped when the on-disk shape changes in a way an old reader would misread rather than reject.
FORMAT = "brian.stream/1"


class StreamError(ValueError):
    """The file is not a stream this version can read. Loud, because silence scores as nothing."""


def align_window_start(moment: datetime, size_s: int,
                       slide_s: Optional[int] = None) -> datetime:
    """The window start `Windower` will emit for an event at `moment`.

    The windower assigns by event time onto a grid of `slide_s` seconds from the epoch (see
    `Windower._window_start`), so a label carrying a raw sample timestamp never matches a digest.
    When it does not, `Evaluator` scores nothing and prints `recall 0.000` — a number that reads
    like a finding about the detector and is in fact a finding about the label. Round 19 hit this
    on the RCAEval stream, where the labels were sample times (`…588`) and the digests were grid
    times (`…560`); rounds 2-3 had reported exactly that zero.
    """
    if size_s <= 0:
        raise StreamError("window size must be positive, got %r" % (size_s,))
    slide = max(1, int(slide_s if slide_s is not None else size_s))
    epoch = int(moment.timestamp())
    start = (epoch // slide) * slide - max(0, int(size_s) - slide)
    return datetime.fromtimestamp(start, tz=moment.tzinfo)


@dataclass
class Stream:
    """One recording: a run of labelled windows with its own baseline history.

    Streams are kept separate rather than concatenated because each recording has its own
    baseline. Scoring case B's first window against case A's history measures the seam between two
    recordings, not the detector — the same trap as sharing one baseline across machines.
    """

    name: str
    windows: List[LabeledWindow] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def events(self) -> List[Event]:
        return [event for window in self.windows for event in window.events]

    def stats(self) -> Dict[str, Any]:
        scored = [window for window in self.windows if window.scored]
        return {
            "name": self.name,
            "windows": len(self.windows),
            "scored": len(scored),
            "anomalous": sum(1 for window in scored if (window.expected or {}).get("is_anomaly")),
            "events": sum(len(window.events) for window in self.windows),
            **self.meta,
        }


@dataclass
class StreamSet:
    """A whole file: the recordings, where they came from, and the base config they need.

    `overrides` is carried, not applied, here — this module has no business knowing what a
    `Settings` object looks like. `evolve.apply_overrides` applies it before any candidate is
    measured, so every candidate is compared on the same base.
    """

    streams: List[Stream] = field(default_factory=list)
    dataset: str = ""
    window_s: int = 0
    overrides: Dict[str, Any] = field(default_factory=dict)

    def stats(self) -> Dict[str, Any]:
        return {**summarise(self.streams), "dataset": self.dataset, "window_s": self.window_s,
                "overrides": self.overrides}


def _window(payload: Dict[str, Any], name: str, index: int) -> LabeledWindow:
    try:
        start = parse_iso(payload.get("start"))
    except ContractError as exc:
        raise StreamError("%s: window %d has no readable `start` (%s)" % (name, index, exc)) from exc
    if start is None:
        raise StreamError("%s: window %d has no readable `start`" % (name, index))
    service = payload.get("service") or ""
    events: List[Event] = []
    for event in payload.get("events") or []:
        moment = parse_iso(event.get("time")) or start
        events.append(Event(event_time=moment, service=event.get("service") or service,
                            kind=event.get("kind") or "metric",
                            payload=dict(event.get("payload") or {}),
                            ingest_time=parse_iso(event.get("ingest")) or moment,
                            source=event.get("source") or "stream"))
    if not service and events:
        service = events[0].service
    return LabeledWindow(
        service=service,
        window_start=start,
        scenario=payload.get("scenario") or name,
        events=events,
        expected={"is_anomaly": bool(payload.get("anomaly")),
                  "category": payload.get("category"),
                  "severity": payload.get("severity")},
        scored=bool(payload.get("scored", True)),
    )


def load_stream_file(path: Path) -> "StreamSet":
    """Read a stream file. Rejects an unknown format rather than guessing at its meaning."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StreamError("%s: expected an object at the top level" % path)
    version = raw.get("format")
    if version != FORMAT:
        raise StreamError("%s: format %r, this build reads %r" % (path, version, FORMAT))
    entries = raw.get("streams")
    if not isinstance(entries, list) or not entries:
        raise StreamError("%s: no streams to measure" % path)

    streams: List[Stream] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if not name:
            raise StreamError("%s: a stream has no name" % path)
        windows = [_window(item, name, index)
                   for index, item in enumerate(entry.get("windows") or [])]
        meta = dict(entry.get("meta") or {})
        meta.pop("name", None)
        streams.append(Stream(name=name, windows=windows, meta=meta))

    overrides = raw.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise StreamError("%s: `overrides` must be an object of section -> fields" % path)
    return StreamSet(streams=streams, dataset=str(raw.get("dataset") or ""),
                     window_s=int(raw.get("window_s") or 0), overrides=overrides)


def dump_stream_file(path: Path, streams: Sequence[Stream], dataset: str,
                     window_s: int, overrides: Optional[Dict[str, Any]] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Path:
    """Write a stream file. Adapters use this so both sides agree on the shape by construction."""
    payload = {
        "format": FORMAT,
        "dataset": dataset,
        "window_s": window_s,
        "overrides": dict(overrides or {}),
        "streams": [
            {
                "name": stream.name,
                "meta": dict(stream.meta),
                "windows": [
                    {
                        "start": iso(window.window_start),
                        "service": window.service,
                        "scenario": window.scenario,
                        "anomaly": bool((window.expected or {}).get("is_anomaly")),
                        "scored": window.scored,
                        "events": [
                            {"time": iso(event.event_time), "service": event.service,
                             "kind": event.kind, "payload": event.payload,
                             "source": event.source}
                            for event in window.events
                        ],
                    }
                    for window in stream.windows
                ],
            }
            for stream in streams
        ],
    }
    if extra:
        payload.update(extra)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=False) + "\n", encoding="utf-8")
    return target


def summarise(streams: Sequence[Stream]) -> Dict[str, Any]:
    """Totals the round record quotes, computed the same way whether one stream or a hundred."""
    rows = [stream.stats() for stream in streams]
    return {
        "streams": len(rows),
        "windows": sum(row["windows"] for row in rows),
        "scored": sum(row["scored"] for row in rows),
        "anomalous": sum(row["anomalous"] for row in rows),
        "events": sum(row["events"] for row in rows),
        "names": [row["name"] for row in rows],
    }


def flatten_streams(streams: Sequence[Stream]) -> List[Event]:
    """All events, still ordered by time. Used only where per-recording order cannot matter."""
    return sorted((event for stream in streams for event in stream.events()),
                  key=lambda event: (event.event_time, event.event_id))
