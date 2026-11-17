"""Who judges a digest: the model, or a deterministic reader of the same text.

This module exists because of one measurement. On SMD, a threshold on the loudest deviation the
host renderer prints reaches AUC **0.827** on `machine-1-6` and **0.597** on `machine-2-2`, while
the engine reading the same text reaches 0.674 and 0.347 (`brian evolve` prints both numbers every
round as its state ceiling). A rule with no parameters is not supposed to beat an encoder, so the
useful question is not "is the model good" but "is the model paying for itself on this domain".

`scoring="deviation"` answers that question with a switch:

* the score is a pure function of the digest, so the reading is reproducible from the record;
* it costs no GPU and no tokens, so it cannot be too expensive per window;
* it is judged by the same labelled data as everything else — the loop can accept or reject it on
  recall at a false-positive budget, exactly as it judges a cost change.

What it is **not**: a replacement for the classifier. It answers "is there an incident" and has no
opinion on category, severity or blast radius, so the gate routes it conservatively and System 2
still writes the narrative. Default stays `"model"`: a deterministic detector that has only been
measured on two SMD hosts must not become the production default by accident.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence, Tuple

from .config import Settings
from .contracts import Digest, Routing
from .laya.client import LayaClient, LayaResult

#: Which judge answers a digest. Any other value is a configuration error, not a silent fallback:
#: a typo that quietly restored the model would make the measurement describe the wrong system.
SCORING_MODES: Tuple[str, str] = ("model", "deviation")

#: The deviation at which the reading crosses the shipped `anomaly_probability` bar (0.33 for the
#: SMD stream). Not a tuned constant — it is the arithmetic of `dev / (1 + dev)` at dev = 0.5.
CROSSOVER_DEVIATION = 0.5


def _entries(comparison: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The flat list of rendered comparison entries, whatever nesting the payload uses."""
    values = []
    for entry in comparison.values():
        if not isinstance(entry, Mapping):
            continue
        if "value" in entry:
            values.append(entry)
        else:
            values.extend(_entries(entry))
    return values


def deviation_score(digest: Digest) -> Optional[float]:
    """The loudest ratio deviation the renderer would print, or `None` if it prints none.

    Prefers the long horizon because the host renderer prefers it (`_render_host_digest`): a reader
    that peaked at a number the text never shows would be measuring a different system.
    """
    comparison = digest.metrics.get("comparison") if isinstance(digest.metrics, Mapping) else None
    if not isinstance(comparison, Mapping):
        return None
    deviations = []
    for entry in _entries(comparison):
        value = entry.get("long_delta_ratio")
        if value is None:
            value = entry.get("delta_ratio")
        if value is None:
            continue
        try:
            deviations.append(abs(float(value) - 1.0))
        except (TypeError, ValueError):
            continue
    return max(deviations) if deviations else None


def deviation_probability(digest: Digest) -> Optional[float]:
    """Map the deviation onto (0, 1), monotonically, so the gate's thresholds still mean something.

    `dev / (1 + dev)` is strictly increasing, so any threshold-free metric (AUC, recall at a
    false-positive budget) is identical to the one computed on the raw deviation. It is not a
    calibrated probability and this module never claims it is: what it buys is that a 1.5x
    deviation lands at 0.33, which is where the shipped `anomaly_probability` bar sits.
    """
    score = deviation_score(digest)
    if score is None:
        return None
    return score / (1.0 + score)


def deviation_result(digest: Digest) -> Optional[LayaResult]:
    """A `LayaResult` shaped like the engine's, so the gate cannot tell who answered."""
    probability = deviation_probability(digest)
    if probability is None:
        return None
    score = deviation_score(digest)
    return LayaResult(
        answers={"is_anomaly": {"type": "noul", "noul": round(probability, 4)}},
        routing=Routing(model="deviation-reader",
                        reason="deterministic digest scoring (digest.scoring=deviation)",
                        repo=None),
        latency_ms=0.0,
        raw={
            "scoring": "deviation",
            "deviation": score,
            "crossover": CROSSOVER_DEVIATION,
            # No category, severity or blast radius is claimed: those questions were never asked,
            # and inventing answers here would put a fabricated category into the incident record.
            "answers_absent": ["category", "severity", "needs_human", "blast_radius"],
        },
    )


#: A rendered ratio, e.g. `1.07x` or `3.60x`. The host renderer prints them inside parentheses.
_RATIO = re.compile(r"(-?\d+(?:\.\d+)?)x\b")


def text_deviation(text: str) -> Optional[float]:
    """The loudest deviation a reader of the rendered *text* can see.

    `deviation_score` reads the structured `MetricValue` fields, so it knows numbers the rendering may
    have dropped, rounded or omitted (a ratio suppressed by `host_ratio_floor`, a level printed as
    `0.00`). The engine only ever sees the text. When those two readers disagree, the difference is an
    information loss that belongs to the renderer, not to the model — so the ceiling reported for "what
    the state can support" has to be measured twice, once on each side of that boundary.

    Returns `None` when the text carries no ratio: a window with no baseline yet renders a sentence
    ("No baseline is available yet"), and scoring it as a perfect zero would be inventing evidence.
    """
    values = [abs(float(match) - 1.0) for match in _RATIO.findall(text or "")]
    return max(values) if values else None


def judge(digest: Digest, settings: Settings, laya: LayaClient) -> LayaResult:
    """The single place that decides who judges a digest.

    Falls back to the model when the deterministic reader has nothing to read (the first windows of a
    recording have no baseline yet), because "no deviation to measure" is not the same finding as
    "no incident" and must not be silently reported as a normal verdict.
    """
    mode = settings.digest.scoring
    if mode not in SCORING_MODES:
        raise ValueError("unknown digest scoring %r (pick from %s)"
                         % (mode, ", ".join(SCORING_MODES)))
    if mode == "deviation":
        result = deviation_result(digest)
        if result is not None:
            return result
    return laya.decide(digest)
