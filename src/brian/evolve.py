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

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .bus import MemoryBus
from .config import ENV_VARS, Settings
from .contracts import Event
from .cost import CostLedger, ledger_from_traces
from .eval import EvalReport, Evaluator
from .laya import LayaClient
from .llm.prompts import evidence_coverage
from .pipeline import Pipeline, WindowTrace
from .streams import Stream, flatten_streams, summarise

#: Build the reference evidence pool at this cap, so every candidate's coverage is measured against the
#: same pool rather than against the candidate's own truncation. It only has to be larger than any
#: candidate's `max_context_events`.
REFERENCE_EVENTS = 64

#: Where rounds, version files and the append-only ledger live. Gitignored, like the rest of `var/`.
EVOLVE_DIR = "var/evolve"


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
    """Which `Settings` section the change lands in: `llm`, `gate` or `window`."""

    field: str
    value: Any
    veto: Optional[str] = None
    """A disqualifier that no metric in this module can see. See `analyze-pages-only`."""

    def apply(self, settings: Settings) -> Settings:
        section = getattr(settings, self.section)
        changed = replace(section, **{self.field: self.value})
        return settings.with_overrides(**{self.section: changed})

    def describe(self) -> str:
        return "%s.%s = %r" % (self.section, self.field, self.value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "section": self.section,
            "field": self.field,
            "value": self.value,
            "veto": self.veto,
        }


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
    ]


# --------------------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------------------
@dataclass
class Measurement:
    """One configuration, measured on the fixed evaluator. Every field is comparable across candidates."""

    candidate: str
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

    @property
    def coverage_floor(self) -> float:
        return min(self.coverage.values()) if self.coverage else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate,
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
        }


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
    for (name, sweep), (_n, scores) in zip(sweeps, readable):
        usable = [
            (float(score.predicted_probability), bool(score.expected_anomaly))
            for score in scores
            if score.predicted_probability is not None and score.error is None
        ]
        positives = sum(1 for _p, y in usable if y)
        negatives = len(usable) - positives
        cut = float(sweep["threshold"])  # type: ignore[arg-type]
        totals["tp"] += sum(1 for p, y in usable if y and p >= cut)
        totals["fp"] += sum(1 for p, y in usable if not y and p >= cut)
        totals["positives"] += positives
        totals["negatives"] += negatives
        thresholds[name] = sweep["threshold"]
    return {
        "budget": budget,
        "threshold": None,
        "recall": totals["tp"] / totals["positives"] if totals["positives"] else None,
        "fpr": totals["fp"] / totals["negatives"] if totals["negatives"] else None,
        "alerts": totals["tp"] + totals["fp"],
        "thresholds": thresholds,
        "recordings": len(readable),
        "pooled": False,
    }


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def apply_overrides(settings: Settings, overrides: Dict[str, Any]) -> Settings:
    """Apply a stream file's base configuration before any candidate is measured.

    The overrides are part of the *data*, not of the candidates: they describe what it takes to run
    this pipeline on this dataset at all (a different domain needs its own threshold, because the
    shipped one was calibrated elsewhere). They are applied once, outside the loop, so every
    candidate is compared on the same base and the round record says what that base was.
    """
    changed: Dict[str, Any] = {}
    for section, values in overrides.items():
        current = getattr(settings, section, None)
        if current is None:
            raise ValueError("stream overrides name an unknown settings section: %r" % section)
        if not isinstance(values, dict):
            raise ValueError("stream overrides for %r must be an object of fields" % section)
        unknown = sorted(set(values) - set(vars(current)))
        if unknown:
            raise ValueError("%s has no field(s) %s" % (section, ", ".join(unknown)))
        changed[section] = replace(current, **values)
    return settings.with_overrides(**changed)


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


