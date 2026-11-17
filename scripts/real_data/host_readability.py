"""Can this engine read host-shaped state at all? Four renderings of the same window.

The question this answers came out of four rounds of failed repairs on the SMD host profile:

* round 8 fixed printed levels that had rounded to `0.00` (a real defect, AUC 0.523 -> 0.538);
* round 9 removed ratios computed over a near-zero baseline (no help);
* round 10 showed the ceiling is a *text* ceiling, so the renderer is not withholding anything;
* round 11 showed the engine's answer barely moves when the ratios are flattened or amplified.

So the remaining possibility is the shape of the text itself. This script renders the same window
four ways and asks the same question of each:

    full      `CPU 0.11 (baseline 0.20, 0.56x).`     the shipped digest
    values    `CPU 0.11.`                            the level without the comparison
    ratios    `CPU 0.56x.`                           the comparison without the level
    words     `Cpu is 44% below its normal level.`   the comparison stated in prose
    header    the first line only                    a control with no numbers in it

`header` is the control worth reading first: if it lands at chance and every other rendering lands
within noise of it, then nothing in this text family gives the engine a ranking, and the answer is
not a better digest but a different scorer for this kind of state.

Samples are balanced and drawn by loudest printed deviation, so these are the *strongest* cases in
each recording — a readability probe, not an estimate of deployment prevalence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.calibration import auc  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.evolve import apply_overrides  # noqa: E402
from brian.laya import LayaClient  # noqa: E402
from brian.scoring import text_deviation  # noqa: E402
from brian.state import (  # noqa: E402
    BaselineTracker, Windower, baseline_keys, build_digest, digest_specs,
)
from brian.streams import flatten_streams, load_stream_file  # noqa: E402

LINE = re.compile(r"^(?P<label>[^()]+?) (?P<value>-?\d+(?:\.\d+)?)"
                  r" \(baseline (?P<baseline>-?\d+(?:\.\d+)?), (?P<ratio>-?\d+(?:\.\d+)?)x\)\.$")
WINDOW = re.compile(r"a (?P<seconds>\d+)-second window with \d+ samples\.")
WANTED = ("CPU", "load 1m", "free memory")
VARIANTS = ("full", "values", "ratios", "words", "header")
QUESTIONS = {
    # "catalog" is what the pipeline actually sends; `LayaClient.decide(text)` uses it.
    "catalog": None,
    # "bench" is the wording `scripts/real_data/bench_smd.py` used for its best archived runs.
    "bench": {"is_anomaly": {"type": "noul", "instructions":
              "Do the measurements in this state show a real anomaly that needs attention, for this "
              "host, given the reference values stated? Answer yes if any measurement departs "
              "clearly from its own baseline."}},
}


def renditions(text: str) -> Dict[str, str]:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return {name: text or "" for name in VARIANTS}
    header = lines[0].strip()
    sentences: List[str] = [WINDOW.sub("the last \\g<seconds> seconds.", header)]
    full, values, ratios = [header], [header], [header]
    for line in lines[1:]:
        match = LINE.match(line)
        if match is None:                       # "No baseline is available yet" and friends
            full.append(line)
            continue
        full.append(line)
        label = match.group("label").strip()
        values.append("%s %s." % (label, match.group("value")))
        ratios.append("%s %sx." % (label, match.group("ratio")))
        if label not in WANTED:
            continue
        change = round((float(match.group("ratio")) - 1.0) * 100.0)
        if abs(change) < 5:
            sentences.append("%s is about normal." % label.capitalize())
        else:
            sentences.append("%s is %d%% %s its normal level."
                             % (label.capitalize(), abs(change),
                                "above" if change > 0 else "below"))
    return {"full": "\n".join(full), "values": "\n".join(values), "ratios": "\n".join(ratios),
            "words": " ".join(sentences), "header": header}


def replay(settings: Any, stream: Any) -> List[Tuple[str, bool]]:
    """Every rendered digest of one recording, paired with its label."""
    windower = Windower(
        size_s=settings.window.size_s,
        slide_s=settings.window.slide_s,
        allowed_lateness_s=settings.window.allowed_lateness_s,
        min_events=settings.window.min_events,
        specs=digest_specs(settings.digest.profile, settings.digest.host_features),
    )
    baselines = BaselineTracker(
        window=settings.window.baseline_windows,
        long_window=settings.window.baseline_windows_long,
        keys=baseline_keys(settings.digest.profile, settings.digest.host_features),
    )
    labels = {(window.service, int(window.window_start.timestamp())):
              bool((window.expected or {}).get("is_anomaly")) for window in stream.windows}
    out: List[Tuple[str, bool]] = []

    def consume(aggregators: Sequence[Any]) -> None:
        for aggregator in aggregators:
            digest = build_digest(
                aggregator, baselines, profile=settings.digest.profile,
                precision=settings.digest.host_precision, ratio_floor=settings.digest.host_ratio_floor,
            )
            baselines.observe(digest.service, digest.metrics)
            key = (digest.service, int(digest.window_start.timestamp()))
            if key in labels:
                out.append((digest.interpretation, labels[key]))

    for event in flatten_streams([stream]):
        windower.add(event)
        consume(windower.ready())
    consume(windower.flush())
    return out


def sample(pool: Sequence[Tuple[str, bool]], limit: int) -> List[Tuple[str, bool]]:
    """`limit` fault windows and `limit` normal ones, loudest printed deviation first."""
    faults = sorted((item for item in pool if item[1]),
                    key=lambda item: -(text_deviation(item[0]) or 0.0))[:limit]
    normals = sorted((item for item in pool if not item[1]),
                     key=lambda item: -(text_deviation(item[0]) or 0.0))[:limit]
    return list(faults) + list(normals)


def measure(client: LayaClient, rows: Sequence[Tuple[str, bool]], questions: Any) -> Dict[str, Any]:
    scores, truth = [], []
    for text, label in rows:
        result = client.decide(text, questions, use_cache=False)
        scores.append(float(result.answers["is_anomaly"]["noul"]))
        truth.append(bool(label))
    positives = sum(truth)
    return {
        "auc": auc(scores, truth),
        "distinct": len(set(scores)),
        "mean_fault": sum(s for s, t in zip(scores, truth) if t) / max(1, positives),
        "mean_normal": sum(s for s, t in zip(scores, truth) if not t) / max(1, len(truth) - positives),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", default="data/smd/stream.json")
    parser.add_argument("--samples", type=int, default=100, help="fault windows (and as many normal)")
    parser.add_argument("--questions", default="catalog,bench")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    stream_set = load_stream_file(REPO_ROOT / args.stream)
    settings = apply_overrides(load_settings(), stream_set.overrides)
    client = LayaClient(settings.laya)
    wanted = [name.strip() for name in args.questions.split(",") if name.strip()]
    report: Dict[str, Any] = {}
    for stream in stream_set.streams:
        pool = replay(settings, stream)
        rows = sample(pool, args.samples)
        labels = [label for _text, label in rows]
        block: Dict[str, Any] = {"windows": len(rows), "positives": sum(labels), "questions": {}}
        print("=" * 78)
        print("%s  %d windows (%d fault)  window=%ds features=%s"
              % (stream.name, block["windows"], block["positives"], settings.window.size_s,
                 settings.digest.host_features or "all"))
        for question in wanted:
            texts_by_variant = {name: [renditions(text)[name] for text, _label in rows]
                                for name in VARIANTS}
            table: Dict[str, Any] = {}
            print("  question=%-8s %-8s %-7s %-11s %-11s"
                  % (question, "variant", "auc", "mean fault", "mean normal"))
            for name in VARIANTS:
                result = measure(client, list(zip(texts_by_variant[name], labels)),
                                 QUESTIONS[question])
                table[name] = result
                print("  %-17s %-8s %8.4f %11.4f %11.4f  (%d distinct)"
                      % ("", name, result["auc"] or 0.0, result["mean_fault"],
                         result["mean_normal"], result["distinct"]))
            table["words_sample"] = texts_by_variant["words"][0]
            block["questions"][question] = table
        report[stream.name] = block
    if args.json_out:
        path = REPO_ROOT / args.json_out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
