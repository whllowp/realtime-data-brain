"""Final comparison: the stream with every round's optimisation off versus on.

Both sides are measured with the current (calibrated) estimator, so the comparison is like-for-like. The
pre-round-1 absolute was additionally ~30% too low because the estimator itself was wrong; that correction
is reported separately rather than folded into the saving, since a corrected measurement is not an
optimisation.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus
from brian.config import load_settings
from brian.demo import DEFAULT_SEQUENCE, generate
from brian.pipeline import Pipeline


def run(max_events, reuse):
    base = load_settings()
    settings = base.with_overrides(
        gate=type(base.gate)(**{**vars(base.gate), "reuse_report_within_s": reuse}),
        llm=type(base.llm)(**{**vars(base.llm), "max_context_events": max_events}),
    )
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
    tokens = sum((r.prompt_tokens + r.completion_tokens) if r.prompt_tokens else r.estimated_prompt_tokens
                 for r in reports)
    reused = sum(1 for r in reports if getattr(r, "reused_from", None))
    return {"reports": len(reports), "tokens": tokens, "reused": reused,
            "per_report": tokens / len(reports) if reports else 0}


off = run(20, 0)
on = run(8, 300)

print("windows 35, alerts with a report: %d" % off["reports"])
print("")
print("%-34s %10s %12s %10s" % ("configuration", "tokens", "per report", "reused"))
print("-" * 70)
print("%-34s %10d %12.1f %10d" % ("all optimisations off (20 ev, no reuse)", off["tokens"],
                                  off["per_report"], off["reused"]))
print("%-34s %10d %12.1f %10d" % ("all on (8 ev, reuse 300 s)", on["tokens"],
                                  on["per_report"], on["reused"]))
saved = off["tokens"] - on["tokens"]
print("")
print("saving %d tokens = %.1f%%" % (saved, 100 * saved / off["tokens"] if off["tokens"] else 0))
print("report records %d -> %d (every alert still explained)" % (off["reports"], on["reports"]))
