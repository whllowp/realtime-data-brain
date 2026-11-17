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
from collections import deque
from typing import Any, Deque, Dict, Mapping, Optional, Sequence, Tuple

from .config import Settings
from .contracts import Digest, Routing
from .laya.client import LayaClient, LayaResult

#: Which judge answers a digest. Any other value is a configuration error, not a silent fallback:
#: a typo that quietly restored the model would make the measurement describe the wrong system.
#: `"auto"` picks by profile — the reader for `host`, the model for `service` — because the two
#: domains are not the same text problem: on service-shaped state the model separates incidents from
#: normal windows (0.875 recall / 0.000 false positives on the synthetic suite), and on host-shaped
#: state it does not (round 12: five renderings of the same window, two question wordings, every
#: cell between 0.17 and 0.67 AUC with the direction flipping between the two SMD hosts).
SCORING_MODES: Tuple[str, ...] = ("model", "deviation", "auto")

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


#: How much history the rolling reader needs before it will speak. Below this it falls back to the
#: level mapping, which is worse but at least is not a rank computed from four samples.
ROLLING_MIN_SAMPLES = 10


class RollingWindow:
    """A per-service trailing history of reader scores, so the alert bar can be a rank.

    The deviation reader's level drifts inside a single recording: on `smd-machine-1-6` the median
    score moves 0.846 -> 0.434 -> 0.768 across the four quarters of one file while the fault/normal
    separation in the mean is about 0.03, so any fixed cut is a bet on that drift (measured in
    `docs/RSI_LOOP.zh.md` 3.20). A rank inside a trailing window needs only the ordering, which is what
    survives: "top 10% of the last 100" scored 0.261 recall at 0.016 false positives on that host,
    against 0.128 at 0.095 for the fixed cut — and it is causal, so those numbers are out-of-sample.

    Emitting a rank keeps the rest of the pipeline unchanged: the gate still compares one number
    against `anomaly_probability`, except that `0.90` now means "louder than 90% of the recent past"
    instead of "deviation above 1.75x".
    """

    def __init__(self, size: int, min_samples: int = ROLLING_MIN_SAMPLES) -> None:
        if size < 1:
            raise ValueError("rolling window size must be positive, got %r" % (size,))
        self.size = size
        self.min_samples = max(1, min(int(min_samples), size))
        self._history: Dict[str, Deque[float]] = {}

    def rank(self, service: str, score: float) -> Optional[float]:
        """Share of the current window at or below `score`, or `None` before there is history.

        The current score joins the window before counting, so a window's loudest value scores 1.0
        and the gate's `anomaly_probability=0.90` pages on roughly the top tenth of windows.
        """
        trail = self._history.setdefault(service, deque(maxlen=self.size))
        trail.append(score)
        if len(trail) < self.min_samples:
            return None
        return sum(1 for value in trail if value <= score) / len(trail)


def deviation_probability(digest: Digest, window: Optional[RollingWindow] = None) -> Optional[float]:
    """Map the deviation onto (0, 1), monotonically, so the gate's thresholds still mean something.

    `dev / (1 + dev)` is strictly increasing, so any threshold-free metric (AUC, recall at a
    false-positive budget) is identical to the one computed on the raw deviation. It is not a
    calibrated probability and this module never claims it is: what it buys is that a 1.5x
    deviation lands at 0.33, which is where the shipped `anomaly_probability` bar sits.

    With a `window`, the level is replaced by the rank inside that window — the drift-robust form.
    """
    return _reading(digest, window)[0]


def _reading(digest: Digest,
             window: Optional[RollingWindow]) -> Tuple[Optional[float], str]:
    """The number the gate will compare, and whether it is a rank or a level."""
    score = deviation_score(digest)
    if score is None:
        return None, "none"
    if window is not None:
        rank = window.rank(digest.service, score)
        if rank is not None:
            return rank, "rank"
    return score / (1.0 + score), "level"


def deviation_result(digest: Digest, window: Optional[RollingWindow] = None) -> Optional[LayaResult]:
    """A `LayaResult` shaped like the engine's, so the gate cannot tell who answered."""
    probability, reported = _reading(digest, window)
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
            # "level" until the window has enough history: a rank of one sample is not a rank, and
            # reporting it as one would hide that the reader is still warming up.
            "reported_as": reported,
            "window": None if window is None else window.size,
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


def judge(digest: Digest, settings: Settings, laya: LayaClient,
          window: Optional[RollingWindow] = None) -> LayaResult:
    """The single place that decides who judges a digest.

    Falls back to the model when the deterministic reader has nothing to read (the first windows of a
    recording have no baseline yet), because "no deviation to measure" is not the same finding as
    "no incident" and must not be silently reported as a normal verdict.

    `scoring="auto"` resolves against the digest profile rather than the payload, so one deployment
    can serve two domains whose text this engine reads very differently.

    `window` is the caller's per-service history (`Pipeline` and `Evaluator` each own one when
    `digest.rolling_window` is set). It is passed in rather than held here because module-level state
    would make one evaluation leak into the next one's numbers.
    """
    mode = settings.digest.scoring
    if mode not in SCORING_MODES:
        raise ValueError("unknown digest scoring %r (pick from %s)"
                         % (mode, ", ".join(SCORING_MODES)))
    if mode == "auto":
        mode = "deviation" if settings.digest.profile == "host" else "model"
    if mode == "deviation":
        result = deviation_result(digest, window)
        if result is not None:
            return result
    return laya.decide(digest)
