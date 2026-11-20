"""Round 153: a round can stand on a file a person claimed, and the geometry said no.

Round 152 gave the out-of-reach half a verb, and the verb records the verdict it rested on. Nothing said so
at the moment the verdict mattered -- when the queue re-measures that round on that file, and when the page
counts it as paid. These tests hold the sentence, its position (warn only about a measurement that happens),
and the page count.
"""

import inspect
import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import claim_note, claimed_rounds

CLAIMED = {
    "round": "round-out-of-reach.json",
    "stream_path": "data/smd/stream-one.json",
    "stream_path_history": [
        {"from": "", "to": "data/smd/stream-one.json", "at": "2026-11-20T08:38:50+00:00",
         "geometry": "disagrees", "why": "claimed by hand"},
    ],
}


def test_a_record_with_no_claim_says_nothing():
    assert claim_note({}) is None
    assert cli._claim_note_line({}) == ""


def test_a_claim_the_geometry_confirmed_says_nothing():
    record = dict(CLAIMED, stream_path_history=[dict(CLAIMED["stream_path_history"][0],
                                                     geometry="agrees")])

    assert claim_note(record) is None
    assert cli._claim_note_line(record) == ""


def test_a_disputed_claim_names_the_file_and_the_verdict():
    note = claim_note(CLAIMED)

    assert note is not None
    assert "data/smd/stream-one.json" in note and "disagrees with" in note
    line = cli._claim_note_line(CLAIMED)
    assert line.startswith("  careful: ")
    assert "evidence about that file, not about the one the round first measured" in line
    assert "(boundary 371)" in line


def test_an_unchecked_claim_says_it_was_never_checked():
    record = dict(CLAIMED, stream_path_history=[dict(CLAIMED["stream_path_history"][0],
                                                     geometry="unknown")])

    note = claim_note(record)

    # `unknown` is not `agrees`: a claim nobody checked says so, rather than reading as confirmed.
    assert note is not None and "was never checked against" in note


def test_a_claim_for_another_file_is_not_read_as_this_one():
    # The record has since come to name another file: the old claim is about a file this record no longer
    # stands on, so it says nothing about the measurement that would run today.
    record = {"round": "round-a.json", "stream_path": "data/smd/stream-two.json",
              "stream_path_history": CLAIMED["stream_path_history"]}

    assert claim_note(record) is None


def test_the_page_lists_the_rounds_that_rest_on_a_claim():
    rows = claimed_rounds([CLAIMED, {"round": "round-clean.json"}])

    # Round 155 added the chain columns: a claim recorded by hand is its own origin, with no inheritance yet.
    # Round 157 added whether the age came from a repair; a hand-recorded claim's date never did.
    assert rows == [{"round": "round-out-of-reach.json", "stream_path": "data/smd/stream-one.json",
                     "geometry": "disagrees", "at": "2026-11-20T08:38:50+00:00",
                     "origin": "", "since": "2026-11-20T08:38:50+00:00", "hops": 0,
                     "backfilled": False}]


def test_a_round_whose_claim_agrees_is_not_counted():
    agreed = dict(CLAIMED, stream_path_history=[dict(CLAIMED["stream_path_history"][0],
                                                     geometry="agrees")])

    assert claimed_rounds([agreed]) == []


def test_the_campaign_page_prints_the_claim_lines(tmp_path):
    page = format_campaign(campaign(tmp_path), claims=claimed_rounds([CLAIMED]))

    assert "claims            1 round(s) name a file a person claimed" in page
    assert "round-out-of-reach.json data/smd/stream-one.json (disagrees)" in page
    assert "a re-measurement on such a round is evidence about that file" in page


def test_the_campaign_payload_carries_the_claim_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "var" / "evolve").mkdir(parents=True)
    record = dict(CLAIMED, accepted=[], rejected=[], applied=["v020"], trials=[],
                  started_at="2026-11-20T08:00:00+00:00")
    (tmp_path / "var" / "evolve" / "round-out-of-reach.json").write_text(
        json.dumps(record), encoding="utf-8")
    # The ledger is the index: a round file the ledger does not name is invisible to every page.
    (tmp_path / "var" / "evolve" / "ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    code = cli.main(["evolve", "--campaign", "--json-out", "claims.json"])

    payload = json.loads((tmp_path / "claims.json").read_text(encoding="utf-8"))
    assert code == 0
    assert [row["round"] for row in payload["claims"]] == ["round-out-of-reach.json"]
    assert payload["claims"][0]["geometry"] == "disagrees"


def test_the_warning_comes_after_the_record_is_known_to_be_re_measurable():
    # Boundary 372: the first cut printed the sentence and then measured nothing, because the record kept no
    # candidate definitions. The order is the property, so the order is what this test reads.
    source = inspect.getsource(cli)
    start = source.index("def run_one(")
    body = source[start: source.index("for domain, name, _why, files in ready[:limit]:")]
    nothing = body.index("there is nothing to measure again")
    warning = body.index("note = _claim_note_line(record)")

    assert nothing < warning
