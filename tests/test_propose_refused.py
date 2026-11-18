"""A refusal the ledger already answered must not become a proposal again.

Round 36 filtered the *measurement* after the mechanism had already spent its proposal budget, so a
candidate this stream refused still occupied a slot: the round said "not measured 14" and then measured
the same eight neighbours it would have measured anyway. Round 37 moves the filter into the generator,
which makes two things testable — the candidate is never proposed, and the count of candidates the
mechanism did not put forward is written into what the round records.
"""
from __future__ import annotations

from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, catalogue
from brian.propose import Mode, Proposal, describe_proposal, format_proposal, propose


def _candidate(name: str = "events-8") -> Candidate:
    return Candidate(name, "hypothesis", "llm", "max_context_events", 8)


def _round_file(*candidates: Candidate, source: str = "SMD") -> dict:
    trials = []
    for candidate in candidates:
        trials.append(
            {"candidate": candidate.to_dict(),
             "verdict": {"candidate": candidate.name, "accepted": True, "rule": "cost",
                         "reason": "test"}}
        )
    return {
        "finished_at": "2026-01-01T00:00:00+00:00",
        "stream": {"source": source},
        "accepted": [candidate.name for candidate in candidates],
        "trials": trials,
    }


def _measurement(name: str) -> Measurement:
    return Measurement(
        candidate=name, recall=0.40, fpr=0.05, precision=0.8, reports=100,
        tokens_per_report=2000.0, tokens_per_window=200.0, latency_p95_ms=1.0,
        recall_at_budget=0.40, fpr_at_budget=0.05,
    )


def test_a_refused_candidate_is_not_proposed_from_the_catalogue() -> None:
    proposal = propose([], mode=Mode("catalogue", "forced"), entries=catalogue())

    assert "events-8" in [candidate.name for candidate in proposal.candidates]
    assert proposal.refused == 0


def test_a_refused_catalogue_entry_is_dropped_and_counted() -> None:
    proposal = propose([], mode=Mode("catalogue", "forced"), entries=catalogue(),
                       refused=["events-8", "events-4"])

    names = [candidate.name for candidate in proposal.candidates]
    assert "events-8" not in names
    assert "events-4" not in names
    assert proposal.refused == 2


def test_a_catalogue_that_is_entirely_refused_says_so() -> None:
    entries = catalogue()
    proposal = propose([], mode=Mode("catalogue", "forced"), entries=entries,
                       refused=[candidate.name for candidate in entries])

    assert proposal.candidates == []
    assert "was refused on this stream at this sampling" in proposal.reason
    assert str(len(entries)) in proposal.reason


def test_generated_neighbours_the_ledger_refused_are_never_generated() -> None:
    seed = Candidate("events-8", "hypothesis", "llm", "max_context_events", 8)
    rounds = [_round_file(seed)]

    first = propose(rounds, source="SMD")
    names = [candidate.name for candidate in first.candidates]
    assert names

    second = propose(rounds, source="SMD", refused=names)
    assert not set(names) & {candidate.name for candidate in second.candidates}
    assert second.refused == len(names)


def test_a_space_with_nothing_but_refusals_falls_back_to_the_catalogue() -> None:
    """When every neighbour this stream may not re-measure, the mechanism must say so, not go silent."""
    seed = Candidate("events-8", "hypothesis", "llm", "max_context_events", 8)
    rounds = [_round_file(seed)]

    full = propose(rounds, source="SMD")
    refused = [candidate.name for candidate in full.candidates]
    filtered = propose(rounds, source="SMD", refused=refused)

    assert filtered.mode == "catalogue"
    assert filtered.refused == len(refused)
    assert "already refused on this stream at this sampling" in filtered.reason
    assert not set(refused) & {candidate.name for candidate in filtered.candidates}


