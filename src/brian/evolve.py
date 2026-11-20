"""The improvement mechanism, as an executable object rather than a person.

Two surveys with different vocabularies ask the same question of a system like this one. "The Path to
Recursive Self-Improving Agents" (D2I.ai) models an agent system as
`x = (M, H, D, T, Imp)` and calls something self-improving when `Imp` turns measured evidence into a
persistent change to `x`; its L1–L5 ladder grades how much of that loop is autonomous. "Recursive
Self-Improvement in AI" (arXiv:2607.07663) makes the evaluator the load-bearing part: improvement
strength tracks the verification hierarchy, and every failure mode follows from a loop whose signal is
weaker than it looks.

This module is `Imp` for the pipeline in this repository, and nothing more. Concretely:

    diagnose  -> the ledger from `brian cost`: which part of the bill is where
    propose   -> `catalogue()`: harness settings, each with a stated hypothesis
    evaluate  -> three signals, strongest first (see `EvolveRules`)
    select    -> `decide()`: a hard non-regression gate, then a Pareto gain
    integrate -> `persist()`: a version file plus the environment variables that reproduce it

What it deliberately is not
---------------------------
* It does not choose *what to measure*. The objective is fixed in `EvolveRules`, outside the loop, so
  the loop cannot talk itself into a better score by moving the goalposts. That is the difference
  between bounded self-refinement and open-ended RSI, and only the bounded one is attempted here.
* It never uses the system's own judgement of its own output as a signal. The three signals are a
  labelled dataset (recall/FPR), a deterministic measurement of the prompt (evidence coverage), and a
  token ledger. Self-assessment is the weakest rung of the verification hierarchy for a reason.
* It does not search an open-ended space. `catalogue()` is a hand-written list of knobs that already
  exist, each with a hypothesis that predicts its effect — the smallest thing that can still be called
  an improvement mechanism, and the largest thing that can be verified on this data.

The grader is `grade()`, which reports the level this system actually reaches, with the evidence and
the missing capability named. A loop that grades itself L4 is a self-confirming loop.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bus import MemoryBus
from .calibration import auc
from .config import ENV_VARS, Settings
from .config import apply_overrides as config_apply_overrides
from .contracts import Event
from .cost import CostLedger, ledger_from_traces
from .eval import EvalReport, Evaluator
from .laya import LayaClient
from .llm.prompts import evidence_coverage
from .pipeline import Pipeline, WindowTrace
from .scoring import deviation_probability, text_deviation
from .state import BaselineTracker, Windower, baseline_keys, build_digest, digest_specs
from .streams import Stream, flatten_streams, summarise

#: Build the reference evidence pool at this cap, so every candidate's coverage is measured against the
#: same pool rather than against the candidate's own truncation. It only has to be larger than any
#: candidate's `max_context_events`.
REFERENCE_EVENTS = 64

#: Where rounds, version files and the append-only ledger live. Gitignored, like the rest of `var/`.
EVOLVE_DIR = "var/evolve"
WITHHELD_DIR = "withheld"
"""Where a `replicate` refusal is written down (round 42).

A version file is a change the loop decided to ship. A refusal is a change the loop decided *not* to ship,
and until this round it existed only as a verdict inside one round's record — so "why wasn't this promoted"
required reading a round, and "would it still be blocked" required a hypothetical rejudge. A refusal is
also a claim: it says this candidate loses this much on this recording against this bar. Writing it in its
own numbered family makes the claim readable, checkable and countable the way a version is."""

REFUSAL_EXPIRY_DAYS = 14.0
"""How long a `skip` refusal speaks for itself before the round measures the candidate again (round 43).

Not a judgement about the candidate: a judgement about evidence. The same horizon as a version claim
(`promote.CLAIM_EXPIRY_DAYS`), because re-testing a refusal is cheaper than re-testing a deployment — one
candidate slot against a full re-check — so there is no reason to let the weaker claim live longer.

It sits at the top of the module on purpose: round 43 declared it just above `refusal_plan`, and round 44's
`withheld_due` then used it as a default argument forty lines *earlier*, which is a `NameError` at import
time rather than at call time. A default is evaluated where the `def` is read, not where the constant is
wanted.
"""


# --------------------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    """One proposed change to the harness, with the hypothesis that predicts its effect.

    The hypothesis is not decoration. A candidate that cannot say in advance what it expects to change
    is not a hypothesis, it is a poke — and when it happens to lower a number there is no way to tell
    whether it did so for the stated reason.
    """

    name: str
    hypothesis: str
    section: str
    """Which `Settings` section the change lands in: `llm`, `gate`, `window` or `digest`."""

    field: str
    value: Any
    also: Tuple[Tuple[str, str, Any], ...] = ()
    """Companion changes that are only useful together with the first one.

    Added in round 5, after a candidate that was right about the ranking and wrong about the threshold
    could not be proposed at all: `digest.scoring=deviation` improves how the text is read and shifts
    every probability onto a different scale, so shipping it alone alerts on 93% of windows. A catalogue
    whose entries are single fields can only ever express half of such a fix — which makes "the loop
    rejected it" a statement about the catalogue, not about the change. Widening the mutation space is
    the one improvement the survey says a bounded loop *can* make safely: nothing about the objective
    or the verification changes, only the shape of a proposal.
    """

    veto: Optional[str] = None
    """A disqualifier that no metric in this module can see. See `analyze-pages-only`."""

    promise: Optional[float] = None
    """For a *reshaped* candidate: the recall the version in force promised when it was accepted.

    A reshaping proposes the same configuration at the window count that covers the same wall-clock span
    on a differently-sampled stream (round 28 measured that 200 windows covered 600000 s where 286 did).
    It is not a competitor to the baseline: it is trying to make the claim true again where the sampling
    changed what a window counts. Judged against the baseline it would be accepted for 0.297 -> 0.427,
    which is a number nobody was promised; judged against the promise it answers the question the claim
    actually made.
    """

    provenance: str = ""
    """Where a non-catalogue candidate came from, so a later reader knows it was not a grid step."""

    def changes(self) -> Tuple[Tuple[str, str, Any], ...]:
        """Every setting this candidate writes, as `(section, field, value)`."""
        return ((self.section, self.field, self.value),) + tuple(self.also)

    def apply(self, settings: Settings) -> Settings:
        grouped: Dict[str, Dict[str, Any]] = {}
        for section, field, value in self.changes():
            grouped.setdefault(section, {})[field] = value
        # `apply_overrides` owns the validation, so a typo in a companion change is reported the same
        # way as a typo in the first one instead of half-applying.
        return apply_overrides(settings, grouped)

    def describe(self) -> str:
        return ", ".join("%s.%s = %r" % change for change in self.changes())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "changes": [list(change) for change in self.changes()],
            "veto": self.veto,
            "promise": self.promise,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Candidate":
        """Rebuild a candidate from a recorded round, so a stored verdict can be re-judged later.

        Reads both shapes: the pre-patch one with a single `section`/`field`/`value`, and the current
        `changes` list. Old rounds are the ones most worth re-judging — they are the decisions that were
        made under rules we have since changed.
        """
        changes = [tuple(change) for change in payload.get("changes") or []]
        if not changes:
            changes = [(payload["section"], payload["field"], payload["value"])]
        first = changes[0]
        return cls(
            name=payload["name"],
            hypothesis=payload.get("hypothesis") or "",
            section=first[0],
            field=first[1],
            value=first[2],
            veto=payload.get("veto"),
            also=tuple(changes[1:]),
            promise=payload.get("promise"),
            provenance=payload.get("provenance") or "",
        )


def catalogue() -> List[Candidate]:
    """The knobs this harness is allowed to move, and why each one might be worth moving.

    Every entry was previously examined by hand in the optimisation log; putting them in one list makes
    the loop able to re-derive those verdicts from measurements instead of trusting the write-up. That
    replication is the only reason to trust the loop on a knob nobody has tried yet.
    """
    return [
        Candidate(
            name="events-8",
            hypothesis="Most of the prompt is the event block, and 8 selected events already retain every "
                       "distinct error signature of a window; the bill should fall with no loss of evidence.",
            section="llm",
            field="max_context_events",
            value=8,
        ),
        Candidate(
            name="events-4",
            hypothesis="The same argument taken further: if 8 is free, 4 should be cheaper still.",
            section="llm",
            field="max_context_events",
            value=4,
        ),
        Candidate(
            name="reuse-300",
            hypothesis="A re-alert of an unchanged incident within 300 s asks the same question again, so "
                       "its report can be reused instead of re-bought.",
            section="gate",
            field="reuse_report_within_s",
            value=300,
        ),
        Candidate(
            name="analyze-pages-only",
            hypothesis="Only page_human alerts need a reasoning pass; auto_triage needs a narrative and "
                       "deep_analysis is the case the ladder asked to think about — so spending on both "
                       "should be avoidable.",
            section="gate",
            field="analyze_actions",
            value="page_human",
            veto="drops the reasoning pass that `deep_analysis` exists to request; the saving is real but "
                 "the change removes the capability the action ladder was built to spend on",
        ),
        Candidate(
            name="page-severity-3",
            hypothesis="Paging only on critical severity should cut the pages, and the cost with them.",
            section="gate",
            field="page_severity_rank",
            value=3,
        ),
        Candidate(
            name="confidence-auto-85",
            hypothesis="Being stricter about when the class/severity can be trusted without the LLM should "
                       "move alerts into deep_analysis.",
            section="gate",
            field="confidence_auto",
            value=0.85,
        ),
        Candidate(
            name="anomaly-p-40",
            hypothesis="On RCAEval the engine puts faulted and healthy windows at nearly the same "
                       "probability (~0.36), so the neutral threshold silences it; asking whether a "
                       "lower bar recovers recall without raising false positives faster is the only "
                       "way the labels can prefer one configuration to another on that domain.",
            section="gate",
            field="anomaly_probability",
            value=0.40,
        ),
        Candidate(
            name="host-features-3",
            hypothesis="On host telemetry the digest should read three series (cpu_r, load_1, mem_free) "
                       "rather than all eight: the archived bench runs reach AUC 0.75 with three and "
                       "0.42 with eight, so the extra series are attention spent on the noisiest one.",
            section="digest",
            field="host_features",
            value="cpu_r,load_1,mem_free",
        ),
        Candidate(
            name="host-scoring-deviation",
            hypothesis="On host telemetry a threshold on the loudest rendered deviation should judge the "
                       "digest, not the engine: the same text separates at AUC 0.827 (machine-1-6) and "
                       "0.597 (machine-2-2) for a rule with no parameters, against 0.674 and 0.347 for "
                       "the engine. If the labels prefer the rule at an equal false-positive budget the "
                       "engine is not paying for itself on this domain, and that is a finding about the "
                       "architecture rather than about a threshold.",
            section="digest",
            field="scoring",
            value="deviation",
            also=(("gate", "anomaly_probability", 0.87), ("gate", "notify_probability", 0.87)),
        ),
        Candidate(
            name="deviation-bar-free",
            hypothesis="The reader's 0.860 recall at a 10% budget was chosen by scanning the labels, so no "
                       "deployment can reproduce it. This candidate is the bar the stream's own score "
                       "distribution implies at a 10% alert rate — 0.9831 on the RCAEval stream, read by "
                       "`brian evolve --calibrate-bar` and using no label — and the labels decide whether "
                       "the deployable form keeps the recall or whether the fitted number was an upper "
                       "bound nobody can reach.",
            section="digest",
            field="scoring",
            value="deviation",
            also=(("gate", "anomaly_probability", 0.9831), ("gate", "notify_probability", 0.9831)),
        ),
        Candidate(
            name="host-precision-3",
            hypothesis="A host series measured in units of one needs a third digit before the model can "
                       "tell two windows apart: machine-2-2's CPU is 0.0016, which two decimals print as "
                       "0.00. Giving the digest one more digit ranked machine-2-2 at AUC 0.436 against "
                       "0.297 on the same windows, so the labels are the arbiter of whether that is a "
                       "gain worth the longer text.",
            section="digest",
            field="host_precision",
            value=3,
        ),
        Candidate(
            name="host-ratio-floor-02",
            hypothesis="A ratio over a near-zero baseline is arithmetic noise: machine-2-2 prints 40% "
                       "of its ratios from |baseline| < 0.02, where the ratio column separates at AUC "
                       "0.47-0.54 while the level column reaches 0.69-0.78. Dropping those ratios while "
                       "keeping the levels is a rendering change the labels can judge, and the reader "
                       "that scores the fields is unaffected by construction.",
            section="digest",
            field="host_ratio_floor",
            value=0.02,
        ),
        Candidate(
            name="host-ratio-floor-10",
            hypothesis="Same rendering change at a stricter floor, as a dose-response check: if 0.02 "
                       "helps only because it removes a few lines, 0.10 removes most ratios on this "
                       "domain (every baseline below a tenth of a unit) and should show the trend "
                       "rather than one point.",
            section="digest",
            field="host_ratio_floor",
            value=0.1,
        ),
        Candidate(
            name="host-rolling-rank",
            hypothesis="The reader's level is not stationary: inside one recording of machine-1-6 the "
                       "median score moves 0.846 -> 0.434 -> 0.768 while the fault/normal separation "
                       "in the mean is about 0.03, so a cut fitted on one half of the file alerts on "
                       "nothing (0.000/0.000) or on everything (1.000/0.551) in the other half. Report "
                       "the answer as a rank inside the last 100 windows instead of a level and ask the "
                       "gate for the top tenth: that rule reached 0.261 recall at 0.016 false positives "
                       "on that host, causally, against 0.128 at 0.095 for the in-sample cut. If the "
                       "labels do not prefer it, drift is not the reason that host looks undetectable.",
            section="digest",
            field="scoring",
            # "auto" rather than "deviation": on a host stream the two resolve to the same reader, so this
            # round's measurement is unchanged, but on a service stream auto keeps the engine. Exporting
            # "deviation" would ship the reader to a domain whose text was never checked against it.
            value="auto",
            also=(("digest", "rolling_window", 100),
                  # A rank needs its own bar: 0.90 on a probability means "almost certainly", on a rank it
                  # means "top tenth", and round 14 measured what happens when the two are confused (the
                  # service suite fell from 0.875 recall to 0.625). `anomaly_probability`/`notify_probability`
                  # stay at their defaults so a deployment can carry both domains.
                  ("gate", "rank_probability", 0.90)),
        ),
    ]


# --------------------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------------------
@dataclass
class Measurement:
    """One configuration, measured on the fixed evaluator. Every field is comparable across candidates."""

    candidate: str
    """The candidate's *name*: a measurement is a record of one configuration, not a reference to it."""

    recall: Optional[float]
    fpr: Optional[float]
    precision: Optional[float]
    reports: int
    tokens_per_report: float
    tokens_per_window: float
    latency_p95_ms: Optional[float]
    coverage: Dict[str, float] = field(default_factory=dict)
    trial_s: float = 0.0
    reasoner: str = ""
    windows: int = 0
    """Judged windows behind the rates. Carried because a rate without its sample size is a rumour."""
    confusion: Dict[str, int] = field(default_factory=dict)
    recall_at_budget: Optional[float] = None
    """Best recall this configuration reaches while staying inside the false-positive budget.

    `recall` above is what the *shipped threshold* yields, and on a domain the threshold was not
    calibrated for that number says more about the threshold than about the detector: on SMD every
    answer sits between 0.30 and 0.47, so a cut at 0.5 reports recall 0 for a configuration whose
    ranking is good. Comparing candidates at a common budget removes the constant from the
    comparison, which is what makes a detector change and a cost change comparable at all.
    """
    fpr_at_budget: Optional[float] = None
    threshold_at_budget: Optional[float] = None
    budget: float = 0.0
    """The false-positive budget the at-budget columns were measured at."""
    thresholds_at_budget: Dict[str, Optional[float]] = field(default_factory=dict)
    """The cut each recording chose at the budget; empty when one cut served all of them."""
    at_budget_pooled: bool = False
    """True when a recording could not be swept and the pooled sweep was used instead."""
    recall_at_free_cut: Optional[float] = None
    """Recall of the bar a deployment can compute from its own scores, with no label read."""
    fpr_at_free_cut: Optional[float] = None
    """The false-positive rate that bar actually spends, which is the budget in practice."""
    alert_budget: float = 0.0
    """The alert rate the label-free bar was set at. A different number from `budget`: see
    `label_free_ceiling` for why the two cannot be the same quantity."""
    prevalence: Optional[float] = None
    """Share of scored windows that are anomalous, i.e. what the alert budget is being divided into."""
    alerts_at_free_cut: Optional[int] = None
    """How many windows the label-free bar actually paged on. Equal to the target rate only when the
    scores are distinct enough for the bar to land on a value few windows share."""
    scored_at_free_cut: Optional[int] = None
    auc_by_recording: Dict[str, Optional[float]] = field(default_factory=dict)
    """What the model itself scored on each recording, so the round table can show the gap it has to close."""

    recall_by_recording: Dict[str, Optional[float]] = field(default_factory=dict)
    """At-budget recall on each recording separately (round 39).

    The pooled number is one division: a change that lifts one host and lowers another can leave it
    unchanged, and nothing in the record says which happened. This is filled from the sweep that already
    runs per recording, so it costs no extra measurement.
    """
    fpr_by_recording: Dict[str, Optional[float]] = field(default_factory=dict)
    """The false-positive rate of those same per-recording cuts."""

    promise: Optional[float] = None
    """The recall a reshaped candidate was meant to restore, carried so `decide()` can judge it (round 30).

    It lives here rather than on the candidate because a measurement keeps the candidate's *name*, not the
    object: the first version read `trial.candidate.promise`, which silently did nothing and filed the
    reshape under `quality`. The stored round record is what exposed it.
    """

    window: Optional[int] = None
    """The rolling window this measurement was taken with, or `None` when it does not roll.

    Round 32: without it a record cannot say what it compared. `run_round` measures every candidate
    against a reference at the candidate's *own* window (round 31), but a replay could only see
    `baseline`, so re-deciding a window change compared 286 against 200 and reported the geometry as a
    rule change. The geometry is part of the measurement, so it travels with it.
    """

    @property
    def free_cut_alert_rate(self) -> Optional[float]:
        """The share of windows the label-free bar actually paged on."""
        if not self.alerts_at_free_cut or not self.scored_at_free_cut:
            return None
        return self.alerts_at_free_cut / self.scored_at_free_cut

    @property
    def label_free_ceiling(self) -> Optional[float]:
        """The most recall a fixed alert rate can buy on a stream with this prevalence.

        Alerting on the top `a` share of windows cannot find more than `a / prevalence` share of the
        faults: if half the windows are faults, a 10% alert rate can at best catch one fifth of them.
        Round 23 measured recall 0.226 (service domain, prevalence 61%) and 0.284 (host, prevalence 16%)
        against this ceiling, which is why the number belongs next to the recall it limits.

        The rate used is the one the bar actually spent when that is known: a fifteen-window sample cannot
        page on 1.5 windows, so the bar pages on two and spends 13.3% at a 10% target. Comparing recall
        with the nominal budget in that case would report a violation of a rate the bar was never able to
        respect.
        """
        if not self.prevalence or self.prevalence <= 0:
            return None
        rate = self.free_cut_alert_rate
        if rate is None:
            if not self.alert_budget:
                return None
            rate = self.alert_budget
        return min(1.0, rate / self.prevalence)

    @property
    def coverage_floor(self) -> float:
        return min(self.coverage.values()) if self.coverage else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate,
            "promise": _round(self.promise),
            "window": self.window,
            "recall": _round(self.recall),
            "fpr": _round(self.fpr),
            "precision": _round(self.precision),
            "windows": self.windows,
            "confusion": dict(self.confusion),
            "reports": self.reports,
            "tokens_per_report": _round(self.tokens_per_report, 1),
            "tokens_per_window": _round(self.tokens_per_window, 1),
            "latency_p95_ms": _round(self.latency_p95_ms, 1),
            "coverage": {k: _round(v, 4) for k, v in self.coverage.items()},
            "coverage_floor": _round(self.coverage_floor, 4),
            "trial_s": _round(self.trial_s, 2),
            "reasoner": self.reasoner,
            "budget": _round(self.budget, 4),
            "recall_at_budget": _round(self.recall_at_budget),
            "fpr_at_budget": _round(self.fpr_at_budget),
            "threshold_at_budget": _round(self.threshold_at_budget, 6),
            "thresholds_at_budget": {k: _round(v, 6) for k, v in self.thresholds_at_budget.items()},
            "at_budget_pooled": self.at_budget_pooled,
            "recall_at_free_cut": _round(self.recall_at_free_cut),
            "fpr_at_free_cut": _round(self.fpr_at_free_cut),
            "alert_budget": _round(self.alert_budget, 4),
            "prevalence": _round(self.prevalence, 4),
            "label_free_ceiling": _round(self.label_free_ceiling, 4),
            "alerts_at_free_cut": self.alerts_at_free_cut,
            "scored_at_free_cut": self.scored_at_free_cut,
            "free_cut_alert_rate": _round(self.free_cut_alert_rate, 4),
            "auc_by_recording": {k: _round(v, 6) for k, v in self.auc_by_recording.items()},
            "recall_by_recording": {k: _round(v) for k, v in self.recall_by_recording.items()},
            "fpr_by_recording": {k: _round(v) for k, v in self.fpr_by_recording.items()},
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Measurement":
        """Rebuild a measurement from a recorded round.

        The recorded numbers are the evidence, so the defaults here must never invent one: a field the
        round file does not carry comes back as `None`/empty, and a stored `None` stays `None`.
        """
        return cls(
            candidate=payload.get("candidate") or "",
            recall=payload.get("recall"),
            fpr=payload.get("fpr"),
            precision=payload.get("precision"),
            reports=int(payload.get("reports") or 0),
            tokens_per_report=payload.get("tokens_per_report"),
            tokens_per_window=payload.get("tokens_per_window"),
            latency_p95_ms=payload.get("latency_p95_ms"),
            coverage=dict(payload.get("coverage") or {}),
            trial_s=float(payload.get("trial_s") or 0.0),
            reasoner=payload.get("reasoner") or "",
            windows=int(payload.get("windows") or 0),
            confusion=dict(payload.get("confusion") or {}),
            recall_at_budget=payload.get("recall_at_budget"),
            fpr_at_budget=payload.get("fpr_at_budget"),
            threshold_at_budget=payload.get("threshold_at_budget"),
            budget=float(payload.get("budget") or 0.0),
            thresholds_at_budget=dict(payload.get("thresholds_at_budget") or {}),
            at_budget_pooled=bool(payload.get("at_budget_pooled")),
            recall_at_free_cut=payload.get("recall_at_free_cut"),
            fpr_at_free_cut=payload.get("fpr_at_free_cut"),
            alert_budget=float(payload.get("alert_budget") or 0.0),
            prevalence=payload.get("prevalence"),
            alerts_at_free_cut=payload.get("alerts_at_free_cut"),
            scored_at_free_cut=payload.get("scored_at_free_cut"),
            auc_by_recording=dict(payload.get("auc_by_recording") or {}),
            recall_by_recording=dict(payload.get("recall_by_recording") or {}),
            fpr_by_recording=dict(payload.get("fpr_by_recording") or {}),
            promise=payload.get("promise"),
            window=None if payload.get("window") is None else int(payload["window"]),
        )


def operating_point(scores: Sequence[Any], budget: float) -> Dict[str, Optional[float]]:
    """The best recall reachable at or below `budget` false positives per healthy window.

    This is a sweep over the probabilities the run already produced, so it costs nothing beyond the
    scoring pass and it is invariant to where the shipped threshold sits. It is the same construction
    `scripts/real_data/bench_smd.py` uses, kept here so the loop and the bench cannot drift apart.

    Thresholds are tried at the observed values, highest first: a cut at `p` alerts the windows whose
    probability is at least `p`, so the reachable confusion pairs are finite and no interpolation is
    needed. Ties go to the higher threshold, which is the stricter reading of the same recall.
    """
    usable = [
        (float(score.predicted_probability), bool(score.expected_anomaly))
        for score in scores
        if score.predicted_probability is not None and score.error is None
    ]
    empty = {"budget": budget, "threshold": None, "recall": None, "fpr": None, "alerts": None}
    if not usable:
        return empty
    positives = sum(1 for _p, y in usable if y)
    negatives = len(usable) - positives
    if not positives or not negatives:
        # A budget is a ratio to the healthy population; with none of it there is nothing to hold.
        return empty
    best: Optional[Dict[str, Optional[float]]] = None
    best_tp = -1
    for threshold in sorted({p for p, _y in usable}, reverse=True):
        tp = sum(1 for p, y in usable if y and p >= threshold)
        fp = sum(1 for p, y in usable if not y and p >= threshold)
        fpr = fp / negatives
        if fpr > budget + 1e-12:
            continue
        if tp > best_tp:
            best_tp = tp
            best = {
                "budget": budget,
                "threshold": threshold,
                "recall": tp / positives,
                "fpr": fpr,
                "alerts": float(tp + fp),
            }
    return best if best is not None else empty


