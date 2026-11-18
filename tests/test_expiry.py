# -*- coding: utf-8 -*-
"""Round 29 tests: a recorded verdict expires.

The gap this closes: `--verify` selected versions with no verdict at all, so a `holds` from a year ago and
an `unmeasurable` from last March were both permanent. A verdict is evidence about the day it ran; past the
expiry it stays the record but stops being the evidence, the version becomes due again, and the campaign
counts it as expired.
"""
import json
from datetime import datetime, timedelta, timezone

from brian.campaign import Campaign, CampaignRow, campaign, format_campaign
from brian.promote import (
    CLAIM_EXPIRY_DAYS,
    UNMEASURABLE,
    VERIFIED,
    format_promotions,
    load_promotions,
    verification_age_days,
)

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _version_file(tmp_path, *, verdict=VERIFIED, checked_days_ago=1.0, stream=None):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 20,
        "candidate": {"name": "host-rolling-rank-0-85-200", "hypothesis": "h",
                      "changes": [["digest", "rolling_window", 200]], "veto": None},
        "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
        "claim": {"recall_at_budget": 0.507},
        "created_at": _stamp(2),
        "stream": stream or {"source": "SMD", "windows": 948, "scored": 828},
        "verification": {"checked_at": _stamp(checked_days_ago), "stream": "SMD",
                         "verdict": verdict, "reason": "recall 0.507 against a promised 0.507"},
    }
    (directory / "v020.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory / "v020.json"


def _state_file(tmp_path):
    directory = tmp_path / "var" / "evolve" / "domains" / "smd"
    directory.mkdir(parents=True)
    (directory / "current.json").write_text(json.dumps({
        "domain": "smd", "source": "SMD",
        "env": {"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
        "sources": {"BRIAN_DIGEST_ROLLING_WINDOW": "20"},
        "updated_at": _stamp(1),
    }), encoding="utf-8")


def test_the_age_of_a_verdict_is_measured_in_days(tmp_path):
    assert abs(verification_age_days({"checked_at": _stamp(3)}, NOW) - 3.0) < 1e-6
    assert verification_age_days(None, NOW) is None
    assert verification_age_days({}, NOW) is None
    assert verification_age_days({"checked_at": "yesterday"}, NOW) is None


def test_a_stamp_without_an_offset_is_read_as_utc_not_as_now():
    # Guessing a date is how a stale check starts looking fresh.
    stamp = "2026-11-15T09:00:00"
    assert abs(verification_age_days({"checked_at": stamp}, NOW) - 3.0) < 1e-6


def test_a_verdict_older_than_the_horizon_is_overdue(tmp_path):
    _version_file(tmp_path, checked_days_ago=20)
    promotion = load_promotions(tmp_path)[0]

    assert promotion.overdue(NOW) is True
    assert promotion.overdue(NOW, days=30) is False
    assert promotion.verdict == VERIFIED


def test_a_fresh_verdict_is_not_due_again(tmp_path):
    _version_file(tmp_path, checked_days_ago=1)
    promotion = load_promotions(tmp_path)[0]

    assert promotion.due(NOW) is False


def test_an_overdue_refusal_is_due_again(tmp_path):
    # This is the version that used to be permanent: `unmeasurable` is not `unverified`, so `pending` was
    # False and `--verify` never picked it up again.
    _version_file(tmp_path, verdict=UNMEASURABLE, checked_days_ago=20)
    promotion = load_promotions(tmp_path)[0]

    assert promotion.pending is False
    assert promotion.due(NOW) is True


def test_an_overdue_hold_is_due_again(tmp_path):
    _version_file(tmp_path, verdict=VERIFIED, checked_days_ago=CLAIM_EXPIRY_DAYS + 1)
    promotion = load_promotions(tmp_path)[0]

    assert promotion.due(NOW) is True


def test_a_never_checked_version_is_still_due(tmp_path):
    path = _version_file(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("verification")
    path.write_text(json.dumps(document), encoding="utf-8")
    promotion = load_promotions(tmp_path)[0]

    assert promotion.pending is True
    assert promotion.due(NOW) is True


def test_the_table_prints_the_age_and_marks_the_overdue(tmp_path):
    _version_file(tmp_path, checked_days_ago=3)
    fresh = format_promotions(load_promotions(tmp_path), domain="SMD", now=NOW)

    assert "checked 3 days ago" in fresh
    assert "overdue" not in fresh

    _version_file(tmp_path, checked_days_ago=40)
    stale = format_promotions(load_promotions(tmp_path), domain="SMD", now=NOW)
    assert "checked 40 days ago" in stale
    assert "overdue" in stale


def test_the_horizon_can_be_shortened_at_the_call_site(tmp_path):
    _version_file(tmp_path, checked_days_ago=3)
    table = format_promotions(load_promotions(tmp_path), domain="SMD", expiry_days=1, now=NOW)

    assert "overdue" in table


def test_the_campaign_counts_expired_verdicts(tmp_path):
    _version_file(tmp_path, checked_days_ago=20)
    _state_file(tmp_path)
    result = campaign(tmp_path, expiry_days=CLAIM_EXPIRY_DAYS, now=NOW)

    assert result.overdue == 1
    assert result.holding == 1
    assert result.to_dict()["versions_overdue"] == 1


def test_a_fresh_campaign_does_not_print_an_expired_line(tmp_path):
    _version_file(tmp_path, checked_days_ago=1)
    _state_file(tmp_path)
    text = format_campaign(campaign(tmp_path, now=NOW))

    assert "expired" not in text
    assert "overdue" not in text


def test_the_campaign_marks_the_in_force_row_overdue(tmp_path):
    _version_file(tmp_path, checked_days_ago=20)
    _state_file(tmp_path)
    text = format_campaign(campaign(tmp_path, now=NOW))

    assert "expired           1 of them are older than 14 day(s)" in text
    assert "holds (overdue)" in text


def test_the_campaign_line_keeps_its_shape():
    row = CampaignRow(domain="SMD", rounds=1, in_force="20",
                      env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
                      verdict=UNMEASURABLE, overdue=True)
    text = format_campaign(Campaign(rows=[row], overdue=1, holding=0, not_comparable=1), expiry_days=14)

    assert "not comparable (overdue)" in text
    assert "expired           1 of them are older than 14 day(s)" in text
