"""Round 155: the inheritance chain keeps its origin, so an old claim does not read as a new one.

Round 154 copies the claim onto the successor record. The copy remembered one hop only (`inherited_from`) and
always carried the newest `at`, so after a second re-measurement nothing said the assertion was older than the
record standing on it.
"""

from brian.campaign import campaign, format_campaign
from brian.evolve import claimed_rounds, inherit_claim

FIRST = "2026-11-20T08:38:50+00:00"
CLAIMED = {
    "round": "round-out-of-reach.json",
    "stream_path": "data/smd/stream-one.json",
    "stream_path_history": [
        {"from": "", "to": "data/smd/stream-one.json", "at": FIRST, "geometry": "disagrees",
         "why": "claimed by hand"},
    ],
}


def _successor(history):
    return {"round": "round-first-successor.json", "stream_path": "data/smd/stream-one.json",
            "stream_path_history": history}


def test_a_hand_recorded_claim_is_its_own_origin():
    entry = inherit_claim(CLAIMED, "data/smd/stream-one.json")[0]

    assert entry["claim_origin"] == "round-out-of-reach.json"
    assert entry["claim_since"] == FIRST
    assert entry["claim_hops"] == 1


def test_a_second_re_measurement_keeps_the_first_origin():
    first = _successor(inherit_claim(CLAIMED, "data/smd/stream-one.json"))
    second_entry = inherit_claim(first, "data/smd/stream-one.json")[0]

    assert second_entry["claim_origin"] == "round-out-of-reach.json"   # not the middle round
    assert second_entry["claim_since"] == FIRST                        # the moment it was first recorded
    assert second_entry["claim_hops"] == 2
    assert second_entry["inherited_from"] == "round-first-successor.json"


def test_the_measurement_moment_moves_while_the_claim_moment_does_not():
    first = _successor(inherit_claim(CLAIMED, "data/smd/stream-one.json"))
    second_entry = inherit_claim(first, "data/smd/stream-one.json")[0]

    assert second_entry["at"] != first["stream_path_history"][0]["at"]
    assert second_entry["claim_since"] == first["stream_path_history"][0]["claim_since"]


def test_the_rows_carry_the_chain():
    first = _successor(inherit_claim(CLAIMED, "data/smd/stream-one.json"))
    rows = claimed_rounds([first])

    assert rows[0]["hops"] == 1
    assert rows[0]["origin"] == "round-out-of-reach.json"
    assert rows[0]["since"] == FIRST


def test_the_page_says_how_old_the_assertion_is(tmp_path):
    first = _successor(inherit_claim(CLAIMED, "data/smd/stream-one.json"))
    page = format_campaign(campaign(tmp_path), claims=claimed_rounds([first]))

    assert ("claims            the one on round-first-successor.json was inherited 1 time(s) and was first "
            "recorded on round-out-of-reach.json at " + FIRST) in page


def test_a_claim_recorded_on_the_round_itself_is_not_called_inherited(tmp_path):
    page = format_campaign(campaign(tmp_path), claims=claimed_rounds([CLAIMED]))

    assert "claims            the rows: round-out-of-reach.json data/smd/stream-one.json (disagrees)" in page
    assert "was inherited" not in page
