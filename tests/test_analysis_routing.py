"""Per-action analysis routing: parsing must be safe and the default must stay conservative.

Round 2 measured a 37.4% analysis-budget saving by routing `deep_analysis` alerts to the deterministic
reasoner. That number is real and the policy is debatable — `deep_analysis` literally means "the LLM must
reason harder" — so these tests pin the mechanics the policy relies on, and leave the policy itself to the
operator and the ledger.
"""
from __future__ import annotations

from brian.config import GateThresholds
from brian.pipeline import _analyze_action_set


def test_action_set_parsing_drops_unknown_names() -> None:
    """A typo must not crash the pipeline, and must not silently disable analysis for everything.

    Dropping unknown names means a misspelled action is simply not analysed — visible in the ledger as a
    smaller bill — instead of raising over a policy string.
    """
    parsed = _analyze_action_set("page_human, typo_action ,deep_analysis")
    assert parsed == frozenset({"page_human", "deep_analysis"})


def test_empty_action_set_is_respected() -> None:
    """Empty means "no billed analysis", which is a legitimate policy, not a parse failure."""
    assert _analyze_action_set("") == frozenset()
    assert _analyze_action_set("   ") == frozenset()


def test_gate_default_keeps_every_alerting_action() -> None:
    """The shipped default must be the conservative one; routing is opt-in.

    Turning this on changes where money is spent on every subsequent alert, so it cannot be a default that
    arrives with an upgrade.
    """
    gate = GateThresholds()
    assert gate.always_analyze_alerts is True
    assert set(_analyze_action_set(gate.analyze_actions)) == {
        "auto_triage", "deep_analysis", "page_human"
    }
