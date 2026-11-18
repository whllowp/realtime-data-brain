"""The proposal rule: what the loop may propose, and the bound that keeps it honest.

The tests are mostly about the bound. A generator that can propose anything is indistinguishable from a
search over a space nobody declared, which is the failure this module exists to avoid.
"""
from __future__ import annotations

from brian.config import load_settings
from brian.evolve import Candidate, catalogue
from brian.propose import (
    LIMITS,
    MAX_PROPOSALS,
    PAIRS,
    SPACE_FIELDS,
    Mode,
    ProposalSpace,
    default_space,
    format_proposal,
    mode_from_history,
    neighbour_values,
    neighbours,
    propose,
    proposal_capable,
    space_from_catalogue,
)


def _record(*candidates: Candidate, accepted: bool = True, source: str = "SMD") -> dict:
    # A round that measured a candidate changing the window also measured the deployment at that window
    # (round 31); without it the acceptance cannot be re-decided at the geometry the change was made on,
    # so it is not a seed. These fixtures record the reference the round would have taken.
    references = {}
    for candidate in candidates:
        for section, field, value in candidate.changes():
            if section == "digest" and field == "rolling_window" and isinstance(value, int):
                references[str(value)] = {"candidate": "baseline@%d" % value,
                                          "recall_at_budget": 0.10, "fpr_at_budget": 0.05}
    return {
        "finished_at": "2026-01-01T00:00:00+00:00",
        "stream": {"source": source},
        "accepted": [candidate.name for candidate in candidates] if accepted else [],
        "references": references,
        "trials": [
            {"candidate": candidate.to_dict(),
             "verdict": {"candidate": candidate.name, "accepted": accepted, "rule": "cost",
                         "reason": "test"}}
            for candidate in candidates
        ],
    }


def _seed(entries: list) -> Candidate:
    return next(candidate for candidate in entries if candidate.name == "host-rolling-rank")


def test_the_catalogue_is_the_source_until_the_ledger_says_otherwise():
    proposal = propose([])
    assert proposal.mode == "catalogue"
    assert [candidate.name for candidate in proposal.candidates] == [c.name for c in catalogue()]


def test_a_magnitude_accepted_on_one_stream_is_not_a_seed_on_another():
    """600 seconds of alert reuse was measured on a stream whose scenario repeats every minute.

    Carrying that number into a host recording whose faults are twenty minutes apart would be a
    proposal nobody measured, so the seeds have to come from the stream this round will run on.
    """
    seed = _seed(catalogue())
    rounds = [_record(seed, source="demo")]
    elsewhere = propose(rounds, source="SMD")
    assert elsewhere.mode == "catalogue"
    assert "no recorded round on SMD" in elsewhere.reason
    here = propose(rounds, source="demo")
    assert here.mode == "expand"
    assert here.source == "demo"
    assert "on demo" in here.reason

    refused = propose([_record(seed, accepted=False, source="SMD")], source="SMD",
                      mode=Mode("expand", "forced for the test"))
    assert refused.mode == "catalogue"
    assert "no change has ever been accepted on SMD" in refused.reason


def test_an_unsourced_ledger_still_seeds_the_search():
    rounds = [_record(_seed(catalogue()), source="")]
    proposal = propose(rounds)
    assert proposal.mode == "expand"
    assert all(candidate.name.startswith("host-rolling-rank")
               for candidate in proposal.candidates)


def test_an_accepted_change_is_a_measured_point_and_its_neighbours_are_the_question():
    entries = catalogue()
    proposal = propose([_record(_seed(entries))], entries=entries)
    assert proposal.mode == "expand"
    names = [candidate.name for candidate in proposal.candidates]
    # `digest.rolling_window = 100` sits inside (0, 1000), so 50 and 200 are the two magnitudes either
    # side of it; 101 is not offered because one more window is the same magnitude, not a new one.
    assert any(name.endswith("-50") for name in names)
    assert any(name.endswith("-200") for name in names)
    assert not any(name.endswith("-101") for name in names)


def test_a_neighbour_stays_inside_the_declared_interval():
    precision = next(candidate for candidate in catalogue() if candidate.name == "host-precision-3")
    space = default_space()
    proposed = neighbours(precision, space)
    values = [candidate.changes()[0][2] for candidate in proposed]
    low, high = LIMITS[("digest", "host_precision")]
    assert values, "a bounded integer field should have neighbours"
    assert all(low <= value <= high for value in values)
    assert 6 not in values, "one step past the interval is not a proposal"


def test_a_field_without_an_interval_is_never_neighboured():
    # `digest.scoring` is a string choice: there is no interval between "auto" and anything else, so a
    # neighbour would be an invented ordinal, not a magnitude.
    only_string = Candidate(name="only-string", hypothesis="h", section="digest", field="scoring",
                            value="auto")
    assert neighbours(only_string, default_space()) == []


def test_a_declared_pair_is_shifted_as_a_band_and_never_split():
    band = next(candidate for candidate in catalogue() if candidate.name == "host-scoring-deviation")
    space = default_space()
    proposed = neighbours(band, space)
    assert proposed, "the pair has a numeric member, so it has neighbours"
    for candidate in proposed:
        changes = dict(((section, field), value) for section, field, value in candidate.changes())
        first = changes[("gate", "anomaly_probability")]
        second = changes[("gate", "notify_probability")]
        assert first == second, "the pair must keep its gap, or the proposal is half a fix"
        assert "-band-" in candidate.name


