"""Round 11: what is the engine actually responding to on host state?

Round 10 closed the accounting question: the ceiling (0.827 / 0.597) is a *text* ceiling, so the engine
is leaving information on the table that its input demonstrably contains. This script asks the next
question, which no amount of configuration can answer: **which tokens is it reading?**

Two measurements, both cheap:

1. Correlation pass over every scored window: the answer against the loudest printed ratio, the length
   of the text, and the number of ratios printed. If the answer tracks text length better than it
   tracks the signal, it is reading the wrong feature of the same input.
2. A controlled probe on a balanced sample: the same window rendered four ways — verbatim, with the
   host renamed to the other recording, with every ratio flattened to `1.00x`, with every ratio
   amplified by 1.5 — so the effect of the name and of the magnitudes can be read off separately.

Every call uses `use_cache=False`; a cached answer would measure the cache.
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.eval import Evaluator  # noqa: E402
from brian.evolve import apply_overrides  # noqa: E402
from brian.laya import LayaClient  # noqa: E402
from brian.scoring import text_deviation  # noqa: E402
from brian.streams import load_stream_file  # noqa: E402

PATH = REPO_ROOT / "data" / "smd" / "stream.json"
RATIO = re.compile(r"(-?\d+(?:\.\d+)?)x\b")
RATIO_WITH_CONTEXT = re.compile(r"\(([^()]*?), (-?\d+(?:\.\d+)?)x\)")
SAMPLE = 20


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / den if den else None


def variants(text: str, other: str) -> dict:
    """Four renderings of the same window: what the engine is fed, changed one axis at a time."""
    renamed = re.sub(r"smd-machine-[0-9-]+", "smd-machine-" + other, text)
    flat = RATIO.sub("1.00x", text)
    def amplify(match: re.Match) -> str:
        value = float(match.group(1))
        return "%.2fx" % (1.0 + 1.5 * (value - 1.0))
    loud = RATIO.sub(amplify, text)
    return {"verbatim": text, "renamed": renamed, "flattened": flat, "amplified": loud}


def main() -> int:
    stream_set = load_stream_file(PATH)
    settings = apply_overrides(load_settings(), stream_set.overrides)
    names = {"smd-machine-1-6": "2-2", "smd-machine-2-2": "1-6"}
    report: dict = {}

    for stream in stream_set.streams:
        client = LayaClient(settings.laya)
        result = Evaluator(settings, laya=client).run(stream.windows)
        scores = [s for s in result.scores if s.predicted_probability is not None]
        truth = [bool(s.expected_anomaly) for s in scores]
        probabilities = [s.predicted_probability for s in scores]
        deviations = [text_deviation(s.digest_text) or 0.0 for s in scores]
        lengths = [float(len(s.digest_text)) for s in scores]
        ratios = [float(len(RATIO.findall(s.digest_text))) for s in scores]

        block = {
            "windows": len(scores),
            "positives": sum(truth),
            "auc_engine": auc(probabilities, truth),
            "auc_text_deviation": auc(deviations, truth),
            "pearson_p_vs_deviation": pearson(probabilities, deviations),
            "pearson_p_vs_length": pearson(probabilities, lengths),
            "pearson_p_vs_ratio_count": pearson(probabilities, ratios),
            "mean_chars": statistics.fmean(lengths) if lengths else None,
        }

        # balanced sample, extremes first so the probe compares real faults with real normals
        faults = sorted([s for s in scores if s.expected_anomaly],
                        key=lambda s: -(text_deviation(s.digest_text) or 0.0))[:SAMPLE]
        normals = sorted([s for s in scores if not s.expected_anomaly],
                         key=lambda s: -(text_deviation(s.digest_text) or 0.0))[:SAMPLE]
        probe: dict = {}
        for label, group in (("fault", faults), ("normal", normals)):
            means = {key: [] for key in ("verbatim", "renamed", "flattened", "amplified")}
            for score in group:
                for key, text in variants(score.digest_text, names[stream.name]).items():
                    answer = client.decide(text, {"is_anomaly": {"type": "noul", "instructions":
                                          "Is this host state anomalous?"}}, use_cache=False)
                    means[key].append(float(answer.answers["is_anomaly"]["noul"]))
            probe[label] = {key: statistics.fmean(values) for key, values in means.items()}
            probe[label]["n"] = len(group)
        block["probe"] = probe
        report[stream.name] = block
        print("=" * 78)
        print(stream.name)
        for key in ("windows", "positives", "auc_engine", "auc_text_deviation",
                    "pearson_p_vs_deviation", "pearson_p_vs_length",
                    "pearson_p_vs_ratio_count", "mean_chars"):
            print("  %-24s %s" % (key, block[key]))
        for label in ("fault", "normal"):
            row = probe[label]
            print("  %-6s n=%-3d  verbatim %.4f  renamed %.4f  flattened %.4f  amplified %.4f"
                  % (label, row["n"], row["verbatim"], row["renamed"],
                     row["flattened"], row["amplified"]))

    (REPO_ROOT / "var" / "engine_attention.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
