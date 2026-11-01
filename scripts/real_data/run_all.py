"""Reproduce the whole external validation with one command.

Why a runner rather than a list of commands in a document
---------------------------------------------------------
The validation is six steps that only make sense in order, and two of them (the control detector and the
operating curve) are what stop the headline number from being self-congratulatory. A documented sequence
gets skipped; a runner that prints the comparison at the end does not.

    fetch     download SMD machine data (needs network, ~8 MB per machine)
    control   the one-line threshold baseline on the same windows
    bench     the system, through its own rendering path
    curve     operating points and what calibration can and cannot change

Everything is written under `data/smd/`, which is gitignored.

Usage:
    python scripts/real_data/run_all.py                 # machine-1-6, 600 s windows
    python scripts/real_data/run_all.py --machine machine-2-2
    python scripts/real_data/run_all.py --skip-fetch --skip-bench    # re-analyse existing runs
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


def run(label: str, command: list) -> int:
    print("")
    print("=" * 78)
    print("  %s" % label)
    print("=" * 78)
    completed = subprocess.run(command, cwd=str(REPO_ROOT))
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", default="machine-1-6")
    parser.add_argument("--features", default="cpu_r,load_1,mem_free")
    parser.add_argument("--window-s", type=int, default=600)
    parser.add_argument("--long-windows", type=int, default=480)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--tag", default="runall")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-bench", action="store_true")
    args = parser.parse_args()

    data = REPO_ROOT / "data" / "smd" / ("%s.data.txt" % args.machine)
    if not args.skip_fetch and not data.is_file():
        code = run("1/4  fetch dataset", [PY, "scripts/real_data/fetch_smd.py",
                                          "--machine", args.machine])
        if code != 0:
            print("fetch failed; see above")
            return code
    else:
        print("1/4  fetch dataset — skipped (%s %s)"
              % (data.name, "present" if data.is_file() else "missing"))

    run("2/4  control: the one-line threshold this must beat",
        [PY, "scripts/real_data/control_detector.py", "--machine", args.machine,
         "--window-s", str(args.window_s)])

    bench_out = REPO_ROOT / "data" / "smd" / ("bench_product_%s.json" % args.tag)
    if not args.skip_bench:
        code = run("3/4  the system, through its own rendering path",
                   [PY, "scripts/real_data/bench_smd.py", "--machine", args.machine,
                    "--digest", "product", "--features", args.features,
                    "--window-s", str(args.window_s), "--long-windows", str(args.long_windows),
                    "--stride", str(args.stride), "--tag", args.tag])
        if code != 0:
            print("bench failed; is the decision service running on 127.0.0.1:8077?")
            return code
    else:
        print("3/4  bench — skipped")

    if bench_out.is_file():
        run("4/4  operating points and calibration",
            [PY, "scripts/real_data/operating_curve.py", str(bench_out)])

        print("")
        print("=" * 78)
        print("  VERDICT")
        print("=" * 78)
        result = json.loads(bench_out.read_text(encoding="utf-8"))
        print("system   AUC %.4f   recall %.4f at a 10%% false-positive budget   "
              "(%.0f engine calls/s)"
              % (result["auc"], result["recall_at_fpr"], result["engine_calls_per_s"]))
        print("")
        print("Compare the line above with the control reported in step 2. If the threshold wins,")
        print("that is the honest result for this machine, not a defect to be tuned away.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
