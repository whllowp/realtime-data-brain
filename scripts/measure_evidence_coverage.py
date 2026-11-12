"""What does cutting the event context actually cost?

Round 3 measured that dropping the event context from 20 to 4 cuts 36.8% of the prompt tokens. That number
is only half an answer: the other half is what the model stops seeing. Without a configured model the report
quality itself cannot be measured, so this measures the thing underneath it — **evidence coverage** — which
is decidable from the data alone:

    log events retained      the raw error lines are the most diagnostic thing in the prompt
    first error retained     did the *first* failure survive, or only its tail?
    distinct signatures      how many different error templates remain (a repeat of one template is cheap
                             evidence, a second unrelated template is not)
    deploy / config change   the context events that change how everything else reads

A cut that keeps 100% of log evidence at 4 events is free. A cut that keeps the last four *metric* samples
and drops every error line is not a saving, it is a blindfold.

Usage:
    python scripts/measure_evidence_coverage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, flatten, generate  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402

CUTS = (4, 8, 20)


def is_log(event) -> bool:
    return getattr(event, "kind", "") == "log"


def signature(event) -> str:
    payload = getattr(event, "payload", {}) or {}
    return str(payload.get("error") or payload.get("message") or "")[:60]


def is_context_event(event) -> bool:
    """Deploy or config-change markers: they reframe every other measurement in the window."""
    payload = getattr(event, "payload", {}) or {}
    return any(key in payload for key in ("deploy", "version", "config_change", "flag"))


def coverage(history, k):
    full = list(history)
    from brian.llm.prompts import select_events
    selected = select_events(full, k) if k < len(full) else full
    logs_full = [e for e in full if is_log(e)]
    logs_sel = [e for e in selected if is_log(e)]
    sigs_full = {signature(e) for e in logs_full}
    sigs_sel = {signature(e) for e in logs_sel}
    ctx_full = [e for e in full if is_context_event(e)]
    ctx_sel = [e for e in selected if is_context_event(e)]
    first_error_full = logs_full[0].event_time if logs_full else None
    first_error_sel = logs_sel[0].event_time if logs_sel else None
    return {
        "events": len(selected),
        "logs_full": len(logs_full),
        "logs_sel": len(logs_sel),
        "log_retention": (len(logs_sel) / len(logs_full)) if logs_full else 1.0,
        "signature_retention": (len(sigs_sel) / len(sigs_full)) if sigs_full else 1.0,
        "first_error_kept": (first_error_full is not None and first_error_sel == first_error_full),
        "context_full": len(ctx_full),
        "context_sel": len(ctx_sel),
        "context_retention": (len(ctx_sel) / len(ctx_full)) if ctx_full else 1.0,
    }


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

    alerts = [t for t in traces if t.report is not None and t.history]
    if not alerts:
        print("no alerts with event history; nothing to measure")
        return 2

    print("alerts with history: %d" % len(alerts))
    print("full context per alert: mean %.1f events"
          % (sum(len(t.history) for t in alerts) / len(alerts)))
    print("")
    print("%-6s %10s %12s %12s %12s %12s" %
          ("cut", "events", "log keep", "signature", "first error", "deploy/cfg"))
    print("-" * 70)
    for k in CUTS:
        rows = [coverage(t.history, k) for t in alerts]
        mean = lambda key: sum(r[key] for r in rows) / len(rows)
        first_kept = sum(1 for r in rows if r["first_error_kept"]) / len(rows)
        print("%-6d %10.1f %11.0f%% %11.0f%% %11.0f%% %11.0f%%"
              % (k, mean("events"), 100 * mean("log_retention"), 100 * mean("signature_retention"),
                 100 * first_kept, 100 * mean("context_retention")))

    print("")
    print("Interpretation: a cut is free only while it keeps the log evidence. The 'fastest' row is not")
    print("the best row unless its columns still read 100%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
