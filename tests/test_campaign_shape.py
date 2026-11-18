# -*- coding: utf-8 -*-
"""Round 28: the campaign page carries the shape a refused claim was measured at.

The campaign is the page a reader consults to decide what a deployment would now be running. A row that
says `not comparable` and stops there leaves them without the number that says how far off the promise is,
so the recorded `shape` is printed beside it.
"""
from brian.campaign import Campaign, CampaignRow, format_campaign

SHAPE = {"window": 286, "recall_at_budget": 0.4271, "claimed_recall": 0.5075}


def _row(**kw):
    base = dict(domain="SMD", rounds=18, trials=80, accepts={"quality": 9}, rejections={"gain": 16},
                first="2026-11-17T00:00:00+00:00", last="2026-11-18T00:03:10+00:00",
                in_force="20", env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
                claim={"recall_at_budget": 0.507}, verdict="unmeasurable")
    base.update(kw)
    return CampaignRow(**base)


def test_the_campaign_prints_the_shape_of_a_refused_claim():
    text = format_campaign(Campaign(rows=[_row(verification={"verdict": "unmeasurable", "shape": SHAPE})]))

    assert "not comparable" in text
    assert "rolling_window=286" in text
    assert "84%" in text


def test_the_campaign_says_nothing_extra_when_no_shape_was_recorded():
    text = format_campaign(Campaign(rows=[_row(verification={"verdict": "unmeasurable"})]))

    assert "not comparable" in text
    assert "rolling_window" not in text
