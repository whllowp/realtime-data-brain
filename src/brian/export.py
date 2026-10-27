"""Training-data export: turn the system's own records into fine-tuning material.

This module is where the measurement chain closes. The evaluation established that the base
`typed-decisions` checkpoint has *real but unusable* discrimination on this domain (AUC 0.775, every
probability compressed into 0.409-0.461), and that no recalibration can create separation. The only
thing that fixes it is fine-tuning on labelled windows — so the pipeline has to be able to *produce*
those labelled windows, in the exact shape the Laya trainer expects.

Where the labels come from
--------------------------
Two sources, and the distinction is carried through to the training file rather than being averaged
away:

1. **Human outcomes.** Every promoted alert that an operator closes is appended to the `outcomes`
   topic. That is the highest-quality label available and the reason the outcome step exists at all.
2. **The synthetic scenario suite** (`demo.EXPECTED`), which carries a hand-authored ground truth per
   scenario. Used to bootstrap before any real incidents have been reviewed — which is the normal
   situation on day one, since a detector with no labels cannot generate the alerts that would
   produce labels.

A third source is deliberately *not* used silently: an LLM teacher. It is the right way to scale
labelling, but a teacher label and a human label must never be confused, so the export records the
provenance of every answer in `label_mask` and `label_source`.

Why partial labels are kept rather than discarded
-------------------------------------------------
The five questions in the catalog do not all have ground truth in the synthetic suite: `is_anomaly`,
`category` and `severity` do; `blast_radius` and `needs_human` do not. Filling those two in with a
plausible guess would manufacture supervision for questions nobody answered — the model would learn
the *guess*, and the exported file would look complete and be quietly wrong. Instead each answer
carries an explicit provenance:

    "human"    a ground-truth label for this window
    "teacher"  an LLM's answer, to be corrected by review before it is trusted
    "derived"  computed from the metrics by a stated rule (see `derive_*`), not observed

and `label_mask` says which of them may be trained on. A trainer that respects the mask learns from
what is actually known. This is a deliberate choice to hand downstream tooling an *honest, partial*
dataset rather than a complete-looking fabricated one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .calibration import CalibrationFit
from .contracts import Digest, Severity
from .demo import LabeledWindow
from .laya.qcatalog import CATALOG_VERSION, DEFAULT_QUESTIONS, catalog_hash
from .state import BaselineTracker, Windower, build_digest

#: Provenance of an answer. Anything not `human` must not be presented as ground truth.
SOURCE_HUMAN = "human"
SOURCE_DERIVED = "derived"
SOURCE_TEACHER = "teacher"

#: Which catalog questions are answerable from the synthetic scenario ground truth, and which are
#: not. Declared explicitly so a reader can see the gap rather than infer it from what is absent.
GROUND_TRUTH_QUESTIONS = ("is_anomaly", "category", "severity")
#: Questions with no ground truth in the scenario suite. Emitted as `null` with a note.
UNLABELLED_QUESTIONS = ("blast_radius", "needs_human")


@dataclass
class TrainingExample:
    """One fine-tuning record: the state, the questions, the answers, and their provenance."""

    state: str
    answers: Dict[str, Any]
    window_start: datetime
    service: str
    scenario: str
    digest_id: str
    questions: Dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_QUESTIONS)))
    label_mask: Dict[str, bool] = field(default_factory=dict)
    label_source: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    catalog_version: str = CATALOG_VERSION
    digest_version: str = "digest/1"
    observed: Dict[str, Any] = field(default_factory=dict)

    @property
    def n_answers(self) -> int:
        return len(self.answers)

    @property
    def n_trainable(self) -> int:
        return sum(1 for value in self.label_mask.values() if value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "questions": self.questions,
            "answers": self.answers,
            "label_mask": self.label_mask,
            "label_source": self.label_source,
            "meta": {
                "service": self.service,
                "window_start": self.window_start.isoformat().replace("+00:00", "Z"),
                "scenario": self.scenario,
                "digest_id": self.digest_id,
                "catalog_version": self.catalog_version,
                "digest_version": self.digest_version,
                "catalog_hash": catalog_hash(self.questions),
                "questions_trainable": self.n_trainable,
                "questions_total": self.n_answers,
                "observed": self.observed,
                "notes": self.notes,
            },
        }


# --------------------------------------------------------------------------------------
# ground truth -> typed answers
# --------------------------------------------------------------------------------------


def _noul_answer(value: bool, probability: float = 0.95, confidence: Optional[float] = None) -> Dict[str, Any]:
    """Shape a gold `noul` answer the way the model emits one."""
    p_true = probability if value else 1.0 - probability
    return {
        "type": "noul",
        "noul": round(p_true, 4),
        "confidence": round(confidence if confidence is not None else p_true, 4),
    }


def _choice_answer(label: Optional[str], options: Sequence[str], probability: float = 0.9) -> Dict[str, Any]:
    if label is None:
        return {"type": "choice", "choice": None, "probabilities": {}, "confidence": 0.0}
    rest = (1.0 - probability) / max(len(options) - 1, 1)
    return {
        "type": "choice",
        "choice": label,
        "probabilities": {option: round(probability if option == label else rest, 4) for option in options},
        "confidence": round(probability, 4),
    }


def _score_answer(rank: int, levels: Sequence[str], probability: float = 0.9) -> Dict[str, Any]:
    """A gold `score` answer: the expected level, plus a distribution peaked on the true rank."""
    rest = (1.0 - probability) / max(len(levels) - 1, 1)
    probabilities = {
        str(index): round(probability if index == rank else rest, 4) for index in range(len(levels))
    }
    return {
        "type": "score",
        "score": float(rank),
        "legend": {str(index): label for index, label in enumerate(levels)},
        "probabilities": probabilities,
        "confidence": round(probability, 4),
    }


def answers_for_window(window: LabeledWindow, digest: Digest,
                       calibrator: Optional[CalibrationFit] = None,
                       human_source: str = SOURCE_DERIVED) -> Tuple[Dict[str, Any], Dict[str, bool], Dict[str, str], List[str]]:
    """Build the gold answers for one labelled window.

    Returns `(answers, label_mask, label_source, notes)`. The three questions with ground truth are
    filled in; the two without are emitted as explicit `null`s, excluded from the mask, and explained
    in `notes`. The anomaly probability is put through the same calibration the gate uses, so the
    target the model is trained toward is expressed on the scale it will be deployed at — training on
    `0.95` while the deployed threshold sits at `0.68` would teach a confidence the system never sees.
    """
    expected = window.expected
    is_anomaly = bool(expected.get("is_anomaly"))
    category = expected.get("category")
    severity_name = expected.get("severity")
    notes: List[str] = []

    category_options = list((DEFAULT_QUESTIONS["category"].get("criteria") or {}).keys())
    severity_levels = list(DEFAULT_QUESTIONS["severity"].get("criteria") or [])

    anomaly_probability = 0.92
    if calibrator is not None:
        calibrated = calibrator.apply(anomaly_probability)
        if calibrated is not None:
            anomaly_probability = calibrated
            notes.append(
                "anomaly probability target expressed on the calibrated scale "
                "(platt slope=%.2f intercept=%+.2f)" % (calibrator.slope, calibrator.intercept)
            )

    severity_rank = Severity.coerce(severity_name).rank if severity_name else None
    answers: Dict[str, Any] = {
        "is_anomaly": _noul_answer(is_anomaly, anomaly_probability),
        "category": _choice_answer(category, category_options),
        "severity": _score_answer(severity_rank, severity_levels) if severity_rank is not None
        else _score_answer(0, severity_levels, probability=0.25),
    }

    mask = {"is_anomaly": True, "category": True, "severity": True}
    source = {"is_anomaly": human_source, "category": human_source, "severity": human_source}

    for question_id in UNLABELLED_QUESTIONS:
        if question_id == "blast_radius":
            answers[question_id] = _choice_answer(
                None, list((DEFAULT_QUESTIONS[question_id].get("criteria") or {}).keys())
            )
        else:
            answers[question_id] = {"type": "noul", "noul": None, "confidence": None}
        # Excluded from training and flagged, rather than filled with a plausible guess: a guessed
        # label is indistinguishable from an observed one once it is in the file.
        mask[question_id] = False
        source[question_id] = "unlabelled"

    if category is None and is_anomaly:
        # A window flagged anomalous with no category ground truth: `none` would be a claim, so the
        # category answer is also excluded rather than asserted.
        mask["category"] = False
        source["category"] = "unlabelled"
        notes.append("anomalous window with no category ground truth; category excluded from training")

    if not is_anomaly:
        # Category and severity are meaningless for a healthy window. Training on "category=none" for
        # every quiet window would teach the model that the category question is a proxy for the
        # anomaly question, which is exactly the entanglement the catalog split was designed to avoid.
        mask["category"] = False
        mask["severity"] = False
        source["category"] = "not_applicable"
        source["severity"] = "not_applicable"
        notes.append("healthy window: category and severity excluded (question is not applicable)")

    notes.append(
        "scenarios carry ground truth for %s only; %s must be labelled by human review or a teacher "
        "before they are trainable (see docs/FINETUNE.zh.md)"
        % (", ".join(GROUND_TRUTH_QUESTIONS), ", ".join(UNLABELLED_QUESTIONS))
    )
    return answers, mask, source, notes


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------


def export_examples(windows: Sequence[LabeledWindow], calibrator: Optional[CalibrationFit] = None,
                    min_label_fraction: float = 0.0,
                    service: Optional[str] = None) -> List[TrainingExample]:
    """Build a training example per scored window.

    Digests are rebuilt here from the events through the identical windower and renderer the pipeline
    uses, so the `state` the trainer sees is byte-for-byte the text the deployed model will be given.
    Reconstructing it any other way — re-rendering from stored metrics, say — would risk training on a
    paraphrase of the real input.
    """
    baselines = BaselineTracker(window=30)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
    examples: List[TrainingExample] = []
    labels = {(w.service, int(w.window_start.timestamp())): w for w in windows}

    for window in windows:
        for event in window.events:
            windower.add(event)

    for aggregator in sorted(windower.flush(), key=lambda a: (a.window_start, a.service)):
        digest = build_digest(aggregator, baselines)
        label = labels.get((digest.service, int(digest.window_start.timestamp())))
        baselines.observe(digest.service, digest.metrics)
        if label is None or not label.scored:
            continue
        if service and digest.service != service:
            continue
        answers, mask, source, notes = answers_for_window(label, digest, calibrator)
        example = TrainingExample(
            state=digest.interpretation,
            answers=answers,
            window_start=digest.window_start,
            service=digest.service,
            scenario=label.scenario,
            digest_id=digest.digest_id,
            label_mask=mask,
            label_source=source,
            notes=notes,
            digest_version=digest.digest_version,
            observed={
                "event_count": digest.metrics.get("event_count"),
                "render_words": (digest.metrics.get("render") or {}).get("words"),
                "baseline_samples": digest.metrics.get("baseline_samples"),
                "anomaly_probability_from_model": None,
            },
        )
        if min_label_fraction and (example.n_trainable / max(example.n_answers, 1)) < min_label_fraction:
            continue
        examples.append(example)
    return examples


def write_jsonl(examples: Sequence[TrainingExample], path: str) -> str:
    """Write one JSON object per line — the format the Laya trainer reads."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example.to_dict(), ensure_ascii=False) + "\n")
    return str(target)


