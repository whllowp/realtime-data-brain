"""Does an RCAEval RE1-OB case actually contain a detectable, labelled change?

Before building an adapter, answer the questions that decide whether any of this is measurable:

  * **cadence** — one row per second? That sets the smallest honest window;
  * **how long before and after the injection** the recording runs, because that decides how many
    labelled windows exist and whether there is a clean pre-fault baseline per case;
  * **which columns move** when the fault is injected. A dataset where nothing visibly changes at
    `inject_time` cannot test a detector, however many columns it has.

The last one is reported as a per-column ratio of post-injection to pre-injection medians, for the golden
signals the catalogue cares about (`_load`, `_latency`, `_error`) as well as cpu/mem. A fault like
`adservice_cpu` should show up in `adservice_cpu`; if the moving signal is something the catalogue cannot
name, that is a coverage finding rather than a detector result.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "rcaeval"


def summarise(case: Path) -> None:
    parquet = case / "metrics.parquet"
    inject_file = case / "inject_time.txt"
    if not parquet.is_file() or not inject_file.is_file():
        print("  %-40s missing files" % case.name)
        return
    inject = int(inject_file.read_text(encoding="utf-8").strip())
    frame = pd.read_parquet(parquet)
    times = frame["time"].astype("int64")
    start, end = int(times.min()), int(times.max())
    cadence = sorted(set(int(b - a) for a, b in zip(times.head(50), times.iloc[1:51])))
    before = frame[times < inject]
    after = frame[times >= inject]

    print("  %-34s rows=%-5d span=%5ds  pre=%5ds post=%5ds  cadence=%s"
          % (case.name, len(frame), end - start, inject - start, end - inject, cadence[:3]))

    interesting = [c for c in frame.columns if c != "time"]
    moves = []
    for column in interesting:
        pre = before[column].dropna()
        post = after[column].dropna()
        if len(pre) < 5 or len(post) < 5:
            continue
        pre_med = float(pre.median())
        post_med = float(post.median())
        if pre_med == 0:
            ratio = float("inf") if post_med > 0 else 1.0
        else:
            ratio = post_med / pre_med
        moves.append((column, ratio, pre_med, post_med))
    moves.sort(key=lambda item: abs(item[1] - 1.0), reverse=True)
    for column, ratio, pre_med, post_med in moves[:6]:
        print("      %-34s post/pre = %8.2fx   (%.4g -> %.4g)" % (column, ratio, pre_med, post_med))


def main() -> int:
    if not DATA.is_dir():
        print("no data under %s" % DATA)
        return 2
    cases = sorted(p for p in DATA.iterdir() if p.is_dir() and p.name.startswith("re1ob_"))
    if not cases:
        print("no re1ob cases yet")
        return 2
    print("cases on disk: %d" % len(cases))
    print("")
    for case in cases[:6]:
        summarise(case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
