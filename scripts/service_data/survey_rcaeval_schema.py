"""Survey the column vocabulary across all RE1-OB cases.

The first two cases already disagreed: a `cpu` case carries `*_load` and `*_latency`, while a `delay` case
carries `*_latency-50` and `*_latency-90` and no load column at all. An adapter written against one case
would fail on the other, so the vocabulary is measured before anything is built — including which *fault
families* provide which golden signal.
"""
from __future__ import annotations

import collections
import pathlib

import pyarrow.parquet as pq

DATA = pathlib.Path("data/rcaeval")
cases = sorted(p for p in DATA.iterdir() if p.is_dir() and p.name.startswith("re1ob_"))
print("cases: %d" % len(cases))

suffixes = collections.Counter()
by_family = collections.defaultdict(collections.Counter)
lengths = collections.Counter()

for case in cases:
    parquet = case / "metrics.parquet"
    if not parquet.is_file():
        continue
    schema = pq.read_schema(parquet)
    names = [name for name in schema.names if name != "time"]
    for name in names:
        if "_" in name:
            suffix = name.rsplit("_", 1)[-1]
            suffixes[suffix] += 1
    family = case.name.split("_")[1] if len(case.name.split("_")) > 1 else "?"
    for golden in ("load", "latency", "latency-50", "latency-90", "error", "cpu", "mem"):
        if any(name.endswith("_" + golden) for name in names):
            by_family[family][golden] += 1
    lengths[(len(names),)] += 1

print("\ncolumn suffixes seen (count of cases containing it):")
for suffix, count in suffixes.most_common(20):
    print("  %-14s %3d" % (suffix, count))

print("\ngolden signals available, by fault family (cases containing it / total in family):")
families = sorted(by_family)
totals = collections.Counter()
for case in cases:
    family = case.name.split("_")[1] if len(case.name.split("_")) > 1 else "?"
    totals[family] += 1
header = "%-10s %6s" % ("family", "cases") + "".join("%12s" % g for g in
                                                    ("load", "latency", "lat-50", "lat-90", "error", "cpu", "mem"))
print(header)
for family in families:
    row = "%-10s %6d" % (family, totals[family])
    for golden in ("load", "latency", "latency-50", "latency-90", "error", "cpu", "mem"):
        row += "%12s" % ("%d" % by_family[family][golden] if by_family[family][golden] else "-")
    print(row)

print("\ncolumn-count distribution: %s" % dict(sorted(lengths.items())[:6]))
