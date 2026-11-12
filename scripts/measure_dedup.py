"""Is the same incident being analysed twice?

`Alert.dedup_key` is `service|category`, so an incident spanning several windows produces one alert per
window with the same key. Each of those alerts currently pays for a full System 2 report, and the second one
is usually describing the same thing as the first.

This measures the repetition before any policy is written:

    alerts                    how many alerts the stream produced
    distinct dedup keys       how many incidents they represent
    repeat alerts             alerts whose key was already seen
    repeat tokens             tokens spent on those repeats - the recoverable share

A repeat is only "the same incident" if it happens inside a window of time; the same service failing twice
a day apart is two incidents. The gap distribution is printed so the cooldown can be chosen from data.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, generate  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402


def main() -> int:
    settings = load_settings()
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    pipeline = Pipeline(settings, bus=MemoryBus())
    traces = []
    for window in windows:
        for event in window.events:
            pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)
    traces.extend(pipeline.drain_ready(final=True))

    alerts = [t for t in traces if t.alert is not None and t.report is not None]
    if not alerts:
        print("no alerts with reports")
        return 2

    seen_at = {}
    gaps = defaultdict(list)
    repeat_tokens = 0
    repeat_count = 0
    total_tokens = 0
    for trace in alerts:
        key = trace.alert.dedup_key
        tokens = (trace.report.prompt_tokens + trace.report.completion_tokens
                  + trace.report.estimated_prompt_tokens
                  if trace.report.prompt_tokens == 0
                  else trace.report.prompt_tokens + trace.report.completion_tokens)
        total_tokens += tokens
        if key in seen_at:
            repeat_count += 1
            repeat_tokens += tokens
            gaps[key].append((trace.digest.window_start - seen_at[key]).total_seconds())
        seen_at[key] = trace.digest.window_start

    keys = {t.alert.dedup_key for t in alerts}
    print("alerts                 %d" % len(alerts))
    print("distinct dedup keys    %d" % len(keys))
    print("repeat alerts          %d  (%.0f%%)" % (repeat_count, 100 * repeat_count / len(alerts)))
    print("total tokens           %d" % total_tokens)
    print("repeat tokens          %d  (%.0f%% of the bill)"
          % (repeat_tokens, 100 * repeat_tokens / total_tokens if total_tokens else 0))
    print("")
    print("gaps between repeats of the same key (seconds):")
    all_gaps = sorted(g for values in gaps.values() for g in values)
    if all_gaps:
        print("  min %.0f  median %.0f  max %.0f" % (all_gaps[0], all_gaps[len(all_gaps) // 2],
                                                     all_gaps[-1]))
    else:
        print("  none")
    print("")
    print("per key:")
    for key in sorted(keys):
        count = sum(1 for t in alerts if t.alert.dedup_key == key)
        print("  %-40s %d alert(s)" % (key, count))

    print("")
    if repeat_tokens:
        print("Recoverable: %.0f%% of the analysis bill is spent on alerts whose incident was already"
              % (100 * repeat_tokens / total_tokens))
        print("analysed inside the same stream. Reusing the previous report while the key is unchanged")
        print("would keep the report count and cut the bill; it would NOT cut detection cost, which is 0.")
    else:
        print("No repeats: every alert has a distinct dedup key, so reuse has nothing to recover here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
