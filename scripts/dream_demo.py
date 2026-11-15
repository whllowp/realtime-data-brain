"""Run the dreaming pass over a real stream and report what it changed.

Produces the transcripts by driving the demo through a MemoryBus, then runs `brian.dreaming.dream` over the
records it published. The note used is deliberately a mix: claims about patterns that are present, a claim
about a pattern that is not, and generic guidance. That is the only way to see whether validation does
anything — a note that is entirely correct proves nothing about the check.

Usage:
    python scripts/dream_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, generate  # noqa: E402
from brian.dreaming import dream, persist  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402
from brian.topics import Topic  # noqa: E402

#: A note with three kinds of line: grounded, stale, and timeless.
SAMPLE_NOTE = """- checkout-api resource_exhaustion: pool exhaustion shows as latency before errors.
- checkout-api dependency_failure: upstream 5xx lead, local errors follow.
- payment-gateway disk saturation: seen repeatedly, watch disk_io.
- When two hypotheses fit, prefer the one whose evidence appears first in the window."""


def main() -> int:
    settings = load_settings()
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    bus = MemoryBus()
    pipeline = Pipeline(settings, bus=bus)
    for window in windows:
        for event in window.events:
            pipeline.ingest(event)
        pipeline.drain_ready()
    pipeline.drain_ready(final=True)

    records = []
    for topic in (Topic.DECISIONS, Topic.RCA, Topic.ALERTS):
        try:
            records.extend(bus.replay(settings.topic(topic)))
        except Exception:  # noqa: BLE001 - a topic with no records is not an error
            continue
    print("transcripts: %d records (decisions + rca + alerts)" % len(records))
    if not records:
        print("no transcripts; nothing to dream about")
        return 2

    report, index, cleaned = dream(records, SAMPLE_NOTE)
    print("")
    print("index entries        %d" % report.index_entries)
    print("claims checked       %d" % report.claims_checked)
    print("claims dropped       %d" % report.claims_dropped)
    for item in report.dropped:
        print("  - %s  (%s)" % (item["claim"][:70], item["reason"]))
    print("")
    print("injected block       %d tokens (cap 400)" % report.injected_tokens)
    print("-" * 70)
    print(report.injected)
    print("-" * 70)

    out = REPO_ROOT / "var" / "memory"
    persist(out, report, index, cleaned)
    print("")
    print("wrote %s/{incident_index.json,injected.md,note.md,dream_report.json}" % out)
    print("")
    print("What to look for: the injected block must stay a bounded index (not prose), and every dropped")
    print("claim must name a pattern the transcripts do not contain. Anything else means the check is")
    print("either too eager or asleep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
