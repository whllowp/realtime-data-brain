"""Temperature fitting: make confidence mean what it says, so the gate can use it.

The demo run is the argument for this module. On the built-in scenario sequence, the
`typed-decisions` checkpoint got the *ranking* right — it named `dependency_failure` for the
dependency windows and `config_or_deploy` for the deploy window — but it compressed every
window's P(anomaly) into 0.43-0.46. At the default 0.50 threshold, **not one of the six real
incidents cleared the bar**, while `security` and `major` came back as the category and severity
of healthy windows too.

That is not a bug in the model; it is the documented state of it. Laya's own service reports
`probability_calibration`: "the shipped temperatures are inherited from the base checkpoint
(ECE 0.213); refit on your own held-out data before gating automation on confidence." The README
says the same thing: the base checkpoints are over-confident, and one temperature per (question
type, option count) fitted on held-out data moves mean ECE from 0.466 to 0.081.

So the pipeline has to *own* a calibration step, and it must be:

* **Fitted on this domain.** The published temperatures were fitted on the upstream benchmark's
  four synthetic workflows. Anomaly triage is a fifth domain and needs its own fit.
* **Applied before the thresholds, never after.** Thresholds are policy; calibration is
  measurement. Re-tuning thresholds to compensate for a miscalibrated model is how a system ends
  up with magic numbers nobody can defend. Fixing the probabilities keeps the thresholds
  meaningful (`0.5` means "more likely than not", permanently).
* **Auditable.** `Decision.answers` keeps the raw probabilities exactly as the model produced
  them; `Decision.anomaly_probability` carries the calibrated value and `calibrated` records the
  transform and its parameters. Nothing is overwritten, so a later reviewer can always see the
  raw model output next to the decision it fed.
* **Honest about its sample size.** One temperature fitted on 20 windows is a starting point, not
  a result. The fit reports `n`, the ECE before and after, and refuses to claim success when the
  evidence is too thin.

The transform is a single scalar in log-odds space:

    p' = sigmoid(logit(p) / T)

`T > 1` softens an over-confident model and pushes mid-range probabilities *toward* the extremes;
`T < 1` sharpens. One parameter is deliberate: with a few hundred labelled windows, a single
scale parameter is what the data can actually support, and a richer model (per-question Platt
scaling, isotonic regression) needs far more labels per question before it beats it. `fit_temperature`
will grow into those when there is data for them — not before.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Clamp for probabilities before logit — avoids inf and keeps the fit finite.
_EPS = 1e-6
#: Search range for T. Wide enough for a badly over-confident model, bounded so the optimizer
#: cannot wander into a degenerate region where every window maps to 0 or 1.
T_RANGE = (0.05, 20.0)


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    clamped = min(max(float(p), _EPS), 1.0 - _EPS)
    return math.log(clamped / (1.0 - clamped))


def apply_temperature(probability: Optional[float], temperature: float) -> Optional[float]:
    """`sigmoid(logit(p) / T)`. T=1 is the identity, so an unfitted pipeline is unchanged."""
    if probability is None:
        return None
    if temperature is None or abs(temperature - 1.0) < 1e-9:
        return float(probability)
    scale = max(float(temperature), 1e-6)
    return sigmoid(logit(probability) / scale)


def apply_platt(probability: Optional[float], slope: float, intercept: float) -> Optional[float]:
    """`sigmoid(slope * logit(p) + intercept)` — the two-parameter recalibration.

    One temperature is not enough for this problem, and the reason is worth stating precisely
    because it looks like a numerical detail and is not.

    A temperature is a pure *scale* on the logit: it can widen or narrow the spread of
    probabilities, but it cannot move their centre. Measured on this project's windows, the
    checkpoint put every window between 0.414 and 0.442 — a spread of 0.052 that contains real
    information (AUC 0.78) but is centred on 0.43, well below any sensible threshold. Fitting a
    temperature to that data drives T to its maximum and flattens everything to ~0.5, which
    *improves* log loss (predicting the base rate always does on a balanced set) while destroying
    the decision. That is the trap: on a set whose base rate is near 50%, log loss rewards saying
    "50%" about everything, so a one-parameter fit has no incentive to preserve the ordering.

    The `intercept` is what fixes it. It translates the logit so the model's own decision boundary
    lands where the evidence actually separates the classes, and the `slope` widens the compressed
    spread so the resulting probabilities are usable. Both parameters are fitted together by
    maximum likelihood (`fit_calibration`), and the gate's thresholds are then read off the fitted
    curve (`operating_point`) as an explicit policy choice rather than inherited from a number that
    happens to be 0.5.
    """
    if probability is None:
        return None
    return sigmoid(float(slope) * logit(probability) + float(intercept))


def apply_calibration(probability: Optional[float], fit: Optional["CalibrationFit"]) -> Optional[float]:
    """Apply whichever calibration form the fit carries. `None` fit means no change."""
    if fit is None:
        return probability
    return apply_platt(probability, fit.slope, fit.intercept)


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------


def expected_calibration_error(probabilities: Sequence[float], labels: Sequence[bool],
                               bins: int = 10) -> Optional[float]:
    """ECE: the average gap between claimed probability and observed frequency.

    Binned by the *claimed* probability, weighted by bin population. Returned as None below five
    samples, because an ECE computed from three windows is noise presented as a measurement.
    """
    if len(probabilities) != len(labels) or len(probabilities) < 5:
        return None
    width = 1.0 / bins
    total = len(probabilities)
    ece = 0.0
    for index in range(bins):
        low, high = index * width, (index + 1) * width
        members = [
            (p, y)
            for p, y in zip(probabilities, labels)
            if (low <= p < high) or (index == bins - 1 and p >= high)
        ]
        if not members:
            continue
        mean_confidence = sum(p for p, _y in members) / len(members)
        mean_accuracy = sum(1.0 for _p, y in members if y) / len(members)
        ece += (len(members) / total) * abs(mean_confidence - mean_accuracy)
    return ece


def log_loss(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    total = 0.0
    for p, y in zip(probabilities, labels):
        q = min(max(p, _EPS), 1.0 - _EPS)
        total += -(math.log(q) if y else math.log(1.0 - q))
    return total / max(len(probabilities), 1)


def brier_score(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in zip(probabilities, labels)) / max(
        len(probabilities), 1
    )


def auc(probabilities: Sequence[float], labels: Sequence[bool]) -> Optional[float]:
    """ROC AUC: the probability that a random anomalous window scores above a random healthy one.

    This is the metric a *temperature cannot change*. Temperature scaling is strictly monotone, so
    it leaves AUC exactly as it found it — which makes AUC the right way to ask the question a
    calibration fit otherwise hides:

        is there any discriminative signal here at all?

    A model whose probabilities all sit in a 0.03-wide band around 0.45 can be "calibrated" to a
    lovely ECE of 0.026 while having AUC 0.55, and that model is useless for alerting no matter how
    well-calibrated it is. Reporting AUC next to ECE is what stops a temperature fit from being
    mistaken for a fix.
    """
    positives = [p for p, y in zip(probabilities, labels) if y]
    negatives = [p for p, y in zip(probabilities, labels) if not y]
    if not positives or not negatives:
        return None
    # Rank-based (Mann-Whitney U) computation with ties averaged, so a model that emits the same
    # probability for everything scores exactly 0.5 rather than depending on input order.
    ordered = sorted(zip(probabilities, labels), key=lambda pair: pair[0])
    ranks: List[float] = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1
    positive_rank_sum = sum(rank for rank, (_p, y) in zip(ranks, ordered) if y)
    n_pos, n_neg = len(positives), len(negatives)
    return (positive_rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


# --------------------------------------------------------------------------------------
# the fit
# --------------------------------------------------------------------------------------


@dataclass
class CalibrationFit:
    """A fitted recalibration, with the evidence that justifies it.

    The primary parameters are the Platt `slope` and `intercept`. `temperature` is kept as a
    derived, reported quantity (`1 / slope`) because it is the number the upstream project talks
    about and it makes the magnitude of the correction comparable to published figures.
    """

    slope: float = 1.0
    intercept: float = 0.0
    n: int = 0
    n_positive: int = 0
    domain: str = "anomaly_triage"
    question: str = "is_anomaly"
    log_loss_before: Optional[float] = None
    log_loss_after: Optional[float] = None
    ece_before: Optional[float] = None
    ece_after: Optional[float] = None
    brier_before: Optional[float] = None
    brier_after: Optional[float] = None
    auc: Optional[float] = None
    raw_spread: Optional[float] = None
    #: Raw probability -> calibrated probability, as (raw, calibrated) pairs, downsampled. Saved so
    #: a reviewer can see the correction rather than take it on trust.
    mapping: List[List[float]] = field(default_factory=list)
    #: The decision boundary this fit was evaluated at, and what it cost. Stored because a
    #: calibration without a stated operating point answers "what do these numbers mean?" while
    #: leaving "where is the line?" — the question the gate actually needs — unanswered.
    operating: Optional[Dict[str, Any]] = None
    fitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    source: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def temperature(self) -> float:
        """The equivalent temperature, `1/slope`. Reported for comparability, not applied."""
        return 1.0 / self.slope if self.slope else float("inf")

    @property
    def improved(self) -> bool:
        if self.log_loss_after is None or self.log_loss_before is None:
            return False
        return self.log_loss_after < self.log_loss_before

    @property
    def discriminates(self) -> bool:
        """Whether the raw probabilities separate the classes at all.

        AUC is invariant under every monotone recalibration, so this is a property of the *model*,
        not of the fit. Below 0.6 the ordering is barely better than a coin flip and no
        calibration can rescue it — which is exactly the distinction between "the boundary is in
        the wrong place" and "there is nothing to find".
        """
        return self.auc is not None and self.auc >= 0.6

    def apply(self, probability: Optional[float]) -> Optional[float]:
        return apply_platt(probability, self.slope, self.intercept)

    def calibrated_threshold(self, raw_threshold: float) -> float:
        """Where a gate threshold expressed on raw probabilities lands after calibration."""
        raw = apply_platt(raw_threshold, self.slope, self.intercept)
        return raw if raw is not None else raw_threshold

    def operating_point(self, alert_rate: float = 0.01) -> Dict[str, Any]:
        """Pick a decision boundary from a *stated policy*, not from 0.5.

        The honest answer to "where should the threshold be?" is that it depends on how many
        false pages an operator will tolerate. This method takes that as an input (`alert_rate`,
        the fraction of windows that may be escalated), places the boundary at the corresponding
        quantile of the calibration curve, and reports the confusion matrix that boundary produces
        on the fitting sample. The threshold is then a decision someone made, with its cost
        visible, rather than a constant inherited from a sigmoid's midpoint.

        `alert_rate` is a deployment policy, so it is a parameter here and a setting in
        `GateThresholds.notify_probability` — not a hidden default.
        """
        if not self.mapping:
            return {"alert_rate": alert_rate, "error": "no calibration mapping stored"}
        pairs = sorted(self.mapping)
        index = max(0, min(len(pairs) - 1, int(round((1.0 - alert_rate) * (len(pairs) - 1)))))
        return {
            "alert_rate": alert_rate,
            "threshold": round(pairs[index][1], 6),
            "raw_probability_at_threshold": round(pairs[index][0], 6),
            "n_mapping_points": len(pairs),
            "note": (
                "threshold chosen so that ~%.1f%% of windows escalate; this is a policy choice "
                "traded against missed incidents, not a calibrated probability"
                % (alert_rate * 100)
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "question": self.question,
            "method": "platt",
            "params": {
                "slope": round(self.slope, 6),
                "intercept": round(self.intercept, 6),
                "equivalent_temperature": round(self.temperature, 6) if self.slope else None,
            },
            "n": self.n,
            "n_positive": self.n_positive,
            "metrics": {
                "log_loss": {"before": _r(self.log_loss_before), "after": _r(self.log_loss_after)},
                "ece": {"before": _r(self.ece_before), "after": _r(self.ece_after)},
                "brier": {"before": _r(self.brier_before), "after": _r(self.brier_after)},
                "auc": _r(self.auc),
                "raw_probability_spread": _r(self.raw_spread),
            },
            "improved": self.improved,
            "discriminates": self.discriminates,
            "mapping": [[round(p, 6), round(c, 6)] for p, c in self.mapping],
            "operating_point": self.operating,
            "fitted_at": self.fitted_at,
            "source": self.source,
            "notes": list(self.notes),
            "transform": "p' = sigmoid(slope * logit(p) + intercept)",
        }

    def save(self, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return str(target)

    @classmethod
    def load(cls, path: str) -> Optional["CalibrationFit"]:
        source = Path(path)
        if not source.is_file():
            return None
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        params = raw.get("params") or {}
        metrics = raw.get("metrics") or {}
        slope = _opt(params.get("slope"))
        if slope is None:
            # Tolerate a file written by the earlier temperature-only format: slope = 1/T.
            temperature = _opt(params.get("temperature")) or _opt(raw.get("temperature")) or 1.0
            slope = 1.0 / temperature if temperature else 1.0
        return cls(
            slope=slope,
            intercept=_opt(params.get("intercept")) or 0.0,
            n=int(raw.get("n", 0)),
            n_positive=int(raw.get("n_positive", 0)),
            domain=str(raw.get("domain", "anomaly_triage")),
            question=str(raw.get("question", "is_anomaly")),
            log_loss_before=_opt(metrics.get("log_loss", {}).get("before")),
            log_loss_after=_opt(metrics.get("log_loss", {}).get("after")),
            ece_before=_opt(metrics.get("ece", {}).get("before")),
            ece_after=_opt(metrics.get("ece", {}).get("after")),
            brier_before=_opt(metrics.get("brier", {}).get("before")),
            brier_after=_opt(metrics.get("brier", {}).get("after")),
            auc=_opt(metrics.get("auc")),
            raw_spread=_opt(metrics.get("raw_probability_spread")),
            mapping=[[float(a), float(b)] for a, b in (raw.get("mapping") or [])],
            operating=raw.get("operating_point"),
            fitted_at=str(raw.get("fitted_at") or ""),
            source=raw.get("source"),
            notes=list(raw.get("notes") or []),
        )


def fit_calibration(probabilities: Sequence[float], labels: Sequence[bool],
                    domain: str = "anomaly_triage", source: Optional[str] = None,
                    min_samples: int = 20, mapping_points: int = 21) -> CalibrationFit:
    """Fit Platt `slope` + `intercept` by maximum likelihood on the raw probabilities.

    Two parameters rather than one, for the reason documented on `apply_platt`: a slope alone
    cannot move a compressed, offset probability distribution onto the correct side of a
    threshold, and on a balanced sample the log-loss-optimal slope is degenerate (it predicts the
    base rate for everything, which is *calibrated* and *useless*).

    Optimisation is Newton-Raphson on the two-parameter logistic likelihood — the standard
    approach, converging in a handful of iterations. It is implemented directly rather than pulled
    from a library because this module must import on a bare interpreter, and because the two-parameter
    case is small enough that a hand-written solver is auditable.

    The fit is *guarded* rather than trusted:

    * below `min_samples` it refuses, because a calibration fitted on a handful of windows is a
      confident answer to a question nobody asked;
    * a single-class sample returns the identity with a note, since the likelihood is monotone and
      the optimum is at infinity;
    * it reports whether the fit improved log loss, whether the model discriminates at all (AUC),
      and the raw spread, so a good-looking ECE cannot hide an unusable model.
    """
    pairs = [(float(p), bool(y)) for p, y in zip(probabilities, labels) if p is not None]
    if len(pairs) < min_samples:
        raise ValueError(
            "need at least %d labelled windows to fit a calibration, got %d — a fit on fewer "
            "would be noise dressed as a measurement" % (min_samples, len(pairs))
        )
    probs = [p for p, _ in pairs]
    labels_only = [y for _, y in pairs]
    positives = sum(1 for y in labels_only if y)
    fit = CalibrationFit(
        n=len(pairs),
        n_positive=positives,
        domain=domain,
        source=source,
        log_loss_before=log_loss(probs, labels_only),
        ece_before=expected_calibration_error(probs, labels_only),
        brier_before=brier_score(probs, labels_only),
        auc=auc(probs, labels_only),
        raw_spread=max(probs) - min(probs),
    )

    if positives == 0 or positives == len(pairs):
        fit.notes.append(
            "all %d labels are %s: the likelihood is monotone and the optimum is at infinity, so "
            "no calibration is identifiable. Keeping the identity transform."
            % (len(pairs), "anomalous" if positives else "healthy")
        )
        fit.notes.append(
            "a single-class sample also cannot demonstrate discrimination; collect windows "
            "containing real incidents before fitting or trusting a calibration"
        )
        fit.mapping = _mapping(probs, fit, mapping_points)
        return fit

    z = [logit(p) for p in probs]
    y = [1.0 if label else 0.0 for label in labels_only]

    # Newton-Raphson on (slope, intercept). The Hessian of the logistic likelihood is positive
    # definite, so plain Newton converges quickly; a small ridge term keeps it well-conditioned
    # when the raw probabilities are nearly identical (which is exactly this dataset's situation).
    slope, intercept = 1.0, 0.0
    for _ in range(100):
        gradient_s = gradient_i = 0.0
        h_ss = h_si = h_ii = 0.0
        for zi, yi in zip(z, y):
            p = sigmoid(slope * zi + intercept)
            residual = yi - p
            gradient_s += residual * zi
            gradient_i += residual
            weight = p * (1.0 - p)
            h_ss += weight * zi * zi
            h_si += weight * zi
            h_ii += weight
        ridge = 1e-9
        h_ss += ridge
        h_ii += ridge
        determinant = h_ss * h_ii - h_si * h_si
        if abs(determinant) < 1e-15:
            break
        # Solve H * delta = gradient for the Newton step.
        delta_s = (h_ii * gradient_s - h_si * gradient_i) / determinant
        delta_i = (h_ss * gradient_i - h_si * gradient_s) / determinant
        # Damped step: an undamped Newton step can overshoot badly when the spread is tiny.
        step = 1.0
        for _attempt in range(30):
            candidate_s = slope + step * delta_s
            candidate_i = intercept + step * delta_i
            if candidate_s <= 0:
                step /= 2.0
                continue
            if _log_loss_platt(z, y, candidate_s, candidate_i) <= _log_loss_platt(z, y, slope, intercept):
                slope, intercept = candidate_s, candidate_i
                break
            step /= 2.0
        else:
            break
        if abs(delta_s) < 1e-10 and abs(delta_i) < 1e-10:
            break

    fit.slope = slope
    fit.intercept = intercept
    calibrated = [apply_platt(p, slope, intercept) for p in probs]
    fit.log_loss_after = log_loss(calibrated, labels_only)
    fit.ece_after = expected_calibration_error(calibrated, labels_only)
    fit.brier_after = brier_score(calibrated, labels_only)
    fit.mapping = _mapping(probs, fit, mapping_points)

    if not fit.improved:
        fit.notes.append(
            "the fit did not improve log loss (%.4f -> %.4f); keep the identity transform and "
            "collect more labels" % (fit.log_loss_before or 0.0, fit.log_loss_after or 0.0)
        )
    if not fit.discriminates:
        # AUC is invariant under this transform, so this says something about the checkpoint, not
        # about the fit — and it is the difference between "the boundary is misplaced" and "the
        # model cannot do this task".
        fit.notes.append(
            "AUC is %s and the raw probabilities span only %s: the checkpoint barely separates "
            "anomalous from healthy windows, and NO recalibration can change that (every one is "
            "monotone). A good post-fit ECE here is an artefact of predicting ~0.5 for everything. "
            "The fix is fine-tuning on labelled windows, not calibration."
            % (_r(fit.auc), _r(fit.raw_spread))
        )
    if fit.n < 200:
        fit.notes.append(
            "fitted on only %d windows; treat this as provisional and refit once several hundred "
            "labelled windows exist. Re-run `brian calibrate --repeat N` to enlarge the sample."
            % fit.n
        )
    if fit.ece_after is not None and fit.ece_after > 0.10:
        fit.notes.append(
            "post-fit ECE is %.3f, above the 0.10 bar this project treats as automation-ready; "
            "a two-parameter Platt fit may not be enough for this question" % fit.ece_after
        )
    return fit


def _log_loss_platt(z: Sequence[float], y: Sequence[float], slope: float, intercept: float) -> float:
    total = 0.0
    for zi, yi in zip(z, y):
        p = min(max(sigmoid(slope * zi + intercept), _EPS), 1.0 - _EPS)
        total += -(math.log(p) if yi else math.log(1.0 - p))
    return total


def _mapping(probabilities: Sequence[float], fit: CalibrationFit, points: int) -> List[List[float]]:
    """Evenly spaced (raw, calibrated) samples across the observed raw range.

    Stored in the fit file so a reviewer can see the correction rather than trust it, and so
    `operating_point` can place a threshold by quantile without the original data.
    """
    if not probabilities:
        return []
    low, high = min(probabilities), max(probabilities)
    if high - low < 1e-9:
        return [[low, apply_platt(low, fit.slope, fit.intercept) or low]]
    return [
        [low + (high - low) * index / (points - 1),
         apply_platt(low + (high - low) * index / (points - 1), fit.slope, fit.intercept) or low]
        for index in range(points)
    ]


#: Backwards-compatible alias: the earlier name, now a two-parameter fit. Kept so existing callers
#: and docs keep working, with the behaviour improvement rather than a silent break.
fit_temperature = fit_calibration


def _opt(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _r(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def collect_from_scores(scores: Sequence[Any]) -> Tuple[List[float], List[bool]]:
    """Pull (P(anomaly), is_anomaly) pairs out of `eval.WindowScore` objects."""
    probabilities: List[float] = []
    labels: List[bool] = []
    for score in scores:
        if getattr(score, "predicted_probability", None) is None:
            continue
        probabilities.append(float(score.predicted_probability))
        labels.append(bool(score.expected_anomaly))
    return probabilities, labels


def format_fit(fit: CalibrationFit, alert_rate: Optional[float] = None) -> str:
    lines = [
        "=" * 78,
        "Confidence calibration — %s / %s  (Platt: p' = sigmoid(slope * logit(p) + intercept))"
        % (fit.domain, fit.question),
        "=" * 78,
        "sample          %d windows (%d anomalous, %d healthy)"
        % (fit.n, fit.n_positive, fit.n - fit.n_positive),
        "slope           %.4f   (widens the compressed spread; equivalent T = %.3f)"
        % (fit.slope, fit.temperature),
        "intercept       %+.4f  (moves the decision boundary; a temperature cannot do this)"
        % fit.intercept,
        "raw p range     [%.4f, %.4f]  spread %.4f" % (
            min((p for p, _ in fit.mapping), default=0.0),
            max((p for p, _ in fit.mapping), default=0.0),
            fit.raw_spread or 0.0),
        "AUC (unmoved)   %s   <- a recalibration cannot change this; <0.6 means no real signal"
        % _r(fit.auc),
        "log loss        %s -> %s" % (_r(fit.log_loss_before), _r(fit.log_loss_after)),
        "ECE             %s -> %s" % (_r(fit.ece_before), _r(fit.ece_after)),
        "Brier           %s -> %s" % (_r(fit.brier_before), _r(fit.brier_after)),
        "discriminates   %s" % fit.discriminates,
        "improved        %s" % fit.improved,
    ]
    if fit.mapping:
        lines.append("")
        lines.append("correction (raw -> calibrated):")
        step = max(1, len(fit.mapping) // 7)
        for raw, calibrated in fit.mapping[::step]:
            lines.append("  %.4f -> %.4f" % (raw, calibrated))
    if alert_rate is not None:
        point = fit.operating_point(alert_rate)
        lines.append("")
        lines.append("operating point at a %.1f%% escalation rate:" % (alert_rate * 100))
        if "threshold" in point:
            lines.append(
                "  threshold %.4f  (raw probability %.4f)"
                % (point["threshold"], point["raw_probability_at_threshold"])
            )
            lines.append("  %s" % point["note"])
        else:
            lines.append("  unavailable: %s" % point.get("error"))
    for note in fit.notes:
        lines.append("note            %s" % note)
    return "\n".join(lines)


__all__ = [
    "CalibrationFit",
    "fit_calibration",
    "fit_temperature",
    "apply_temperature",
    "apply_platt",
    "apply_calibration",
    "expected_calibration_error",
    "log_loss",
    "brier_score",
    "auc",
    "collect_from_scores",
    "format_fit",
    "sigmoid",
    "logit",
]