def test_a_pair_declaration_covers_the_two_probability_bars():
    assert any({("gate", "anomaly_probability"), ("gate", "notify_probability")} == set(pair)
               for pair in PAIRS)


def test_a_rejected_change_is_not_a_seed():
    entries = catalogue()
    rejected = _record(_seed(entries), accepted=False)
    proposal = propose([rejected, rejected, rejected], entries=entries)
    # The mode is "expand" — three empty rounds — but nothing was ever accepted, so there is no measured
    # magnitude to search around and the catalogue stays the source. That reason is the whole point.
    assert proposal.mode == "catalogue"
    assert "no change has ever been accepted" in proposal.reason
    assert [candidate.name for candidate in proposal.candidates] == [c.name for c in entries]


def test_three_empty_rounds_are_evidence_about_the_catalogue():
    mode = mode_from_history([{"accepted": []}, {"accepted": []}, {"accepted": []}])
    assert mode.name == "expand"
    assert "the last 3 rounds accepted nothing" in mode.reason


def test_two_empty_rounds_are_not_yet_evidence():
    mode = mode_from_history([{"accepted": ["x"]}, {"accepted": []}, {"accepted": []}])
    assert mode.name == "catalogue"


def test_the_cap_is_respected():
    entries = catalogue()
    proposal = propose([_record(_seed(entries))], entries=entries, max_proposals=2)
    assert len(proposal.candidates) == 2
    assert default_space().max_proposals == MAX_PROPOSALS


def test_the_proposal_is_deterministic():
    entries = catalogue()
    rounds = [_record(_seed(entries))]
    first = [candidate.name for candidate in propose(rounds, entries=entries).candidates]
    again = [candidate.name for candidate in propose(rounds, entries=entries).candidates]
    assert first == again


def test_every_proposal_can_actually_be_applied():
    settings = load_settings()
    entries = catalogue()
    proposal = propose([_record(_seed(entries))], entries=entries)
    assert proposal.candidates
    for candidate in proposal.candidates:
        applied = candidate.apply(settings)
        assert applied is not settings


def test_the_space_fingerprint_moves_only_when_the_space_moves():
    base = default_space()
    assert base.fingerprint() == default_space().fingerprint()
    wider = ProposalSpace(fields=base.fields,
                          limits={**base.limits, ("digest", "host_precision"): (0, 9)},
                          max_proposals=base.max_proposals)
    assert wider.fingerprint() != base.fingerprint()
    capped = ProposalSpace(fields=base.fields, limits=base.limits, max_proposals=1)
    assert capped.fingerprint() != base.fingerprint()


def test_an_unknown_field_cannot_enter_the_space_through_the_catalogue():
    invented = Candidate(name="invented", hypothesis="h", section="gate", field="no_such_field", value=1)
    space = space_from_catalogue([invented])
    assert space.fields == ()
    assert not space.allowed("gate", "no_such_field", 1)


def test_the_space_fields_are_a_subset_of_what_the_catalogue_touches():
    touched = {(section, name) for candidate in catalogue() for section, name, _value in candidate.changes()}
    assert set(SPACE_FIELDS) >= {pair for pair in touched if pair in LIMITS}


def test_the_proposal_record_is_what_a_round_stores():
    entries = catalogue()
    proposal = propose([_record(_seed(entries))], entries=entries)
    payload = proposal.to_dict()
    assert payload["mode"] == "expand"
    assert payload["space_fingerprint"] == default_space().fingerprint()
    assert payload["candidates"] == [candidate.name for candidate in proposal.candidates]
    assert payload["reason"]


def test_the_table_says_what_is_outside_the_generator():
    text = format_proposal(propose([], entries=catalogue()))
    assert "mode   catalogue" in text
    assert "what changes here is what is proposed" in text


def test_an_unimplemented_mode_falls_back_to_the_catalogue():
    entries = catalogue()
    proposal = propose([], entries=entries, mode=Mode("wishful", "because"))
    # A mode this build does not implement must behave like `catalogue`, not like "do something clever".
    assert proposal.mode == "wishful"
    assert [c.name for c in proposal.candidates] == [c.name for c in entries]


def test_the_capability_check_is_a_code_path_not_a_promise():
    assert proposal_capable() is True


def test_neighbour_values_are_typed_like_their_parent():
    assert all(isinstance(value, int) for value in neighbour_values(8))
    assert all(isinstance(value, float) for value in neighbour_values(0.9))
    assert neighbour_values("auto") == []


def test_a_neighbour_of_a_neighbour_keeps_a_name_a_reader_can_use():
    # Two demo rounds produced `reuse-300-600-1200`: the parent's own magnitude was glued onto the name
    # the child had to carry, so a third round would have produced `reuse-300-600-1200-2400`. The value
    # in the name should be the one the candidate proposes, not the whole path it took to get there.
    parent = Candidate(name="reuse-300-600", hypothesis="h", section="gate",
                       field="reuse_report_within_s", value=600)
    names = [candidate.name for candidate in neighbours(parent, default_space())]
    assert names
    assert all(name.startswith("reuse-300-") for name in names), names
    assert not any(name.startswith("reuse-300-600-") for name in names), names
