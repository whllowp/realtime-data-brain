"""Closing the loop: reading human outcomes back as labels.

`Outcome` is the only record type that carries a *human* judgement of whether the machine was right;
everything else is the system's opinion of itself. That makes it the source of truth for three things,
and this module is the single place they all read from:

* **Calibration.** `brian calibrate --from-outcomes` fits the confidence correction on real labelled
  traffic instead of the synthetic scenario suite.
* **Evaluation.** The same labels score the detector on production windows, which is the only way to
  find out whether the offline benchmark meant anything.
* **Training.** `blast_radius` and `needs_human` have no ground truth in the scenario suite; a real
  operator outcome is where their labels come from, if anyone recorded them.

The join is deliberately explicit and narrow
-------------------------------------------
An outcome names an `alert_id` and a `decision_id`. A label for *calibration* additionally needs the
raw probability the model produced for that window, which lives on the `Decision` — not on the
outcome. So `LabelSource` reads both topics and joins them on `decision_id`, and it refuses to
synthesise a label when the join fails (an outcome whose decision was never stored, or a decision
whose raw probability is absent because it predates calibration being recorded). A silently dropped
join would shrink the sample without anyone noticing, which is worse than a loud gap.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from .bus import Bus
from .calibration import CalibrationFit, fit_calibration
from .config import Settings
from .contracts import Decision, Outcome, OutcomeResult, loads
from .topics import Topic


@dataclass
class OutcomeLabel:
    """One usable label: the human verdict plus the model output it judges."""

    alert_id: str
    decision_id: str
    service: str
    is_anomaly: bool
    #: The model's raw P(anomaly) for this window — what a calibration is fitted against.
    raw_probability: Optional[float] = None
    predicted_anomaly: Optional[bool] = None
    predicted_category: Optional[str] = None
    result: Optional[OutcomeResult] = None
    summary: str = ""
    recorded_at: Optional[datetime] = None
    #: Whether a human recorded which of the RCA's hypotheses turned out to be right.
    confirmed_hypothesis_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "decision_id": self.decision_id,
            "service": self.service,
            "is_anomaly": self.is_anomaly,
            "raw_probability": self.raw_probability,
            "predicted_anomaly": self.predicted_anomaly,
            "predicted_category": self.predicted_category,
            "result": self.result.value if self.result else None,
            "summary": self.summary,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "confirmed_hypothesis_index": self.confirmed_hypothesis_index,
        }


@dataclass
class LabelSource:
    """Reads outcomes + decisions + alerts from the memory and joins them into labels."""

    settings: Settings
    episodes: List[OutcomeLabel] = field(default_factory=list)
    #: Outcomes that implied no label (`unresolved`, `unknown`) and so were skipped.
    unlabelled_outcomes: int = 0
    #: Outcomes whose decision could not be found, or which carried no raw probability.
    broken_joins: List[Dict[str, str]] = field(default_factory=list)
    #: Outcomes with no alert record (should not happen; recorded so it is visible if it does).
    orphan_outcomes: int = 0

    def load(self, bus: Bus, limit: int = 100000) -> "LabelSource":
        decisions: Dict[str, Decision] = {}
        raw_by_decision: Dict[str, Optional[float]] = {}
        for record in bus.replay(self.settings.topic(Topic.DECISIONS), limit=limit):
            try:
                decision = loads(record.value, Decision)
            except Exception:  # noqa: BLE001 - a bad record must not block the label load
                continue
            decisions[decision.decision_id] = decision
            # The raw probability is what a calibration is fitted on. Newer decisions record it in
            # `answers.is_anomaly.noul` (the model's own output); the calibrated value is separate.
            raw: Optional[float] = None
            answer = (decision.answers or {}).get("is_anomaly") or {}
            if isinstance(answer, Mapping) and answer.get("noul") is not None:
                try:
                    raw = float(answer["noul"])
                except (TypeError, ValueError):
                    raw = None
            if raw is None:
                raw = decision.anomaly_probability
            raw_by_decision[decision.decision_id] = raw

        alerts: Dict[str, Dict[str, Any]] = {}
        for record in bus.replay(self.settings.topic(Topic.ALERTS), limit=limit):
            value = record.value or {}
            if value.get("alert_id"):
                alerts[value["alert_id"]] = value

        for record in bus.replay(self.settings.topic(Topic.OUTCOMES), limit=limit):
            try:
                outcome = loads(record.value, Outcome)
            except Exception as exc:  # noqa: BLE001
                self.broken_joins.append({"outcome": str(record.record_id), "error": str(exc)})
                continue
            label = outcome.anomaly_label
            if label is None:
                # `unresolved` / `unknown`: no claim about whether the window was anomalous, so it is
                # not a label. Counting it as either class would inject noise into every fit.
                self.unlabelled_outcomes += 1
                continue
            decision = decisions.get(outcome.decision_id)
            if decision is None:
                self.broken_joins.append(
                    {"outcome": outcome.outcome_id, "error": "decision %s not found" % outcome.decision_id}
                )
                continue
            if outcome.alert_id not in alerts:
                self.orphan_outcomes += 1
            self.episodes.append(
                OutcomeLabel(
                    alert_id=outcome.alert_id,
                    decision_id=outcome.decision_id,
                    service=outcome.service,
                    is_anomaly=bool(label),
                    raw_probability=raw_by_decision.get(outcome.decision_id),
                    predicted_anomaly=decision.is_anomaly,
                    predicted_category=decision.category.value if decision.category else None,
                    result=outcome.result,
                    summary=outcome.summary,
                    recorded_at=outcome.recorded_at,
                    confirmed_hypothesis_index=outcome.confirmed_hypothesis_index,
                )
            )
        return self

    # -- what the labels say about the detector ---------------------------------
    @property
    def n(self) -> int:
        return len(self.episodes)

    @property
    def usable_for_calibration(self) -> List[OutcomeLabel]:
        """Labels that can actually be fitted on: a raw probability is required."""
        return [label for label in self.episodes if label.raw_probability is not None]

    def confusion(self) -> Dict[str, int]:
        """The production confusion matrix — the number the synthetic benchmark cannot give you."""
        matrix = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for label in self.episodes:
            if label.predicted_anomaly is None:
                continue
            if label.is_anomaly and label.predicted_anomaly:
                matrix["tp"] += 1
            elif label.is_anomaly and not label.predicted_anomaly:
                matrix["fn"] += 1
            elif not label.is_anomaly and label.predicted_anomaly:
                matrix["fp"] += 1
            else:
                matrix["tn"] += 1
        return matrix

    def fit(self) -> CalibrationFit:
        """Fit a calibration on real outcomes. Raises ValueError when there is too little data."""
        usable = self.usable_for_calibration
        if len(usable) < 20:
            raise ValueError(
                "only %d outcomes carry both a label and the model's raw probability; at least 20 are "
                "needed before a fit means anything. Keep closing alerts (the `outcomes` topic is the "
                "source) or fall back to `brian calibrate` on the synthetic suite."
                % len(usable)
            )
        return fit_calibration(
            [label.raw_probability for label in usable],  # type: ignore[arg-type]
            [label.is_anomaly for label in usable],
            source="outcomes (%d labelled episodes%s)"
            % (len(usable), ", %d unlabelled skipped" % self.unlabelled_outcomes
               if self.unlabelled_outcomes else ""),
        )

    def summary(self) -> Dict[str, Any]:
        matrix = self.confusion()
        positives = matrix["tp"] + matrix["fn"]
        negatives = matrix["fp"] + matrix["tn"]
        false_alarms = sum(1 for label in self.episodes if label.result is OutcomeResult.FALSE_ALARM)
        by_result: Dict[str, int] = {}
        for label in self.episodes:
            key = label.result.value if label.result else "unknown"
            by_result[key] = by_result.get(key, 0) + 1
        return {
            "labelled_episodes": self.n,
            "usable_for_calibration": len(self.usable_for_calibration),
            "unlabelled_outcomes_skipped": self.unlabelled_outcomes,
            "broken_joins": len(self.broken_joins),
            "orphan_outcomes": self.orphan_outcomes,
            "by_result": by_result,
            "confusion": matrix,
            "recall": round(matrix["tp"] / positives, 4) if positives else None,
            "false_positive_rate": round(matrix["fp"] / negatives, 4) if negatives else None,
            "precision": (
                round(matrix["tp"] / (matrix["tp"] + matrix["fp"]), 4)
                if (matrix["tp"] + matrix["fp"]) else None
            ),
            "human_confirmed_false_alarms": false_alarms,
            "hypotheses_confirmed": sum(
                1 for label in self.episodes if label.confirmed_hypothesis_index is not None
            ),
            "evidence": self.broken_joins[:5],
        }


def format_summary(summary: Mapping[str, Any]) -> str:
    """Render the production-versus-synthetic picture. Reported separately, never merged."""
    lines = [
        "=" * 78,
        "Outcome labels — the detector judged by humans, not by a benchmark",
        "=" * 78,
        "labelled episodes          %s" % summary["labelled_episodes"],
        "usable for calibration     %s (needs a stored raw probability)" % summary["usable_for_calibration"],
        "unlabelled outcomes        %s (unresolved/unknown: no claim either way, not counted)"
        % summary["unlabelled_outcomes_skipped"],
        "broken joins               %s (an outcome whose decision is missing)" % summary["broken_joins"],
        "orphan outcomes            %s (no alert record found)" % summary["orphan_outcomes"],
        "",
        "production confusion       tp=%d fp=%d tn=%d fn=%d" % (
            summary["confusion"]["tp"], summary["confusion"]["fp"],
            summary["confusion"]["tn"], summary["confusion"]["fn"]),
        "recall                     %s" % _fmt(summary["recall"]),
        "false positive rate        %s" % _fmt(summary["false_positive_rate"]),
        "precision                  %s" % _fmt(summary["precision"]),
        "human-confirmed false alarms %s" % summary["human_confirmed_false_alarms"],
        "outcomes confirming a hypothesis %s" % summary["hypotheses_confirmed"],
        "",
        "by result: %s" % json.dumps(summary["by_result"]),
    ]
    if summary["labelled_episodes"] == 0:
        lines.extend(
            [
                "",
                "No labels yet. This is expected on a fresh deployment and is the cold-start problem:",
                "the detector cannot produce the alerts that would generate the labels that would teach",
                "it. Two ways out, in order of preference:",
                "  1. run in observe mode and close every alert — even a false alarm is a labelled",
                "     example, and `brian outcome --result false_alarm` is how it is recorded;",
                "  2. bootstrap on the synthetic suite (`brian calibrate`) and use those outcomes to",
                "     replace it once real ones accumulate.",
            ]
        )
    if summary["broken_joins"]:
        lines.append("")
        lines.append("Broken joins (an outcome that cannot be used, and why):")
        for entry in summary["evidence"]:
            lines.append("  %s" % json.dumps(entry))
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "n/a" if value is None else "%.4f" % float(value)


__all__ = ["OutcomeLabel", "LabelSource", "format_summary"]
