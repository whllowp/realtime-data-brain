"""Fetch a REAL ops dataset (SMD / ServerMachineDataset) for independent validation.

Why this file is at the repository root and not under `tools/`
--------------------------------------------------------------
The sandbox grants write to the workspace root but not to its subdirectories (only the root carries
an access entry for the current user), so a validation script has to live at the root to be writable.
It is a one-off probe, not part of the package; delete it after use if you want a clean tree.

What it fetches
---------------
SMD is the dataset from the KDD 2019 OmniAnomaly paper: 28 servers, 38 metrics each (CPU, memory,
disk, network), sampled every minute. Two label sets ship with it:

  test/machine-X-Y.txt              the 38-dim multivariate series
  test_label/machine-X-Y.txt        one 0/1 label per row (point-level anomaly)
  interpretation_label/machine-X-Y.txt   which dimensions are responsible, as `start-end:dim,...`

The repository hosts the data in-tree, so it is fetched over HTTP. It is downloaded at the
*workspace root* for the permission reason above.

Selection: pick the machine with the most labelled anomaly points, so the evaluation has something
to measure. The anomaly *fraction* is printed for every machine so the choice is auditable rather
than arbitrary.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/NetManAIOps/OmniAnomaly/master/ServerMachineDataset"

#: Repository root: this script lives at `scripts/real_data/`, so it is two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the downloaded series land. Large (a single machine is ~8 MB), so `data/` is gitignored.
OUT = REPO_ROOT / "data" / "smd"
MACHINES = [f"machine-{g}-{i}" for g in (1, 2, 3) for i in range(1, 12) if not (g == 1 and i == 9)]


def fetch(relative: str, timeout: float = 90.0) -> bytes:
    url = "%s/%s" % (BASE, relative)
    request = urllib.request.Request(url, headers={"User-Agent": "brian-validation/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="",
                        help="fetch this machine's series (default: the one with most labelled anomalies)")
    parser.add_argument("--labels-only", action="store_true", help="skip the series download")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    print("fetching %d label files..." % len(MACHINES))

    stats = []
    failures = []
    for name in MACHINES:
        try:
            raw = fetch("test_label/%s.txt" % name)
        except Exception as exc:  # noqa: BLE001 - one missing machine must not stop the sweep
            failures.append("%s: %s" % (name, exc))
            continue
        (OUT / ("%s.label.txt" % name)).write_bytes(raw)
        text = raw.decode("utf-8", errors="replace")
        labels = [int(tok) for tok in text.replace(",", " ").split() if tok in ("0", "1")]
        if not labels:
            continue
        positives = sum(labels)
        stats.append({"machine": name, "rows": len(labels), "positive": positives,
                      "fraction": round(positives / len(labels), 4)})

    if not stats:
        print("no labels fetched; failures: %s" % failures[:3])
        return 2

    stats.sort(key=lambda row: (row["positive"], row["fraction"]), reverse=True)
    print("\n%-14s %8s %9s %10s" % ("machine", "rows", "anomalous", "fraction"))
    print("-" * 46)
    for row in stats:
        print("%-14s %8d %9d %10.4f" % (row["machine"], row["rows"], row["positive"], row["fraction"]))

    # Highest anomaly count, but skip a series that is almost entirely anomalous: it would score well
    # by predicting "always anomalous", which measures nothing.
    candidates = [row for row in stats if row["fraction"] < 0.5] or stats
    if args.machine:
        name = args.machine
        chosen = next((row for row in stats if row["machine"] == name),
                      {"machine": name, "positive": 0, "rows": 0, "fraction": 0.0})
        print("\nrequested: %s" % name)
    else:
        chosen = candidates[0]
        name = chosen["machine"]
    print("\nchosen: %s (%d anomalous rows of %d, %.2f%%)"
          % (name, chosen["positive"], chosen["rows"], chosen["fraction"] * 100))

    for kind, suffix in (("test", "data"), ("interpretation_label", "interpret")):
        if args.labels_only and kind == "test":
            continue
        try:
            payload = fetch("%s/%s.txt" % (kind, name))
        except Exception as exc:  # noqa: BLE001
            print("WARNING: %s/%s failed: %s" % (kind, name, exc))
            continue
        path = OUT / ("%s.%s.txt" % (name, suffix))
        path.write_bytes(payload)
        print("wrote %s (%.1f KB)" % (path, len(payload) / 1024))

    (OUT / "manifest.json").write_text(
        json.dumps({"base": BASE, "machines": stats, "chosen": name,
                    "failures": failures, "positive": chosen["positive"], "rows": chosen["rows"]},
                   indent=2),
        encoding="utf-8",
    )
    print("\nwrote %s/manifest.json" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
