"""Round 157: an age a repair produced must not read as a date the record carried all along."""

import json

from brian.campaign import campaign, format_campaign
from brian.evolve import EVOLVE_DIR, backfill_claims, claimed_rounds, inherit_claim

ORIGIN_AT = "2026-11-20T08:38:50+00:00"
MEASURED_AT = "2026-11-20T09:06:13+00:00"


def _pre155(root):
    directory = root / EVOLVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    origin = {"round": "round-origin.json", "started_at": ORIGIN_AT, "finished_at": MEASURED_AT,
              "stream": {"source": "SMD"}, "stream_path": "data/smd/stream-one.json", "applied": ["v020"],
              "trials": [], "accepted": [], "rejected": [],
              "stream_path_history": [{"from": "", "to": "data/smd/stream-one.json", "at": ORIGIN_AT,
                                       "geometry": "disagrees", "why": "claimed by hand"}]}
    successor = {"round": "round-successor.json", "started_at": ORIGIN_AT, "finished_at": MEASURED_AT,
                 "stream": {"source": "SMD"}, "stream_path": "data/smd/stream-one.json", "applied": ["v020"],
                 "trials": [], "accepted": [], "rejected": [],
                 "stream_path_history": [{"from": "data/smd/stream-one.json",
                                          "to": "data/smd/stream-one.json", "at": MEASURED_AT,
                                          "geometry": "disagrees", "why": "claimed by hand",
                                          "inherited_from": "round-origin.json"}]}
    for record in (origin, successor):
        (directory / record["round"]).write_bytes(json.dumps(record, indent=2).encode("utf-8"))
        with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"round": record["round"]}) + "\n")
    return root


def test_a_repair_made_age_is_flagged_in_the_row(tmp_path):
    root = _pre155(tmp_path)
    backfill_claims(root)

    rows = claimed_rounds([json.loads((root / EVOLVE_DIR / "round-successor.json").read_text(encoding="utf-8"))])

    assert rows[0]["backfilled"] is True          # this entry's own fields were completed by the backfill
    assert rows[0]["since"] == ORIGIN_AT


def test_an_age_a_record_carried_is_not_flagged(tmp_path):
    record = {"round": "round-plain.json", "stream_path": "data/smd/stream-one.json",
              "stream_path_history": [{"from": "", "to": "data/smd/stream-one.json", "at": ORIGIN_AT,
                                       "geometry": "disagrees", "why": "", "claim_origin": "round-plain.json",
                                       "claim_since": ORIGIN_AT, "claim_hops": 0,
                                       "claim_since_source": "derived"}]}

    assert claimed_rounds([record])[0]["backfilled"] is False


def test_the_successor_inherits_the_repair_not_just_the_date(tmp_path):
    root = _pre155(tmp_path)
    backfill_claims(root)
    successor = json.loads((root / EVOLVE_DIR / "round-successor.json").read_text(encoding="utf-8"))

    entry = inherit_claim(successor, "data/smd/stream-one.json")[0]

    # The date is the same one; what must not be lost is that a repair produced it (boundary 379).
    assert entry["claim_since"] == ORIGIN_AT
    assert entry["claim_since_source"] == "backfilled"
    rows = claimed_rounds([{"round": "round-third.json", "stream_path": "data/smd/stream-one.json",
                            "stream_path_history": [entry]}])
    assert rows[0]["backfilled"] is True
    assert rows[0]["hops"] == 2


def test_the_page_says_the_date_was_completed_by_a_repair(tmp_path):
    root = _pre155(tmp_path)
    backfill_claims(root)
    rows = claimed_rounds([json.loads((root / EVOLVE_DIR / "round-successor.json").read_text(encoding="utf-8"))])

    page = format_campaign(campaign(root), claims=rows)

    assert ("was inherited 1 time(s) and was first recorded on round-origin.json at " + ORIGIN_AT
            + " (that date was completed by a repair, not carried by the record)") in page


def test_a_carried_date_gets_no_parenthesis(tmp_path):
    record = {"round": "round-plain.json", "stream_path": "data/smd/stream-one.json",
              "stream_path_history": [{"from": "", "to": "data/smd/stream-one.json", "at": ORIGIN_AT,
                                       "geometry": "disagrees", "why": "", "claim_origin": "round-previous.json",
                                       "claim_since": ORIGIN_AT, "claim_hops": 1,
                                       "claim_since_source": "derived"}]}

    page = format_campaign(campaign(tmp_path), claims=claimed_rounds([record]))

    assert "was inherited 1 time(s)" in page
    assert "completed by a repair" not in page
