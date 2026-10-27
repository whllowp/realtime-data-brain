"""Throwaway experiment: can the digest *phrasing* create separation the base checkpoint lacks?

The 120-window evaluation puts the base `typed-decisions` checkpoint at AUC 0.506 with a raw
probability spread of 0.038 — no signal. Before concluding "fine-tuning is the only path", it is
worth checking whether the *question or the state rendering* is at fault, because those are levers
this project controls and both are cheap to change.

Four variants, same 120 labelled windows, same checkpoint, same everything else:

    A  the production digest text + the metric dict          (what ships)
    B  the production digest text only
    C  a blunt z-score ledger ("error fraction is 12.4x its baseline") and nothing else
    D  one `score` question instead of the `noul` gate       (is `noul` the wrong primitive?)

The comparison that matters is AUC, not accuracy: AUC is threshold-free, so it measures whether
the model *orders* the windows correctly rather than whether a particular threshold happens to
work. If no variant beats ~0.6, the finding is about the checkpoint, not the prompt.

    python scripts/experiment_digest_phrasing.py --repeat 4
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brian.calibration import auc  # noqa: E402
from brian.contracts import Event  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, LabeledWindow, generate  # noqa: E402
from brian.state import BaselineTracker, Windower, build_digest  # noqa: E402

URL = "http://127.0.0.1:8077"

NUOL = {
    "is_anomaly": {
        "type": "noul",
        "instructions": (
            "Does this telemetry show an anomaly that needs attention, rather than normal "
            "variation for this service?"
        ),
    }
}

SCORE = {
    "anomaly_level": {
        "type": "score",
        "instructions": "How abnormal is this telemetry window for this service?",
        "criteria": [
            "completely normal for this service",
            "slightly unusual but explainable",
            "clearly abnormal, worth investigating",
            "severely abnormal, an incident",
        ],
    }
}

#: Variant C: strip the prose, state only the comparison. If the model is reading for
#: "larger/smaller than baseline", a bare ledger is the cleanest way to say it.
LEDGER_QUESTION = {
    "is_anomaly": {
        "type": "noul",
        "instructions": "Is any measurement in this state clearly abnormal for this service?",
    }
}


def _ledger(digest: Any) -> str:
    lines = ["Measurements for %s, compared with the recent baseline:" % digest.service]
    for key, entry in (digest.metrics.get("comparison") or {}).items():
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        lines.append(
            "%s: %s (baseline %s, %sx, z=%s)"
            % (key, entry.get("value"), entry.get("baseline"), entry.get("delta_ratio"), entry.get("z"))
        )
    signature = digest.metrics.get("error_signature") or {}
    if signature.get("template"):
        lines.append("most frequent error: %s" % signature["template"])
    return "\n".join(lines)


def _post(path: str, body: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    request = urllib.request.Request(
        URL + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _decide(state: Any, questions: Dict[str, Any], model: str) -> Dict[str, Any]:
    return _post("/v1/decide", {"state": state, "questions": questions, "model": model})


def build_windows(repeat: int, warmup: int, service: str, seed: int) -> List[LabeledWindow]:
    windows: List[LabeledWindow] = []
    cursor = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    for index in range(max(1, repeat)):
        created = generate(
            sequence=list(DEFAULT_SEQUENCE),
            service=service,
            start=cursor,
            warmup_minutes=warmup if index == 0 else 0,
            seed=seed + index,
        )
        windows.extend(created)
        cursor = cursor + timedelta(minutes=len(created) + 1)
    return windows


def judge_window(events: Sequence[Event], baselines: BaselineTracker, specs: Any) -> Any:
    """Build a digest from one window's events, in isolation (the window starts at the events')."""
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
    for event in events:
        windower.add(event)
    digests = [build_digest(agg, baselines) for agg in windower.flush()]
    return digests[0] if digests else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--service", default="checkout-api")
    parser.add_argument("--seed", type=int, default=20260114)
    parser.add_argument("--models", default="typed-decisions,english")
    parser.add_argument("--out", default="var/experiment_digest_phrasing.json")
    args = parser.parse_args()

    windows = build_windows(args.repeat, args.warmup, args.service, args.seed)
    # Digests are built once and reused across every variant, so the only thing that changes
    # between measurements is the phrasing or the question — not the state.
    baselines = BaselineTracker(window=30)
    prepared: List[Tuple[LabeledWindow, Any]] = []
    for window in windows:
        digest = judge_window(window.events, baselines, None)
        if digest is not None:
            prepared.append((window, digest))
            baselines.observe(digest.service, digest.metrics)
    scored = [(w, d) for w, d in prepared if w.scored]
    print("windows: %d prepared, %d scored (%d anomalous)"
          % (len(prepared), len(scored), sum(1 for w, _ in scored if w.expected["is_anomaly"])))
    print()

    variants: List[Tuple[str, str, Dict[str, Any]]] = [
        ("A_digest_plus_metrics", "typed-decisions", NUOL),
        ("B_digest_only", "typed-decisions", NUOL),
        ("C_zscore_ledger", "typed-decisions", LEDGER_QUESTION),
        ("D_score_primitive", "typed-decisions", SCORE),
    ]

    results: List[Dict[str, Any]] = []
    for name, model, questions in variants:
        probabilities: List[float] = []
        labels: List[bool] = []
        spread_values: List[float] = []
        for window, digest in scored:
            if name == "B_digest_only":
                state: Any = digest.interpretation
            elif name == "C_zscore_ledger":
                state = _ledger(digest)
            else:
                state = {
                    "service": digest.service,
                    "summary": digest.interpretation,
                    "metrics": digest.metrics,
                }
            try:
                response = _decide(state, questions, model)
            except Exception as exc:  # noqa: BLE001
                print("  %s failed: %s" % (name, exc))
                probabilities = []
                break
            answers = response.get("answers") or {}
            if name == "D_score_primitive":
                score = (answers.get("anomaly_level") or {}).get("score")
                probability = None if score is None else min(max(float(score) / 3.0, 0.0), 1.0)
            else:
                probability = (answers.get("is_anomaly") or {}).get("noul")
            if probability is None:
                continue
            probabilities.append(float(probability))
            labels.append(bool(window.expected["is_anomaly"]))
            spread_values.append(float(probability))
        if not probabilities:
            continue
        window_auc = auc(probabilities, labels)
        row = {
            "variant": name,
            "model": model,
            "n": len(probabilities),
            "auc": None if window_auc is None else round(window_auc, 4),
            "p_min": round(min(spread_values), 4),
            "p_max": round(max(spread_values), 4),
            "p_spread": round(max(spread_values) - min(spread_values), 4),
            "mean_anomalous": round(
                sum(p for p, y in zip(probabilities, labels) if y) / max(sum(1 for y in labels if y), 1), 4
            ),
            "mean_healthy": round(
                sum(p for p, y in zip(probabilities, labels) if not y) / max(sum(1 for y in labels if not y), 1), 4
            ),
        }
        results.append(row)
        print("%-24s model=%-16s AUC=%s  spread=%.4f  mean(anomalous)=%.4f mean(healthy)=%.4f"
              % (name, model, row["auc"], row["p_spread"], row["mean_anomalous"], row["mean_healthy"]))

    print()
    best = max((r["auc"] or 0.0) for r in results) if results else 0.0
    print("best AUC across variants: %.4f" % best)
    if best < 0.6:
        print("=> no phrasing change produces separation; the limitation is the checkpoint, not the prompt.")
        print("   Fine-tuning on labelled windows is required (see docs/FINETUNE.md).")
    else:
        print("=> a phrasing variant does separate the classes; adopt it in engine/state.py.")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
