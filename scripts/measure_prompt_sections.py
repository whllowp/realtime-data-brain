"""Where does the per-report bill actually sit?

Round 5's category attribution came back flat: 3,807-4,072 tokens per report across five categories, 0.9-1.0x
the median. So the bill is not driven by the incident — it is driven by the prompt's fixed structure. That
makes the useful question "which *section* costs what", and it is answerable by building the same messages
with sections removed.

    full                everything the model gets today
    without metrics     drop the machine-readable block (the JSON dump)
    without events      drop the raw event list
    without both        classifier verdict + window summary only (the irreducible floor)
    instructions only   the system/user instructions with no per-alert content at all

The floor is what any policy has to pay per alert, and it bounds how much any per-alert optimisation can win.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus
from brian.config import load_settings
from brian.demo import DEFAULT_SEQUENCE, flatten, generate
from brian.llm.prompts import build_rca_messages, prompt_token_estimate
from brian.pipeline import Pipeline

settings = load_settings()
windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
pipeline = Pipeline(settings, bus=MemoryBus())
traces = []
for event in flatten(windows):
    pipeline.ingest(event)
    for trace in pipeline.drain_ready():
        traces.append(trace)
traces.extend(pipeline.drain_ready(final=True))

pairs = [(t.digest, t.decision, list(t.history or [])) for t in traces
         if t.report is not None and t.decision is not None]
if not pairs:
    print("no alerts")
    raise SystemExit(2)

def mean_tokens(build) -> float:
    return sum(prompt_token_estimate(build(d, dec, e)) for d, dec, e in pairs) / len(pairs)

full = mean_tokens(lambda d, dec, e: build_rca_messages(d, dec, events=e, max_events=20))
few = mean_tokens(lambda d, dec, e: build_rca_messages(d, dec, events=e, max_events=8))
no_events = mean_tokens(lambda d, dec, e: build_rca_messages(d, dec, events=None, max_events=0))

# The instructions alone: strip the per-alert content by building with an empty-ish digest is not
# possible without fabricating one, so the floor is reported as "no events" and instruction size is
# measured directly from the message text of one alert.
sample = build_rca_messages(*pairs[0])
instruction_chars = sum(len(m.get("content", "")) for m in sample if m.get("role") == "system")

print("alerts: %d" % len(pairs))
print("")
print("%-34s %12s %s" % ("section", "tokens/alert", "note"))
print("-" * 70)
print("%-34s %12.1f %s" % ("full (20 events)", full, "what ships today"))
print("%-34s %12.1f %s" % ("8 events (round-4 policy)", few,
                           "%+.1f%% vs full" % (100 * (few - full) / full)))
print("%-34s %12.1f %s" % ("no raw events", no_events,
                           "%+.1f%% vs full" % (100 * (no_events - full) / full)))
print("%-34s %12.1f %s" % ("system instruction text", instruction_chars / 4.0,
                           "%d chars / 4 as a rough token count" % instruction_chars))
print("")
print("event block       %8.1f tokens (%.0f%% of full)" % (full - no_events, 100 * (full - no_events) / full))
print("everything else   %8.1f tokens (%.0f%% of full)"
      % (no_events, 100 * no_events / full))
print("")
print("The 'everything else' line is the floor per alert: verdict, window summary, and the JSON metric")
print("block. Any per-alert saving has to come out of it, and no event-selection policy can touch it.")