def operating_point_by_recording(groups: Sequence[Tuple[str, Sequence[Any]]],
                                 budget: float) -> Dict[str, Any]:
    """The same sweep, run once per recording and the counts summed.

    Pooling the probabilities of two hosts and sweeping one cut across them answers a question about
    a shared scale that does not exist: on SMD one machine's answers top out at 0.35 and the other's
    bottom out at 0.30, so a pooled sweep silently mixes a cut that suits the first with negatives
    that belong to the second. Measured here, machine-1-6 ranks at AUC 0.674 and machine-2-2 at 0.347
    with the same three columns, and the pooled number is neither.

    Each recording therefore chooses its own cut inside the same budget and the outcomes are added,
    which is what a per-host calibration does in deployment. A recording with no positives or no
    healthy windows cannot be swept; rather than dropping it (which would hide its false positives or
    its incidents) the whole sweep falls back to the pooled one and says so.
    """
    readable = [(name, list(scores)) for name, scores in groups if scores]
    if len(readable) <= 1:
        name = readable[0][0] if readable else "baseline"
        flat = readable[0][1] if readable else []
        result = dict(operating_point(flat, budget))
        result["thresholds"] = {name: result["threshold"]} if result["threshold"] is not None else {}
        result["recordings"] = 1
        result["pooled"] = True
        result["by_recording"] = (
            {name: {"recall": result["recall"], "fpr": result["fpr"],
                    "positives": _positives(flat)}} if result["recall"] is not None else {}
        )
        return result

    sweeps: List[Tuple[str, Dict[str, Optional[float]]]] = []
    for name, scores in readable:
        sweep = operating_point(scores, budget)
        if sweep["recall"] is None:
            pooled = dict(operating_point([s for _n, group in readable for s in group], budget))
            pooled["thresholds"] = {}
            pooled["recordings"] = len(readable)
            pooled["pooled"] = True
            return pooled
        sweeps.append((name, sweep))

    totals = {"tp": 0.0, "fp": 0.0, "positives": 0.0, "negatives": 0.0}
    thresholds: Dict[str, Optional[float]] = {}
    by_recording: Dict[str, Dict[str, Optional[float]]] = {}
    for (name, sweep), (_n, scores) in zip(sweeps, readable):
        usable = [
            (float(score.predicted_probability), bool(score.expected_anomaly))
            for score in scores
            if score.predicted_probability is not None and score.error is None
        ]
        positives = sum(1 for _p, y in usable if y)
        negatives = len(usable) - positives
        cut = float(sweep["threshold"])  # type: ignore[arg-type]
        tp = sum(1 for p, y in usable if y and p >= cut)
        fp = sum(1 for p, y in usable if not y and p >= cut)
        totals["tp"] += tp
        totals["fp"] += fp
        totals["positives"] += positives
        totals["negatives"] += negatives
        thresholds[name] = sweep["threshold"]
        # Round 39: the same sweep, kept per recording. A pooled number cannot say whether a change helped
        # both hosts or moved one while the other lost: the pooled rate is one division.
        by_recording[name] = {
            "recall": (tp / positives) if positives else None,
            "fpr": (fp / negatives) if negatives else None,
            "positives": positives,
        }
    return {
        "budget": budget,
        "threshold": None,
        "recall": totals["tp"] / totals["positives"] if totals["positives"] else None,
        "fpr": totals["fp"] / totals["negatives"] if totals["negatives"] else None,
        "alerts": totals["tp"] + totals["fp"],
        "thresholds": thresholds,
        "recordings": len(readable),
        "pooled": False,
        "by_recording": by_recording,
    }


def _positives(scores: Sequence[Any]) -> int:
    """Fault windows in one recording's scores — the denominator of its recall."""
    return sum(1 for score in scores
               if getattr(score, "predicted_probability", None) is not None
               and getattr(score, "error", None) is None
               and bool(score.expected_anomaly))


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def apply_overrides(settings: Settings, overrides: Dict[str, Any]) -> Settings:
    """Apply a stream file's base configuration before any candidate is measured.

    The overrides are part of the *data*, not of the candidates: they describe what it takes to run
    this pipeline on this dataset at all (a different domain needs its own threshold, because the
    shipped one was calibrated elsewhere). They are applied once, outside the loop, so every
    candidate is compared on the same base and the round record says what that base was.

    Delegates to `config.apply_overrides` so the loop and a hot-reloading pipeline share one
    implementation — two merge rules would be two definitions of "in force", and a candidate could be
    accepted under one and loaded under the other.
    """
    if not isinstance(overrides, dict):
        raise ValueError("stream overrides must be an object of sections")
    return config_apply_overrides(settings, overrides)


def _tally(report: EvalReport) -> Dict[str, int]:
    """Confusion counts, counted the same way `EvalReport` counts them.

    Recomputed here rather than read off the report because a multi-stream measurement needs the
    counts, not each stream's rate: averaging rates from recordings of different lengths weights a
    short recording the same as a long one, which is how a 3-window fluke becomes a 30% move.
    """
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for score in report.scores:
        if score.predicted_anomaly is None:
            continue
        if score.expected_anomaly:
            counts["tp" if score.predicted_anomaly else "fn"] += 1
        else:
            counts["fp" if score.predicted_anomaly else "tn"] += 1
    return counts


def _rate(counts: Dict[str, int], numerator: str, denominator: str) -> Optional[float]:
    bottom = counts[numerator] + counts[denominator]
    return counts[numerator] / bottom if bottom else None


def _p95(values: Sequence[float]) -> Optional[float]:
    ordered = sorted(value for value in values if value)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]


def build_pool(settings: Settings, streams: Sequence[Stream]) -> List[List[Event]]:
    """The full recent-event history of every alert, captured once at a fixed, generous cap.

    Coverage must be judged against a pool that does not depend on the candidate being judged, or a
    candidate that truncates harder would look safer by shrinking its own denominator. One pipeline
    per recording, for the same reason the evaluator runs one per recording: each recording has its
    own baseline, and sharing it measures the seam between two runs.
    """
    reference = replace(settings.llm, max_context_events=REFERENCE_EVENTS)
    pool: List[List[Event]] = []
    for stream in streams:
        pipeline = Pipeline(settings.with_overrides(llm=reference), bus=MemoryBus(),
                            keep_events=REFERENCE_EVENTS)
        traces = _run(pipeline, flatten_streams([stream]))
        pool.extend(list(trace.history) for trace in traces
                    if trace.report is not None and trace.history)
    return pool


def _run(pipeline: Pipeline, events: Sequence[Event]) -> List[WindowTrace]:
    traces: List[WindowTrace] = []
    for event in events:
        pipeline.ingest(event)
        traces.extend(pipeline.drain_ready())
    traces.extend(pipeline.drain_ready(final=True))
    return traces


def cost_run(settings: Settings,
             streams: Sequence[Stream]) -> Tuple[CostLedger, List[WindowTrace], str]:
    """Run every stream end to end and return one ledger over all of them.

    The same code path as `brian cost`, deliberately: an improvement loop that measured a cheaper,
    simplified pipeline would be optimising something other than the thing that ships.
    """
    traces: List[WindowTrace] = []
    reasoner = ""
    for stream in streams:
        pipeline = Pipeline(settings, bus=MemoryBus())
        traces.extend(_run(pipeline, flatten_streams([stream])))
        reasoner = pipeline.rca.mode or reasoner
    return ledger_from_traces(traces), traces, reasoner


def _deviation(entry: Mapping[str, Any]) -> Optional[float]:
    """How far one rendered line is from its baseline, on the horizon the renderer shows.

    The host renderer prefers the long horizon (`_render_host_digest`), so the reader uses it too: a
    reader that peaked at a number the renderer never prints would be measuring a different system.
    """
    value = entry.get("long_delta_ratio")
    if value is None:
        value = entry.get("delta_ratio")
    if value is None:
        return None
    try:
        return abs(float(value) - 1.0)
    except (TypeError, ValueError):
        return None


def state_ceiling(settings: Settings, streams: Sequence[Stream]) -> Dict[str, Any]:
    """How much of the label a *trivial* reader of the same state recovers, per recording.

    This is the number every model has to beat to justify itself. It renders each window exactly as the
    pipeline does, takes the loudest deviation the renderer would show (`max |ratio - 1|`), and measures
    that single number against the labels. No Laya call is involved, so it is a hard ceiling on what the
    state can support and it costs nothing to compute.

    Reported per recording rather than pooled: two hosts put their answers on different scales, so a
    pooled AUC would measure the scale difference as much as the signal. `None` where a recording has one
    class only — a ceiling that cannot be computed is not evidence of a weak state.

    Two readers, because the state and the text are not the same thing. `by_stream` reads the
    structured `MetricValue` fields (it can see a ratio the renderer suppressed or a level it rounded
    away); `text_by_stream` parses only the characters the engine is given, out of
    `digest.interpretation`. When the two disagree, the gap belongs to the renderer, and a model
    cannot be judged against a ceiling that includes numbers its input never contained.

    On SMD the field reader reaches 0.827 (machine-1-6) and 0.597 (machine-2-2) while the engine
    reaches 0.674 and 0.347 on three features, and 0.517 and 0.299 on all eight. The two ceiling
    columns agree, so that gap is the model's, not the renderer's — the fact a cost-only round table
    cannot show.
    """
    by_stream: Dict[str, Optional[float]] = {}
    text_by_stream: Dict[str, Optional[float]] = {}
    for stream in streams:
        scores, text_scores, truth = _replay_scores(settings, stream)
        labelled = [bool(value) for value in truth]
        by_stream[stream.name] = auc(scores, labelled) if scores else None
        text_by_stream[stream.name] = auc(text_scores, labelled) if text_scores else None
    return {"by_stream": by_stream, "text_by_stream": text_by_stream,
            "windows": sum(len(s.windows) for s in streams)}


def _replay_scores(settings: Settings, stream: Stream) -> Tuple[List[float], List[float], List[int]]:
    """Replay one recording and return the reader's score, the text score and the label per window."""
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
    scores: List[float] = []
    text_scores: List[float] = []
    truth: List[int] = []

    def record(digest: Any) -> None:
        key = (digest.service, int(digest.window_start.timestamp()))
        if key not in labels:
            return
        # The reader's own answer, not the raw deviation: `deviation_probability` is monotone in
        # `max |ratio - 1|` (so every AUC below is unchanged) and it is the number the gate compares
        # against a bar, so a bar calibrated here is a bar the deployment can use.
        probability = deviation_probability(digest)
        scores.append(float(probability) if probability is not None else 0.0)
        text_scores.append(text_deviation(getattr(digest, "interpretation", "") or "") or 0.0)
        truth.append(1 if labels[key] else 0)

    def consume(aggregators: Sequence[Any]) -> None:
        for aggregator in aggregators:
            digest = build_digest(aggregator, baselines, profile=settings.digest.profile,
                              precision=settings.digest.host_precision,
                              ratio_floor=settings.digest.host_ratio_floor)
            baselines.observe(digest.service, digest.metrics)
            record(digest)

    for event in flatten_streams([stream]):
        windower.add(event)
        consume(windower.ready())
    consume(windower.flush())
    return scores, text_scores, truth


def reader_scores(settings: Settings, streams: Sequence[Stream]) -> Dict[str, List[Tuple[float, bool]]]:
    """The reader's answer and the label for every scored window, per recording. No Laya call."""
    labelled: Dict[str, List[Tuple[float, bool]]] = {}
    for stream in streams:
        scores, _text, truth = _replay_scores(settings, stream)
        labelled[stream.name] = [(float(score), bool(int(label)))
                                 for score, label in zip(scores, truth)]
    return labelled


def _count_at_or_above(values: Sequence[float], cut: float) -> int:
    """How many of the sorted `values` are >= `cut`."""
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] < cut:
            low = middle + 1
        else:
            high = middle
    return len(values) - low


def label_free_cut(scores: Sequence[float], budget: float) -> Optional[float]:
    """The bar a deployment could set from its own traffic so that at most `budget` of it pages.

    This is a quantile, but not the naive one. Round 23 measured a reader whose scores take few distinct
    values: on RCAEval a nominally 10% bar alerted on 14.9% of windows, because dozens of windows shared
    the value the quantile landed on. A bar that pages on more than the rate it was set for is not a 10%
    bar, so the cut is raised until the number of windows at or above it fits. When even the highest
    score is shared by too many windows the sample cannot support that rate, and `None` is returned
    rather than a bar that lies.
    """
    values = sorted(float(value) for value in scores)
    count = len(values)
    if count == 0:
        return None
    if budget <= 0.0:
        return values[-1]
    if budget >= 1.0:
        return values[0]
    allowed = max(1, int(round(count * budget)))
    best: Optional[float] = None
    for cut in sorted(set(values), reverse=True):
        if _count_at_or_above(values, cut) <= allowed:
            best = cut
        else:
            break
    return best