def test_a_reshape_this_sampling_already_answered_is_not_measured_twice() -> None:
    """The promise rule is a different *judge*, not a licence to re-ask an answered question."""
    promise = Candidate("v020@hint286", "hypothesis", "digest", "rolling_window", 286,
                        promise=0.507, provenance="v020's own settings")
    proposal = propose([], mode=Mode("catalogue", "forced"), entries=catalogue(),
                       reshaped=promise, refused=[promise.name])

    assert promise.name not in [candidate.name for candidate in proposal.candidates]
    assert proposal.refused == 1


def test_a_reshape_this_sampling_has_not_answered_is_proposed_first() -> None:
    promise = Candidate("v020@hint286", "hypothesis", "digest", "rolling_window", 286,
                        promise=0.507, provenance="v020's own settings")
    proposal = propose([], mode=Mode("catalogue", "forced"), entries=catalogue(),
                       reshaped=promise)

    assert [candidate.name for candidate in proposal.candidates][0] == "v020@hint286"
    assert proposal.refused == 0


def test_the_count_travels_in_the_proposal_document() -> None:
    proposal = Proposal("catalogue", "reason", [], None, "")
    proposal.refused = 3

    assert proposal.to_dict()["refused_skipped"] == 3


def test_the_proposal_printers_name_the_drop() -> None:
    proposal = Proposal("catalogue", "reason", [], None, "")
    proposal.refused = 2

    assert any("2 candidate(s) dropped" in line for line in describe_proposal(proposal))
    assert "2 candidate(s) were dropped because this stream already refused them" in format_proposal(proposal)


def test_the_round_record_says_how_many_measurements_it_did_not_spend() -> None:
    proposal = Proposal("catalogue", "reason", [], None, "")
    proposal.refused = 4
    result = RoundResult(
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        stream={"source": "SMD"},
        rules=EvolveRules(),
        baseline=_measurement("baseline"),
        trials=[],
        ceiling={},
        proposal=proposal.to_dict(),
    )

    assert result.to_dict()["proposal"]["refused_skipped"] == 4


def _seeds() -> list:
    """Two measured points, so the neighbourhood is wider than one seed's two neighbours."""
    return [Candidate("events-8", "hypothesis", "llm", "max_context_events", 8),
            Candidate("events-4", "hypothesis", "llm", "max_context_events", 4)]


def _neighbour_names(max_proposals: int, refused=()) -> list:
    proposal = propose([_round_file(*_seeds())], source="SMD",
                       max_proposals=max_proposals, refused=list(refused))
    return [candidate.name for candidate in proposal.candidates]


def test_a_slot_a_refusal_freed_is_named() -> None:
    """Round 38: with a capped proposal the saving is not cheaper — the cap refills the slot."""
    unfiltered = _neighbour_names(4)
    assert len(unfiltered) == 4
    proposal = propose([_round_file(*_seeds())], source="SMD", max_proposals=2,
                       refused=unfiltered[:1])

    assert [candidate.name for candidate in proposal.candidates] == unfiltered[1:3]
    assert proposal.substitutes == [unfiltered[2]]
    assert proposal.refused == 1


def test_an_uncapped_proposal_names_no_substitute() -> None:
    # Nothing was cut, so nothing could have been admitted in a refusal's place: an empty list is the
    # honest answer, and a substitute invented here would be a claim about a cap that never bound.
    names = _neighbour_names(100)
    proposal = propose([_round_file(*_seeds())], source="SMD", max_proposals=100,
                       refused=names[:1])

    assert proposal.refused == 1
    assert proposal.substitutes == []


def test_the_printers_name_the_substitute_and_the_empty_case_prints_nothing() -> None:
    proposal = Proposal("expand", "reason", [], None, "")
    proposal.refused = 2
    proposal.substitutes = ["events-12"]

    described = "\n".join(describe_proposal(proposal))
    printed = format_proposal(proposal)
    assert "1 slot(s) went to candidate(s) the cap would have cut: events-12" in described
    assert "1 slot(s) went to candidate(s) the cap would otherwise have cut: events-12" in printed

    proposal.substitutes = []
    assert "slot(s)" not in "\n".join(describe_proposal(proposal))
    assert "slot(s)" not in format_proposal(proposal)