def measure(settings: Settings, candidate: Optional[Candidate], streams: Sequence[Stream],
            pool: Sequence[Sequence[Event]], clock=None, budget: float = 0.10) -> Measurement:
    """Measure one configuration: detection from labels, cost from the ledger, coverage from the pool.

    A fresh `LayaClient` and a fresh `Pipeline` per measurement *and per recording*, so no result
    depends on the order the candidates were tried in, and no recording is scored against another
    recording's history. The model is deterministic, so the quality signal is reproducible; the cost
    signal is a token ledger and does not involve sampling at all.
    """
    import time as _time

    started = (_time.perf_counter if clock is None else clock)()
    applied = candidate.apply(settings) if candidate is not None else settings

    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    latencies: List[float] = []
    score_groups: List[Tuple[str, List[Any]]] = []
    for stream in streams:
        report = Evaluator(applied, laya=LayaClient(applied.laya)).run(stream.windows)
        for key, value in _tally(report).items():
            counts[key] += value
        latencies.extend(score.latency_ms for score in report.scores if score.latency_ms)
        score_groups.append((stream.name, list(report.scores)))

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
    return Measurement(
        candidate=candidate.name if candidate is not None else "baseline",
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
        windows=counts["tp"] + counts["fp"] + counts["tn"] + counts["fn"],
        confusion=counts,
        recall_at_budget=operating["recall"],
        fpr_at_budget=operating["fpr"],
        threshold_at_budget=operating["threshold"],
        budget=budget,
        thresholds_at_budget=operating.get("thresholds") or {},
        at_budget_pooled=bool(operating.get("pooled")),
    )


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_gain": self.min_gain,
            "min_quality_gain": self.min_quality_gain,
            "cost_tolerance": self.cost_tolerance,
            "recall_tolerance": self.recall_tolerance,
            "fpr_tolerance": self.fpr_tolerance,
            "fpr_budget": self.fpr_budget,
        }


@dataclass
class Verdict:
    candidate: str
    accepted: bool
    rule: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate,
            "accepted": self.accepted,
            "rule": self.rule,
            "reason": self.reason,
        }


