"""The cost ledger must not invent numbers, mix units, or hide the deterministic-mode case.

"The RSI Tax" (arXiv:2611.13524v1) is about an accounting failure: an improvement process that looks free
because its cost never entered the ledger. These tests protect the ledger itself from the same class of
mistake — a report that shows a plausible number computed from the wrong quantity is worse than no report.
"""
from __future__ import annotations

from brian.contracts import RcaReport
from brian.cost import CostLedger, format_ledger, ledger_from_counters, ledger_from_traces


class _Action:
    def __init__(self, value: str) -> None:
        self.value = value


class _Decision:
    def __init__(self, action: str, latency: float = 10.0) -> None:
        self.action = _Action(action)
        self.laya_latency_ms = latency


class _Trace:
    def __init__(self, action: str, report=None) -> None:
        self.decision = _Decision(action)
        self.report = report


def _report(prompt: int, completion: int, estimated: int = 0) -> RcaReport:
    return RcaReport(alert_id="a", decision_id="d", service="svc", summary="s", root_cause="r",
                     prompt_tokens=prompt, completion_tokens=completion,
                     estimated_prompt_tokens=estimated)


def test_billed_tokens_are_summed_per_report_and_attributed_to_the_action() -> None:
    traces = [
        _Trace("page_human", _report(1000, 200)),
        _Trace("notify", _report(800, 100)),
        _Trace("page_human", _report(1200, 300)),
    ]
    ledger = ledger_from_traces(traces)
    assert ledger.windows == 3
    assert ledger.reports == 3
    assert ledger.billed_tokens == 3600
    assert ledger.by_action["page_human"]["tokens"] == 2700
    assert ledger.by_action["notify"]["tokens"] == 900


def test_share_below_page_counts_only_page_human_as_reaching_a_person() -> None:
    """Only `page_human` counts as used; `notify` is spend with no person on the other end.

    Named `share_below_page`, not "unused": `deep_analysis` and `auto_triage` are actions whose report *is*
    the deliverable, so the paper's r_no-use does not transfer to this ladder. What the number measures is
    spend that never interrupts anyone.
    """
    traces = [_Trace("page_human", _report(300, 0)), _Trace("auto_triage", _report(100, 0))]
    ledger = ledger_from_traces(traces)
    assert ledger.share_below_page() == 0.25


def test_estimate_is_used_only_when_nothing_was_billed() -> None:
    """Deterministic mode has no bill but does have a prompt of known size."""
    offline = ledger_from_traces([_Trace("page_human", _report(0, 0, estimated=1500))])
    assert offline.billed_tokens == 0
    assert offline.total_tokens == 1500

    billed = ledger_from_traces([_Trace("page_human", _report(900, 0, estimated=1500))])
    assert billed.total_tokens == 900, "a real bill must win over the estimate, not be added to it"


def test_decomposition_reports_a_single_call_system_as_pure_write() -> None:
    """One LLM call per report means no verification and no exploration budget, by construction."""
    ledger = ledger_from_traces([_Trace("page_human", _report(500, 100))])
    split = ledger.cost_by_category()
    assert split["verify"] == 0
    assert split["explore"] == 0
    assert split["write"] == 600


def test_units_are_never_blended() -> None:
    """Tokens and milliseconds stay in separate fields; a ratio is only formed within a unit."""
    ledger = ledger_from_traces([_Trace("page_human", _report(100, 100))])
    assert ledger.tokens_per_window == 200.0
    assert ledger.laya_ms_per_window == 10.0
    text = format_ledger(ledger)
    assert "tokens / window" in text
    assert "ms/window" in text


def test_counters_path_agrees_with_the_trace_path() -> None:
    """The two builders must not drift: one is used when traces are dropped for memory."""
    counters = {
        "windows_closed": 10, "decisions": 9, "laya_latency_ms_total": 90.0,
        "rca_reports": 4, "rca_degraded": 1,
        "rca_prompt_tokens": 4000, "rca_completion_tokens": 1000,
    }
    ledger = ledger_from_counters(counters)
    assert ledger.windows == 10
    assert ledger.laya_calls == 9
    assert ledger.reports == 4
    assert ledger.degraded_reports == 1
    assert ledger.total_tokens == 5000
    assert ledger.by_action == {}, "counters cannot attribute spend to an action; they must not pretend to"


def test_zero_spend_does_not_divide_by_zero() -> None:
    ledger = CostLedger()
    assert ledger.tokens_per_report is None
    assert ledger.tokens_per_window is None
    assert ledger.share_below_page() is None
    format_ledger(ledger)  # must not raise
