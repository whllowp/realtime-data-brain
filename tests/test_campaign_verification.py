# -*- coding: utf-8 -*-
"""Round 27 additions to the campaign tests: the report says how the in-force claims were checked."""
from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.evolve import EvolveRules
from brian.promote import UNMEASURABLE, VERIFIED, apply_promotion, load_promotions, write_verification

from test_campaign import _round


def _in_force_with(tmp_path: Path, payload):
    _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")
    write_verification(tmp_path, promotion, payload)
    return promotion


def test_a_version_whose_check_holds_is_counted_as_holding(tmp_path):
    _in_force_with(tmp_path, {"verdict": VERIFIED, "reason": "recall 0.507"})

    result = campaign(tmp_path)

    assert result.holding == 1
    assert result.not_comparable == 0
    assert result.never_checked == 0
    assert "1 holding, 0 not comparable, 0 never re-checked" in format_campaign(result)
    assert "(holds)" in format_campaign(result)


def test_a_version_checked_on_another_shape_says_not_comparable(tmp_path):
    # Round 26's outcome: the stream could not judge the claim, so the deployment stays and the report
    # must not call the check a pass.
    _in_force_with(tmp_path, {"verdict": UNMEASURABLE, "reason": "1235 scored windows against 828"})

    result = campaign(tmp_path)
    table = format_campaign(result)

    assert result.not_comparable == 1
    assert result.holding == 0
    assert "0 holding, 1 not comparable" in table
    assert "(not comparable)" in table


def test_a_version_never_re_checked_is_not_described_as_holding(tmp_path):
    _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")

    result = campaign(tmp_path)

    assert result.never_checked == 1
    assert "(unverified)" in format_campaign(result)
    assert result.to_dict()["versions_never_checked"] == 1


def test_a_reverted_version_is_counted_once(tmp_path):
    # The old code incremented the counter in two places; a single retraction read as two.
    _round(tmp_path, source="SMD", accepted=True)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")
    from brian.promote import FAILED, revert_promotion

    revert_promotion(tmp_path, promotion, domain="SMD")
    write_verification(tmp_path, promotion, {"verdict": FAILED, "reason": "recall fell"},
                       reverted_at="2026-11-18T00:27:07+00:00")

    assert campaign(tmp_path).reverted == 1


def test_earlier_checks_are_counted_next_to_the_verdict(tmp_path):
    promotion = _in_force_with(tmp_path, {"verdict": UNMEASURABLE, "reason": "different shape"})
    write_verification(tmp_path, promotion, {"verdict": VERIFIED, "reason": "recall 0.507"})

    result = campaign(tmp_path)
    table = format_campaign(result)

    assert result.rows[0].checks == 1
    assert "holds, 1 earlier check" in table
    assert EvolveRules().min_gain > 0
