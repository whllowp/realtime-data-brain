"""What does a bounded note actually cost, and what could it replace?

The honest question for this system. SRN saves tokens because the methods it beats spend 74.8-93.6% of
their budget on *verification*, and SRN does none. This pipeline already does none either — one call per
report, no candidates, no re-scoring. So a note cannot save that spend; it is not there.

What a note can do is **substitute**: carry standing lessons so the per-alert prompt needs less raw
evidence. That is testable — build the same prompts under four configurations and compare token estimates:

    baseline        no note, 20 events   (what ships today)
    note only       note, 20 events      (pure addition: the cost of the idea)
    note + few      note, 4 events       (substitution: does the note earn the space it takes?)
    few only        4 events, no note    (is the evidence reduction doing the work by itself?)

Only `note + few` beating `baseline` on tokens *without* collapsing the evidence is a win. The last row is
the control that stops a saving being attributed to the note when it came from deleting context.

Usage:
    python scripts/measure_note_cost.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, flatten, generate  # noqa: E402
from brian.llm.prompts import build_rca_messages, prompt_token_estimate  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402

#: A plausible note: four short lessons, well inside the 2,048-token cap.
SAMPLE_NOTE = """- checkout-api + orders-db: pool exhaustion shows as latency rising before error rate.
- deploy within the window: assume deploy first, verify by rolling back one version.
- traffic surge with flat errors is capacity, not failure; do not page for it alone.
- data_quality: schema errors lead; downstream 5xx are symptoms, not the cause."""


def main() -> int:
    settings = load_settings()
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    pipeline = Pipeline(settings, bus=MemoryBus())
    traces = []
    for event in flatten(windows):
        pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)
    traces.extend(pipeline.drain_ready(final=True))

    pairs = [(t.digest, t.decision, list(t.history or [])) for t in traces if t.report is not None and t.decision is not None]
    if not pairs:
        print("no alerts in this stream; nothing to measure")
        return 2

    configurations = [
        ("baseline      (no note, 20 events)", dict(max_events=20)),
        ("note only     (note, 20 events)", dict(max_events=20, note=SAMPLE_NOTE)),
        ("note + few    (note, 4 events)", dict(max_events=4, note=SAMPLE_NOTE)),
        ("few only      (4 events, no note)", dict(max_events=4)),
    ]

    print("alerts measured: %d" % len(pairs))
    print("note size      : %d chars" % len(SAMPLE_NOTE))
    print("")
    print("%-36s %10s %10s %s" % ("configuration", "tokens/alt", "total", "vs baseline"))
    print("-" * 74)
    baseline_total = None
    for label, options in configurations:
        estimates = []
        for digest, decision, events in pairs:
            messages = build_rca_messages(digest, decision, events=events, **options)
            estimates.append(prompt_token_estimate(messages))
        total = sum(estimates)
        mean = total / len(estimates)
        if baseline_total is None:
            baseline_total = total
            change = "-"
        else:
            change = "%+.1f%%" % (100 * (total - baseline_total) / baseline_total)
        print("%-36s %10.1f %10d %10s" % (label, mean, total, change))

    print("")
    print("Reading this table:")
    print("  'note only'  shows what the note costs when nothing is removed for it.")
    print("  'note + few' is the only line that could be a win; 'few only' is the control that")
    print("  separates the note's contribution from simply deleting evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