def summarise(examples: Sequence[TrainingExample]) -> Dict[str, Any]:
    """What is actually in the file — the numbers a reviewer needs before training on it."""
    if not examples:
        return {"n_examples": 0}
    per_question: Dict[str, Dict[str, int]] = {}
    per_scenario: Dict[str, int] = {}
    per_source: Dict[str, int] = {}
    for example in examples:
        per_scenario[example.scenario] = per_scenario.get(example.scenario, 0) + 1
        for question, trainable in example.label_mask.items():
            row = per_question.setdefault(question, {"trainable": 0, "total": 0})
            row["total"] += 1
            row["trainable"] += 1 if trainable else 0
        for question, source in example.label_source.items():
            per_source[source] = per_source.get(source, 0) + 1
    words = [len(example.state.split()) for example in examples]
    return {
        "n_examples": len(examples),
        "state_words": {"min": min(words), "max": max(words), "mean": round(sum(words) / len(words), 1)},
        "per_question": per_question,
        "per_scenario": dict(sorted(per_scenario.items())),
        "label_sources": dict(sorted(per_source.items())),
        "trainable_answers_total": sum(example.n_trainable for example in examples),
        "answers_total": sum(example.n_answers for example in examples),
    }


def format_summary(summary: Mapping[str, Any]) -> str:
    if not summary.get("n_examples"):
        return "no examples"
    lines = [
        "=" * 78,
        "Training export",
        "=" * 78,
        "examples            %d" % summary["n_examples"],
        "digest state words  min %d, mean %s, max %d  (the model's document budget)"
        % (summary["state_words"]["min"], summary["state_words"]["mean"], summary["state_words"]["max"]),
        "answers             %d total, %d trainable (the rest are explicitly masked out)"
        % (summary["answers_total"], summary["trainable_answers_total"]),
        "",
        "%-16s %10s %10s" % ("question", "trainable", "total"),
        "-" * 40,
    ]
    for question, row in sorted(summary["per_question"].items()):
        lines.append("%-16s %10d %10d" % (question, row["trainable"], row["total"]))
    lines.append("")
    lines.append("label sources: %s" % json.dumps(summary["label_sources"]))
    lines.append("")
    lines.append("windows per scenario:")
    for scenario, count in summary["per_scenario"].items():
        lines.append("  %-22s %d" % (scenario, count))
    return "\n".join(lines)


__all__ = [
    "TrainingExample",
    "export_examples",
    "write_jsonl",
    "summarise",
    "format_summary",
    "answers_for_window",
    "GROUND_TRUTH_QUESTIONS",
    "UNLABELLED_QUESTIONS",
]
