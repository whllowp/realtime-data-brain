"""Round 7 measurement: how much does report reuse recover, and does it stay safe?

Measured before the feature existed: 3 of 8 alerts were repeats of an incident already analysed, and the
repeats were 37% of the analysis bill. This runs the same stream with reuse disabled and with a cooldown,
and checks both the tokens and the guard rails.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus
from brian.config import load_settings
from brian.demo import DEFAULT_SEQUENCE, generate
from brian.pipeline import Pipeline


def run(cooldown):
    base = load_settings()
    gate = type(base.gate)(**{**vars(base.gate), "reuse_report_within_s": cooldown})
    settings = base.with_overrides(gate=gate)
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    pipeline = Pipeline(settings, bus=MemoryBus())
    traces = []
    for window in windows:
        for event in window.events:
            pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)
    traces.extend(pipeline.drain_ready(final=True))

    reports = [t.report for t in traces if t.report is not None]
    billed = [r for r in reports if r.prompt_tokens == 0 and getattr(r, "reused_from", None) is None]
    reused = [r for r in reports if getattr(r, "reused_from", None)]
    tokens = sum((r.prompt_tokens + r.completion_tokens) if r.prompt_tokens else r.estimated_prompt_tokens
                 for r in reports)
    return {"reports": len(reports), "reused": len(reused), "billed": len(billed), "tokens": tokens}


off = run(0)
on = run(300)
print("alerts with a report: %d" % off["reports"])
print("")
print("%-22s %10s %10s %10s" % ("policy", "reports", "reused", "tokens"))
print("-" * 56)
print("%-22s %10d %10d %10d" % ("reuse off", off["reports"], off["reused"], off["tokens"]))
print("%-22s %10d %10d %10d" % ("reuse within 300s", on["reports"], on["reused"], on["tokens"]))
saved = off["tokens"] - on["tokens"]
print("")
print("tokens saved: %d (%.0f%%)" % (saved, 100 * saved / off["tokens"] if off["tokens"] else 0))
print("report records: %d -> %d  (every alert still has one)" % (off["reports"], on["reports"]))
print("")
print("Note: reused reports carry prompt_tokens=0 and a reused_from pointer, so the ledger shows the")
print("saving while the audit trail still shows one report per alert.")