def decide(baseline: Measurement, trial: Measurement, rules: EvolveRules,
           veto: Optional[str] = None) -> Verdict:
    """Accept only a Pareto improvement: better on one axis, not worse on the other.

    Two moves qualify, and both are checked in the same order. A cost move lowers tokens per report by
    `min_gain` at no loss of at-budget recall, false-positive rate, or evidence. A quality move raises
    at-budget recall by `min_quality_gain` without raising the false-positive rate and without costing
    more than `cost_tolerance` per report (undefined cost counts as no objection, because a domain the
    shipped threshold silences has no bill to compare and refusing to decide there would leave the
    dilution this loop exists to find in place).

    The label check reads the *at-budget* pair when both sides have one. A fixed absolute threshold on
    an uncalibrated answer is not a shared yardstick: on SMD every answer lands between 0.30 and 0.47,
    so a cut at the shipped 0.5 reports recall 0 for a detector whose ranking is 0.75 AUC, and a
    candidate that shifted the probabilities without changing the ranking would look like an
    improvement. Comparing at a common false-positive budget asks the question the label column is
    meant to answer: can this configuration find more incidents for the same number of false alarms?
    """
    if veto:
        return Verdict(trial.candidate, False, "veto", veto)

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

    before, after = baseline.tokens_per_report, trial.tokens_per_report
    before_recall = baseline.recall_at_budget
    after_recall = trial.recall_at_budget
    quality_gain = (
        before_recall is not None
        and after_recall is not None
        and after_recall >= before_recall * (1.0 + rules.min_quality_gain)
    )
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
    if quality_gain:
        return Verdict(trial.candidate, False, "cost",
                       "recall improves but the bill rises %.0f -> %.0f tokens/report, more than "
                       "the %.0f%% a quality gain may cost"
                       % (before, after, 100 * rules.cost_tolerance))
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

    @property
    def accepted(self) -> List[str]:
        return [verdict.candidate for _c, _m, verdict in self.trials if verdict.accepted]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stream": self.stream,
            "rules": self.rules.to_dict(),
            "baseline": self.baseline.to_dict(),
            "trials": [
                {"candidate": candidate.to_dict(), "measurement": measurement.to_dict(),
                 "verdict": verdict.to_dict()}
                for candidate, measurement, verdict in self.trials
            ],
            "accepted": self.accepted,
        }


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def run_round(settings: Settings, streams: Sequence[Stream],
              selected: Optional[Sequence[Candidate]] = None,
              rules: Optional[EvolveRules] = None,
              source: str = "demo",
              on_candidate: Optional[Callable[[Candidate, Measurement, Verdict], None]] = None,
              ) -> RoundResult:
    """One full pass: build the reference pool, measure the baseline, then each candidate.

    Everyone is measured against the *same* recordings and the *same* evidence pool, so a candidate
    cannot improve its own score by changing what it is scored on. `source` records where the
    recordings came from, because "accept" against a hand-written suite and "accept" against
    fault-injection data are different claims and the version file must not conflate them.
    """
    started = _now()
    active_rules = rules if rules is not None else EvolveRules()
    chosen = list(selected) if selected is not None else catalogue()
    pool = build_pool(settings, streams)
    baseline = measure(settings, None, streams, pool, budget=active_rules.fpr_budget)

    trials: List[Tuple[Candidate, Measurement, Verdict]] = []
    for candidate in chosen:
        trial = measure(settings, candidate, streams, pool, budget=active_rules.fpr_budget)
        verdict = decide(baseline, trial, active_rules, veto=candidate.veto)
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
    round_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n",
                          encoding="utf-8")

    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "round": round_path.name,
            "started_at": result.started_at,
            "accepted": result.accepted,
            "rejected": [v.candidate for _c, _m, v in result.trials if not v.accepted],
            "applied": list(result.accepted) if write_versions else [],
            "baseline_tokens_per_report": result.baseline.to_dict()["tokens_per_report"],
        }) + "\n")

    written: List[str] = []
    if not write_versions:
        return {"round": str(round_path), "versions": written}
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
        env_key = (candidate.section, candidate.field)
        payload = {
            "version": version,
            "created_at": result.finished_at,
            "candidate": candidate.to_dict(),
            "change": candidate.describe(),
            "env": "%s=%s" % (ENV_VARS.get(env_key, "?"), _env_value(candidate.value)),
            "measurement": measurement.to_dict(),
            "verdict": verdict.to_dict(),
            "rollback": "unset %s  # restores the default" % ENV_VARS.get(env_key, "?"),
        }
        path = versions / ("v%03d.json" % version)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return {"round": str(round_path), "versions": written}


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def history(root: Path) -> List[Dict[str, Any]]:
    """Every round recorded so far, oldest first, as the full round merged with its ledger line.

    The jsonl file is the index — it records order and survives a half-written round file — and the
    `round-*.json` file holds the measurements. Returning the merge means a caller that wants "what did
    the last round decide" does not have to know there are two files.
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
        rows.append(merged)
    return rows


def previously_rejected(root: Path) -> Dict[str, str]:
    """candidate name -> the rule that rejected it, from earlier rounds.

    A loop that re-proposes its own rejected ideas has not accumulated anything; the survey calls the
    symptom scientific amnesia. This is the smallest cure: the next round can see what was already
    decided and why, and skip it instead of paying to rediscover it.
    """
    rejected: Dict[str, str] = {}
    for row in history(root):
        for trial in row.get("trials") or []:
            verdict = trial.get("verdict") or {}
            if not verdict.get("accepted"):
                rejected.setdefault(verdict.get("candidate", "?"), verdict.get("reason", ""))
    return rejected


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
    lines.append("objective         accept a Pareto move: tokens/report down >= %.0f%% or recall at"
                 " <=%.0f%% false positives up >= %.0f%%, never at a cost of the other column"
                 % (100 * result.rules.min_gain, 100 * result.rules.fpr_budget,
                    100 * result.rules.min_quality_gain))
    lines.append("")
    lines.append("%-22s %8s %7s %9s %9s %8s %-9s" %
                 ("candidate", "recall@b", "fpr@b", "tok/rep", "tok/win", "evidence", "verdict"))
    lines.append("-" * 96)

    def row(m: Measurement, label: str, verdict: str) -> str:
        return "%-22s %8s %7s %9s %9s %8s %-9s" % (
            label,
            "n/a" if m.recall_at_budget is None else "%.3f" % m.recall_at_budget,
            "n/a" if m.fpr_at_budget is None else "%.3f" % m.fpr_at_budget,
            "n/a" if m.tokens_per_report is None else "%.0f" % m.tokens_per_report,
            "n/a" if m.tokens_per_window is None else "%.1f" % m.tokens_per_window,
            "%.0f%%" % (100 * m.coverage_floor) if m.coverage else "n/a",
            verdict,
        )

    lines.append(row(result.baseline, "baseline", "-"))
    for candidate, measurement, verdict in result.trials:
        lines.append(row(measurement, candidate.name, "accept" if verdict.accepted else "reject"))
    lines.append("")
    lines.append("recall@b: best recall at <=%.0f%% false positives, the cut chosen per configuration, because"
                 % (100 * result.rules.fpr_budget))
    lines.append("an absolute threshold was calibrated on another domain and is not comparable here.")
    for candidate, measurement, _verdict in [(None, result.baseline, None)] + list(result.trials):
        label = "baseline" if candidate is None else candidate.name
        if measurement.recall is None or measurement.fpr is None:
            continue
        lines.append("  shipped threshold  %-22s recall %.3f  fpr %.3f"
                     % (label, measurement.recall, measurement.fpr))
    lines.append("")
    for candidate, _measurement, verdict in result.trials:
        lines.append("%s %-22s %s" % ("+" if verdict.accepted else "x", candidate.name,
                                      _wrap(verdict.reason, 68)))
    if not result.trials:
        lines.append("no candidates were tried")
    lines.append("")
    lines.append("Interpretation: 'accept' means one measured configuration was better than another on this")
    lines.append("stream and lost nothing this evaluator can see. It is not a claim that the change is right")
    lines.append("in production — the evidence column is what stands in for the explanation quality that")
    lines.append("cannot be measured without a configured model.")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> str:
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
    return ("\n" + " " * 25).join(lines)


# --------------------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------------------
def grade(has_catalogue: bool, has_history: bool, mutates_mechanism: bool = False,
          applies_changes: bool = False) -> Dict[str, Any]:
    """Where this system actually sits on the D2I.ai L1–L5 ladder, and what is missing for the next rung.

    Deliberately takes the facts as arguments rather than reading the filesystem: a grader that reports
    what it finds is a measurement, one that reports what it hopes is a self-confirming loop. Callers
    pass what exists; `brian evolve --grade` fills these in from the repository.

    `applies_changes` is separate from `has_history` on purpose, and it is the fact that decides between
    L2 and L3. Writing a version file is *recording* a decision; the ladder's L3 requires the change to
    take effect in the running system without a human. Here a human still exports the variable and
    restarts the service, so this repo is L2 however autonomous its selection has become. Grading it L3
    because it writes tidy JSON would be exactly the self-flattery the survey warns about.
    """
    has_propose = has_catalogue
    has_evaluate = has_catalogue
    has_integrate = has_history

    capabilities: List[str] = []
    if has_propose:
        capabilities.append("proposes changes from a fixed catalogue, each with a testable hypothesis")
    if has_evaluate:
        capabilities.append("evaluates them against labels, prompt evidence and a token ledger")
    if has_integrate:
        capabilities.append("records an accepted change as a version file with env and rollback")
    if applies_changes:
        capabilities.append("applies an accepted change to the running configuration without a human")
    if mutates_mechanism:
        capabilities.append("revises its own improvement mechanism, not only the settings it moves")

    level = "L1"
    missing: List[str] = ["a candidate catalogue with testable hypotheses"]
    if has_propose:
        level = "L2"
        missing = ["autonomous implementation: applying an accepted change without a human"]
    if has_propose and has_evaluate and has_integrate and applies_changes:
        level = "L3"
        missing = ["a mutable improvement mechanism (the catalogue and the objective are fixed)"]
    if level == "L3" and mutates_mechanism:
        level = "L4"
        missing = ["transfer beyond this stream and this catalogue"]
    return {
        "level": level,
        "capabilities": capabilities,
        "missing_for_next": missing,
        "note": "Graded from the primitives that exist, not from intent. The evaluator is external and "
                "fixed, which is what keeps this bounded self-refinement rather than a self-confirming loop.",
    }
