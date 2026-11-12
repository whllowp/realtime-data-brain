"""Cost accounting for the half of this system that has a bill attached.

Why this module exists
----------------------
Every evaluation in this repository reported accuracy and latency, and none reported **token spend**.
That is exactly the blind spot "The RSI Tax" (arXiv:2611.13524v1) identifies: an improvement process looks
free when its cost is not in the ledger, and expensive when it is. Their finding is that a bounded-note
baseline matches costlier self-improvement methods while using 65.6-71.0% fewer tokens, because 74.8-93.6%
of the improvement budget goes to *verification* and a large share produces artifacts never used again.

The mapping onto this system, stated explicitly because the paper's setting is different:

    paper N_task        -> Laya decisions. Fixed local model, no tokens; its cost is GPU time.
    paper N_RSI         -> System 2: one LLM call per promoted alert. This is the spend.

So the analogue of the RSI Tax here is **tokens per decision**, and the analogue of "unused artifact" is
**tokens spent on a report attached to an alert that does not reach a human**. Both are reported.

The decomposition is also mapped, and the honest answer is a one-line result: this system makes exactly one
LLM call per report, so its analysis budget is 100% *write*, 0% *verify*, 0% *explore*. It already sits on
the cheap side of the trade-off the paper is about — which is worth knowing before "optimising" it.

Units are never mixed silently: tokens and milliseconds are reported separately, and ratios are only formed
within a unit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional


@dataclass
class CostLedger:
    """End-to-end cost of serving a task stream, split into execution and analysis."""

    windows: int = 0
    laya_calls: int = 0
    laya_ms: float = 0.0

    reports: int = 0
    degraded_reports: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_prompt_tokens: int = 0

    #: action -> {"reports": n, "tokens": t}. Lets "was this spend ever used?" be answered from data
    #: rather than from intent: a report attached to a `notify` alert may never be read by anyone.
    by_action: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def billed_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def total_tokens(self) -> int:
        """Billed tokens, or the measured prompt size when nothing was billed.

        Deterministic mode has no bill, but it does have a prompt of a known size — the same number the
        engine uses to warn about oversized requests. Falling back to it keeps the accounting comparable
        across modes instead of reporting zero for one of them.
        """
        if self.billed_tokens:
            return self.billed_tokens
        return self.estimated_prompt_tokens

    @property
    def tokens_per_report(self) -> Optional[float]:
        return (self.total_tokens / self.reports) if self.reports else None

    @property
    def tokens_per_window(self) -> Optional[float]:
        """The tax rate on the stream: analysis tokens per judged window."""
        return (self.total_tokens / self.windows) if self.windows else None

    @property
    def laya_ms_per_window(self) -> Optional[float]:
        return (self.laya_ms / self.windows) if self.windows else None

    def cost_by_category(self) -> Dict[str, int]:
        """The paper's Verify / Explore / Write split.

        One call per report and no candidate search means this system has no verification or exploration
        budget by construction; naming that explicitly is more useful than leaving it implicit.
        """
        return {"verify": 0, "explore": 0, "write": self.total_tokens, "other": 0}

    def unused_share(self) -> Optional[float]:
        """Share of analysis tokens attached to alerts that do not reach a human.

        `page_human` is the only action that puts a report in front of a person; `notify` records the
        incident for the record. Treating `notify` spend as "unused" is a deliberately strict reading of
        the paper's r_no-use: it is a lower bound on what could be saved, not a claim that no one ever
        looks.
        """
        if not self.total_tokens:
            return None
        unused = sum(entry["tokens"] for action, entry in self.by_action.items()
                     if action != "page_human")
        return unused / self.total_tokens

    def as_dict(self) -> Dict[str, Any]:
        return {
            "windows": self.windows,
            "laya_calls": self.laya_calls,
            "laya_ms": round(self.laya_ms, 1),
            "reports": self.reports,
            "degraded_reports": self.degraded_reports,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_report": _round(self.tokens_per_report, 1),
            "tokens_per_window": _round(self.tokens_per_window, 1),
            "laya_ms_per_window": _round(self.laya_ms_per_window, 2),
            "cost_by_category": self.cost_by_category(),
            "unused_share": _round(self.unused_share(), 3),
            "by_action": dict(self.by_action),
        }


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def ledger_from_traces(traces: Iterable[Any]) -> CostLedger:
    """Build a ledger from `WindowTrace` objects, i.e. from what the pipeline actually did.

    Reading it off the traces rather than off process-wide counters is what makes attribution possible:
    each report is charged to the action of the decision that produced it.
    """
    ledger = CostLedger()
    for trace in traces:
        ledger.windows += 1
        decision = getattr(trace, "decision", None)
        if decision is not None:
            ledger.laya_calls += 1
            ledger.laya_ms += float(getattr(decision, "laya_latency_ms", 0.0) or 0.0)
        report = getattr(trace, "report", None)
        if report is None:
            continue
        ledger.reports += 1
        if getattr(report, "degraded", False):
            ledger.degraded_reports += 1
        tokens = int(getattr(report, "prompt_tokens", 0) or 0) + \
            int(getattr(report, "completion_tokens", 0) or 0)
        ledger.prompt_tokens += int(getattr(report, "prompt_tokens", 0) or 0)
        ledger.completion_tokens += int(getattr(report, "completion_tokens", 0) or 0)
        estimate = int(getattr(report, "estimated_prompt_tokens", 0) or 0)
        ledger.estimated_prompt_tokens += estimate
        if not tokens:
            tokens = estimate
        action = decision.action.value if decision is not None else "unknown"
        bucket = ledger.by_action.setdefault(action, {"reports": 0, "tokens": 0})
        bucket["reports"] += 1
        bucket["tokens"] += tokens
    return ledger


def ledger_from_counters(counters: Mapping[str, Any], *, laya_calls: Optional[int] = None) -> CostLedger:
    """Build a ledger from pipeline counters, when the traces themselves are not retained."""
    ledger = CostLedger()
    ledger.windows = int(counters.get("windows_closed", 0) or 0)
    ledger.laya_calls = int(laya_calls if laya_calls is not None else counters.get("decisions", 0) or 0)
    ledger.laya_ms = float(counters.get("laya_latency_ms_total", 0.0) or 0.0)
    ledger.reports = int(counters.get("rca_reports", 0) or 0)
    ledger.degraded_reports = int(counters.get("rca_degraded", 0) or 0)
    ledger.prompt_tokens = int(counters.get("rca_prompt_tokens", 0) or 0)
    ledger.completion_tokens = int(counters.get("rca_completion_tokens", 0) or 0)
    return ledger


def format_ledger(ledger: CostLedger, label: str = "") -> str:
    """Human-readable ledger. Deliberately prints tokens *and* milliseconds, never a blended number."""
    lines = []
    if label:
        lines.append(label)
    lines.append("windows            %d" % ledger.windows)
    lines.append("laya decisions     %d  (%.2f ms/window)"
                 % (ledger.laya_calls, ledger.laya_ms_per_window or 0.0))
    lines.append("rca reports        %d  (%d degraded)"
                 % (ledger.reports, ledger.degraded_reports))
    if ledger.billed_tokens:
        lines.append("tokens             %d  (prompt %d + completion %d)"
                     % (ledger.billed_tokens, ledger.prompt_tokens, ledger.completion_tokens))
    else:
        lines.append("tokens             %d  (prompt estimate; nothing billed - deterministic mode)"
                     % ledger.estimated_prompt_tokens)
    if ledger.tokens_per_report is not None:
        lines.append("tokens / report    %.1f" % ledger.tokens_per_report)
    if ledger.tokens_per_window is not None:
        lines.append("tokens / window    %.1f" % ledger.tokens_per_window)
    categories = ledger.cost_by_category()
    lines.append("split              verify %d  explore %d  write %d  other %d"
                 % (categories["verify"], categories["explore"], categories["write"],
                    categories["other"]))
    share = ledger.unused_share()
    lines.append("unused share       %s" % ("n/a" if share is None else "%.1f%%" % (100 * share)))
    if ledger.by_action:
        lines.append("by action:")
        for action in sorted(ledger.by_action):
            entry = ledger.by_action[action]
            lines.append("  %-14s reports %-4d tokens %d"
                         % (action, entry["reports"], entry["tokens"]))
    return "\n".join(lines)