def _comparison_values(comparison: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The flat list of rendered comparison entries, whatever nesting the payload uses."""
    values: List[Mapping[str, Any]] = []
    for entry in comparison.values():
        if not isinstance(entry, Mapping):
            continue
        if "value" in entry:
            values.append(entry)
            continue
        values.extend(_comparison_values(entry))
    return values


def measure(settings: Settings, candidate: Optional[Candidate], streams: Sequence[Stream],
            pool: Sequence[Sequence[Event]], clock=None, budget: float = 0.10,
            alert_budget: Optional[float] = None) -> Measurement:
    """Measure one configuration: detection from labels, cost from the ledger, coverage from the pool.

    Two budgets, not one. `budget` caps the false-positive rate of the sweep that reads the labels; the
    label-free bar is set from `alert_budget`, because the share of windows that page and the share of
    healthy windows that page are different numbers whenever the stream is not mostly healthy
    (round 23). A fresh `LayaClient` and a fresh `Pipeline` per measurement *and per recording*, so no
    result depends on the order the candidates were tried in, and no recording is scored against another
    recording's history.
    """
    import time as _time

    alert = budget if alert_budget is None else alert_budget
    started = (_time.perf_counter if clock is None else clock)()
    applied = candidate.apply(settings) if candidate is not None else settings

    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    latencies: List[float] = []
    score_groups: List[Tuple[str, List[Any]]] = []
    auc_by_recording: Dict[str, Optional[float]] = {}
    for stream in streams:
        report = Evaluator(applied, laya=LayaClient(applied.laya)).run(stream.windows)
        for key, value in _tally(report).items():
            counts[key] += value
        latencies.extend(score.latency_ms for score in report.scores if score.latency_ms)
        score_groups.append((stream.name, list(report.scores)))
        usable = [score for score in report.scores if score.predicted_probability is not None
                  and score.predicted_anomaly is not None]
        auc_by_recording[stream.name] = (
            auc([score.predicted_probability for score in usable],
                [bool(score.expected_anomaly) for score in usable])
            if usable else None
        )

    ledger, _traces, reasoner = cost_run(applied, streams)
    cap = applied.llm.max_context_events
    readings = [evidence_coverage(history, cap) for history in pool]
    coverage = (
        {
            key: sum(r[key] for r in readings) / len(readings)
            for key in readings[0]
        }
        if readings
        else {"log_retention": 1.0, "signature_retention": 1.0,
              "first_error_kept": 1.0, "context_retention": 1.0}
    )

    operating = operating_point_by_recording(score_groups, budget)
    free = free_cut_rates(score_groups, alert)
    scored = counts["tp"] + counts["fp"] + counts["tn"] + counts["fn"]
    faults = counts["tp"] + counts["fn"]
    return Measurement(
        candidate=candidate.name if candidate is not None else "baseline",
        promise=candidate.promise if candidate is not None else None,
        window=configuration_window(settings, candidate),
        recall=_rate(counts, "tp", "fn"),
        fpr=_rate(counts, "fp", "tn"),
        precision=_rate(counts, "tp", "fp"),
        reports=ledger.reports,
        tokens_per_report=ledger.tokens_per_report,
        tokens_per_window=ledger.tokens_per_window,
        latency_p95_ms=_p95(latencies),
        coverage=coverage,
        trial_s=(_time.perf_counter if clock is None else clock)() - started,
        reasoner=reasoner,
        windows=scored,
        confusion=counts,
        recall_at_budget=operating["recall"],
        fpr_at_budget=operating["fpr"],
        threshold_at_budget=operating["threshold"],
        budget=budget,
        thresholds_at_budget=operating.get("thresholds") or {},
        at_budget_pooled=bool(operating.get("pooled")),
        recall_at_free_cut=free["recall"],
        fpr_at_free_cut=free["fpr"],
        alert_budget=alert,
        prevalence=(faults / scored) if scored else None,
        alerts_at_free_cut=free.get("alerts"),
        scored_at_free_cut=free.get("scored"),
        auc_by_recording=auc_by_recording,
        recall_by_recording={
            name: (stats or {}).get("recall")
            for name, stats in (operating.get("by_recording") or {}).items()
        },
        fpr_by_recording={
            name: (stats or {}).get("fpr")
            for name, stats in (operating.get("by_recording") or {}).items()
        },
    )


def free_cut_rates(groups: Sequence[Tuple[str, Sequence[Any]]],
                   alert_budget: float) -> Dict[str, Optional[float]]:
    """Recall and false-positive rate of the bar a deployment could compute without labels.

    `recall_at_budget` is an upper bound: it is found by sweeping the observed scores and keeping the cut
    with the highest recall inside the *false-positive* budget, which reads the labels. This computes the
    bar a deployment can set from its own traffic instead: the (1 - `alert_budget`) quantile of the scores,
    one per recording. It controls the share of windows paged on, not the share of healthy windows paged
    on, and those differ by the prevalence — `Measurement.label_free_ceiling` says by how much.

    It exists because round 20 found the loop using the fitted bound as the reference for "is this
    deployable": a configuration whose bar can be computed from traffic alone was rejected for falling
    short of a number no deployment can reach. Both numbers are reported; only this one can be shipped.
    """
    tp = fp = positives = negatives = 0
    for _name, scores in groups:
        usable = [score for score in scores
                  if getattr(score, "predicted_probability", None) is not None
                  and getattr(score, "predicted_anomaly", None) is not None
                  and not getattr(score, "error", None)]
        if not usable:
            continue
        cut = label_free_cut([float(score.predicted_probability) for score in usable], alert_budget)
        if cut is None:
            continue
        for score in usable:
            hit = float(score.predicted_probability) >= cut
            if bool(score.expected_anomaly):
                positives += 1
                tp += 1 if hit else 0
            else:
                negatives += 1
                fp += 1 if hit else 0
    return {
        "recall": (tp / positives) if positives else None,
        "fpr": (fp / negatives) if negatives else None,
        "alerts": tp + fp,
        "scored": positives + negatives,
    }


# --------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EvolveRules:
    """The objective, fixed outside the loop.

    The order of the checks is the verification hierarchy, applied: the labelled dataset decides first,
    the deterministic evidence measurement second, the cost ledger last. A candidate that saves tokens by
    losing recall never reaches the cost check.
    """

    #: Tokens per report must fall by at least this fraction to count as a gain.
    min_gain: float = 0.05
    #: At-budget recall must rise by at least this fraction to count as a quality gain.
    min_quality_gain: float = 0.05
    #: A quality gain may cost this much more per report and still be accepted.
    cost_tolerance: float = 0.05
    #: Allowed regression in recall / FPR. Zero: a cheaper system that detects less is a different
    #: system, not a better one.
    recall_tolerance: float = 0.0
    fpr_tolerance: float = 0.0
    #: The false-positive budget the label comparison is made at. A rate is only comparable at a stated
    #: budget, and the budget is part of the objective, not of any candidate.
    fpr_budget: float = 0.10
    #: The share of windows a label-free bar is allowed to page on. Separate from `fpr_budget` because it
    #: is a different quantity: on a stream where half the windows are faults, paging on 10% of windows
    #: is a 13% false-positive rate, and a deployment can compute the first but never the second.
    alert_budget: float = 0.10
    #: A hard ceiling on the false-positive rate the configuration would *ship* with. The at-budget curve
    #: chooses a cut the loop is allowed to sweep, so it can accept a change that only looks cheap because
    #: the recorded configuration pages on everything; round 5 hit exactly that (a candidate whose shipped
    #: threshold alerted on 93.5% of windows was accepted on its at-budget numbers alone). This ceiling is
    #: not a comparison against the baseline — a baseline that alerts on everything is a finding about the
    #: deployed configuration, not a licence for the candidate to do the same.
    deploy_fpr_ceiling: float = 0.50
    #: How far the shipped false-positive rate may move and still count as *inheriting* the deployed
    #: configuration rather than claiming a new one. Exact equality was too strict: round 9 found a
    #: rendering candidate that moved 1.000 to 0.999 and was rejected by the deploy floor, hiding that the
    #: label column had already rejected it for losing recall at the budget. Half a percent of windows is
    #: the scale at which an operator would not call the behaviour different, and it is a judgement.
    deploy_fpr_tolerance: float = 0.005
    #: How much recall the recorded configuration may give up against the cut the loop computes itself
    #: before that counts as a regression. The strict form (any shortfall at all) rejected round 14's
    #: rolling-rank candidate: its recorded 0.90 bar finds 0.216 at the 10% budget while the sweep finds
    #: 0.224, an 0.008 gap, against a baseline at 0.060 — the rule turned a 3.7x recall gain into a
    #: rejection over 3.6% of the free cut's recall. A cut chosen from a finite sample is not exactly
    #: optimal, so a candidate is allowed to land that far short, and 10% is a judgement.
    operational_tolerance: float = 0.10
    #: How far one recording's at-budget recall may fall while the *pooled* number rises before the gain
    #: stops counting as replicated. `None` means the same bar a quality gain must clear, so a refusal can
    #: never use a stricter measure than the acceptance did. It is a judgement and it is per domain: a
    #: recording that sits below chance (SMD's machine-2-2 reads 0.037) loses a relative 5% for a
    #: rounding difference, while on a strong recording that is a real loss. Round 39 shipped the pooled
    #: check; round 40 makes the bar a number in the ruleset, so a re-deciding reader can disagree with it
    #: without re-measuring.
    replication_tolerance: Optional[float] = None

    @property
    def replication_bar(self) -> float:
        """The drop that counts as a loss of replication (absolute recall at the budget)."""
        return self.min_quality_gain if self.replication_tolerance is None else self.replication_tolerance

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_gain": self.min_gain,
            "min_quality_gain": self.min_quality_gain,
            "cost_tolerance": self.cost_tolerance,
            "recall_tolerance": self.recall_tolerance,
            "fpr_tolerance": self.fpr_tolerance,
            "fpr_budget": self.fpr_budget,
            "alert_budget": self.alert_budget,
            "deploy_fpr_ceiling": self.deploy_fpr_ceiling,
            "deploy_fpr_tolerance": self.deploy_fpr_tolerance,
            "operational_tolerance": self.operational_tolerance,
            "replication_tolerance": self.replication_tolerance,
        }


@dataclass
class Verdict:
    candidate: str
    accepted: bool
    rule: str
    reason: str
    #: The numbers a re-deciding reader needs, per rule. `replicate` stores which recording fell and by how
    #: much against which tolerance; prose in `reason` cannot be re-decided, and a refusal nobody can
    #: re-check reads as a finding when it is an assumption (round 40).
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate,
            "accepted": self.accepted,
            "rule": self.rule,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def alerts_per_1000(measurement: "Measurement") -> Optional[float]:
    """False positives per 1,000 scored windows at the *recorded* configuration, or None.

    The same number as the false-positive rate in units an operator can argue about. It is displayed
    next to the shipped recall because the two together are the only honest description of what a
    threshold does: a cut that alerts on everything has recall 1.000 and is useless.

    It counts *false* positives, not all pages (round 21: a configuration with shipped recall 0.211
    and zero false positives printed `0`, which is correct here and would be a lie under the heading
    "alerts" — the table says `fp/1k` for that reason).
    """
    if measurement.fpr is None:
        return None
    return 1000.0 * measurement.fpr


def beats_own_cut(measurement: "Measurement") -> bool:
    """Whether the recorded configuration is at least as good as the cut a deployment could compute.

    The reference is `recall_at_free_cut` when it exists — the bar taken from the scores alone — and the
    budget-fitted recall only as a fallback for measurements recorded before that column existed. The
    distinction matters: round 20 found the loop rejecting a configuration *because* its bar was
    computable from traffic (`recall 0.211` against a fitted bound of `0.860`), which is comparing a
    deployment with an upper bound no deployment can reach. True when neither reference exists.
    """
    if measurement.recall is None:
        return False
    reference = (measurement.recall_at_free_cut if measurement.recall_at_free_cut is not None
                 else measurement.recall_at_budget)
    if reference is None:
        return True
    return measurement.recall + 1e-9 >= reference


def own_cut_shortfall(measurement: "Measurement") -> float:
    """The share of the deployable cut's recall that the recorded configuration gives up.

    `beats_own_cut` is the two-valued form, and for acceptance two values are right: nothing is gained by
    calling a configuration "almost as good as the free one". For *rejection* the size matters, because a
    cut fitted to a finite sample is not exactly optimal and a candidate can land a little short of it
    while still being a large improvement on the baseline. Zero when the comparison is undefined, so an
    unmeasurable candidate is never rejected for a shortfall nobody measured.
    """
    reference = (measurement.recall_at_free_cut if measurement.recall_at_free_cut is not None
                 else measurement.recall_at_budget)
    if measurement.recall is None or reference is None:
        return 0.0
    if reference <= 0.0:
        return 0.0
    return max(0.0, (reference - measurement.recall) / reference)


def tokens_per_incident(measurement: "Measurement") -> Optional[float]:
    """Tokens the configuration would spend per *incident it found*, at the budget cut.

    The loop's cost column counts tokens per report, which is what a report costs and says nothing
    about what an incident costs. A configuration that finds ten times as many incidents for 5% more
    tokens per report is cheaper per incident by an order of magnitude, and a per-report bar cannot
    see that: round 19 rejected the deterministic reader on the service domain for a 5.3% rise in
    tokens per report while its recall went 0.068 -> 0.860. This function is the other denominator.

    None when there is nothing to divide: no scored fault, no budget recall, or no bill.
    """
    counts = measurement.confusion or {}
    faults = int(counts.get("tp", 0)) + int(counts.get("fn", 0))
    if faults <= 0 or measurement.recall_at_budget is None or measurement.tokens_per_window is None:
        return None
    found = measurement.recall_at_budget * faults
    if found <= 0.0:
        return None
    windows = measurement.windows if measurement.windows > 0 else 1
    return measurement.tokens_per_window * windows / found


def deployable(measurement: "Measurement", rules: "EvolveRules") -> bool:
    """Whether the configuration *as recorded* is one an operator could run.

    Two conditions, and both are needed. It has to sit at or below the deploy false-positive ceiling
    (nobody answers 90% of all windows), and its recall has to be no worse than the recall of the cut
    the budget rule would pick. The second condition is what makes a *threshold-only* candidate
    judgeable at all: such a candidate does not move the at-budget curve — the loop still sweeps its
    own cut — so the only place its effect exists is the recorded configuration, and the standard for
    that configuration is that it must not be beaten by a cut the loop can compute from the same
    scores. SMD's shipped 0.33 fails the first condition (alerts on every window) and its shipped 0.40
    fails the second (recall 0.007 against the budget cut's 0.060), which is why neither is accepted.
    """
    if measurement.fpr is None or measurement.recall is None:
        return False
    if measurement.fpr > rules.deploy_fpr_ceiling + 1e-9:
        return False
    return beats_own_cut(measurement)


def replication_loss(baseline: Measurement, trial: Measurement,
                     tolerance: float) -> Optional[Dict[str, Any]]:
    """The recording the candidate loses, as numbers, when the pooled gain hides it (round 39/40).

    A pooled at-budget recall is one division over both recordings' incidents. On SMD the two hosts are
    not comparable — machine-2-2 sits below chance on the model's own score — so a change can lift the
    recording with more incidents and lower the other while the pooled number rises. Reading that as a
    detection gain reports a change nobody can deploy on both hosts.

    Returns the first recording (in name order) whose recall falls by more than `tolerance`, with the
    pooled numbers next to it, so the refusal can be stored as a measurement rather than as a sentence.
    """
    before = baseline.recall_by_recording or {}
    after = trial.recall_by_recording or {}
    shared = sorted(set(before) & set(after))
    if len(shared) < 2:
        return None
    for name in shared:
        old, new = before.get(name), after.get(name)
        if old is None or new is None:
            continue
        if new < old - max(tolerance, 1e-9):
            return {
                "recording": name,
                "before": _round(old),
                "after": _round(new),
                "drop": _round(old - new),
                "tolerance": _round(tolerance),
                "pooled_before": _round(baseline.recall_at_budget),
                "pooled_after": _round(trial.recall_at_budget),
                "budget": _round(baseline.budget),
            }
    return None


def _replication_sentence(detail: Dict[str, Any]) -> str:
    """The refusal sentence for a `replication_loss` detail."""
    return ("recall at the %.0f%% budget rises %.3f -> %.3f pooled, but %s falls %.3f -> %.3f: "
            "the gain belongs to the recordings that rose, so this is not a replicated one"
            % (100 * (detail["budget"] or 0.0), detail["pooled_before"] or 0.0,
               detail["pooled_after"] or 0.0, detail["recording"],
               detail["before"] or 0.0, detail["after"] or 0.0))


def replication_sentence(detail: Dict[str, Any]) -> str:
    """Public name for the refusal sentence (round 43): the write-back path builds the same sentence."""
    return _replication_sentence(detail)


def _replication_loss(baseline: Measurement, trial: Measurement,
                      threshold: float) -> Optional[str]:
    """The refusal sentence for `replication_loss`, or None when the gain replicates."""
    detail = replication_loss(baseline, trial, threshold)
    return None if detail is None else _replication_sentence(detail)


def decide(baseline: Measurement, trial: Measurement, rules: EvolveRules,
           veto: Optional[str] = None) -> Verdict:
    """Accept only a Pareto improvement: better on one axis, not worse on the other.

    Three moves qualify. A cost move lowers tokens per report by `min_gain` at no loss of at-budget
    recall, false-positive rate, or evidence. A quality move raises at-budget recall by
    `min_quality_gain` without raising the false-positive rate and without costing more than
    `cost_tolerance` per report (undefined cost counts as no objection, because a domain the shipped
    threshold silences has no bill to compare and refusing to decide there would leave the dilution
    this loop exists to find in place). The third is `operational`: the configuration *as recorded*
    moves from undeployable to deployable (see `deployable`), which is the only column that can judge
    a threshold-only candidate — such a candidate leaves the at-budget curve identical because the
    loop sweeps its own cut, so before this branch the catalogue could propose thresholds forever and
    the labels would answer "no difference" every time.

    A quality gain is checked for replication first (round 39, rule `replicate`): the pooled recall@b is
    one division over every recording, so a candidate that lifts one recording and lowers another can
    still raise it. `_replication_loss` names the recording that fell and the gain is refused as one
    recording's improvement — the deployment runs on all of them.

    The label check reads the *at-budget* pair when both sides have one. A fixed absolute threshold on
    an uncalibrated answer is not a shared yardstick: on SMD every answer lands between 0.30 and 0.47,
    so a cut at the shipped 0.5 reports recall 0 for a detector whose ranking is 0.75 AUC, and a
    candidate that shifted the probabilities without changing the ranking would look like an
    improvement. Comparing at a common false-positive budget asks the question the label column is
    meant to answer: can this configuration find more incidents for the same number of false alarms?
    """
    if veto:
        return Verdict(trial.candidate, False, "veto", veto)

    if trial.promise is not None:
        # A reshaping is judged against the promise, not against the baseline. Two outcomes, and both
        # say something the operator needs: either the promise is restored on this sampling, or it is
        # not — and then what looked like a stable claim was partly a property of how the stream was
        # sampled. Returning here is deliberate: letting a reshaping fall through to the quality branch
        # would accept it for beating a baseline it was never meant to beat.
        measured = trial.recall_at_budget
        promise = trial.promise
        if measured is None:
            return Verdict(trial.candidate, False, "reshape",
                           "the reshape has no at-budget recall to compare with the promised %.3f"
                           % promise)
        if measured >= promise - rules.recall_tolerance:
            return Verdict(trial.candidate, True, "reshape",
                           "restores the promised %.3f at the %.0f%% budget: %.3f on this sampling"
                           % (promise, 100 * trial.budget, measured))
        return Verdict(trial.candidate, False, "reshape",
                       "reaches %.3f of the promised %.3f at the %.0f%% budget, so part of the claim "
                       "was a property of how the stream was sampled: this restores comparability, not "
                       "the promise, and the deployment stands"
                       % (measured, promise, 100 * trial.budget))

    if trial.fpr is not None and trial.fpr > rules.deploy_fpr_ceiling + 1e-9:
        # A candidate that changes what the shipped threshold would alert on has to land inside the
        # ceiling: it is making a deployability claim of its own. A candidate that leaves the deployed
        # alerting outcome *identical* is inheriting a defect of the deployment, not introducing one —
        # on SMD the shipped 0.33 alerts on every window, so a pure prompt-budget cut inherits fpr 1.000
        # by doing nothing to the scoring path. Blaming it for that would reject every cost change on a
        # domain whose deployment is already broken, which is the opposite of useful. The inherited
        # defect is printed as a warning instead.
        inherited = (
            baseline.fpr is not None
            and abs(trial.fpr - baseline.fpr) <= rules.deploy_fpr_tolerance
            and (baseline.recall is None or trial.recall is None
                 or abs(trial.recall - baseline.recall) <= rules.deploy_fpr_tolerance)
        )
        if not inherited:
            return Verdict(trial.candidate, False, "deploy",
                           "would alert on %.0f%% of windows at its shipped threshold (the deployed "
                           "configuration: %.0f%%), above the %.0f%% deploy ceiling"
                           % (100 * trial.fpr,
                              100 * baseline.fpr if baseline.fpr is not None else float("nan"),
                              100 * rules.deploy_fpr_ceiling))

    if baseline.recall_at_budget is not None and trial.recall_at_budget is not None:
        if trial.recall_at_budget < baseline.recall_at_budget - rules.recall_tolerance:
            return Verdict(trial.candidate, False, "recall",
                           "recall at %.0f%% false positives %.3f -> %.3f"
                           % (100 * baseline.budget, baseline.recall_at_budget,
                              trial.recall_at_budget))
        # Inside a fixed budget the false-positive rate is a *permission*, not a score to minimise:
        # the sweep already guarantees it cannot exceed the budget, so rejecting every increase would
        # reject a detector that finds three times as many incidents for four more percentage points of
        # a budget it was allowed to spend. Spending more of the budget without finding more incidents
        # is still a regression, because those are pages somebody has to answer.
        if trial.fpr_at_budget is not None and trial.fpr_at_budget > rules.fpr_budget + 1e-9:
            return Verdict(trial.candidate, False, "fpr",
                           "false-positive rate %.3f exceeds the %.0f%% budget"
                           % (trial.fpr_at_budget, 100 * rules.fpr_budget))
        quality_gain = (
            trial.recall_at_budget >= baseline.recall_at_budget * (1.0 + rules.min_quality_gain)
        )
        if (not quality_gain
                and trial.fpr_at_budget is not None
                and baseline.fpr_at_budget is not None
                and trial.fpr_at_budget > baseline.fpr_at_budget + rules.fpr_tolerance):
            return Verdict(trial.candidate, False, "fpr",
                           "spends %.3f of the %.0f%% false-positive budget against %.3f without "
                           "finding more incidents: recall %.3f -> %.3f"
                           % (trial.fpr_at_budget, 100 * rules.fpr_budget,
                              baseline.fpr_at_budget, baseline.recall_at_budget,
                              trial.recall_at_budget))
    else:
        if baseline.recall is not None and trial.recall is not None:
            if trial.recall < baseline.recall - rules.recall_tolerance:
                return Verdict(trial.candidate, False, "recall",
                               "recall %.3f -> %.3f" % (baseline.recall, trial.recall))
        if baseline.fpr is not None and trial.fpr is not None:
            if trial.fpr > baseline.fpr + rules.fpr_tolerance:
                return Verdict(trial.candidate, False, "fpr",
                               "false-positive rate %.3f -> %.3f" % (baseline.fpr, trial.fpr))

    lost = sorted(
        key for key, value in trial.coverage.items()
        if value < baseline.coverage.get(key, 1.0) - 1e-9
    )
    if lost:
        detail = ", ".join("%s %.0f%%" % (key, 100 * trial.coverage[key]) for key in lost)
        return Verdict(trial.candidate, False, "evidence",
                       "keeps less evidence: %s" % detail)

    if deployable(trial, rules) and not deployable(baseline, rules):
        # The one move that no other column can see. A candidate that only moves a threshold leaves
        # the at-budget curve identical, because the loop sweeps its own cut; the change exists only
        # in the configuration that would be deployed. Without this branch the catalogue could
        # propose all the thresholds it liked and the labels would answer "no difference" every time.
        # It does not license an unbounded bill: fixing deployability at three times the cost is the
        # kind of trade a Pareto move excludes, and the version file would record the bill either way.
        affordable = (
            baseline.tokens_per_report is None
            or trial.tokens_per_report is None
            or trial.tokens_per_report <= baseline.tokens_per_report * (1.0 + rules.cost_tolerance)
        )
        if affordable:
            return Verdict(trial.candidate, True, "operational",
                           "the recorded configuration goes from %.0f alerts per 1,000 windows with "
                           "recall %.3f to %.0f per 1,000 with recall %.3f: at or below the %.0f%% "
                           "deploy ceiling and no worse than the cut the budget rule would pick"
                           % (alerts_per_1000(baseline), baseline.recall,
                              alerts_per_1000(trial), trial.recall,
                              100 * rules.deploy_fpr_ceiling))

    before, after = baseline.tokens_per_report, trial.tokens_per_report
    if (baseline.recall is not None and trial.recall is not None
            and trial.recall < baseline.recall - rules.recall_tolerance
            and before is not None and after is not None
            and after <= before * (1.0 - rules.min_gain)):
        # A real saving plus a lower shipped recall, and the at-budget curve did not rise — every path
        # that could legitimately pay for a smaller bill has already returned above. The candidate is
        # buying its bill with silence, and the token ledger alone would reward it. Checked before the
        # deployability regression below because a saving names the motive: the bill is what changed.
        return Verdict(trial.candidate, False, "silence",
                       "pays for a %.0f%% smaller bill with silence: shipped recall %.3f -> %.3f while "
                       "recall at the %.0f%% false-positive budget did not rise"
                       % (100 * (1.0 - after / before), baseline.recall, trial.recall,
                          100 * rules.fpr_budget))

    trial_shortfall = own_cut_shortfall(trial)
    if (trial_shortfall > rules.operational_tolerance
            and own_cut_shortfall(baseline) <= rules.operational_tolerance):
        # The mirror image of the operational branch: the recorded configuration was at least as good as
        # the cut the loop computes, and the candidate makes it materially worse. `anomaly-p-40` is this
        # case on SMD: the shipped 0.33 pages on everything, but at least finds every incident, and
        # raising the bar to 0.40 keeps 27 alerts per 1,000 windows and 0.7% of the incidents. Saving 26
        # tokens a report by not paging on failures is not a Pareto move, so this is stated against the
        # same column that acceptance uses, not against a generic "no gain".
        #
        # The size of the shortfall matters, which is why this is not `not beats_own_cut(trial)`: round 14's
        # rolling-rank candidate finds 0.216 where the sweep finds 0.224 and was rejected over 3.6% of the
        # free cut's recall while finding 3.7x what the baseline found. A fitted cut is not exactly optimal
        # on a finite sample, so a candidate may land `operational_tolerance` short of it.
        return Verdict(trial.candidate, False, "operational",
                       "the recorded configuration pages on %.0f%% fewer of the failures the cut the budget "
                       "rule already computes would find: shipped recall %.3f against %.3f at the %.0f%% "
                       "budget, past the %.0f%% shortfall tolerated"
                       % (100 * trial_shortfall, trial.recall,
                          trial.recall_at_budget if trial.recall_at_budget is not None else 0.0,
                          100 * rules.fpr_budget, 100 * rules.operational_tolerance))

    before_recall = baseline.recall_at_budget
    after_recall = trial.recall_at_budget
    quality_gain = (
        before_recall is not None
        and after_recall is not None
        and after_recall >= before_recall * (1.0 + rules.min_quality_gain)
    )
    if quality_gain:
        # A pooled gain is one division over every recording's incidents, so it can be one host's
        # improvement (round 39: `host-features-3` lifts SMD's machine-2-2 from 0.037 to 0.220 and lowers
        # machine-1-6 from 0.491 to 0.382 for a +5% pooled reading). The deployment runs on both, so the
        # gain has to replicate before it is allowed to name itself one. The bar is `replication_bar`,
        # which defaults to the same significance the acceptance needed and can be set per domain.
        spread = replication_loss(baseline, trial, rules.replication_bar)
        if spread is not None:
            return Verdict(trial.candidate, False, "replicate",
                           _replication_sentence(spread), detail=spread)
    if before is None or after is None:
        # No report on one side means no alert on that side, so there is no bill to compare. That is
        # the normal case on a domain where the shipped threshold silences the detector: the whole cost
        # ledger is empty. Refusing to decide would make the loop blind to the only change that can
        # bring such a domain back, so the label column alone decides when it moves enough.
        if quality_gain:
            return Verdict(trial.candidate, True, "quality",
                           "no report on either side, so cost is undefined; recall at %.0f%% "
                           "false positives %.3f -> %.3f"
                           % (100 * baseline.budget, before_recall, after_recall))
        side = "baseline" if before is None else "candidate"
        return Verdict(trial.candidate, False, "no-reports",
                       "the %s run produced no report, so tokens/report is undefined; "
                       "a cost comparison needs at least one alert on each side" % side)
    if before and after <= before * (1.0 - rules.min_gain):
        return Verdict(trial.candidate, True, "cost",
                       "%.0f -> %.0f tokens/report (%.1f%%)"
                       % (before, after, 100 * (after / before - 1.0)))
    if quality_gain and after <= before * (1.0 + rules.cost_tolerance):
        return Verdict(trial.candidate, True, "quality",
                       "recall at %.0f%% false positives %.3f -> %.3f for %.0f -> %.0f "
                       "tokens/report"
                       % (100 * baseline.budget, before_recall, after_recall, before, after))
    before_incident = tokens_per_incident(baseline)
    after_incident = tokens_per_incident(trial)
    recall_holds = (
        before_recall is None
        or after_recall is None
        or after_recall >= before_recall - rules.recall_tolerance
    )
    # A cheaper incident bill can also be bought by paging on fewer of them: fewer alerts means fewer
    # reports, a smaller total bill and — because `recall_at_budget` is invariant under a
    # threshold-only change — a per-incident number that looks better while the deployment misses
    # failures. The audit of round 21's stored round showed exactly that (`deviation-bar-free`
    # gain -> cost, shipped recall 0.806 -> 0.211), so the shipped column has to hold too.
    paging_holds = (
        baseline.recall is None
        or trial.recall is None
        or trial.recall >= baseline.recall - rules.recall_tolerance
    )
    if (recall_holds and paging_holds and before_incident and after_incident
            and after_incident <= before_incident * (1.0 - rules.min_gain)):
        # The unit a detector is paid in is the incident it finds, not the report it writes. When the
        # bill per incident falls by the same bar the report bill is judged by, and recall did not fall,
        # the change is cheaper in the only currency that survives the report count changing
        # (round 24; the branch was reachable in principle since round 19 but only ran when recall rose
        # by the quality bar, so a 3% recall gain bought for a 1% bill rise was called a rejection).
        return Verdict(trial.candidate, True, "cost",
                       "the bill per incident it finds falls %.0f -> %.0f tokens with recall at the "
                       "%.0f%% budget %.3f -> %.3f, even though the bill per report moves "
                       "%.0f -> %.0f"
                       % (before_incident, after_incident, 100 * baseline.budget,
                          before_recall if before_recall is not None else 0.0,
                          after_recall if after_recall is not None else 0.0, before, after))
    if (quality_gain and before_incident and after_incident
            and after_incident <= before_incident * (1.0 - rules.min_quality_gain)):
        # The bill per report may rise and the bill per *incident* still fall, because the change
        # finds more incidents. Refusing this would price a detection gain against the wrong unit:
        # round 19's service-domain reader raised tokens/report by 5.3% while recall went 0.068 ->
        # 0.860, and the per-report bar alone called that "costing more".
        return Verdict(trial.candidate, True, "quality",
                       "recall at %.0f%% false positives %.3f -> %.3f, and the bill per incident "
                       "it finds falls %.0f -> %.0f tokens even though the bill per report rises "
                       "%.0f -> %.0f"
                       % (100 * baseline.budget, before_recall, after_recall,
                          before_incident, after_incident, before, after))
    if quality_gain:
        return Verdict(trial.candidate, False, "cost",
                       "recall improves but the bill rises %.0f -> %.0f tokens/report, more than "
                       "the %.0f%% a quality gain may cost, and the bill per incident it finds "
                       "does not fall by %.0f%%"
                       % (before, after, 100 * rules.cost_tolerance,
                          100 * rules.min_quality_gain))
    return Verdict(trial.candidate, False, "gain",
                   "%.0f -> %.0f tokens/report is below the %.0f%% bar and recall at the budget "
                   "did not rise by %.0f%%"
                   % (before, after, 100 * rules.min_gain, 100 * rules.min_quality_gain))


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------
@dataclass
class RoundResult:
    """One pass of the loop, with everything needed to reproduce or reverse a decision."""

    started_at: str
    finished_at: str
    stream: Dict[str, Any]
    rules: EvolveRules
    baseline: Measurement
    trials: List[Tuple[Candidate, Measurement, Verdict]] = field(default_factory=list)
    ceiling: Dict[str, Any] = field(default_factory=dict)
    proposal: Dict[str, Any] = field(default_factory=dict)
    """Which proposal rule produced this round's candidate list, and why.

    Recorded for the same reason the ruleset is: "the mechanism changed" is a claim, and without the
    mode, the space fingerprint and the stated reason next to the numbers, two rounds that differ cannot
    be told apart from two rounds that were asked different questions.
    """

    references: Dict[str, Measurement] = field(default_factory=dict)
    """Baselines measured at *other* windows, keyed by the window.

    Round 31: a candidate that moves `digest.rolling_window` changes what a window means, so it cannot be
    credited against the baseline measured at the old geometry -- that credits the geometry change rather
    than the change the candidate makes. On a stream sampled differently from the one the deployment's
    claim was made on, the whole "gain" can be the geometry: on SMD's 1.49x stream the in-force version
    reads 0.297 at window 200 and 0.427 at the comparable window 286, and a candidate that merely moved
    the window was accepted for 0.297 -> 0.427 while changing nothing about the promise.
    """

    claim: Dict[str, Any] = field(default_factory=dict)
    """What the configuration in force promised, and whether this stream can test it.

    Filled by the caller (the round cannot ask `promote` without a circular import): the version, the
    promise, what this round measured, the verdict and its reason. A round whose baseline cannot be
    compared with the promise is still a valid measurement of *this sampling*, but it is not evidence
    about the promise, and the record has to say so.
    """

    stream_path: str = ""
    """The stream file this round measured, as the caller named it (round 46).

    `stream` is a summary (source, windows, scored, cadence) and does not identify a file: `SMD` is two
    files in this repo, sampled differently, and the same dataset name would otherwise let a later check
    measure a different sampling and call the difference a result (rounds 26, 44).
    """

    @property
    def accepted(self) -> List[str]:
        return [verdict.candidate for _c, _m, verdict in self.trials if verdict.accepted]

    queue: Dict[str, Any] = field(default_factory=dict)
    """What this round paid, when it was run as one entry of the re-run queue (round 58).

    The queue drains several rounds in one command, and each of them is a separate measurement. The record
    of the queue's own work — which domain this round paid down and which domain was the largest payable
    debt when it started — belongs in the round it paid, because that is the file a later reader opens; a
    printed summary lives only in the terminal that happened to run it.
    """

    limits: Dict[str, Any] = field(default_factory=dict)
    """The wait ceilings this round was judged under, and where each came from (round 68).

    A ceiling does not live in `EvolveRules`, so `rules_hash` cannot see it change: the same rulebook, the
    same measurements, and a different verdict, because somebody tightened a wait in a deployment state. A
    record that cannot say which ceilings it was judged under leaves that difference invisible to `--audit`
    — which is the one place built to notice that a stored verdict no longer matches the rules.
    """

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stream": self.stream,
            # Round 46: the summary says which dataset was measured, not which file it came from, and two
            # files can hold the same dataset sampled differently (`data/smd/stream.json` is stride 10,
            # `data/smd/stream-check.json` is stride 7). A record that cannot name its own file leaves the
            # next check depending on the operator's memory.
            "stream_path": self.stream_path or None,
            "rules": self.rules.to_dict(),
            # A round is evidence *for a ruleset*. Storing the numbers without saying which rules judged
            # them produces a record that looks authoritative and cannot be audited, which is the failure
            # the survey calls "no versioned infrastructure": traces exist, validation results exist, and
            # nothing ties them to the version of the objective that produced them.
            "rules_hash": rules_fingerprint(self.rules),
            "baseline": self.baseline.to_dict(),
            "trials": [
                {"candidate": candidate.to_dict(), "measurement": measurement.to_dict(),
                 "verdict": verdict.to_dict()}
                for candidate, measurement, verdict in self.trials
            ],
            "accepted": self.accepted,
            "ceiling": self.ceiling,
            "references": {key: measurement.to_dict()
                           for key, measurement in sorted(self.references.items())},
            "claim": self.claim or None,
            # Absent in rounds written before the mechanism became mutable; `None` says "this round was
            # proposed by whatever the code did at the time", which is exactly what a reader should
            # conclude about them.
            "proposal": self.proposal or None,
            "queue": dict(self.queue) or None,
            # Absent in rounds written before ceilings were recorded; `None` there means "this round did not
            # say", which is what a reader should conclude about them (boundary 147).
            "limits": dict(self.limits) or None,
        }


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def rules_fingerprint(rules: EvolveRules) -> str:
    """A short, stable hash of the objective a round was judged under.

    Short because it is printed next to a round id in a table, stable because `to_dict` is sorted, and
    derived from the rule *values* rather than from the file mtime — two identical rulesets must produce
    the same fingerprint no matter when they were written.
    """
    blob = json.dumps(rules.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def configuration_window(settings: Settings, candidate: Optional[Candidate] = None) -> Optional[int]:
    """The rolling window this configuration measures with, or `None` when it does not roll.

    One place decides what "the same geometry" means: the settings as they would be after applying the
    candidate. Comparing window counts across configurations is the same mistake as comparing them
    across streams (round 27), because the window *is* how much traffic a sample covers.
    """
    applied = candidate.apply(settings) if candidate is not None else settings
    return getattr(applied.digest, "rolling_window", None)


def _reference_candidate(window: int) -> Candidate:
    """The deployment restricted to a different window: the baseline a geometry change is measured against."""
    return Candidate(
        name="baseline@%d" % window,
        hypothesis="the deployment at the window a candidate changes to, so the comparison does not "
                   "credit the geometry change to the candidate",
        section="digest", field="rolling_window", value=window,
    )


def recorded_reference(payload: Mapping[str, Any], measurement: Measurement,
                       baseline: Measurement) -> Optional[Measurement]:
    """The baseline a stored trial was compared with, or `None` when the record cannot say.

    Round 32. `run_round` compares a candidate against a reference measured at the candidate's own
    window (round 31) and stores those references in the round file. A replay that reads only
    `baseline` compares the trial against a measurement taken at *another* window, and the geometry
    shows up as a rule change: the round-31 record was rejudged this way and reported "3 verdict(s)
    changed" while not one rule had moved.

    Rounds before the geometry was recorded carry no `window`. If such a record also carries no
    references it was decided against `baseline`, and replaying it that way is honest. But a round
    written between round 31 and round 32 stored *references* without recording which trial used
    which: the reference is there, the link is not. Filling that gap with `baseline` is a guess that
    puts the geometry back into the comparison — rejudging the round-31 record under today's rules
    still reports three verdicts "changed" with identical rules. So an unlabelled trial in a record
    that has references is withheld instead of guessed.
    """
    references = payload.get("references") or {}
    window = measurement.window
    if window is None:
        return None if references else baseline
    stored = references.get(str(window))
    if stored is not None:
        return Measurement.from_dict(stored)
    if baseline.window is None or baseline.window == window:
        return baseline
    return None


def run_round(settings: Settings, streams: Sequence[Stream],
              selected: Optional[Sequence[Candidate]] = None,
              rules: Optional[EvolveRules] = None,
              source: str = "demo",
              on_candidate: Optional[Callable[[Candidate, Measurement, Verdict], None]] = None,
              proposal: Optional[Dict[str, Any]] = None,
              claim: Optional[Mapping[str, Any]] = None,
              ) -> RoundResult:
    """One full pass: build the reference pool, measure the baseline, then each candidate.

    Everyone is measured against the *same* recordings and the *same* evidence pool, so a candidate
    cannot improve its own score by changing what it is scored on. `source` records where the
    recordings came from, because "accept" against a hand-written suite and "accept" against
    fault-injection data are different claims and the version file must not conflate them.

    `proposal` is the record of *how* `selected` was arrived at (`propose.Proposal.to_dict()`). The
    round does not need it to measure anything, which is the point: the mechanism that chooses what to
    propose stays outside the measurement, so a different policy changes the question and never the
    answer.
    """
    started = _now()
    active_rules = rules if rules is not None else EvolveRules()
    chosen = list(selected) if selected is not None else catalogue()
    pool = build_pool(settings, streams)
    ceiling = state_ceiling(settings, streams)
    baseline = measure(settings, None, streams, pool, budget=active_rules.fpr_budget,
                       alert_budget=active_rules.alert_budget)

    base_window = configuration_window(settings)
    references: Dict[str, Measurement] = {}

    def baseline_for(candidate: Candidate) -> Measurement:
        """The baseline this candidate must be compared against.

        Same geometry: the deployment as it stands. Different window: the deployment *at that window*,
        measured once per distinct window and kept in the record. A candidate is then credited only with
        what it adds on the geometry both numbers share.
        """
        window = configuration_window(settings, candidate)
        if window is None or window == base_window:
            return baseline
        key = str(window)
        if key not in references:
            references[key] = measure(settings, _reference_candidate(window), streams, pool,
                                      budget=active_rules.fpr_budget,
                                      alert_budget=active_rules.alert_budget)
        return references[key]

    trials: List[Tuple[Candidate, Measurement, Verdict]] = []
    for candidate in chosen:
        trial = measure(settings, candidate, streams, pool, budget=active_rules.fpr_budget,
                        alert_budget=active_rules.alert_budget)
        verdict = decide(baseline_for(candidate), trial, active_rules, veto=candidate.veto)
        trials.append((candidate, trial, verdict))
        if on_candidate is not None:
            on_candidate(candidate, trial, verdict)

    return RoundResult(
        started_at=started,
        finished_at=_now(),
        stream={"source": source, **summarise(streams)},
        rules=active_rules,
        baseline=baseline,
        trials=trials,
        ceiling=ceiling,
        proposal=dict(proposal or {}),
        references=references,
        claim=dict(claim or {}),
    )


def persist(root: Path, result: RoundResult, *, write_versions: bool = True) -> Dict[str, Any]:
    """Write the round, the append-only ledger line, and (optionally) a version per accepted change.

    The round is recorded even when nothing is accepted and even when nothing is applied. A record
    that only exists for accepted changes would let the next round re-propose a settled question,
    which is precisely the "scientific amnesia" the survey names: behaviour improves across campaigns
    while no methodological knowledge accumulates. `write_versions=False` therefore records the
    measurement without claiming the change was integrated — deciding and integrating are different
    facts, and the ladder grades them separately.

    The version file exists for the rollback requirement in both surveys' open problems: a change that
    cannot be named, diffed and reversed is not a persisted improvement, it is a drift. Each file holds
    the exact environment assignment that reproduces the configuration, because that is how this service
    is configured in deployment.
    """
    directory = root / EVOLVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    versions = directory / "versions"
    versions.mkdir(exist_ok=True)

    stamp = result.started_at.replace(":", "").replace("-", "").replace("+0000", "Z")
    round_path = directory / ("round-%s.json" % stamp)
    # Round 73: two rounds that start in the same second must still be two records. `started_at` is written to
    # the second, so a queue that re-measures two rounds back to back used to overwrite the first successor --
    # and the retirement of the first round then named a file whose content was the second round's. A suffix
    # keeps both; the ledger points at the file by name, so nothing downstream depends on the name's shape.
    suffix = 2
    while round_path.exists():
        round_path = directory / ("round-%s-%d.json" % (stamp, suffix))
        suffix += 1
    round_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n",
                          encoding="utf-8")

    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "round": round_path.name,
            "started_at": result.started_at,
            "accepted": result.accepted,
            "rejected": [v.candidate for _c, _m, v in result.trials if not v.accepted],
            "applied": list(result.accepted) if write_versions else [],
            "rules_hash": rules_fingerprint(result.rules),
            "baseline_tokens_per_report": result.baseline.to_dict()["tokens_per_report"],
        }) + "\n")

    written: List[str] = []
    # Refusals are written whatever `write_versions` says: `--apply` decides whether a *change* is deployed,
    # and a refusal deploys nothing. Leaving them out of the record family would keep "why wasn't this
    # promoted" answerable only by reading one round's file (round 42).
    refusals = _write_withheld(root, directory, round_path.name, result)
    # Round 43: the same call writes the other half of the family — a candidate that already had a record and
    # was measured again gets a check appended to it. Doing it here rather than in the CLI keeps a scripted
    # round as well documented as an interactive one.
    checks = record_withheld_checks(root, result, result.rules, skip_round=round_path.name,
                                    round_name=round_path.name)
    if not write_versions:
        return {"round": str(round_path), "versions": written, "withheld": refusals, "checks": checks}
    existing = sorted(versions.glob("v*.json"))
    version = 0
    for path in existing:
        try:
            version = max(version, int(path.stem.lstrip("v")))
        except ValueError:
            continue
    for candidate, measurement, verdict in result.trials:
        if not verdict.accepted:
            continue
        version += 1
        exports = ["%s=%s" % (ENV_VARS.get((section, field), "?"), _env_value(value))
                   for section, field, value in candidate.changes()]
        unset = "  ".join("unset %s" % ENV_VARS.get((section, field), "?")
                          for section, field, _value in candidate.changes())
        payload = {
            "version": version,
            "created_at": result.finished_at,
            "candidate": candidate.to_dict(),
            "change": candidate.describe(),
            # A joint change is exported as one line per variable: applying half of it would ship a
            # configuration nobody measured, which is exactly what a version file must prevent.
            "env": "; ".join(exports),
            "measurement": measurement.to_dict(),
            # The stream the promise was measured on: verifying on a different one is a transfer test,
            # not a repetition, and the reader of the record has to be able to tell them apart.
            "stream": dict(result.stream),
            # Round 47: the summary says which dataset, not which file -- and `SMD` is two files here,
            # sampled differently (828 scored windows at 3000 s, 1235 at 2100 s). Without the path,
            # `--verify` can only stand on the promise's stream if the operator remembers it, so a version
            # whose check comes back `unmeasurable` may be reporting the operator's choice, not the claim.
            "stream_path": result.stream_path or None,
            "verdict": verdict.to_dict(),
            "rollback": unset + "  # each restores one default",
        }
        path = versions / ("v%03d.json" % version)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return {"round": str(round_path), "versions": written, "withheld": refusals, "checks": checks}


def _next_withheld_number(folder: Path) -> int:
    number = 0
    for path in folder.glob("w*.json"):
        try:
            number = max(number, int(path.stem.lstrip("w")))
        except ValueError:
            continue
    return number


def _write_withheld(root: Path, directory: Path, round_name: str,
                    result: RoundResult) -> List[str]:
    """Write one record per `replicate` refusal, numbered `w###` so it cannot be read as a shipped version.

    Round 43: a candidate that already has a record is **not** given a second one. Measuring a refused
    candidate again is a new observation of the same objection, so it belongs in that record's `checks`
    (written by `record_withheld_checks`); numbering it again would give "why wasn't this promoted" two
    answers and make the family's count a count of measurements rather than of refused candidates.
    """
    refusals = [(candidate, measurement, verdict) for candidate, measurement, verdict in result.trials
                if verdict.rule == "replicate"]
    if not refusals:
        return []
    folder = directory / WITHHELD_DIR
    folder.mkdir(exist_ok=True)
    # `root` is passed rather than derived: `directory.parent` is `var`, not the workspace root, because
    # EVOLVE_DIR has two segments — the first version of this fix looked in `var/var/evolve` and found
    # nothing, so every re-measurement still minted a new number (round 43).
    already = {record.name for record in withheld_records(root)}
    number = _next_withheld_number(folder)
    bar = result.rules.replication_bar
    written: List[str] = []
    for candidate, measurement, verdict in refusals:
        if candidate.name in already:
            continue
        number += 1
        payload = Withheld(
            number=number,
            round=round_name,
            created_at=result.finished_at,
            stream=dict(result.stream),
            candidate=candidate.to_dict(),
            changes=[list(change) for change in candidate.changes()],
            bar=bar,
            loss=dict(verdict.detail or {}),
            measured=measurement.to_dict(),
            reason=verdict.reason,
            rule=verdict.rule,
            stream_path=result.stream_path,
        ).to_dict()
        path = folder / ("w%03d.json" % number)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


def withheld_check(baseline: Measurement, measurement: Measurement,
                   rules: EvolveRules, *, checked_at: str, stream: Mapping[str, Any],
                   round_name: str = "") -> Dict[str, Any]:
    """The check a re-measurement of a refused candidate writes into its record (rounds 43, 44).

    Shared by the round's write-back and by `--verify-withheld` so the two paths cannot disagree about what
    "still refused" means. A measurement without per-recording numbers is `unknown`: writing "no longer"
    there would claim the refusal died on evidence nobody took (boundary 72 at the level of a re-check).
    """
    loss = replication_loss(baseline, measurement, rules.replication_bar)
    comparable = sum(1 for value in (measurement.recall_by_recording or {}).values()
                     if value is not None) >= 2
    if not comparable:
        verdict_name = "unknown"
        reason = ("this measurement does not say how each recording did, so it cannot test an objection "
                  "about one of them")
    elif loss:
        verdict_name, reason = "still", replication_sentence(loss)
    else:
        verdict_name = "no longer"
        reason = ("no recording fell by more than %.3f at the %.0f%% budget, so the objection no longer "
                  "holds" % (rules.replication_bar, 100 * rules.fpr_budget))
    return {
        "checked_at": checked_at,
        "stream": dict(stream),
        "bar": rules.replication_bar,
        "round": round_name,
        "verdict": verdict_name,
        "loss": dict(loss or {}),
        "reason": reason,
        "recall_at_budget": measurement.recall_at_budget,
        "tokens_per_report": measurement.tokens_per_report,
    }


def withheld_evidence_age(record: Withheld, now: Optional[datetime] = None) -> Optional[float]:
    """How old a refusal's most recent evidence is, in days (round 44).

    The last check if there is one, else the day the refusal was written: a record that was re-measured
    recently is not stale no matter how old the refusal is.
    """
    last = record.last_check or {}
    return _refusal_age_days(str(last.get("checked_at") or record.created_at or ""), now)


def withheld_due(records: Sequence[Withheld], now: Optional[datetime] = None,
                 expiry_days: float = REFUSAL_EXPIRY_DAYS) -> List[Withheld]:
    """The refusals whose evidence is older than the horizon (round 44).

    A record whose latest check already says `no longer` is not due: its objection has been retired, and what
    remains is a candidate a round must consider, not a refusal to re-test.
    """
    due: List[Withheld] = []
    for record in records:
        last = record.last_check or {}
        if str(last.get("verdict") or "") == "no longer":
            continue
        age = withheld_evidence_age(record, now)
        if age is not None and age > float(expiry_days):
            due.append(record)
    return due


def record_withheld_checks(root: Path, result: RoundResult, rules: EvolveRules,
                          skip_round: Optional[str] = None,
                          round_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Append this round's measurement of every already-refused candidate to its record (round 43).

    The write-back is what turns a refusal from a memory into a fact: without it the `w###` record answers
    "why wasn't this promoted" with a number from a round that predates the question, and the only way to
    notice that an objection stopped holding is to read a round file nobody opens.

    `skip_round` names the round whose measurements *created* the record, so a refusal is not immediately
    filed as a re-check of itself.
    """
    measured = {candidate.name: (candidate, measurement, verdict)
                for candidate, measurement, verdict in result.trials}
    written: List[Dict[str, Any]] = []
    for record in withheld_records(root):
        if record.name not in measured:
            continue
        if skip_round and record.round == skip_round:
            continue
        _candidate, measurement, _verdict = measured[record.name]
        check = withheld_check(result.baseline, measurement, rules,
                               checked_at=result.finished_at, stream=result.stream,
                               round_name=round_name or "")
        if record_withheld_check(root, record.name, check) is not None:
            written.append({"name": record.name, "label": record.label, "verdict": check["verdict"]})
    return written


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


@dataclass
class Withheld:
    """A candidate the loop measured and refused, written like a version it decided not to ship (round 42).

    The refusal is a claim in the same sense a version is: it says this candidate loses this much on this
    recording against this bar. Its number is `w###` so it can never be mistaken for a version that shipped.

    `loss` is `Verdict.detail` from `replication_loss`: which recording fell, by how much, and the bar that
    judged it. A record without it (a refusal written before round 40) can still be read, but its objection
    cannot be checked, and saying so is better than pretending the row is empty.
    """

    number: int
    round: str
    created_at: str
    stream: Dict[str, Any] = field(default_factory=dict)
    candidate: Dict[str, Any] = field(default_factory=dict)
    changes: List[List[Any]] = field(default_factory=list)
    bar: Optional[float] = None
    loss: Dict[str, Any] = field(default_factory=dict)
    measured: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    rule: str = "replicate"
    # Every later measurement of the same candidate, oldest first (round 43). A refusal that is re-measured
    # must record what the re-measurement said, or the record answers "why wasn't this promoted" with a
    # number from a round that predates the configuration it is being asked about.
    checks: List[Dict[str, Any]] = field(default_factory=list)
    # The stream file, not just the dataset name (round 46). `--verify-withheld` with no arguments finds
    # each refusal's own stream from here; a record written before this field can only be re-checked by an
    # operator who names the file, which is what boundary 101 is about.
    stream_path: str = ""

    @property
    def domain(self) -> str:
        return str((self.stream or {}).get("source") or "(unattributed)")

    @property
    def name(self) -> str:
        return str((self.candidate or {}).get("name") or "?")

    @property
    def label(self) -> str:
        return "w%03d" % self.number

    @property
    def last_check(self) -> Optional[Dict[str, Any]]:
        return dict(self.checks[-1]) if self.checks else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "withheld": self.number,
            "round": self.round,
            "created_at": self.created_at,
            "stream": dict(self.stream),
            "candidate": dict(self.candidate),
            "changes": [list(change) for change in self.changes],
            "bar": self.bar,
            "loss": dict(self.loss),
            "measured": dict(self.measured),
            "reason": self.reason,
            "rule": self.rule,
            "checks": [dict(check) for check in self.checks],
            "stream_path": self.stream_path or None,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Withheld":
        bar = payload.get("bar")
        return cls(
            number=int(payload.get("withheld") or 0),
            round=str(payload.get("round") or ""),
            created_at=str(payload.get("created_at") or ""),
            stream=dict(payload.get("stream") or {}),
            candidate=dict(payload.get("candidate") or {}),
            changes=[list(change) for change in (payload.get("changes") or [])],
            bar=None if bar is None else float(bar),
            loss=dict(payload.get("loss") or {}),
            measured=dict(payload.get("measured") or {}),
            reason=str(payload.get("reason") or ""),
            rule=str(payload.get("rule") or "replicate"),
            checks=[dict(check) for check in (payload.get("checks") or [])],
            stream_path=str(payload.get("stream_path") or ""),
        )


def withheld_records(root: Path) -> List[Withheld]:
    """Every refusal written by `persist`, oldest first. Missing directory means none were written."""
    folder = root / EVOLVE_DIR / WITHHELD_DIR
    if not folder.is_dir():
        return []
    records: List[Withheld] = []
    for path in sorted(folder.glob("w*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record = Withheld.from_dict(payload)
        if not record.round:
            record.round = path.stem
        records.append(record)
    return sorted(records, key=lambda item: item.number)


def withheld_streams(root: Path) -> Tuple[Dict[str, Path], List[Withheld]]:
    """Which stream file each refusing domain was measured on, and the records that do not say (round 46).

    The mapping is keyed by the domain as the record names it (`"SMD"`); callers slug it for display. A
    record written before `stream_path` existed lands in the second list: it can still be re-checked, but
    only by an operator who names the file, and pretending otherwise would let this command pick whatever
    stream happened to be configured and report the comparison as a re-check of the objection.
    """
    paths: Dict[str, Path] = {}
    missing: List[Withheld] = []
    for record in withheld_records(root):
        if not record.stream_path:
            missing.append(record)
            continue
        paths.setdefault(record.domain, Path(record.stream_path))
    return paths, missing


def record_withheld_check(root: Path, name: str, check: Mapping[str, Any]) -> Optional[Path]:
    """Append a re-measurement of `name` to the `w###` record that refused it (round 43).

    Only appends when the new check differs from the previous one in what it says about the candidate
    (`verdict`, `reason`, `loss`): re-running the same configuration on the same stream would otherwise pile
    up identical lines, which is how "4 earlier checks" came to mean three copies (round 28). Returns the
    path that was written, or None when there is no record for that name.
    """
    folder = root / EVOLVE_DIR / WITHHELD_DIR
    for path in sorted(folder.glob("w*.json")) if folder.is_dir() else []:
        try:
            record = Withheld.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if record.name != name:
            continue
        previous = record.last_check or {}
        signature = lambda item: (item.get("verdict"), item.get("reason"), json.dumps(item.get("loss") or {},
                                                                                      sort_keys=True))
        if previous and signature(previous) == signature(check):
            return None
        payload = record.to_dict()
        payload["checks"] = list(payload.get("checks") or []) + [dict(check)]
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path
    return None


def withheld_staleness(record: Withheld, rules: EvolveRules) -> Dict[str, Any]:
    """Does the objection still hold at today's bar? Answers with the recorded numbers, not a re-measure.

    The bar is a judgement stored with the refusal (round 40), so a later reader can disagree with it
    without measuring anything: if the recorded loss is now inside today's bar, the refusal was a judgement
    about a loss the objective no longer calls a loss. The answer is deliberately narrower than "would be
    accepted": the other branches still have to pass, and claiming otherwise would be the round-20 mistake
    (a bound reported as a decision).
    """
    drop = (record.loss or {}).get("drop")
    bar = rules.replication_bar
    # A re-measurement outranks the arithmetic on the record: it is a fresh measurement of the same
    # candidate at a bar that was recorded with it, and saying otherwise would prefer an old subtraction to
    # a new observation (round 43).
    last = record.last_check or {}
    if last:
        verdict = str(last.get("verdict") or "")
        if verdict == "no longer":
            return {"still": False, "comparable": True,
                    "note": "re-measured %s: %s" % (str(last.get("checked_at") or "?")[:10],
                                                    last.get("reason") or "the objection no longer holds")}
        if verdict == "still":
            return {"still": True, "comparable": True,
                    "note": "re-measured %s: %s" % (str(last.get("checked_at") or "?")[:10],
                                                    last.get("reason") or "the objection still holds")}
        # `unknown` falls through to the recorded arithmetic: a re-check that could not test the objection
        # leaves the objection exactly where it was.
    if drop is None:
        return {"still": True, "comparable": False, "note": "the record does not say how much was lost, so "
                                                           "this objection cannot be checked"}
    drop = float(drop)
    recorded = record.bar if record.bar is not None else None
    if drop >= bar:
        shown = "%.3f" % drop
        if recorded is None:
            return {"still": True, "comparable": True,
                    "note": "the loss was %s, still above today's bar of %.3f" % (shown, bar)}
        return {"still": True, "comparable": True,
                "note": "the loss was %s against a bar of %.3f; today's bar is %.3f, so it still holds"
                        % (shown, recorded, bar)}
    return {"still": False, "comparable": True,
            "note": "the loss was %.3f against a bar of %.3f; today's bar is %.3f, so this objection no "
                    "longer holds" % (drop, recorded if recorded is not None else bar, bar)}


def format_withheld(records: Sequence[Withheld], rules: Optional[EvolveRules] = None) -> str:
    active = rules if rules is not None else EvolveRules()
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - candidates measured and refused, written like versions not shipped")
    lines.append("=" * 96)
    lines.append("bar in force      %.3f" % active.replication_bar)
    lines.append("records           %d refusal(s) recorded" % len(records))
    if not records:
        lines.append("")
        lines.append("no refusal has been written down yet: `replicate` verdicts are recorded by the next "
                     "round that produces one.")
        return "\n".join(lines)
    stale = [record for record in records if not withheld_staleness(record, active)["still"]]
    lines.append("no longer hold    %d of them would not be refused at today's bar" % len(stale))
    if any(record.checks for record in records):
        # A refusal that was measured again is the only kind whose sentence is a fact rather than a memory
        # (round 43). Saying how many of them have been re-measured keeps the rest from borrowing their
        # credibility.
        lines.append("re-measured       %d of them have been measured again since the refusal"
                     % sum(1 for record in records if record.checks))
    described = sum(1 for record in records if record.stream_path)
    if described < len(records):
        # Round 46: `--verify-withheld` re-measures a record on the stream it was measured on, and a record
        # that does not name its file can only be re-checked by an operator who remembers it. Printing the
        # coverage keeps "no arguments needed" from reading as a property of the whole family.
        lines.append("stream files      %d of %d record(s) say which file they were measured on; the rest "
                     "need --withheld-stream" % (described, len(records)))
    lines.append("")
    header = "%-6s %-24s %-22s %8s %8s %s" % ("id", "candidate", "domain", "bar", "loss", "whose gain it was")
    lines.append(header)
    lines.append("-" * len(header))
    for record in records:
        loss = record.loss or {}
        lines.append("%-6s %-24s %-22s %8s %8s %s"
                     % (record.label, record.name[:24], record.domain[:22],
                        "%.3f" % record.bar if record.bar is not None else "-",
                        "%.3f" % float(loss["drop"]) if loss.get("drop") is not None else "-",
                        str(loss.get("recording") or "-")))
    lines.append("")
    for record in records:
        state = withheld_staleness(record, active)
        lines.append("%-6s %-24s %s" % (record.label, record.name[:24], state["note"]))
    return "\n".join(lines)


def history(root: Path, include_retired: bool = False) -> List[Dict[str, Any]]:
    """Every round recorded so far, oldest first, as the full round merged with its ledger line.

    The jsonl file is the index — it records order and survives a half-written round file — and the
    `round-*.json` file holds the measurements. Returning the merge means a caller that wants "what did
    the last round decide" does not have to know there are two files.

    Round 34: a retired round is skipped by default. Retiring is how a superseded record stops being
    evidence without being edited — the measurement stays exactly as it was measured, and what changes
    is whether the mechanism still has to answer for it.
    """
    directory = root / EVOLVE_DIR
    path = directory / "ledger.jsonl"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            index = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(index.get("round"))
        merged: Dict[str, Any] = {}
        round_file = directory / name
        if round_file.exists():
            try:
                merged = json.loads(round_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                merged = {}
        merged.update({key: value for key, value in index.items() if key != "round"})
        merged["round"] = name
        if not include_retired and merged.get("retired"):
            continue
        rows.append(merged)
    return rows


def retire(root: Path, name: str, re_measured_as: Optional[str] = None,
           at: Optional[str] = None) -> Dict[str, Any]:
    """Mark a recorded round superseded, without touching what it measured.

    The reachable alternative was worse in both directions. Back-filling the missing reference would
    hand that experiment a comparison it never made, and deleting the record would remove a measurement
    the campaign counts. Retiring keeps the record readable and takes it out of the checks: the debt a
    `--strict` run reports is a debt of the *current* ledger, and a round nobody will re-run is not one.

    The mark names the record that replaced it, because "superseded" without a successor is just a
    deletion with extra steps. Retiring twice is a no-op rather than an error: a re-run script must be
    safe to execute again.

    The successor is then checked against the record it replaces, because "re-measured as X" is a claim
    about candidates, not about files. A re-run that happens to measure one of the eight trials does not
    answer the other seven: they are questions the loop no longer asks, and the mark says which is which
    instead of letting a single shared candidate launder the whole record.
    """
    directory = root / EVOLVE_DIR
    path = directory / "ledger.jsonl"
    if not path.exists():
        raise ValueError("no ledger at %s" % path)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    target = name if name.endswith(".json") else "%s.json" % name
    moment = at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rewritten: List[str] = []
    mark: Optional[Dict[str, Any]] = None
    found = False
    for line in lines:
        try:
            index = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue
        recorded = str(index.get("round") or "")
        if recorded == target or recorded.startswith(target):
            found = True
            if index.get("retired"):
                mark = dict(index["retired"])
            else:
                # The overlap is computed before the mark is written, so the record states at the moment
                # of retirement how much of itself the successor actually re-measured.
                index["retired"] = {"at": moment,
                                    "re_measured_as": re_measured_as,
                                    "overlap": retirement_overlap(root, target, re_measured_as)}
                mark = dict(index["retired"])
            line = json.dumps(index)
        rewritten.append(line)
    if not found:
        raise ValueError("no recorded round matches %s" % name)
    if mark is not None:
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text("\n".join(rewritten) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    return mark or {}


def _moment(stamp: Any) -> Optional[datetime]:
    """A recorded ISO stamp as an aware UTC moment, or None when it cannot be read.

    A stamp with no offset is the write time of a record on a machine that writes UTC, and reading it as
    "now" would make an old record look new -- the rule round 29 set for claim checks applies to the debt
    as well.
    """
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def debt_history(root: Path, days: float = 7.0, now: Optional[datetime] = None) -> Dict[str, Any]:
    """How far the unrecorded share has moved, reconstructed from the ledger (round 55).

    A single number says how much is left and cannot say whether the loop is paying it down or the ledger
    is growing around it. Both events are recorded: a round arrives at `started_at` and leaves at
    `retired.at`, so the share at any past moment is recomputable. Nothing is written and nothing is
    measured -- progress stays derived from the ledger rather than kept in a second bookkeeping file
    (round 53's rule), which also means this cannot disagree with the ledger it describes.

    `points` is the fall in percentage points across the window, positive when the share fell. `per_day`
    is only defined over at least one day: a drain that finished inside a minute says what happened but
    not at what rate, and inventing a rate from it is how a projection becomes a promise.
    """
    moment_now = now or datetime.now(timezone.utc)
    rounds = history(root, include_retired=True)
    events: List[tuple] = []
    for row in rounds:
        name = str(row.get("round") or "")
        started = _moment(row.get("started_at") or row.get("finished_at"))
        if started is not None:
            events.append((started, 1, name, str(row.get("stream_path") or "")))
        retired = row.get("retired")
        if isinstance(retired, Mapping):
            gone = _moment(retired.get("at"))
            if gone is not None:
                events.append((gone, -1, name, ""))
    # Arrivals before departures at the same instant, so a round that replaced another never makes the
    # denominator dip below zero on the way through.
    events.sort(key=lambda item: (item[0], -item[1]))
    since = moment_now - timedelta(days=days)
    events = [item for item in events if item[0] <= moment_now]
    alive: Dict[str, bool] = {}
    opening: Optional[tuple] = None
    # The window asked for can reach back further than the ledger does. Then "seven days ago" is an empty
    # ledger, and comparing today's share with it would print "0 points" for a debt that has visibly been
    # paid down: the baseline becomes the first state the record can name, and `truncated` makes the caller
    # say "since the first round" instead of "in the last seven days".
    truncated = bool(events) and events[0][0] > since
    baseline_moment: Optional[datetime] = None if truncated else since
    paid = 0
    arrived = 0

    def snapshot() -> tuple:
        total = len(alive)
        empty = sum(1 for named in alive.values() if not named)
        return total, empty

    for when, kind, name, path in events:
        if not truncated and when > since and opening is None:
            opening = snapshot()
        if kind == 1:
            alive[name] = bool(path)
            if when > since:
                arrived += 1
        else:
            alive.pop(name, None)
            if when > since:
                paid += 1
        if truncated and opening is None and alive:
            opening = snapshot()
            baseline_moment = when
    rounds_now, empty_now = snapshot()
    if opening is None:
        opening = (0, 0)
        baseline_moment = moment_now
    before_rounds, before_empty = opening
    share_now = (empty_now / rounds_now) if rounds_now else 0.0
    share_before = (before_empty / before_rounds) if before_rounds else share_now
    points = (share_before - share_now) * 100.0
    elapsed = ((moment_now - baseline_moment).total_seconds() / 86400.0) if baseline_moment else days
    per_day: Optional[float] = (points / elapsed) if elapsed >= 1.0 else None
    reaches_zero: Optional[str] = None
    if per_day is not None and per_day > 0 and share_now > 0:
        needed = (share_now * 100.0) / per_day
        reaches_zero = (moment_now + timedelta(days=needed)).date().isoformat()
    return {
        "days": elapsed,
        "asked_days": days,
        "truncated": truncated,
        "rounds": rounds_now,
        "without_file": empty_now,
        "share": share_now,
        "before_rounds": before_rounds,
        "before_file": (before_rounds - before_empty),
        "before_share": share_before,
        "points": points,
        "paid": paid,
        "arrived": arrived,
        "per_day": per_day,
        "reaches_zero": reaches_zero,
    }


def retirement_overlap(root: Path, retired: str, successor: Optional[str]) -> Dict[str, Any]:
    """Which of the retired round's trials the successor round measured again.

    Both sides are read, never inferred. A missing successor is reported as unreadable rather than as an
    empty overlap: "nobody measured these again" and "nobody looked" are different statements, and only
    the first one is the mechanism's own decision.
    """
    directory = root / EVOLVE_DIR
    old = _trial_names(directory / _round_name(retired))
    if successor is None:
        return {"matches": [], "not_repeated": old, "successor_readable": False}
    new = _trial_names(directory / _round_name(successor))
    if not (directory / _round_name(successor)).exists():
        return {"matches": [], "not_repeated": old, "successor_readable": False}
    return {"matches": [name for name in old if name in new],
            "not_repeated": [name for name in old if name not in new],
            "successor_readable": True}


def replacement_links(rounds: Sequence[Mapping[str, Any]],
                      settled: Sequence[Mapping[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Every replacement the ledger claims, checked against the other side of the link (round 81).

    A retirement writes one end of a relation (`retired.re_measured_as`) and the re-run queue writes the
    other (`queue.paid_round`). Both ends are claims by a command; neither is a check on the other, so a
    record can name a successor that never named it back, or two records can name each other's predecessors
    differently. Until this function existed the pages printed each side's own version and nobody compared
    them.

    Four outcomes, and only three are defects:

    - `mismatch` — the successor says it replaced P, and P says it was re-measured as something else
      (several rounds may name the same successor; each disagreeing nomination is its own finding, and
      `names` carries every record involved, not just the one that happened to be iterated first).
    - `unretired` — the successor says it replaced P, and P carries no retirement mark at all.
    - `dangling` — a link names a round the ledger does not hold.
    - `legacy` — the retired round names a successor that records no `queue` block: successors written
      before round 78 (and copies made by hand with `--retire --re-measured-as`) cannot name their
      predecessor, so this end was unbackfillable rather than wrong.

    `kind` is the charge, not the shape: `--strict` exits 2 on the first three (a build defect) and warns on
    `legacy`, exactly as round 33 split its two debts.

    Round 112: a contradiction nobody can measure is settled by a person, so a `(record, successor)` pair the
    operator settled is not a defect any more. The settlement is *not* consulted to pick a winner between two
    contradicting books -- it silences that one claimant's claim, and the successor's own end is still printed.
    """
    decided = {(str(item.get("record") or ""), str(item.get("successor") or "")) for item in settled}
    names = {str(record.get("round") or "") for record in rounds}
    by_name = {str(record.get("round") or ""): record for record in rounds}
    findings: List[Dict[str, Any]] = []

    # Round 82: several rounds can name the same successor (a queue pays one re-run per round, so this
    # happens whenever two rounds were re-measured into the same record). Grouping by the claimed successor
    # first is what lets one finding name every participant instead of whichever came first in iteration.
    nominators: Dict[str, List[str]] = {}
    for record in rounds:
        retired = record.get("retired")
        if not isinstance(retired, Mapping):
            continue
        claimed = str(retired.get("re_measured_as") or "")
        if claimed:
            nominators.setdefault(claimed, []).append(str(record.get("round") or ""))

    for claimed in sorted(nominators):
        # Round 112: a settled pair is a decision, so that claimant no longer contradicts the successor.
        claimants = [name for name in nominators[claimed] if (name, claimed) not in decided]
        if not claimants:
            continue
        if claimed not in names:
            findings.append({"round": claimants[0], "names": sorted({claimed} | set(claimants)),
                             "kind": "dangling", "claimant": claimants[0], "claimed": claimed,
                             "detail": "the retired round names a successor the ledger does not hold"})
            continue
        # The successor's own end of the link, if it wrote one.
        link = by_name[claimed].get("queue") or {}
        if not isinstance(link, Mapping) or not link:
            findings.append({"round": claimants[0], "names": sorted({claimed} | set(claimants)),
                             "kind": "legacy", "claimant": claimants[0], "claimed": claimed,
                             "detail": "the successor predates the queue's `paid_round` field"})
            continue
        named = str(link.get("paid_round") or "")
        if not named:
            findings.append({"round": claimants[0], "names": sorted({claimed} | set(claimants)),
                             "kind": "legacy", "claimant": claimants[0], "claimed": claimed,
                             "detail": "the successor was written by a command that did not record what "
                                       "it replaced"})
            continue
        # One end of the link names exactly one predecessor; every other claimant disagrees with it.
        for claimant in claimants:
            if claimant == named:
                continue
            findings.append({"round": claimant, "names": sorted({claimant, claimed, named} - {""}),
                             "kind": "mismatch", "claimant": claimant, "claimed": claimed,
                             "detail": "%s names %s as its successor, but %s says it replaced %s"
                                       % (claimant, claimed, claimed, named)})

    for record in rounds:
        successor = str(record.get("round") or "")
        link = record.get("queue")
        if not isinstance(link, Mapping):
            continue
        named = str(link.get("paid_round") or "")
        if not named:
            continue
        if named not in names:
            findings.append({"round": successor, "names": sorted({successor, named}), "kind": "dangling",
                             "detail": "the successor names a round the ledger does not hold"})
            continue
        if not isinstance(by_name[named].get("retired"), Mapping):
            findings.append({"round": successor, "names": sorted({successor, named}), "kind": "unretired",
                             "detail": "the successor says it replaced a round that was never retired"})
    return findings


LINK_DEFECTS = ("mismatch", "unretired", "dangling")

#: Whether the file a citation names could be opened when the citation was written (round 91). Declared here
#: because `promote.py` imports this module: a constant the page and the writer must agree on cannot live on the
#: side that would close the cycle.
EVIDENCE_PRESENT = "present"
EVIDENCE_MISSING = "missing"
EVIDENCE_UNNAMED = "unnamed"

#: Round 86: how long a copied end may stand before `--strict` asks for the one command that clears it. Kept
#: here as well as in `promote` so the page and the gate read the same number without an import cycle.
IN_FORCE_GRACE_DAYS = 14.0

# Round 83: the mark a repaired end carries. A `legacy` link is repaired by copying the retirement's claim
# into the successor record, which makes both ends say the same thing -- so the mark has to say *how* they
# came to agree, or the next audit will treat a copied statement as a second witness.
BACKFILL_KEY = "backfilled"


SETTLEMENTS = "settlements.json"
SETTLEMENT_FORMAT = "brian.settled/1"

def settlement_summary(settlements: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """One account of the settlement store, so the pages and the JSON cannot disagree (round 116).

    Four states are kept apart because folding any two of them lets the default pick the answer: a decision that
    came with a reason, one that gained a note later, one that has neither, and -- inside each -- a decider or
    explainer who was named versus one who was not (boundaries 166/260). A page that printed only
    "N settlements" would make four different books read the same (boundary 213 applied to decisions).

    `reaffirmed` counts the decisions that have been re-taken at least once (round 119): "settled once" and
    "settled and re-confirmed twice" are different amounts of work behind the same word.
    """
    pairs = list(settlements or [])
    explained = sum(1 for item in pairs if str(item.get("why") or "").strip())
    later = sum(1 for item in pairs if item.get("notes"))
    unexplained = sum(1 for item in pairs
                      if not str(item.get("why") or "").strip() and not item.get("notes"))
    explainers: Dict[str, int] = {}
    deciders: Dict[str, int] = {}
    for item in pairs:
        who = str(item.get("by") or "").strip()
        deciders[who] = deciders.get(who, 0) + 1
        for note in item.get("notes") or []:
            if not isinstance(note, Mapping):
                continue
            author = str(note.get("by") or "").strip()
            explainers[author] = explainers.get(author, 0) + 1
    repeated = sum(1 for item in pairs if len([note for note in item.get("reaffirmations") or []
                                                if isinstance(note, Mapping)]) >= 3)
    return {"reaffirmed": sum(1 for item in pairs if item.get("reaffirmations")), "repeated": repeated,
            "pairs": len(pairs), "explained": explained, "explained_later": later,
            "unexplained": unexplained,
            "deciders": dict(sorted(deciders.items())), "explainers": dict(sorted(explainers.items())),
            "unattributed": sum(1 for item in pairs if not str(item.get("by") or "").strip())}

def retake_timeline(settlements: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Each decision's re-takes in order, with what each one covered (round 124).

    The store keeps `at` (when the decision was taken) plus `reaffirmations` (each re-take with the claims it
    covered), so "re-taken three times" was countable and "re-taken in this order, covering these claims" was not.
    A count without the moments cannot be checked against the books (boundary 290), and "taken" and "re-taken"
    mean different things about the same pair (boundary 289), which is why every entry carries its kind.

    Keyed by `"<record>=<successor>"` so the JSON payload is a plain mapping and the pages can look one pair up.
    """
    rows: Dict[str, List[Dict[str, Any]]] = {}
    for item in settlements or []:
        record = str(item.get("record") or "")
        successor = str(item.get("successor") or "")
        if not record or not successor:
            continue
        entries = [{"at": str(item.get("at") or ""), "why": str(item.get("why") or ""),
                    "by": str(item.get("by") or ""), "kind": "taken"}]
        for note in item.get("reaffirmations") or []:
            if not isinstance(note, Mapping):
                continue
            entries.append({"at": str(note.get("at") or ""), "why": str(note.get("why") or ""),
                            "by": str(note.get("by") or ""), "kind": "retaken",
                            "claimants": [str(name) for name in note.get("claimants") or []]})
        rows["%s=%s" % (record, successor)] = sorted(entries, key=lambda entry: entry["at"])
    return rows


def frequent_retakes(settlements: Sequence[Mapping[str, Any]],
                     at_least: int = 3) -> List[Dict[str, Any]]:
    """Decisions re-taken at least `at_least` times, most re-taken first (round 124).

    A re-take says "I have read today's books and the decision still stands". Doing that repeatedly says the
    decision keeps going stale, which is evidence about the decision, not about the books (boundary 288): the
    answer is a new decision, and the page should name the pairs so a reader can look at the claim itself.
    """
    rows: List[Dict[str, Any]] = []
    for key, entries in retake_timeline(settlements).items():
        retaken = [entry for entry in entries if entry.get("kind") == "retaken"]
        if len(retaken) < max(1, int(at_least)):
            continue
        last = retaken[-1]
        rows.append({"pair": key, "record": key.split("=", 1)[0], "successor": key.split("=", 1)[-1],
                     "retakes": len(retaken), "last_at": last.get("at", ""), "last_by": last.get("by", "")})
    rows.sort(key=lambda row: (-row["retakes"], row["pair"]))
    return rows


def unexplained_decisions(settlements: Sequence[Mapping[str, Any]],
                          now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The decisions nobody has explained, oldest first, with how long they have stood (round 117).

    A decision with no `why` and no `notes` is not "pending": nothing in the loop will ever fill it in, and the
    only command that can is `--settle-note`. That makes it debt, and debt a page counts but no gate charges is
    debt that only a careful reader ever pays (boundary 154's rule, applied to a decision instead of a round).
    """
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    for item in settlements or []:
        if str(item.get("why") or "").strip() or item.get("notes"):
            continue
        taken = item.get("at")
        age: Optional[float] = None
        if isinstance(taken, str) and taken:
            try:
                age = max(0.0, (moment - datetime.fromisoformat(taken)).total_seconds() / 86400.0)
            except ValueError:
                age = None
        rows.append({"record": str(item.get("record") or ""), "successor": str(item.get("successor") or ""),
                     "at": str(taken or ""), "age_days": age})
    rows.sort(key=lambda row: (row["at"] or "", row["record"]))
    return rows

def _is_later(moment: str, than: str) -> bool:
    """Whether `moment` is after `than`, both being isoformat stamps; an unreadable or empty pair is not later."""
    if not moment or not than:
        return False
    try:
        return datetime.fromisoformat(moment) > datetime.fromisoformat(than)
    except ValueError:
        return moment > than


def _walks_since(runs: Optional[Sequence[Mapping[str, Any]]], moment: str) -> Optional[int]:
    """How many recorded queue walks happened after `moment`; None when the caller supplied no runs.

    Round 121: `last_retaken` alone cannot separate "re-taken this morning, then three walks" from "re-taken this
    morning and nobody has walked since", and those say different things about a file that still owes.
    """
    if runs is None:
        return None
    if not moment:
        return len(runs)
    return sum(1 for run in runs if str(run.get("at") or "") > moment)


def _effective_moment(item: Mapping[str, Any]) -> str:
    """The moment a decision speaks about: its own date, or a later re-affirmation of it (round 119).

    A re-affirmation is the operator saying "I have read today's books and the decision still stands", so claims
    that were already in the ledger are covered by it. Without this, re-taking a decision would change nothing:
    the same claims would go on being "later than" the original date for ever.
    """
    moments = [str(item.get("at") or "")]
    for entry in item.get("reaffirmations") or []:
        if isinstance(entry, Mapping):
            moments.append(str(entry.get("at") or ""))
    dated = [moment for moment in moments if moment]
    if not dated:
        return ""

    def _key(moment: str) -> Any:
        try:
            return datetime.fromisoformat(moment)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    return max(dated, key=_key)


def reaffirm_link(root: Path, record: str, successor: str, *, why: str = "", by: str = "",
                  claimants: Sequence[str] = (), now: Optional[Any] = None) -> Dict[str, Any]:
    """Re-take a decision after reading today's books (round 119).

    The decision keeps its first date -- that is when the question was settled -- and gains a re-affirmation that
    says when it was last confirmed and what it saw. A re-affirmation is a new decision, so it needs a reason
    (round 113's rule for `--settle-link`, applied to the second time of asking), and it is refused when the
    settled record no longer names that successor: re-affirming a pair the ledger denies would assert something
    false, and the honest remedies there are a new `--settle-link` or dropping the decision (boundary 273).
    """
    name = str(record or "").strip()
    target = str(successor or "").strip()
    if not name or not target:
        raise ValueError("a re-affirmation needs a record and a successor, like RECORD=SUCCESSOR")
    reason = str(why or "").strip()
    if not reason:
        raise ValueError("a re-affirmation needs a reason: --settle-why TEXT. Re-taking a decision without saying "
                         "what changed is how a decision nothing supports stays alive")
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    settlements = load_settlements(root)
    for item in settlements:
        if str(item.get("record") or "") != name or str(item.get("successor") or "") != target:
            continue
        rounds = history(root, include_retired=True)
        record_row = next((row for row in rounds if str(row.get("round") or "") == name), None)
        retired = (record_row or {}).get("retired")
        named = str(retired.get("re_measured_as") or "") if isinstance(retired, Mapping) else ""
        if record_row is None:
            raise ValueError("no recorded round named %s: the decision describes a record the ledger no longer "
                             "holds, so there is nothing to re-affirm" % name)
        if named != target:
            raise ValueError("%s no longer names %s as its successor (%s): a re-affirmation would assert something "
                             "the ledger denies. Take a new decision with --settle-link, or drop this one"
                             % (name, target, named or "no retirement mark"))
        stored = [entry for entry in item.get("reaffirmations") or [] if isinstance(entry, Mapping)]
        if stored and str(stored[-1].get("at") or "") == moment.isoformat():
            return {"record": name, "successor": target, "at": moment.isoformat(), "changed": False,
                    "reaffirmations": len(stored)}
        stored.append({"at": moment.isoformat(), "why": reason, "by": str(by or ""),
                       "claimants": sorted(str(claim) for claim in claimants)})
        item["reaffirmations"] = stored
        path = root / EVOLVE_DIR / SETTLEMENTS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": SETTLEMENT_FORMAT, "settlements": settlements},
                                   indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
        return {"record": name, "successor": target, "at": moment.isoformat(), "changed": True,
                "reaffirmations": len(stored)}
    raise ValueError("no settlement recorded for %s -> %s: `--settle-link` records a decision, `--settle-reaffirm` "
                     "re-takes one" % (name, target))


def reaffirm_plan(rounds: Sequence[Mapping[str, Any]],
                  settlements: Sequence[Mapping[str, Any]],
                  runs: Optional[Sequence[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Which decisions need re-taking, grouped by the file they are about (round 120).

    Round 119 gave the debt a command, one pair at a time, and said nothing about how many pairs a reader was
    looking at: "one decision moved" and "eleven of them did, all on the same file" printed the same row. The
    group is the round's own `stream_path` -- what the ledger already knows about where the promise lives -- and
    the command is spelled out per group so a reader does not retype a path (round 101's rule).

    Sorted by the number of stale decisions, then by file: a page that reshuffles between two runs teaches the
    reader not to trust it (round 110).
    """
    moved = {(str(row.get("record") or ""), str(row.get("successor") or "")): row
             for row in moved_settlements(rounds, settlements)}
    by_name = {str(record.get("round") or ""): record for record in rounds}
    files: Dict[str, Dict[str, Any]] = {}
    for item in settlements or []:
        record = str(item.get("record") or "")
        successor = str(item.get("successor") or "")
        origin = str((by_name.get(record) or {}).get("stream_path") or "")
        bucket = files.setdefault(origin, {"origin": origin, "decisions": 0, "stale": 0, "retaken": 0,
                                           "never": 0, "again": 0, "records": [], "commands": [],
                                           "pairs": []})
        bucket["decisions"] += 1
        bucket.setdefault("_retaken", []).extend(
            str(entry.get("at") or "") for entry in item.get("reaffirmations") or []
            if isinstance(entry, Mapping) and str(entry.get("at") or ""))
        bucket["_taken"] = max(str(bucket.get("_taken") or ""), str(item.get("at") or ""))
        if item.get("reaffirmations"):
            bucket["retaken"] += 1
        if (record, successor) in moved:
            bucket["stale"] += 1
            bucket["records"].append(record)
            # Round 123: the pair carries its kind, because the two kinds are paid as separate commands. A batch
            # that could only take "everything stale" forced a reader to do the cheap work and the expensive work
            # in the same breath, and the reason typed for one then covers the other.
            bucket["pairs"].append({"record": record, "successor": successor,
                                    "kind": "again" if item.get("reaffirmations") else "never"})
            bucket["commands"].append("brian evolve --settle-reaffirm %s=%s" % (record, successor))
            # Round 122: two kinds of stale owe different work. `never` is a promise nobody has read the books
            # for; `again` is one where the books moved *after* the last reading, so re-stamping it would cover a
            # claim while looking like upkeep (boundary 282).
            if item.get("reaffirmations"):
                bucket["again"] += 1
            else:
                bucket["never"] += 1
    rows = [bucket for bucket in files.values() if bucket["stale"]]
    for row in rows:
        moments = row.pop("_retaken", None) or []
        taken = str(row.pop("_taken", "") or "")
        # A file nobody has re-taken is measured from its newest decision, so the count answers "how much has
        # happened since anyone last looked" instead of "since the beginning of time".
        row["last_retaken"] = max(moments) if moments else ""
        row["walks_since"] = _walks_since(runs, row["last_retaken"] or taken)
        row["pairs"] = sorted(row["pairs"], key=lambda entry: (entry["record"], entry["successor"]))
        # One command for the whole file, spelled the way a reader can paste it: eleven pasted pairs is how the
        # reason drifts and how one gets skipped (boundary 277's lesson, one level up).
        row["batch"] = "brian evolve --settle-reaffirm-file %s --settle-why TEXT --settle-by NAME" % row["origin"]
    rows.sort(key=lambda row: (-row["stale"], row["origin"]))
    return rows


def settlement_moment(settlements: Sequence[Mapping[str, Any]], record: str, successor: str) -> str:
    """The moment the decision for a pair speaks about, or "" when no such decision is stored.

    Round 120 needs this to say what a fresh confirmation was measured against: the CLI has to know the moment
    *before* it appends, because appending is what moves it (boundary 276).
    """
    for item in settlements:
        if str(item.get("record") or "") == str(record) and str(item.get("successor") or "") == str(successor):
            return _effective_moment(item)
    return ""


def moved_clearable(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The moved decisions `--settle-reaffirm` can clear: the pair still exists, only a claim arrived later."""
    return [dict(row) for row in rows if row.get("clearable")]

def moved_settlements(rounds: Sequence[Mapping[str, Any]],
                      settlements: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Settled decisions whose evidence has changed since they were taken (round 118).

    A settlement is a statement about the books as they stood: "this record's claim is the one we drop". Two
    things make that statement stale, and both are visible in the ledger itself:

    - `retargeted`: the settled record no longer claims that successor at all (it was re-measured into another
      round, or its retirement mark is gone). The decision now describes a pair that does not exist.
    - `new_claimant`: another record now claims the same successor. The decision settled *one* claimant's claim
      (round 112), so a second claimant is not covered by it -- the contradiction is back, one book larger.

    A decision can be re-taken (`reaffirm_link`, round 119): a re-affirmation moves the date the decision speaks
    about, so a claim that arrived before it is covered and only a later one moves the evidence again. Two kinds
    stay uncharged even then -- when the pair itself is gone (`retargeted`/`gone`) there is nothing to re-affirm,
    so the remedy is a new decision, not a re-dating (boundaries 154/171/271/273).
    """
    by_name = {str(record.get("round") or ""): record for record in rounds}
    claims: Dict[str, List[Dict[str, Any]]] = {}
    for record in rounds:
        retired = record.get("retired")
        named = str(retired.get("re_measured_as") or "") if isinstance(retired, Mapping) else ""
        if named:
            # When the claim was made: the retirement's own stamp and nothing else. `finished_at` is when the
            # round ran, not when the retirement was written (rounds 83/93 write them long after), so a claim
            # without a stamp of its own cannot be dated at all -- boundary 272.
            claims.setdefault(named, []).append({
                "record": str(record.get("round") or ""),
                "at": str((retired or {}).get("at") or "")})
    rows: List[Dict[str, Any]] = []
    for item in settlements or []:
        name = str(item.get("record") or "")
        successor = str(item.get("successor") or "")
        record = by_name.get(name)
        reasons: List[str] = []
        # The pair the decision describes has to still exist for `reaffirm_link` to have anything to re-take; the
        # later-claim reasons are exactly what re-taking covers. Tracked as a boolean because the sentences below
        # both mention the successor, and a substring test cannot tell "the pair moved" from "a claim arrived"
        # (round 119's own defect, caught by its first test run).
        pair_gone = record is None
        if record is None:
            reasons.append("the settled record is no longer in the ledger")
        else:
            retired = record.get("retired")
            named = str(retired.get("re_measured_as") or "") if isinstance(retired, Mapping) else ""
            if not named:
                reasons.append("it carries no retirement mark any more")
                pair_gone = True
            elif named != successor:
                reasons.append("it now names %s as its successor" % named)
                pair_gone = True
        own = _effective_moment(item)
        later = [claim for claim in claims.get(successor, [])
                 if claim["record"] != name and _is_later(claim["at"], own)]
        if later:
            reasons.append("%d claim(s) on the same successor arrived after the decision: %s"
                           % (len(later), ", ".join(sorted(claim["record"] for claim in later))))
        # An undated claim is neither later nor earlier: it cannot be shown to postdate the decision, so it does
        # not move the evidence, and it is never counted as one that does (boundary 272).
        if reasons:
            rows.append({"record": name, "successor": successor, "at": str(item.get("at") or ""),
                         "effective_at": own,
                         # The file the round was measured on. A decision is about a promise, a promise is about
                         # a file, and a page that says "two decisions moved" without the file has no first step.
                         "origin": str((record or {}).get("stream_path") or ""),
                         "reaffirmed": len(item.get("reaffirmations") or []),
                         "clearable": not pair_gone,
                         "reasons": reasons,
                         "claimants": sorted(str(claim["record"]) for claim in later)})
    rows.sort(key=lambda row: (row["at"] or "", row["record"]))
    return rows

def load_settlements(root: Path) -> List[Dict[str, Any]]:
    """The link decisions somebody wrote down, newest last (round 112).

    A mismatch is two claims and no evidence about which is right, so no measurement can settle it -- a person
    has to. Until this round the only way to *stop* `--strict` charging it was to edit a record by hand, which
    leaves no trace of who decided. A settlement is the decision itself, stored beside the ledger.
    """
    path = root / EVOLVE_DIR / SETTLEMENTS
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = stored.get("settlements") if isinstance(stored, Mapping) else None
    return [dict(item) for item in rows or [] if isinstance(item, Mapping)]


def settle_link(root: Path, record: str, successor: str, why: str = "", by: str = "",
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Record that `record`'s measurement went into `successor`, and that a person decided it (round 112).

    This writes no history: the retired record keeps its retirement, the successor keeps whatever `paid_round`
    it recorded, and the audit keeps showing both ends on separate lines. What changes is that the *pair* is
    no longer a contradiction -- it is a decision, dated and attributed, and a reader can see that agreement
    came from somebody rather than from two witnesses (round 83's rule, applied to a contradiction).

    Round 113: `why` is required for a *new* settlement. The store is a record of judgement, and a judgement
    nobody can explain is the one nobody dares review; an already-recorded pair stays idempotent so re-running a
    command does not demand the reason again. `by` is stored as given and may be empty -- a machine account is not
    a person, so an unattributed decision is printed as unattributed rather than filled in here (boundary 254).
    """
    name = str(record or "").strip()
    to = str(successor or "").strip()
    if not name or not to:
        raise ValueError("a settlement needs both ends: --settle-link RECORD=SUCCESSOR")
    if name == to:
        raise ValueError("a record cannot be its own successor: %s" % name)
    rounds = history(root, include_retired=True)
    names = {str(item.get("round") or "") for item in rounds}
    for side, label in ((name, "record"), (to, "successor")):
        if side not in names:
            raise ValueError("no recorded round named %s: %s" % (label, side))
    store = {"format": SETTLEMENT_FORMAT, "settlements": load_settlements(root)}
    existing = [item for item in store["settlements"]
                if str(item.get("record") or "") == name and str(item.get("successor") or "") == to]
    if existing:
        return {"record": name, "successor": to, "at": str(existing[0].get("at") or ""),
                "why": str(existing[0].get("why") or ""), "by": str(existing[0].get("by") or ""),
                "changed": False}
    reason = str(why or "").strip()
    if not reason:
        # Round 115: a refused new decision must name the other route, because the common case is a *recorded*
        # decision that has no reason yet -- that one is explained, never rewritten (rounds 114/257).
        raise ValueError("a settlement needs a reason: --settle-why TEXT. "
                         "A decision nobody can explain is the record nobody dares review. "
                         "If the decision is already recorded, `--settle-note RECORD=SUCCESSOR --settle-why TEXT` "
                         "adds the explanation without moving its date")
    # `evolve` owns no clock helper of its own: `promote._stamp_moment` is on the other side of the import
    # cycle, so the moment is normalised here (round 112).
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    entry = {"record": name, "successor": to, "at": moment.isoformat(), "why": reason,
             "by": str(by or "").strip()}
    store["settlements"].append(entry)
    path = root / EVOLVE_DIR / SETTLEMENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return dict(entry, changed=True)


def settle_note(root: Path, record: str, successor: str, why: str = "", by: str = "",
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Append the explanation a recorded decision lacked, without moving its date or its successor (round 114).

    Rounds before 113 could store a settlement with no reason, and this build can only report those -- editing
    the store by hand is what rounds 83/112 exist to prevent. So the explanation is appended as its own entry:
    the decision keeps `at`, `successor` and whatever `why` it had, and `notes` grows. The distinction matters
    later: a decision explained when it was taken and one explained a month afterwards are not the same evidence
    (boundary 257), so `--campaign` counts them apart instead of letting the note upgrade the record.
    """
    name = str(record or "").strip()
    to = str(successor or "").strip()
    note_why = str(why or "").strip()
    if not name or not to:
        raise ValueError("a note needs both ends: --settle-note RECORD=SUCCESSOR")
    if not note_why:
        raise ValueError("a note needs a reason: --settle-why TEXT")
    store = {"format": SETTLEMENT_FORMAT, "settlements": load_settlements(root)}
    existing = [item for item in store["settlements"]
                if str(item.get("record") or "") == name and str(item.get("successor") or "") == to]
    if not existing:
        raise ValueError("no settlement recorded for %s -> %s: `--settle-link` records a decision, "
                         "`--settle-note` explains one" % (name, to))
    entry = existing[0]
    author = str(by or "").strip()
    notes = entry.get("notes") if isinstance(entry.get("notes"), list) else []
    if notes and str(notes[-1].get("why") or "").strip() == note_why \
            and str(notes[-1].get("by") or "").strip() == author:
        return {"record": name, "successor": to, "at": str(entry.get("at") or ""),
                "notes": len(notes), "changed": False}
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    # Round 116: one author cannot have written two different explanations in the same recorded second, so that
    # pair is refused -- the page would otherwise list a contradiction as a sequence, and the store cannot say
    # which came first. Two *authors* in one second are two people explaining the same decision (boundary 263).
    here = moment.isoformat()[:19]
    clash = [item for item in notes
             if str(item.get("at") or "")[:19] == here
             and str(item.get("by") or "").strip() == author
             and str(item.get("why") or "").strip() != note_why]
    if clash:
        raise ValueError("two different explanations for %s -> %s carry the same second (%s) from the same author; "
                         "a decision read in order needs them to be distinct in time" % (name, to, here))
    note = {"at": moment.isoformat(), "why": note_why, "by": author}
    entry["notes"] = notes + [note]
    path = root / EVOLVE_DIR / SETTLEMENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"record": name, "successor": to, "at": str(entry.get("at") or ""),
            "note": note, "notes": len(entry["notes"]), "changed": True}


def legacy_links(rounds: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The `legacy` links as pairs: a retirement whose successor recorded no end of its own (round 83).

    Round 82 made a finding's `names` a list of participants, because a finding can involve three records.
    A repair needs the one successor to write to, not the participants, so this derives the pair from the
    records instead of indexing a list whose order means something else (round 83's first attempt read
    `names` as a string and skipped every link).
    """
    by_name = {str(record.get("round") or ""): record for record in rounds}
    pairs: List[Dict[str, Any]] = []
    for record in rounds:
        retire = record.get("retired")
        if not isinstance(retire, Mapping):
            continue
        successor_name = str(retire.get("re_measured_as") or "")
        if not successor_name or successor_name not in by_name:
            continue
        link = by_name[successor_name].get("queue")
        if isinstance(link, Mapping) and str(link.get("paid_round") or ""):
            continue
        pairs.append({"retired": str(record.get("round") or ""), "successor": successor_name})
    return pairs


def backfilled_links(rounds: Sequence[Mapping[str, Any]]) -> List[str]:
    """Successors whose `queue` block was written by a repair rather than by the re-run queue (round 83)."""
    found: List[str] = []
    for record in rounds:
        link = record.get("queue")
        if isinstance(link, Mapping) and isinstance(link.get(BACKFILL_KEY), Mapping):
            found.append(str(record.get("round") or ""))
    return found


def repair_legacy(root: Path, *, at: Optional[str] = None, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Write the successor-side end of every `legacy` link, marked as copied (round 83).

    Only the `legacy` case is repaired. A `mismatch` or `dangling` link is a disagreement or a missing
    record, and copying either side of a disagreement would pick a winner with no evidence; those stay for
    a human (round 81 charges them). Repairing a `legacy` end picks no winner: the successor made no claim at
    all, so there is nothing to contradict -- and the repaired block records who it was copied from, when,
    and that the queue never wrote it.

    `dry_run` reports the same rows without writing, because a command that edits recorded history has to be
    able to show what it would write (boundary 175).
    """
    directory = root / EVOLVE_DIR
    rounds = history(root, include_retired=True)
    by_name = {str(record.get("round") or ""): record for record in rounds}
    moment = at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    repaired: List[Dict[str, Any]] = []
    for item in legacy_links(rounds):
        retired_name = str(item.get("retired") or "")
        successor_name = str(item.get("successor") or "")
        retired = by_name.get(retired_name) or {}
        successor = by_name.get(successor_name) or {}
        row = {"retired": retired_name, "successor": successor_name, "would_write": False,
               "written": False, "reason": ""}
        link = successor.get("queue")
        if isinstance(link, Mapping) and str(link.get("paid_round") or ""):
            row["reason"] = "the successor already names %s, so this is not a legacy end" % link.get("paid_round")
            repaired.append(row)
            continue
        path = directory / successor_name
        if not path.exists():
            row["reason"] = "the successor has no round file to write to"
            repaired.append(row)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload.get("queue"), Mapping) and payload["queue"].get("paid_round"):
            row["reason"] = "the round file on disk already names a predecessor"
            repaired.append(row)
            continue
        source = (retired.get("stream") or {}).get("source") or ""
        payload["queue"] = {
            # Round 80 keys `paid by domain` on this field: a repair does not know which domain the queue
            # would have charged, and guessing one would put the repair's invented domain into that table.
            "paid": "",
            "paid_round": retired_name,
            "family": None,
            BACKFILL_KEY: {
                "from": retired_name,
                "at": moment,
                "why": "the retirement mark was the only end of this replacement that existed",
                "stream": str(source),
            },
        }
        # `would_write` and `written` are different facts: a plan says "this end can be repaired", an apply
        # says "it was". Without the first, a dry run prints its repairable links as "skipped" (round 83
        # printed `0 of 1 ... would be repaired` for a link it had just described as repairable).
        row["would_write"] = True
        row["written"] = not dry_run
        if not dry_run:
            path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        repaired.append(row)
    return repaired


def format_repairs(rows: Sequence[Mapping[str, Any]], dry_run: bool = False) -> str:
    """One line per link the repair looked at, and what it did (round 83)."""
    lines: List[str] = []
    if not rows:
        lines.append("no legacy link: every retired round either has a successor that named it back, or none")
        return "\n".join(lines)
    verb = "would write" if dry_run else "wrote"
    for row in rows:
        if row.get("would_write"):
            lines.append("%-14s %s -> %s  (marked `%s`, so the agreement is copied, not a second opinion)"
                         % (verb, row["retired"], row["successor"], BACKFILL_KEY))
        else:
            lines.append("%-14s %s -> %s  %s" % ("skipped", row["retired"], row["successor"], row["reason"]))
    written = sum(1 for row in rows if row.get("would_write"))
    lines.append("%d of %d legacy link(s) %s." % (written, len(rows), "would be repaired" if dry_run
                                                  else "repaired"))
    return "\n".join(lines)


def _round_name(name: str) -> str:
    return name if name.endswith(".json") else "%s.json" % name


def _trial_names(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str((trial.get("candidate") or {}).get("name") or "?")
            for trial in payload.get("trials") or []]


def previously_rejected(root: Path, rules: Optional[EvolveRules] = None,
                        source: Optional[str] = None) -> Dict[str, str]:
    """candidate name -> the rule that rejects it, from the *latest* round that judged it.

    A loop that re-proposes its own rejected ideas has not accumulated anything; the survey calls the
    symptom scientific amnesia. This is the smallest cure: the next round can see what was already
    decided and why, and skip it instead of paying to rediscover it.

    The latest verdict wins, and the reason names the stream it came from. A verdict is a statement
    about a dataset — the same change can be rejected on the synthetic suite and accepted on SMD — so
    keeping the first rejection forever would present a settled question that was later answered the
    other way, which is amnesia with extra paperwork.

    `source` narrows the answer to one stream (round 36). Without it the caller sees every stream's
    refusals mixed together, which is fine for a listing and wrong for a decision: a candidate refused
    on the synthetic suite has not been measured on SMD, and treating the two as the same refusal is
    the cross-stream transfer this loop keeps finding does not work.

    Passing `rules` re-decides each stored measurement under the rules in force *now*, which is what
    makes the ledger self-healing: a rule change (round 6 made the deploy floor one) can invalidate a
    stored rejection without anyone remembering to clear it, and a ledger that keeps poisoning the next
    round with verdicts from retired rules is worse than no ledger. The re-decided reason is prefixed
    `re-judged`, so a reader can tell which verdicts came from stored runs and which from today's rules.
    """
    rejected: Dict[str, str] = {}
    for name, verdict in rejection_provenance(root, rules).items():
        if verdict["accepted"]:
            continue
        if source is not None and verdict["source"] not in ("?", str(source)):
            continue
        rejected[name] = "on %s: %s" % (verdict["source"], verdict["reason"])
    return rejected


def rejection_provenance(root: Path, rules: Optional[EvolveRules] = None) -> Dict[str, Dict[str, Any]]:
    """candidate name -> the round, stream and retirement status behind the verdict that still stands.

    `previously_rejected` answers "what is settled"; this answers "settled by whom". Round 35 needs the
    second question because a refusal outlives the round that recorded it: after round 34 a retired
    record keeps its verdicts on purpose (retirement says the *gate* no longer owes an answer, not that
    the loop forgets), so the campaign can honestly say "23 candidates are still held as rejected"
    while none of those refusals was re-checked today. Reporting the count without naming who carries
    it would let a reader mistake inherited refusals for verified ones — the same defect as a verdict
    that never expires, one level up.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    # Retired records are read here even though the audit skips them (round 34): retirement is a
    # statement about what the *check* still has to answer for, not about what the loop learned. A
    # refusal measured on a stream is still a refusal on that stream, and dropping it at retirement
    # would reintroduce the amnesia this function exists to prevent — with the extra irony that this
    # round's own fix would have caused it.
    for row in history(root, include_retired=True):
        source = (row.get("stream") or {}).get("source") or (row.get("stream") or {}).get("service") or "?"
        retirement = row.get("retired")
        retired = bool(retirement)
        baseline = Measurement.from_dict(row.get("baseline") or {}) if rules is not None else None
        for trial in row.get("trials") or []:
            verdict = trial.get("verdict") or {}
            accepted = bool(verdict.get("accepted"))
            reason = verdict.get("reason", "")
            if rules is not None and baseline is not None:
                candidate = Candidate.from_dict(trial.get("candidate") or {})
                measurement = Measurement.from_dict(trial.get("measurement") or {})
                fresh = decide(baseline, measurement, rules, veto=candidate.veto)
                accepted, reason = fresh.accepted, "re-judged: %s" % fresh.reason
            latest[verdict.get("candidate") or "?"] = {
                "accepted": accepted,
                "reason": reason,
                "source": source,
                "round": row.get("round"),
                # When the refusal was made (round 43). A refusal is evidence about a moment, and the plan
                # cannot tell a refusal measured this morning from one measured in another campaign without it.
                "when": str(row.get("finished_at") or row.get("started_at") or ""),
                "retired": retired,
                "re_measured_as": (retirement or {}).get("re_measured_as") if retired else None,
                "stream": dict(row.get("stream") or {}),
            }
    return latest


def refusal_plan(provenance: Mapping[str, Mapping[str, Any]], *, source: Optional[str] = None,
                 shape: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None,
                 expiry_days: float = REFUSAL_EXPIRY_DAYS) -> Dict[str, List[str]]:
    """Split the refusals that still stand into what this round may skip and what it must measure again.

    A refusal is not a fact about a candidate in the abstract: it was measured on a stream, at a sampling
    density, under rules that may since have changed (and `provenance` already re-decided it under today's
    rules when the caller asked). Round 36 therefore splits four ways:

    * `skip` — refused on *this* stream at *this* sampling and not retired: measuring it again would buy a
      number the ledger already holds;
    * `elsewhere` — refused, but on another stream (or by a round that did not say which): the candidate
      has not been measured here, so this round's measurement is the first one;
    * `inherited` — refused by a retired record: nobody re-checked that verdict, so re-opening it is the
      point of measuring rather than a repeat (boundary 76 says the question was dropped, not answered);
    * `reopened` — refused here, but at a geometry this round cannot match (the record predates the
      cadence field, or a window covers different traffic): the old refusal does not apply to these
      windows (boundary 72);
    * `expired` — refused here, at this sampling, but long enough ago that the evidence has aged (round 43).
      This is not "the refusal was wrong": it is that a `skip` refusal is otherwise never tested again —
      the loop stops proposing the candidate, so the verdict can never be falsified. Re-measuring an
      expired refusal is the only thing that can turn "still refused" into a fact rather than a memory.

    Only `skip` is a decision to save the measurement. The other four are reasons the loop must spend it
    again, and the caller is expected to print which is which rather than a single count.
    """
    groups: Dict[str, List[str]] = {"skip": [], "elsewhere": [], "inherited": [], "reopened": [],
                                    "expired": []}
    for name, verdict in provenance.items():
        if verdict.get("accepted"):
            continue
        if source is not None and verdict.get("source") not in ("?", str(source)):
            groups["elsewhere"].append(name)
            continue
        if verdict.get("retired"):
            groups["inherited"].append(name)
            continue
        if shape is not None and _sampling_differs(verdict.get("stream"), shape):
            groups["reopened"].append(name)
            continue
        age = _refusal_age_days(verdict.get("when"), now)
        if age is not None and age > float(expiry_days):
            groups["expired"].append(name)
            continue
        groups["skip"].append(name)
    for group in groups.values():
        group.sort()
    return groups


def _refusal_age_days(when: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """How old a refusal is, in days. An unreadable stamp is `None`: not reusable as evidence either way."""
    if not when:
        return None
    moment = now if now is not None else datetime.now(timezone.utc)
    try:
        then = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        # Same rule as a claim's age: a stamp without a zone is read as UTC rather than as "now", because
        # guessing a date is how a stale check starts looking fresh (round 29).
        then = then.replace(tzinfo=timezone.utc)
    return (moment - then).total_seconds() / 86400.0


# How far two samplings may differ and still be called the same geometry for a *refusal*. The seed rule
# lives in `propose.SHAPE_TOLERANCE` with the same value; duplicated so this module does not import back
# into the generator that imports it (round 36).
SHAPE_TOLERANCE = 0.25


def _recorded_shape(stream: Optional[Mapping[str, Any]]) -> bool:
    """Whether a recorded stream says enough to compare its sampling with another (`_same_shape` treats
    silence as agreement, which is right for seeds and wrong for refusals)."""
    if not stream:
        return False
    return stream.get("cadence_s") is not None or stream.get("windows") is not None


def _sampling_differs(recorded: Optional[Mapping[str, Any]],
                      shape: Optional[Mapping[str, Any]]) -> bool:
    """Whether a recorded stream and the current one cover different amounts of traffic per window.

    The same rule `propose._same_shape` applies to seeds, written here because `propose` imports this
    module (round 36 needed the comparison on the refusal path, and importing back would be a cycle).
    Unknown on either side counts as *different* here, the opposite of the seed rule: a refusal whose
    geometry nobody recorded is not evidence that it applies to these windows.
    """
    if not _recorded_shape(recorded) or not shape:
        return True
    for key in ("cadence_s", "windows"):
        left = recorded.get(key)
        right = shape.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and left and right:
            if abs(float(right) / float(left) - 1.0) > SHAPE_TOLERANCE:
                return True
    return False


# --------------------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------------------
def format_round(result: RoundResult) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - propose, measure, select")
    lines.append("=" * 96)
    lines.append("reasoner          %s" % (result.baseline.reasoner or "unknown"))
    lines.append("stream            %s" % json.dumps(result.stream, sort_keys=True))
    if result.proposal:
        lines.append("proposal          %s  space %s  (%s)"
                     % (result.proposal.get("mode") or "?",
                        result.proposal.get("space_fingerprint") or "?",
                        result.proposal.get("space") or "?"))
        for reason_line in _wrap("why               "
                                 + (result.proposal.get("reason") or ""), 96):
            lines.append(reason_line)
    if result.claim:
        claim = result.claim
        promise = claim.get("promise")
        measured = claim.get("measured")
        lines.append("in force          v%03d promised recall %s at the %.0f%% budget"
                     % (int(claim.get("version") or 0),
                        "n/a" if promise is None else "%.3f" % promise,
                        100 * result.rules.fpr_budget))
        lines.append("on this stream    recall %s  %s"
                     % ("n/a" if measured is None else "%.3f" % measured,
                        claim.get("verdict") or "?"))
        for line in _wrap("                  " + (claim.get("reason") or ""), 96):
            lines.append(line)
        if claim.get("verdict") != "verified":
            lines.append("                  so this round's baseline is not the configuration the promise")
            lines.append("                  is about: a gain over it is a gain on this sampling, not a")
            lines.append("                  restored promise (boundary 68).")
    lines.append("objective         accept a Pareto move: tokens/report down >= %.0f%%, recall at"
                 " <=%.0f%% false positives up >= %.0f%%, or a recorded configuration that becomes"
                 " deployable; never at a cost of another column"
                 % (100 * result.rules.min_gain, 100 * result.rules.fpr_budget,
                    100 * result.rules.min_quality_gain))
    lines.append("")
    lines.append("%-20s %8s %7s %8s %9s %8s %-9s" %
                 ("candidate", "recall@b", "fpr@b", "tok/rep", "tok/inc", "evidence", "verdict"))
    lines.append("-" * 96)

    def row(m: Measurement, label: str, verdict: str) -> str:
        incident = tokens_per_incident(m)
        return "%-20s %8s %7s %8s %9s %8s %-9s" % (
            label,
            "n/a" if m.recall_at_budget is None else "%.3f" % m.recall_at_budget,
            "n/a" if m.fpr_at_budget is None else "%.3f" % m.fpr_at_budget,
            "n/a" if m.tokens_per_report is None else "%.0f" % m.tokens_per_report,
            "n/a" if incident is None else "%.0f" % incident,
            "%.0f%%" % (100 * m.coverage_floor) if m.coverage else "n/a",
            verdict,
        )

    lines.append(row(result.baseline, "baseline", "-"))
    for key in sorted(result.references, key=lambda value: int(value)):
        lines.append(row(result.references[key], "baseline@%s" % key, "reference"))
    for candidate, measurement, verdict in result.trials:
        lines.append(row(measurement, candidate.name, "accept" if verdict.accepted else "reject"))
    lines.append("")
    if result.references:
        lines.append("a candidate that changes the window is compared against 'baseline@<window>', not")
        lines.append("against the deployment as it stands: the same configuration at another window is a")
        lines.append("different amount of traffic per sample, and crediting that to the candidate would")
        lines.append("report a geometry change as a detection gain (boundary 68).")
    lines.append("recall@b: best recall at <=%.0f%% false positives, the cut chosen per configuration, because"
                 % (100 * result.rules.fpr_budget))
    lines.append("an absolute threshold was calibrated on another domain and is not comparable here.")
    split_rows = []
    baseline_split = result.baseline.recall_by_recording or {}
    for label, measurement in ([("baseline", result.baseline)]
                               + [(candidate.name, m) for candidate, m, _v in result.trials]):
        after = measurement.recall_by_recording or {}
        names = sorted(set(baseline_split) & set(after))
        parts = ["%s %.3f -> %.3f" % (name, baseline_split[name], after[name])
                 for name in names
                 if baseline_split.get(name) is not None and after.get(name) is not None]
        if len(parts) >= 2:
            split_rows.append("  %-20s %s" % (label, "  ".join(parts)))
    if split_rows:
        # The pooled column is one division over every recording's incidents, so it cannot say which one
        # moved. Printed next to the table rather than on request, because a +5% pooled reading that is
        # one host's is exactly the case a reader has no way to notice (round 39, boundary 86).
        lines.append("  recall@b by recording:")
        lines.extend(split_rows)
    for candidate, measurement, _verdict in [(None, result.baseline, None)] + list(result.trials):
        label = "baseline" if candidate is None else candidate.name
        if measurement.recall is None or measurement.fpr is None:
            continue
        lines.append("  shipped threshold  %-22s recall %.3f  fp/1k %6.0f  fpr %.3f"
                     % (label, measurement.recall, alerts_per_1000(measurement), measurement.fpr))
    for candidate, measurement, _verdict in [(None, result.baseline, None)] + list(result.trials):
        label = "baseline" if candidate is None else candidate.name
        if measurement.recall_at_free_cut is None:
            continue
        ceiling = measurement.label_free_ceiling
        lines.append("  label-free cut     %-22s recall %.3f  fpr %s  of at most %s"
                     % (label, measurement.recall_at_free_cut,
                        "n/a" if measurement.fpr_at_free_cut is None
                        else "%.3f" % measurement.fpr_at_free_cut,
                        "n/a" if ceiling is None else "%.3f" % ceiling))
    if any(m.recall_at_free_cut is not None
           for _c, m, _v in [(None, result.baseline, None)] + list(result.trials)):
        lines.append("  label-free: the bar a deployment can compute from its own scores at the %.0f%% alert rate."
                     % (100 * result.rules.alert_budget))
        lines.append("  It controls alert rate, not false-positive rate: paging on the top %.0f%% of windows cannot"
                     % (100 * result.rules.alert_budget))
        lines.append("  find more than %.0f%% / prevalence of the faults, so 'of at most' is that cap,"
                     " not a target." % (100 * result.rules.alert_budget))
    for candidate, measurement, _verdict in [(None, result.baseline, None)] + list(result.trials):
        label = "baseline" if candidate is None else candidate.name
        if not measurement.alerts_at_free_cut or not measurement.scored_at_free_cut:
            continue
        lines.append("  label-free alerts  %-22s %4d / %4d  %5.1f%%  (target %.1f%%)"
                     % (label, measurement.alerts_at_free_cut, measurement.scored_at_free_cut,
                        100 * measurement.alerts_at_free_cut / measurement.scored_at_free_cut,
                        100 * measurement.alert_budget))
    if (result.baseline.fpr is not None
            and result.baseline.fpr > result.rules.deploy_fpr_ceiling + 1e-9):
        lines.append("  ! the deployed configuration already alerts on %.0f%% of windows on this stream,"
                     % (100 * result.baseline.fpr))
        lines.append("    above the %.0f%% deploy ceiling. That is a finding about the deployment, not a"
                     % (100 * result.rules.deploy_fpr_ceiling))
        lines.append("    licence for a candidate to inherit it.")
    lines.append("")
    for candidate, _measurement, verdict in result.trials:
        lines.append("%s %-22s %s" % ("+" if verdict.accepted else "x", candidate.name,
                                      _hang("", verdict.reason, 68, 26)))
    if not result.trials:
        lines.append("no candidates were tried")
    if result.ceiling:
        by_stream = result.ceiling.get("by_stream") or {}
        text_by_stream = result.ceiling.get("text_by_stream") or {}
        lines.append("")
        lines.append("state ceiling  what a trivial reader of the same digest recovers")
        for name in sorted(by_stream):
            fields = by_stream[name]
            text = text_by_stream.get(name)
            engine = (result.baseline.auc_by_recording or {}).get(name)
            lines.append("  %-22s fields %-5s  text %-5s  engine %-5s"
                         % (name,
                            "n/a" if fields is None else "%.3f" % fields,
                            "n/a" if text is None else "%.3f" % text,
                            "n/a" if engine is None else "%.3f" % engine))
        lines.append("  'fields' reads the MetricValue tree, 'text' reads only the characters the engine")
        lines.append("  is given, 'engine' is what the model did with them. A gap between the first two")
        lines.append("  is the renderer's; a gap with the third is the model's, and no cost change moves it.")
    lines.append("")
    lines.append("Interpretation: 'accept' means one measured configuration was better than another on this")
    lines.append("stream and lost nothing this evaluator can see. It is not a claim that the change is right")
    lines.append("in production — the evidence column is what stands in for the explanation quality that")
    lines.append("cannot be measured without a configured model.")
    return "\n".join(lines)


@dataclass
class Rejudge:
    """A stored round re-decided under the rules in force now."""

    path: str
    stream: Dict[str, Any]
    old_rules: Dict[str, Any]
    rules: EvolveRules
    rows: List[Tuple[Candidate, Measurement, Verdict, Verdict]] = field(default_factory=list)
    withheld: List[str] = field(default_factory=list)
    """Trials the record cannot reconstruct: their window differs from the baseline's and no reference
    was stored. Reporting a verdict for them would report the replay's own comparison, not the round's."""

    @property
    def changed(self) -> List[str]:
        return [candidate.name for candidate, _m, old, new in self.rows
                if old.accepted != new.accepted]

    @property
    def relabelled(self) -> List[Tuple[str, str, str]]:
        """Same verdict, different rule — the acceptance stands but for a better reason.

        Round 7's `operational` path does exactly this to round 6's two quality accepts: the change is
        still accepted, but now because it made the recorded configuration deployable rather than
        because it found more incidents inside a budget. Reporting it keeps the rule history visible;
        folding it into "no verdict changed" would hide the one thing a rejudge is for.
        """
        return [(candidate.name, old.rule, new.rule) for candidate, _m, old, new in self.rows
                if old.accepted == new.accepted and old.rule != new.rule]


def rejudge(path: Path, rules: EvolveRules) -> Rejudge:
    """Replay a recorded round's measurements through `decide` with the rules in force today.

    The survey's second open problem is versioned, reproducible validation: results have to be
    comparable across campaigns, or a rule change silently rewrites history and nobody can tell an
    improvement from a bookkeeping change. This re-reads the stored measurements — no re-measurement, no
    Laya — and reports which verdicts move. Round 5's `deploy` rule exists because a stored round turned
    out to have accepted a change that alerting behaviour disqualified; replaying it is how that claim
    gets checked instead of asserted.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    baseline = Measurement.from_dict(payload.get("baseline") or {})
    rows: List[Tuple[Candidate, Measurement, Verdict, Verdict]] = []
    withheld: List[str] = []
    for trial in payload.get("trials") or []:
        candidate = Candidate.from_dict(trial.get("candidate") or {})
        measurement = Measurement.from_dict(trial.get("measurement") or {})
        stored = trial.get("verdict") or {}
        old = Verdict(
            candidate=stored.get("candidate") or candidate.name,
            accepted=bool(stored.get("accepted")),
            rule=stored.get("rule") or "?",
            reason=stored.get("reason") or "",
            # The detail is the part that can be re-decided: dropping it here would make the stored refusal
            # a sentence again, and round 40's whole point is that the numbers survive the round trip.
            detail=dict(stored.get("detail") or {}),
        )
        reference = recorded_reference(payload, measurement, baseline)
        if reference is None:
            withheld.append(candidate.name)
            continue
        new = decide(reference, measurement, rules, veto=candidate.veto)
        rows.append((candidate, measurement, old, new))
    return Rejudge(
        path=str(path),
        stream=payload.get("stream") or {},
        old_rules=payload.get("rules") or {},
        rules=rules,
        rows=rows,
        withheld=withheld,
    )


def format_rejudge(result: Rejudge) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - rejudge a recorded round under today's rules")
    lines.append("=" * 96)
    lines.append("round             %s" % result.path)
    lines.append("stream            %s" % json.dumps(result.stream, sort_keys=True))
    lines.append("recorded rules    %s" % json.dumps(result.old_rules, sort_keys=True))
    lines.append("current rules     %s" % json.dumps(result.rules.to_dict(), sort_keys=True))
    lines.append("")
    lines.append("%-22s %-22s %-22s %s" % ("candidate", "recorded", "re-decided", "change"))
    lines.append("-" * 96)
    for candidate, _measurement, old, new in result.rows:
        before = "%s %s" % ("accept" if old.accepted else "reject", old.rule)
        after = "%s %s" % ("accept" if new.accepted else "reject", new.rule)
        if old.accepted != new.accepted:
            moved = "-> accept" if new.accepted else "-> reject"
        elif old.rule != new.rule:
            moved = "same verdict, new rule"
        else:
            moved = "-"
        lines.append("%-22s %-22s %-22s %s" % (candidate.name, before, after, moved))
    lines.append("")
    changed = result.changed
    relabelled = result.relabelled
    if changed:
        for candidate, _measurement, old, new in result.rows:
            if old.accepted != new.accepted:
                lines.append("%-22s was %s: %s" % (candidate.name,
                                                   "accepted" if old.accepted else "rejected",
                                                   _hang("", old.reason, 60, 32)))
                lines.append("%-22s now  %s: %s" % ("", "accepted" if new.accepted else "rejected",
                                                   _hang("", new.reason, 60, 32)))
                stored_tolerance = (old.detail or {}).get("tolerance")
                if (stored_tolerance is not None
                        and abs(float(stored_tolerance) - result.rules.replication_bar) > 1e-9):
                    # The disagreement is about a bar, not about a measurement, so say which bar the round
                    # used: otherwise the reader cannot tell a re-decided judgement from a re-measured fact.
                    lines.append("%-22s the refusal used tolerance %.3f against %.3f in today's rules"
                                 % ("", float(stored_tolerance), result.rules.replication_bar))
        lines.append("")
        lines.append("%d verdict(s) changed. The stored measurements were not re-run; only the rules moved."
                     % len(changed))
    else:
        if result.withheld:
            # "Today's rules agree with the recorded round" would be a claim about trials this replay never
            # looked at, which is the class of sentence this round exists to remove.
            lines.append("no re-decided verdict changed: %d trial(s) could not be replayed at all."
                         % len(result.withheld))
        else:
            lines.append("no verdict changed: today's rules agree with the recorded round.")
    if result.withheld:
        lines.append("")
        lines.append("%d trial(s) were not re-decided: the record does not say which reference the round"
                     % len(result.withheld))
        lines.append("compared them with, so replaying them against the stored baseline would measure a")
        lines.append("different geometry and report it as a rule change (boundary 71):")
        for name in result.withheld:
            lines.append("  %s" % name)
    if relabelled:
        lines.append("")
        by_name = {candidate.name: new for candidate, _m, _old, new in result.rows}
        for name, old_rule, new_rule in relabelled:
            verdict = by_name.get(name)
            outcome = "accepted" if verdict is not None and verdict.accepted else "rejected"
            lines.append("%-22s still %s, but the rule that decides it moved %s -> %s"
                         % (name, outcome, old_rule, new_rule))
    return "\n".join(lines)


@dataclass
class Audit:
    """Every recorded round re-decided under the rules in force now.

    The survey's versioned-infrastructure problem in one object: a round is a measurement plus the
    ruleset that judged it, and a ruleset that has since moved must be able to show which stored verdicts
    it would now decide differently. `Moved` counts *verdicts*, not rounds, because one round can hold
    ten candidates and moving one of them is a different fact from moving all ten.
    """

    rules: EvolveRules
    fingerprint: str
    rounds: int
    trials: int
    entries: List[Dict[str, Any]] = field(default_factory=list)
    # Rounds the ledger still holds but no longer asks the mechanism to answer for (round 34).
    retired: int = 0
    # Round 81: the replacement links the ledger claims, each checked against the other side. Kept apart
    # from `entries` because a broken link is not a verdict that moved: it says the books disagree with
    # each other, which no amount of re-deciding under today's rules can fix.
    links: List[Dict[str, Any]] = field(default_factory=list)
    # Round 83: successors whose end of a link was written by a repair. Counted on its own because agreeing
    # by copying is not agreeing as two witnesses, and an audit that folds them in makes its own repair look
    # like corroboration.
    backfilled: List[str] = field(default_factory=list)
    #: Round 112: link decisions a person recorded. Printed apart from the findings, because agreement that
    #: came from a decision is not agreement that came from a check.
    settled: List[Dict[str, Any]] = field(default_factory=list)
    # Round 118: the decisions that rest on books which have moved since the decision was taken.
    settled_moved: List[Dict[str, Any]] = field(default_factory=list)
    # Round 85: versions the deployment states say are running whose producing record the books dispute or
    # that a repair copied. Round 84 refused to *stack* a promise there; once a version is applied that refusal
    # is not available, so the fact is reported here instead -- and charged when the two books contradict.
    in_force: List[Dict[str, Any]] = field(default_factory=list)
    # Round 86: disputes this run no longer sees. A fact that stopped being true is reported once -- by name --
    # and then forgotten, so the page never remembers a standing debt that has cleared.
    in_force_cleared: List[Dict[str, Any]] = field(default_factory=list)
    # Round 86: the copies that have stood past the grace period with no verdict that could test the claim. The
    # judgement is made once, by `stale_in_force_disputes`, and handed to both the page and the gate; deciding it
    # twice is how the page came to promise a charge the gate did not make.
    in_force_stale: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def link_content(self) -> bool:
        """Whether `_link_lines` has anything to say at all (round 87).

        The gate used to be `links or backfilled`, written when those were the only two blocks. Rounds 85-87 hung
        the in-force rows, the stale rows and the cleared history off the same printer, so a ledger with none of
        the first two and one of the others printed nothing -- a page that stays silent because an unrelated fact
        is absent (boundary 184). One property, read by both call sites, is the only place that decides.
        """
        return bool(self.links or self.backfilled or self.in_force or self.in_force_cleared
                    or self.in_force_stale or self.settled or self.settled_moved)

    @property
    def broken_links(self) -> int:
        """Links where both sides exist and disagree, or one side points at nothing (round 81)."""
        return sum(1 for item in self.links if item.get("kind") in LINK_DEFECTS)

    @property
    def link_legacy(self) -> int:
        """Links whose successor end could not have been written (pre-round-78 records)."""
        return sum(1 for item in self.links if item.get("kind") == "legacy")

    @property
    def in_force_broken(self) -> int:
        """Versions already deployed on a record whose two ends contradict each other (round 85)."""
        return sum(1 for item in self.in_force if item.get("broken"))

    @property
    def in_force_copied(self) -> int:
        """Versions already deployed on an end a repair copied (round 85): reported, never charged.

        The copy does not make the deployed configuration worse -- it was running before the repair wrote the
        missing end -- and no command can re-write that end while the version stays in force. Charging it would
        be a gate nobody can clear (boundary 171).
        """
        return len(self.in_force) - self.in_force_broken

    @property
    def moved(self) -> int:
        return sum(len(entry["changed"]) for entry in self.entries)

    @property
    def relabelled(self) -> int:
        return sum(len(entry["relabelled"]) for entry in self.entries)

    @property
    def stale(self) -> int:
        """Rounds whose recorded ruleset differs from today's, by fingerprint."""
        return sum(1 for entry in self.entries if entry["recorded_hash"] not in (None, "", self.fingerprint))

    @property
    def cross(self) -> int:
        """Trials whose comparison the record cannot reconstruct, so they carry no re-decided verdict.

        Round 32: a window change is compared against a reference measured at its own window. A record
        that stores the trial's window, a baseline at another window and no reference for the difference
        cannot be replayed honestly — the only comparison available is one the round never made.
        """
        return sum(len(entry["cross"]) for entry in self.entries)

    @property
    def inconsistent(self) -> int:
        """Unreconstructable comparisons in records that carry the geometry and still lack the reference.

        Round 33. Two different debts hide behind `cross`. A round written before round 32 could not
        record which reference each trial used — the field did not exist — so nothing about it is wrong,
        it is only unreadable, and re-running it writes a new record. A round written *after* the field
        exists that still cannot name its reference is a defect in this build. Charging both to the same
        gate would make `--strict` fail forever on history, and a check that always fails is a check
        nobody runs.
        """
        return sum(1 for entry in self.entries for item in entry["cross"] if item["kind"] == "missing")


def audit(root: Path, rules: Optional[EvolveRules] = None,
          in_force: Optional[Sequence[Mapping[str, Any]]] = None,
          in_force_cleared: Optional[Sequence[Mapping[str, Any]]] = None,
          in_force_stale: Optional[Sequence[Mapping[str, Any]]] = None) -> Audit:
    """Re-decide every stored round under `rules` and report what moves.

    No measurement is re-run and no Laya call is made: this answers "do the records still say what this
    objective says", which is the question a rule change raises. A round whose file is unreadable or
    predates `rules_hash` is still audited — the fingerprint is then reported as unrecorded rather than
    guessed, because inventing one would make an old record look versioned.
    """
    active = rules if rules is not None else EvolveRules()
    fingerprint = rules_fingerprint(active)
    entries: List[Dict[str, Any]] = []
    rounds = 0
    trials = 0
    retired = 0
    for record in history(root, include_retired=True):
        name = str(record.get("round") or "?")
        if record.get("retired"):
            retired += 1
            continue
        payload = dict(record)
        baseline = Measurement.from_dict(payload.get("baseline") or {})
        changed: List[Dict[str, Any]] = []
        relabelled: List[Dict[str, Any]] = []
        cross: List[str] = []
        rounds += 1
        for trial in payload.get("trials") or []:
            trials += 1
            candidate = Candidate.from_dict(trial.get("candidate") or {})
            measurement = Measurement.from_dict(trial.get("measurement") or {})
            stored = trial.get("verdict") or {}
            old = Verdict(
                candidate=stored.get("candidate") or candidate.name,
                accepted=bool(stored.get("accepted")),
                rule=stored.get("rule") or "?",
                reason=stored.get("reason") or "",
            )
            reference = recorded_reference(payload, measurement, baseline)
            if reference is None:
                # Round 33: say which debt this is. No `window` means the record predates the field;
                # a `window` with no reference means this build lost something it should have kept.
                cross.append({"candidate": candidate.name,
                              "kind": "legacy" if measurement.window is None else "missing"})
                continue
            new = decide(reference, measurement, active, veto=candidate.veto)
            if old.accepted != new.accepted:
                changed.append({"candidate": candidate.name, "was": old.rule, "now": new.rule,
                                "was_accepted": old.accepted, "now_accepted": new.accepted})
            elif old.rule != new.rule:
                relabelled.append({"candidate": candidate.name, "was": old.rule, "now": new.rule,
                                   "accepted": old.accepted})
        if changed or relabelled or cross:
            entries.append({
                "round": name,
                "stream": (payload.get("stream") or {}).get("source") or "?",
                "recorded_hash": payload.get("rules_hash"),
                "changed": changed,
                "relabelled": relabelled,
                "cross": cross,
            })
    return Audit(rules=active, fingerprint=fingerprint, rounds=rounds, trials=trials,
                 entries=entries, retired=retired,
                 links=replacement_links(history(root, include_retired=True), load_settlements(root)),
                 settled=load_settlements(root),
            settled_moved=moved_settlements(history(root, include_retired=True), load_settlements(root)),
                 backfilled=backfilled_links(history(root, include_retired=True)),
                 in_force=[dict(item) for item in (in_force or [])],
                 in_force_cleared=[dict(item) for item in (in_force_cleared or [])],
                 in_force_stale=[dict(item) for item in (in_force_stale or [])])


def _limit_terms(limits: Optional[Mapping[str, Any]]) -> Optional[Tuple[Optional[float],
                                                                       Tuple[Tuple[str, float], ...],
                                                                       Tuple[Tuple[str, float], ...]]]:
    """The part of a ceilings payload that decides a verdict, as one comparable value (round 68).

    Origins are deliberately left out: `(state)` and `(command line)` answer "who decided", not "what was the
    limit", and a round judged under ten minutes is judged under ten minutes whichever route recorded it.
    """
    if not limits:
        return None
    return (
        None if limits.get("max_minutes") is None else float(limits["max_minutes"]),
        tuple(sorted((str(name), float(value))
                     for name, value in (limits.get("max_minutes_by_domain") or {}).items())),
        tuple(sorted((str(name), float(value))
                     for name, value in (limits.get("max_drain_by_domain") or {}).items())),
    )


def limit_drift(rounds: Sequence[Mapping[str, Any]],
                limits: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Which recorded rounds were judged under ceilings that differ from today's (round 68).

    A ceiling is not part of `EvolveRules`, so `rules_hash` cannot notice it moving: the same rulebook
    re-decides the same measurements and gets a different answer purely because somebody tightened a wait in
    a deployment state. This is the comparison that makes that visible, and it is a *count of rounds*, not a
    diff of numbers, because the gate has to decide whether to fail.

    A round written before ceilings were recorded is counted separately and never guessed into either
    column: saying "unknown" is the only honest report, and treating it as equal would silently claim that
    history was judged under today's limits (boundary 147).
    """
    today = _limit_terms(limits)
    differ: List[str] = []
    unrecorded = 0
    live = [record for record in rounds if not record.get("retired")]
    for record in live:
        recorded = _limit_terms(record.get("limits"))
        if recorded is None:
            unrecorded += 1
            continue
        if recorded != today:
            differ.append(str(record.get("round") or "?"))
    return {"rounds": len(live), "differ": differ, "unrecorded": unrecorded}


def format_limit_drift(drift: Mapping[str, Any]) -> str:
    """One sentence about how many rounds were judged under other ceilings (round 68)."""
    differ = len(drift.get("differ") or [])
    unrecorded = int(drift.get("unrecorded") or 0)
    rounds = int(drift.get("rounds") or 0)
    if not rounds:
        return "ceilings          no recorded round yet, so there is nothing whose ceilings could differ"
    parts = ["ceilings          "]
    if differ:
        names = ", ".join(str(name) for name in (drift.get("differ") or [])[:4])
        more = "" if differ <= 4 else ", ..."
        parts.append("%d of %d round(s) were judged under ceilings that differ from today's (%s%s); "
                     "the rules did not change, the limits did" % (differ, rounds, names, more))
    else:
        parts.append("every recorded round was judged under today's ceilings")
    if unrecorded:
        parts.append("; %d round(s) do not say which ceilings they were judged under" % unrecorded)
    return "".join(parts)


def round_limits_text(limits: Optional[Mapping[str, Any]]) -> str:
    """The wait ceilings one recorded round was judged under, as one phrase (round 70).

    `--audit` and `--campaign` compare recorded ceilings against today's and report how many rounds moved;
    neither answers the smaller question a reader has when looking at one line of `--history`: what was this
    round allowed to wait for? A ledger that lists a round's promises, its file and its verdict but not the
    limits it was judged under reads as one continuous experiment, when a tightening in between can be the
    whole difference between two rows (boundary 146).

    Origins (`state` / `flag`) are deliberately not printed here: a round records what limited it, and which
    of the two wrote the number is a fact about the command that ran, already answered by the page's `limits`
    line. Printing it per round would repeat that line fifty times.
    """
    if not limits:
        return "no ceilings recorded"
    parts: List[str] = []
    blank = limits.get("max_minutes")
    if blank is not None:
        parts.append("one run %.1f minute(s) for every domain" % float(blank))
    for name, value in sorted((limits.get("max_minutes_by_domain") or {}).items()):
        parts.append("one run %.1f minute(s) on %s" % (float(value), name))
    drain_all = limits.get("max_drain_minutes")
    if drain_all is not None:
        parts.append("a drain of %.1f minute(s) for every domain" % float(drain_all))
    for name, value in sorted((limits.get("max_drain_by_domain") or {}).items()):
        parts.append("a drain of %.1f minute(s) on %s" % (float(value), name))
    if not parts:
        return "no ceilings recorded"
    return "; ".join(parts)


def ceiling_shortfall(rounds: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Per domain, how many live rounds record the wait ceilings they were judged under (round 71).

    Round 70 put each round's ceilings on its `--history` line, and round 69 counted them in the campaign
    header. Both counted the whole ledger, so a reader could see "38 do not say" and still not know which
    stream owed them, nor whether any command could pay. A per-domain count is the smallest form that answers
    both: retired rounds are excluded because their records stand as written and no re-run pays them.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for record in rounds:
        if record.get("retired"):
            continue
        name = str((record.get("stream") or {}).get("source") or "(unattributed)")
        bucket = buckets.setdefault(name, {"domain": name, "rounds": 0, "missing": 0})
        bucket["rounds"] += 1
        if not record.get("limits"):
            bucket["missing"] += 1
    rows: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["share"] = float(bucket["missing"]) / float(bucket["rounds"]) if bucket["rounds"] else 0.0
        rows.append(bucket)
    rows.sort(key=lambda row: (-row["share"], str(row["domain"])))
    return rows


def format_ceiling_shortfall(rows: Sequence[Mapping[str, Any]], payable: int = 0,
                             named: int = 0, command: Optional[str] = None) -> Optional[str]:
    """One sentence about the rounds that do not record their ceilings, and whether a command could change it.

    The number is only worth printing when it can move: `payable` is how many of the missing rounds the re-run
    queue could measure again, and each such round writes a new record that carries its ceilings. When the
    remainder is zero the default `--max-limit-drift` cap would arm by itself, which is the sentence a reader
    needs to decide whether to start the queue at all (item 57's leftover, boundary 151).

    Round 74: when a command can lower the debt the sentence ends by naming it. A reader told "this would arm
    the gate" and not told what to type has a diagnosis instead of a next step (round 59's lesson, applied to
    the part of the debt that is still missing rather than to the queue's order).
    """
    missing = sum(int(row.get("missing") or 0) for row in rows)
    total = sum(int(row.get("rounds") or 0) for row in rows)
    if not missing:
        return None
    text = "%d of %d round(s) do not record the ceilings they were judged under" % (missing, total)
    payable = max(0, min(int(payable), missing))
    named = max(0, min(int(named), missing - payable))
    # Round 73: a round that named its file is one the queue could not reach before (`_unrecorded_matters` lists
    # only rounds whose *file* is unknown), so it was counted as unpayable and the sentence said so. The queue
    # now has a path for it, and the sentence names how many of the remainder became reachable.
    reachable = "; %d of them name their file, so --with-ceilings can re-measure them" % named if named else ""
    if not payable:
        if named:
            body = text + ", and none of them can be re-run from the record as it stands" + reachable
        else:
            body = text + ", and none of them can be re-run, so the default --max-limit-drift cap stays a warning"
    else:
        left = missing - payable
        if not left:
            body = (text + "; re-running %d of them would leave none, so the default --max-limit-drift cap "
                           "would arm%s" % (payable, reachable))
        else:
            body = (text + "; re-running %d of them would leave %d without one, so the default --max-limit-drift "
                           "cap stays a warning%s" % (payable, left, reachable))
    if command and (payable or named):
        body += "; measure them with %s" % command
    return body


def format_audit(result: Audit) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - audit every recorded round against today's rules")
    lines.append("=" * 96)
    lines.append("current rules     %s  (%s)" % (result.fingerprint,
                                                json.dumps(result.rules.to_dict(), sort_keys=True)))
    re_decided = result.trials - result.cross
    if result.cross:
        lines.append("recorded rounds   %d  (%d candidate measurements re-decided, %d not reconstructable, "
                     "no re-run)" % (result.rounds, re_decided, result.cross))
    else:
        lines.append("recorded rounds   %d  (%d candidate measurements re-decided, no re-run)"
                     % (result.rounds, re_decided))
    if result.retired:
        lines.append("retired           %d round(s) were re-measured and are not re-decided; their records "
                     "stay as written" % result.retired)
    lines.append("")
    if not result.entries:
        lines.append("every recorded verdict survives today's rules.")
        if result.link_content:
            lines.append("")
            lines.extend(_link_lines(result))
        return "\n".join(lines)
    lines.append("%-26s %-10s %-14s %s" % ("round", "stream", "recorded rules", "what moves today"))
    lines.append("-" * 96)
    for entry in result.entries:
        recorded = entry["recorded_hash"] or "unrecorded"
        if entry["changed"]:
            detail = ", ".join("%s %s -> %s" % (item["candidate"], item["was"], item["now"])
                               for item in entry["changed"])
        elif entry["relabelled"]:
            detail = ", ".join("%s %s -> %s (same verdict)" % (item["candidate"], item["was"], item["now"])
                               for item in entry["relabelled"])
        else:
            detail = "%d comparison(s) not reconstructable" % len(entry["cross"])
        lines.append("%-26s %-10s %-14s %s" % (entry["round"], entry["stream"], recorded, detail))
    lines.append("")
    if result.moved:
        lines.append("%d verdict(s) would be decided differently today; %d more kept their verdict but "
                     "changed rule. Use `brian evolve --rejudge <round>` to read one case in full."
                     % (result.moved, result.relabelled))
    else:
        lines.append("no verdict moves. %d candidate measurement(s) kept their verdict but changed rule."
                     % result.relabelled)
    if result.stale:
        lines.append("%d recorded round(s) carry a different ruleset fingerprint, so their numbers were "
                     "measured under an objective that no longer exists." % result.stale)
    if result.cross:
        lines.append("")
        lines.append("%d comparison(s) could not be reconstructed: the record does not say which reference"
                     % result.cross)
        lines.append("the round compared them with, so replaying them against the stored baseline would")
        lines.append("measure a different geometry and report that as a rule change (boundary 71). No")
        lines.append("verdict is reported for them.")
        if result.inconsistent:
            lines.append("%d of them come from records that carry the geometry and still lack the "
                         "reference: that" % result.inconsistent)
            lines.append("is a defect in this build, not a record written before the field existed.")
        else:
            lines.append("All of them predate the geometry field: they cannot be replayed, but re-running "
                         "such a round")
            lines.append("writes a record that can be.")
    if result.link_content:
        lines.append("")
        lines.extend(_link_lines(result))
    return "\n".join(lines)


def _link_lines(result: Audit) -> List[str]:
    """The replacement links, split into the ones this build broke and the ones history could not write.

    Round 81. Two ends of one relation, two commands, no comparison between them: without this a reader
    learns that round A was re-measured as B from A, and that B replaced C from B, and never finds out that
    B and C disagree. Only the charged kinds are printed as defects.
    """
    lines: List[str] = []
    broken = [item for item in result.links if item.get("kind") in LINK_DEFECTS]
    legacy = [item for item in result.links if item.get("kind") == "legacy"]
    if broken:
        lines.append("%d replacement link(s) disagree, so the two ends of the relation cannot both be true:"
                     % len(broken))
        for item in broken:
            lines.append("  %-26s %-9s %s: %s" % (item["round"], item["kind"],
                                                  ", ".join(item.get("names") or []), item["detail"]))
        lines.append("A link is written by a command; a broken one means the books contradict each other "
                     "and re-deciding")
        lines.append("under today's rules cannot settle it. `--strict` charges these as a build defect.")
    if legacy:
        lines.append("%d replacement link(s) have no successor-side record because the successor was written "
                     "before round 78:" % len(legacy))
        for item in legacy:
            lines.append("  %-26s -> %s (%s)" % (item["round"], item["names"], item["detail"]))
        lines.append("Nothing is wrong with those records; the field did not exist. Re-running the round "
                     "writes both ends.")
    if result.backfilled:
        # Round 83: a repaired end agrees with the other side because it was copied from it. Printed apart
        # from both the defects and the legacy list so that "every link agrees" never silently includes an
        # agreement this build wrote (boundary 174).
        lines.append("%d replacement link(s) were backfilled by a repair, so their two ends agree by copying:"
                     % len(result.backfilled))
        lines.append("  %s" % ", ".join(sorted(result.backfilled)))
        lines.append("Counted separately on purpose: a copied end is not a second witness, and the repair is "
                     "not evidence")
        lines.append("that the replacement happened.")
    if result.settled:
        # Round 112: somebody decided these pairs. Printed as its own block: a settlement ends a charge, it does
        # not produce evidence, and "every link agrees" must never quietly include a decision (boundary 174).
        lines.append("%d replacement link(s) were settled by a recorded decision, not by agreement:"
                     % len(result.settled))
        unattributed = 0
        unexplained = 0
        explained_later = 0
        for item in result.settled:
            by = str(item.get("by") or "").strip()
            if not by:
                unattributed += 1
            if not str(item.get("why") or "").strip():
                unexplained += 1
            if item.get("notes"):
                explained_later += 1
            lines.append("  %-26s -> %-26s  (%s%s)"
                         % (str(item.get("record") or ""), str(item.get("successor") or ""),
                            str(item.get("at") or ""),
                            ", by %s" % by if by else ", and no decider was recorded"))
        if result.settled_moved:
            # Round 118: the decision was about books that have since moved. Reported, not charged: nothing in
            # this build can re-open a decision, and a gate that fails on a sentence no command clears is the
            # gate nobody runs (boundaries 154/171/266).
            lines.append("%d of them rest on books that have moved since the decision was taken:"
                         % len(result.settled_moved))
            for item in result.settled_moved:
                lines.append("  %-26s -> %-26s  (%s)"
                             % (str(item.get("record") or ""), str(item.get("successor") or ""),
                                "; ".join(str(reason) for reason in item.get("reasons") or [])))
            lines.append("A decision is a statement about the books as they stood; when they move, the "
                         "decision must be taken again (boundary 269).")
        lines.append("A settlement says who decided; it is not a second witness. `--audit` no longer charges "
                     "these pairs,")
        lines.append("and `brian evolve --history` still prints both ends where they disagree.")
        if unattributed:
            # Round 113: the sentence above claims attribution, so a store without one has to say so rather than
            # let a reader assume a person signed it (boundary 254).
            lines.append("%d of them were recorded without --settle-by: the decision is dated but not attributed,"
                         % unattributed)
            lines.append("and a decision nobody is named for is the one nobody can be asked to defend.")
        if unexplained:
            # Round 113: rounds before this one accepted a settlement with no reason at all, so a real store can
            # hold one. Round 114 gives it a way to gain one; this line keeps saying that the reason did not come
            # with the decision (boundary 257).
            lines.append("%d of them carry no reason from the moment they were taken: written before --settle-why "
                         "was required, and a later note does not make the original decision explained" % unexplained)
        if explained_later:
            # Round 114: the note is an amendment, not the original judgement. Printed apart so a reader cannot
            # read "explained" as "explained at the time" (boundary 257).
            for item in result.settled:
                notes = item.get("notes") or []
                if not notes:
                    continue
                last = notes[-1] if isinstance(notes[-1], dict) else {}
                author = str(last.get("by") or "").strip()
                lines.append("  %-26s -> %-26s  explained later, on %s%s"
                             % (str(item.get("record") or ""), str(item.get("successor") or ""),
                                str(last.get("at") or ""), ", by %s" % author if author else ""))
            lines.append("%d of them were explained after the fact: the decision keeps its date, and the "
                         "explanation came later." % explained_later)
    if result.in_force:
        # Round 85: round 84 could refuse to stack a promise, because a version that is not yet in force can
        # simply not be applied. A version that is already applied is running; the refusal is gone, and what
        # remains is the fact plus the same split -- a contradiction is a defect, a copied end is a debt.
        lines.append("%d version(s) in force sit on a record whose provenance the books dispute:"
                     % len(result.in_force))
        stale_keys = {(str(item.get("domain") or "default"), int(item.get("version") or 0))
                      for item in result.in_force_stale}
        for item in result.in_force:
            age = item.get("age_days")
            seen = ("" if age is None else
                    "  [first seen %.1f day(s) ago, seen %d time(s)]"
                    % (float(age), int(item.get("observations") or 0)))
            if (str(item.get("domain") or "default"), int(item.get("version") or 0)) in stale_keys:
                if item.get("untested"):
                    seen += ("  [stale: a check on %s could not test the claim]"
                             % str((item.get("untested") or {}).get("at")))
                else:
                    seen += "  [stale: no verdict that could test the claim]"
            lines.append("  %-16s v%03d  %-13s %s%s" % (item.get("domain") or "default",
                                                       int(item.get("version") or 0),
                                                       item.get("kind"), item.get("detail"), seen))
        lines.append("A version is deployed, so refusing to stack it is no longer available. A contradiction is")
        lines.append("still a build defect (`--strict` exits 2); a copied end is a debt, named here, not charged.")
        if result.in_force_stale:
            lines.append("%d copy(ies) have stood longer than %.0f day(s) with no verdict that could test the"
                         % (len(result.in_force_stale), IN_FORCE_GRACE_DAYS))
            lines.append("claim about it, so `--strict` asks for a re-measurement of the deployed version: that is")
            lines.append("one command that clears it.")
            untested = [item for item in result.in_force_stale if item.get("untested")]
            if untested:
                # Round 89: a comparison that could not compare is not an answer, and the remedy is a
                # different measurement -- not the same command on the same stream.
                lines.append("%d of them were measured and the check could not test the claim (%s):"
                             % (len(untested), str((untested[0].get("untested") or {}).get("reason") or "")))
                lines.append("measuring again on the same stream compares nothing again, so the remedy is the "
                             "file that version recorded,")
                lines.append("or retiring the version whose claim cannot be checked (boundary 188).")
    if result.in_force_cleared:
        lines.append("%d version(s) in force are no longer disputed, and the ledger forgets them now:"
                     % len(result.in_force_cleared))
        for item in result.in_force_cleared:
            # Round 90: the citation names the evidence. "(unnamed)" is printed rather than omitted, because a
            # blank field reads as "no measurement was needed" (boundary 193).
            by = ("a re-measurement on %s of %s"
                  % (str(item.get("checked_at")), str(item.get("stream_path") or "(unnamed stream)"))
                  if item.get("by") == "verification" else
                  "no measurement: the ledger no longer reproduces it")
            if item.get("by") == "verification" and item.get("cited_by") == "measurement":
                # Round 88: written by the check that earned it, not deduced from timestamps afterwards.
                by += " (written by that measurement)"
            age = item.get("age_days")
            attempt = int(item.get("attempt") or 0)
            # Round 91: an entry written against a file this checkout cannot open is marked where it is listed,
            # because the row above still claims the debt was answered.
            missing = item.get("evidence") == EVIDENCE_MISSING
            lines.append("  %-16s v%03d  %-13s (first seen %s%s, cleared by %s%s%s)"
                         % (item.get("domain") or "default", int(item.get("version") or 0),
                            item.get("kind"), item.get("first_seen"),
                            "" if age is None else ", stood %.1f day(s)" % float(age), by,
                            "" if not attempt else ", attempt %d" % attempt,
                            "" if not missing else "  [evidence missing: %s]"
                            % (item.get("stream_path") or "")))
        attempted = [item for item in result.in_force_cleared if item.get("tested") is False]
        if attempted:
            lines.append("%d clearing(s) rested on a check that ran and could not test the claim, so they"
                         % len(attempted))
            lines.append("answer nothing: recorded, and counted apart on the campaign page (boundary 188).")
        if any(item.get("by") != "verification" for item in result.in_force_cleared):
            lines.append("A dispute that left the ledger without a measurement is not resolved, it is unobserved:")
            lines.append("counted apart on the campaign page, and charged by nothing (boundary 183).")
    return lines


@dataclass
class SensitivityRow:
    """One stream's refusals, and how many of them a looser replication bar would accept."""

    domain: str
    refused: int = 0
    recorded: int = 0
    flips: Dict[str, int] = field(default_factory=dict)
    # Round 48: the files the refusals in this row were measured on. Same reason as the campaign's column:
    # a row per dataset can add up two samplings, and a table that silently does that answers two questions
    # with one number.
    files: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"domain": self.domain, "refused": self.refused, "recorded": self.recorded,
                "flips": dict(self.flips), "stream_files": dict(sorted(self.files.items()))}


@dataclass
class Sensitivity:
    """How much the `replicate` refusals depend on the bar that produced them (round 41).

    A refusal is a judgement made at a tolerance, and round 40 stored the numbers behind it. This asks the
    obvious next question without measuring anything: at a looser bar, which of those refusals would be
    accepted? A refusal that flips at 1.5x the bar was never about the labels; one that needs 4x is.
    """

    bar: float
    bars: List[float]
    rules: EvolveRules
    rounds: int = 0
    rows: List[SensitivityRow] = field(default_factory=list)
    unreadable: int = 0

    @property
    def refused(self) -> int:
        return sum(row.refused for row in self.rows)

    @property
    def recorded(self) -> int:
        """Refusals that carry the numbers behind them, so a reader can check them without re-measuring."""
        return sum(row.recorded for row in self.rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bar": self.bar,
            "bars": list(self.bars),
            "rounds": self.rounds,
            "refused": self.refused,
            "recorded": self.recorded,
            "unreadable": self.unreadable,
            "rows": [row.to_dict() for row in self.rows],
        }


def _bar_ladder(bar: float, bars: Optional[Sequence[float]] = None) -> List[float]:
    """The bars to test: the one in force, then 2x and 4x it, deduped and ordered."""
    values = list(bars) if bars else [bar, 2 * bar, 4 * bar]
    out: List[float] = []
    for value in values:
        cleaned = round(float(value), 6)
        if cleaned not in out:
            out.append(cleaned)
    return sorted(out)


def refusal_sensitivity(root: Path, rules: Optional[EvolveRules] = None,
                        bars: Optional[Sequence[float]] = None) -> Sensitivity:
    """Re-decide every surviving `replicate` refusal at several bars, per stream, without measuring.

    `replicate` only exists for records that carry per-recording recall, so a round written before round 39
    contributes nothing here — and says so through `unreadable`, which counts trials that could not be
    replayed at all. Computing a sensitivity table over only the rounds that can be replayed and *not*
    saying how many could not would overstate its domain (boundary 90).

    The first bar in `ladder` is the one in force, and every count there must be zero: a bar cannot accept
    what it just refused. That is the table's own control.
    """
    active = rules if rules is not None else EvolveRules()
    bar = active.replication_bar
    ladder = _bar_ladder(bar, bars)
    rows: Dict[str, SensitivityRow] = {}
    unreadable = 0
    rounds = 0
    # Round 48: a refusal written as a `w###` record names its file (round 46), and that record is the more
    # authoritative place to read it from than the round that produced the verdict -- the round predates the
    # field in most cases here. Falling back to it keeps this table from contradicting `--withheld`, which
    # shows the same refusal with its file.
    recorded_paths = {record.name: record.stream_path for record in withheld_records(root)
                      if record.stream_path}
    for record in history(root):
        payload = dict(record)
        rounds += 1
        baseline = Measurement.from_dict(payload.get("baseline") or {})
        domain = str((payload.get("stream") or {}).get("source") or "(unattributed)")
        for trial in payload.get("trials") or []:
            candidate = Candidate.from_dict(trial.get("candidate") or {})
            measurement = Measurement.from_dict(trial.get("measurement") or {})
            reference = recorded_reference(payload, measurement, baseline)
            if reference is None:
                unreadable += 1
                continue
            verdict = decide(reference, measurement, active, veto=candidate.veto)
            if verdict.rule != "replicate":
                continue
            row = rows.setdefault(domain, SensitivityRow(domain=domain))
            row.refused += 1
            # Round 48: which file this refusal's round measured. Rounds before round 47 say nothing, and
            # that is printed as "(not recorded)" rather than deduced from the dataset name.
            #
            # Round 54: one file has one name. A Windows-spelled path and the same path with forward
            # slashes are one file, and counting them apart made the campaign row claim three samplings
            # over two (the same normalisation `campaign()` does; two pages of one ledger must agree).
            path = str(payload.get("stream_path") or recorded_paths.get(candidate.name) or "").replace("\\", "/")
            row.files[path or "(not recorded)"] = row.files.get(path or "(not recorded)", 0) + 1
            if (trial.get("verdict") or {}).get("detail"):
                row.recorded += 1
            for candidate_bar in ladder:
                loosed = EvolveRules(**{**active.to_dict(), "replication_tolerance": candidate_bar})
                again = decide(reference, measurement, loosed, veto=candidate.veto)
                key = "%.3f" % candidate_bar
                row.flips.setdefault(key, 0)
                if again.accepted:
                    row.flips[key] += 1
    ordered = [rows[name] for name in sorted(rows)]
    for row in ordered:
        for key in ("%.3f" % value for value in ladder):
            row.flips.setdefault(key, 0)
    return Sensitivity(bar=bar, bars=ladder, rules=active, rounds=rounds, rows=ordered,
                       unreadable=unreadable)


def format_sensitivity(result: Sensitivity) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - how much a refusal depends on the replication bar")
    lines.append("=" * 96)
    lines.append("current rules     %s" % rules_fingerprint(result.rules))
    lines.append("bar in force      %.3f" % result.bar)
    lines.append("refused           %d candidate(s) are refused for `replicate` today"
                 % result.refused)
    lines.append("with numbers      %d of them record the loss and the bar, so a reader can re-decide them"
                 % result.recorded)
    if result.unreadable:
        lines.append("not replayable    %d trial(s) could not be replayed at all, so they are not in the table"
                     % result.unreadable)
    lines.append("")
    if not result.rows:
        lines.append("no surviving round refuses a candidate for `replicate`, so there is nothing to test.")
        return "\n".join(lines)
    header = "%-22s %8s %10s" % ("domain", "refused", "with nums")
    for value in result.bars:
        header += " %8s" % ("%.3f" % value)
    lines.append(header)
    lines.append("-" * len(header))
    for row in result.rows:
        line = "%-22s %8d %10d" % (row.domain, row.refused, row.recorded)
        for value in result.bars:
            line += " %8d" % row.flips.get("%.3f" % value, 0)
        lines.append(line)
        known = {path: count for path, count in row.files.items() if path != "(not recorded)"}
        unknown = int(row.files.get("(not recorded)") or 0)
        if known:
            # Round 48: the row's counts pool the files it names. A reader comparing domains has to be able
            # to see that without going back to `--history`.
            named = ", ".join("%s (%d)" % (path, count)
                              for path, count in sorted(known.items(), key=lambda item: (-item[1], item[0])))
            if unknown:
                named += ", %d without a recorded file" % unknown
            lines.append("  measured on       %s" % named)
            if len(known) > 1:
                lines.append("  ! %d files here were sampled differently: this row's counts add up more than "
                             "one question" % len(known))
        elif unknown:
            lines.append("  measured on       none of them say which file they were measured on")
    lines.append("")
    lines.append("A column is how many of the refused candidates would be *accepted* at that bar. The first")
    lines.append("column is the bar in force, so it is zero by construction: a bar cannot accept what it")
    lines.append("just refused. A refusal that flips at 2x was a judgement about a small loss; one that")
    lines.append("needs 4x is a judgement about the labels. Nothing here is re-measured — which also means")
    lines.append("nothing here says which bar is right (boundary 89).")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    """Greedy word wrap. Returns lines, because every caller appends them one by one.

    It used to return one joined string; a caller then iterated it and printed the reason one
    character per line, which is how a formatting helper turns into a very quiet bug.
    """
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _hang(prefix: str, text: str, width: int, indent: int) -> str:
    """Wrap one cell of a table so its continuation lines stay under it.

    A row whose reason is a paragraph has to be a single string with newlines, because the caller is
    doing `"%s %s" % (...)`. Keeping the wrap width and the indent together here is what makes the
    table readable instead of one very long line.
    """
    return ("\n" + " " * indent).join(_wrap(prefix + (text or ""), width))


# --------------------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------------------
def grade(has_catalogue: bool, has_history: bool, mutates_mechanism: bool = False,
          applies_changes: bool = False, promotes: bool = False) -> Dict[str, Any]:
    """Where this system actually sits on the D2I.ai L1–L5 ladder, and what is missing for the next rung.

    Deliberately takes the facts as arguments rather than reading the filesystem: a grader that reports
    what it finds is a measurement, one that reports what it hopes is a self-confirming loop. Callers
    pass what exists; `brian evolve --grade` fills these in from the repository.

    `applies_changes` is separate from `has_history` on purpose, and it is the fact that decides between
    L2 and L3. Writing a version file is *recording* a decision; the ladder's L3 requires the change to
    take effect in the running system without a human. Here a human still restarts the service, so this
    repo is L2 however autonomous its selection has become. Grading it L3 because it writes tidy JSON
    would be exactly the self-flattery the survey warns about.

    `promotes` is likewise its own fact and deliberately does **not** move the level: writing a
    deployment state file, carrying per-variable provenance and reverting a version whose claim stopped
    holding are all things the loop now does without a human, but no running process reads the file
    until it is restarted. Promotion narrows the remaining gap to one action instead of closing it.
    """
    has_propose = has_catalogue
    has_evaluate = has_catalogue
    has_integrate = has_history

    capabilities: List[str] = []
    if has_propose:
        capabilities.append("proposes changes from a catalogue of hand-written hypotheses")
    if has_evaluate:
        capabilities.append("evaluates them against labels, prompt evidence and a token ledger")
    if has_integrate:
        capabilities.append("records an accepted change as a version file with env and rollback")
    if promotes:
        capabilities.append(
            "puts an accepted change into a deployment state file with per-variable provenance, and "
            "reverts it when its claim stops holding")
    if applies_changes:
        capabilities.append("applies an accepted change to the running configuration without a human")
    if mutates_mechanism:
        capabilities.append(
            "selects the proposal rule from its own ledger — the catalogue's hypotheses, or a bounded "
            "search around magnitudes that already moved a column — and records the rule, the space "
            "fingerprint and the reason with every round")

    level = "L1"
    missing: List[str] = ["a candidate catalogue with testable hypotheses"]
    if has_propose:
        level = "L2"
        missing = ["a running process that picks the change up without a restart"]
        if not promotes:
            missing = ["autonomous implementation: applying an accepted change without a human"]
    if has_propose and has_evaluate and has_integrate and applies_changes:
        level = "L3"
        missing = ["a mutable improvement mechanism (the catalogue and the objective are fixed)"]
    if level == "L3" and mutates_mechanism:
        level = "L4"
        missing = ["transfer beyond this stream and this declared space"]
    return {
        "level": level,
        "scope": ("the proposal rule only; the objective, decide() and the evaluator stay outside"
                  if mutates_mechanism else ""),
        "capabilities": capabilities,
        "missing_for_next": missing,
        "note": "Graded from the primitives that exist, not from intent. The evaluator is external and "
                "fixed, which is what keeps this bounded self-refinement rather than a self-confirming loop.",
    }
