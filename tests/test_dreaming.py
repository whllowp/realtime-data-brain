"""The dreaming pass must correct memory without deleting memory that is true.

The article's warning is that a note drifting from the transcripts becomes a wrong belief carried into every
future session. The inverse failure is just as bad and harder to notice: a validator that drops *supported*
claims deletes real memory and looks like diligence. The first real run here did exactly that - it dropped
`checkout-api resource_exhaustion` although the index held that pattern seven times, because `checkout-api`
tokenises to `checkout` + `api` and the check compared whole words.

These tests pin both directions.
"""
from __future__ import annotations

from types import SimpleNamespace

from brian.dreaming import (
    INJECTED_MAX_TOKENS,
    IncidentIndex,
    claim_supported,
    dream,
)


def _record(topic: str, **value):
    return SimpleNamespace(topic=topic, value=value, timestamp=1_700_000_000.0, offset=0, key=None)


def _index(*pairs) -> IncidentIndex:
    index = IncidentIndex()
    index.__init__()
    from brian.dreaming import IncidentEntry

    for service, category, count in pairs:
        index.entries["%s|%s" % (service, category)] = IncidentEntry(
            service=service, category=category, count=count, seen_decisions=True)
    return index


def test_a_hyphenated_service_name_still_grounds_a_claim() -> None:
    """The exact regression: `checkout-api` must match text that says `checkout-api`."""
    index = _index(("checkout-api", "resource_exhaustion", 7))
    supported, reason = claim_supported("checkout-api resource_exhaustion: pool exhaustion", index)
    assert supported, "a grounded claim was dropped (%s)" % reason


def test_a_category_alone_grounds_a_claim() -> None:
    index = _index(("checkout-api", "resource_exhaustion", 7))
    assert claim_supported("resource_exhaustion usually starts with latency", index)[0]


def test_a_pattern_absent_from_transcripts_is_dropped() -> None:
    """The case validation exists for: memory about something that never happened here."""
    index = _index(("checkout-api", "resource_exhaustion", 7))
    supported, reason = claim_supported("payment-gateway disk saturation: watch disk_io", index)
    assert not supported
    assert "absent" in reason


def test_generic_guidance_survives() -> None:
    """A rule about how to reason cannot be falsified by the log, so the log must not delete it."""
    index = _index(("checkout-api", "resource_exhaustion", 7))
    assert claim_supported("When two hypotheses fit, prefer the earlier evidence.", index)[0]


def test_an_empty_index_contradicts_nothing() -> None:
    """With no transcripts there is nothing to check against; deleting everything would be a cold start."""
    supported, reason = claim_supported("anything at all about disk", IncidentIndex())
    assert supported
    assert "no transcripts" in reason


def test_dream_reports_what_it_dropped_rather_than_dropping_silently() -> None:
    records = [_record("brian.decisions", service="checkout-api", category="resource_exhaustion",
                       action="page_human", window_start="2026-01-14T10:00:00Z")]
    note = "checkout-api resource_exhaustion: latency first\nghost-service cpu saturation: watch cpu"
    report, index, cleaned = dream(records, note)
    assert report.claims_checked == 2
    assert report.claims_dropped == 1
    assert report.dropped[0]["claim"].startswith("ghost-service")
    assert "checkout-api" in cleaned and "ghost-service" not in cleaned


def test_the_injected_block_is_an_index_and_stays_under_the_cap() -> None:
    """It is charged to every prompt, so its size is a cost decision, not a formatting one."""
    records = [
        _record("brian.decisions", service="svc-%02d" % i, category="resource_exhaustion",
                action="page_human", window_start="2026-01-14T10:00:00Z")
        for i in range(200)
    ]
    report, index, _ = dream(records, "")
    assert report.injected_tokens <= INJECTED_MAX_TOKENS
    assert "omitted" in report.injected, "a truncated index must say so instead of looking complete"


def test_report_only_patterns_are_not_rendered_as_zero_occurrences() -> None:
    """`x0` reads as 'happened zero times', which is a different claim from 'no decision recorded'."""
    records = [_record("brian.rca", service="ghost", category="security", root_cause="guess")]
    report, index, _ = dream(records, "")
    assert "ghost|security" not in report.injected
    assert index.entries["ghost|security"].count == 0
