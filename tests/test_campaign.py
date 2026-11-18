"""The campaign report: one row per stream, read out of what the rounds already recorded.

The point of these tests is that the report cannot invent anything. Every number it prints must be
traceable to a stored round, a stored version, or today's rules — and a round whose stream is unknown
must be visible rather than silently absorbed into a domain it never measured.
"""
from __future__ import annotations

from pathlib import Path

from brian.campaign import CampaignRow, campaign, format_campaign
from brian.config import load_settings
from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, Verdict, persist
from brian.promote import apply_promotion, load_promotions, revert_promotion

PERFECT = {"log_retention": 1.0, "signature_retention": 1.0,
           "first_error_kept": 1.0, "context_retention": 1.0}


def _measurement(tokens: float = 100.0) -> Measurement:
    return Measurement(
        candidate="events-8", recall=0.875, fpr=0.0, precision=1.0, reports=8,
        tokens_per_report=tokens, tokens_per_window=tokens / 4.0, latency_p95_ms=10.0,
        coverage=dict(PERFECT), trial_s=1.0, reasoner="offline",
    )


def _round(root: Path, *, source: str | None, accepted: bool, stamp: str = "2026-11-18T00:00:00+00:00"):
    candidate = Candidate(name="events-8", hypothesis="the event block dominates the prompt",
                          section="llm", field="max_context_events", value=8)
    trial = _measurement(60.0 if accepted else 100.0)
    verdict = Verdict("events-8", accepted, "cost" if accepted else "gain",
                      "cheaper" if accepted else "no gain")
    stream = {} if source is None else {"source": source}
    result = RoundResult(
        started_at=stamp, finished_at=stamp, stream=stream, rules=EvolveRules(),
        baseline=_measurement(), trials=[(candidate, trial, verdict)],
    )
    return persist(root, result)


def test_a_round_is_attributed_to_the_stream_it_measured(tmp_path):
    _round(tmp_path, source="SMD", accepted=True)

    result = campaign(tmp_path)

    assert result.rounds == 1
    assert result.domains == ["SMD"]
    assert result.rows[0].rounds == 1
    assert result.rows[0].accepts == {"cost": 1}


def test_two_domains_stay_apart_and_their_verdicts_are_counted_by_rule(tmp_path):
    _round(tmp_path, source="SMD", accepted=True, stamp="2026-11-18T00:00:00+00:00")
    _round(tmp_path, source="SMD", accepted=False, stamp="2026-11-18T00:01:00+00:00")
    _round(tmp_path, source="RCAEval RE1-OB", accepted=False, stamp="2026-11-18T00:02:00+00:00")

    result = campaign(tmp_path)

    rows = {row.domain: row for row in result.rows}
    assert rows["SMD"].rounds == 2
    assert rows["SMD"].accepts == {"cost": 1}
    assert rows["SMD"].rejections == {"gain": 1}
    assert rows["RCAEval RE1-OB"].rounds == 1
    assert rows["RCAEval RE1-OB"].accepted == 0


def test_a_round_without_a_stream_is_reported_rather_than_guessed(tmp_path):
    _round(tmp_path, source=None, accepted=True)

    result = campaign(tmp_path)

    assert result.unsourced == 1
    # The verdict still counts — dropping it would make the campaign total disagree with the ledger.
    assert result.accepted == 1
    assert result.domains == ["(unattributed)"]
    assert "(no stream recorded)" in format_campaign(result)


def test_the_configuration_in_force_is_read_from_the_domain_it_belongs_to(tmp_path):
    written = _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")

    result = campaign(tmp_path)

    row = result.rows[0]
    assert row.in_force == promotion.version
    assert row.env == {"BRIAN_LLM_MAX_CONTEXT_EVENTS": "8"}
    assert row.claim["tokens_per_report"] == 60.0
    assert written["versions"]


def test_a_version_that_was_reverted_is_visible_in_the_report(tmp_path):
    _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")
    revert_promotion(tmp_path, promotion, domain="SMD")

    result = campaign(tmp_path)

    assert result.rows[0].in_force is None


def test_the_audit_column_is_recomputed_from_todays_rules(tmp_path):
    _round(tmp_path, source="SMD", accepted=True)

    fresh = campaign(tmp_path)
    strict = campaign(tmp_path, EvolveRules(min_gain=0.50))

    assert fresh.moved == 0
    assert strict.moved >= 1


def test_an_empty_ledger_says_so_instead_of_printing_an_empty_table(tmp_path):
    result = campaign(tmp_path)

    assert result.rounds == 0
    assert result.rows == []
    assert "no round recorded a stream" in format_campaign(result)


def test_the_json_form_carries_the_same_numbers_as_the_table(tmp_path):
    _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")

    payload = campaign(tmp_path).to_dict()

    assert payload["rounds"] == 1
    assert payload["accepted"] == 1
    assert payload["domains"][0]["domain"] == "SMD"
    assert payload["domains"][0]["env"] == {"BRIAN_LLM_MAX_CONTEXT_EVENTS": "8"}
    assert payload["verdicts_moved"] == 0


def test_the_row_counts_what_it_was_given():
    row = CampaignRow(domain="SMD", accepts={"cost": 2, "quality": 1}, rejections={"gain": 3})

    assert row.accepted == 3
    assert load_settings().digest.profile in ("service", "host")
