"""Round 10: how much of the label survives into the text the engine actually reads?

`state_ceiling` has always scored the structured `MetricValue` tree. The engine never sees that tree —
it sees `digest.interpretation`, a sentence. So the honest ceiling for "what the engine could have
read" is the one measured on the text, and this script prints both side by side on the SMD stream.

No Laya call: this is a property of the renderer.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.config import load_settings  # noqa: E402
from brian.evolve import apply_overrides, state_ceiling  # noqa: E402
from brian.streams import load_stream_file  # noqa: E402

PATH = REPO_ROOT / "data" / "smd" / "stream.json"


def main() -> int:
    stream_set = load_stream_file(PATH)
    base = apply_overrides(load_settings(), stream_set.overrides)
    print("recordings %d, windows %d, events %d"
          % (len(stream_set.streams),
             sum(len(s.windows) for s in stream_set.streams),
             sum(len(s.events()) for s in stream_set.streams)))

    configs = {"all 8 features": "", "3 features": "cpu_r,load_1,mem_free"}
    rows = []
    for label, features in configs.items():
        settings = dataclasses.replace(base.digest, profile="host", host_features=features)
        settings = base.with_overrides(digest=settings)
        ceiling = state_ceiling(settings, stream_set.streams)
        for name in sorted(ceiling["by_stream"]):
            rows.append((label, name, ceiling["by_stream"][name], ceiling["text_by_stream"][name]))
    print("%-14s %-16s %8s %8s" % ("config", "recording", "fields", "text"))
    for label, name, fields, text in rows:
        print("%-14s %-16s %8s %8s"
              % (label, name,
                 "n/a" if fields is None else "%.3f" % fields,
                 "n/a" if text is None else "%.3f" % text))
    (REPO_ROOT / "var" / "ceilings.json").write_text(
        json.dumps([{"config": c, "recording": n, "fields": f, "text": t}
                    for c, n, f, t in rows], indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
