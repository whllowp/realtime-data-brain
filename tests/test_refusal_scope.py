# -*- coding: utf-8 -*-
"""Round 36 tests: a refusal is a statement about a stream and a sampling, not about a candidate.

Round 35 made the campaign admit how many of its "settled" refusals were inherited from retired
records. This round asks the next question: when the next round decides what to measure, which of those
refusals may it act on? Three of the four answers are "measure it again" — refused elsewhere, refused by
a retired record nobody re-checked, refused at a different sampling — and only one is a real saving.
"""
import json

from brian.campaign import campaign, format_campaign
from brian.evolve import (EvolveRules, _sampling_differs, history, previously_rejected,
                          refusal_plan, rejection_provenance, retire)

RULES = EvolveRules()
SMD = {"source": "SMD", "windows": 1355, "scored": 1235, "cadence_s": 2100.0}
SMD_CHECK = {"source": "SMD", "windows": 948, "scored": 828, "cadence_s": 3000.0}
DEMO = {"source": "demo", "windows": 40, "scored": 35}

REJECT = {"accepted": False, "rule": "gain", "reason": "recall at the budget did not rise"}
ACCEPT = {"accepted": True, "rule": "cost", "reason": "the bill per report fell"}


def _measurement(name, *, recall=0.30, tokens=3200.0):
    return {
        "candidate": name,
        "recall": recall,
        "fpr": 0.09,
        "precision": 0.7,
        "reports": 200,
        "tokens_per_report": tokens,
        "tokens_per_window": 480.0,
        "windows": 1235,
        "recall_at_budget": recall,
        "fpr_at_budget": 0.09,
        "budget": 0.10,
        "window": 200,
    }


def _round_file(root, name, *, stream, verdicts, recall=0.30):
    """`verdicts` is `{candidate name: {"accepted": bool, ...}}`; the measurement is a default one.

    `recall` is the at-budget recall of every trial, and it decides what today's rules make of the stored
    verdict: a trial at the baseline's 0.30 is refused now even where the record says accepted, which is
    how the first version of the "clean domain" fixture produced a refusal it did not intend.
    """
    trials = [{
        "candidate": {"name": candidate, "hypothesis": "h", "changes": [["pipeline", "context_events", 8]],
                      "veto": None},
        "measurement": _measurement(candidate, recall=recall),
        "verdict": dict(REJECT, candidate=candidate, **flags),
    } for candidate, flags in verdicts.items()]
    payload = {
        "round": name,
        "started_at": "2026-11-18T01:00:00+00:00",
        "finished_at": "2026-11-18T01:01:00+00:00",
        "stream": dict(stream),
        "rules": RULES.to_dict(),
        "baseline": _measurement("baseline"),
        "trials": trials,
        "references": {},
    }
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"round": name, "started_at": payload["started_at"], "accepted": [],
                                 "rejected": list(verdicts), "applied": [], "rules_hash": "test",
                                 "baseline_tokens_per_report": 3200.0}) + "\n")
    return name


def test_a_refusal_can_be_asked_for_by_stream(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=DEMO, verdicts={"synthetic-only": {}})
    _round_file(tmp_path, "round-two.json", stream=SMD, verdicts={"events-8": {}})

    assert set(previously_rejected(tmp_path)) == {"synthetic-only", "events-8"}
    assert set(previously_rejected(tmp_path, source="SMD")) == {"events-8"}
    assert set(previously_rejected(tmp_path, source="demo")) == {"synthetic-only"}


def test_provenance_carries_the_stream_the_refusal_was_measured_on(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=SMD_CHECK, verdicts={"events-8": {}})

    provenance = rejection_provenance(tmp_path)["events-8"]

    assert provenance["stream"]["cadence_s"] == 3000.0
    assert provenance["source"] == "SMD"


def test_the_plan_saves_only_the_measurement_this_stream_already_made(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=SMD, verdicts={"same-here": {}})
    _round_file(tmp_path, "round-two.json", stream=DEMO, verdicts={"other-stream": {}})
    retired = _round_file(tmp_path, "round-three.json", stream=SMD, verdicts={"inherited": {}})
    retire(tmp_path, retired, re_measured_as="round-one.json")

    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=SMD)

    assert plan["skip"] == ["same-here"]
    assert plan["elsewhere"] == ["other-stream"]
    assert plan["inherited"] == ["inherited"]
    assert plan["reopened"] == []


def test_a_refusal_at_another_sampling_is_reopened_not_skipped(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=SMD_CHECK, verdicts={"window-bound": {}})

    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=SMD)

    assert plan["skip"] == []
    assert plan["reopened"] == ["window-bound"]


def test_a_refusal_without_a_recorded_geometry_is_not_evidence_here(tmp_path):
    legacy = {"source": "SMD"}
    _round_file(tmp_path, "round-one.json", stream=legacy, verdicts={"old-refusal": {}})

    assert _sampling_differs({"source": "SMD"}, SMD) is True
    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=SMD)
    assert plan["reopened"] == ["old-refusal"]


def test_an_accept_updates_the_plan_and_an_older_refusal_stops_being_skipped(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=SMD, verdicts={"events-8": {}})
    _round_file(tmp_path, "round-two.json", stream=SMD, verdicts={"events-8": {**ACCEPT}})

    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=SMD)

    assert plan["skip"] == []
    assert all(not group for group in plan.values())


def test_the_plan_without_a_shape_leaves_the_geometry_out(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=SMD_CHECK, verdicts={"window-bound": {}})

    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD")

    assert plan["skip"] == ["window-bound"]


def test_the_campaign_names_the_domain_that_holds_the_refusal(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=DEMO, verdicts={"synthetic-only": {}, "second": {}})
    retired = _round_file(tmp_path, "round-two.json", stream=SMD, verdicts={"inherited": {}})
    retire(tmp_path, retired, re_measured_as="round-one.json")

    result = campaign(tmp_path, RULES)
    rows = {row.domain: row for row in result.rows}

    assert result.refusals == 3
    assert result.refusals_retired == 1
    assert rows["demo"].refusals == 2
    assert rows["demo"].refusals_retired == 0
    assert rows["SMD"].refusals == 1
    assert rows["SMD"].refusals_retired == 1
    assert rows["SMD"].to_dict()["refusals_carried"] == 1
    assert rows["SMD"].to_dict()["refusals_from_retired"] == 1

    text = format_campaign(result)
    assert "  refusals          2 held" in text
    assert "  refusals          1 held, 1 inherited from a retired record" in text


def test_a_clean_domain_shows_no_refusal_line(tmp_path):
    _round_file(tmp_path, "round-one.json", stream=DEMO, verdicts={"synthetic-only": {}})
    _round_file(tmp_path, "round-two.json", stream=SMD, verdicts={"events-8": {**ACCEPT}}, recall=0.45)

    text = format_campaign(campaign(tmp_path, RULES))

    assert "  refusals          1 held" in text
    assert text.count("  refusals ") == 1


def test_the_round_drops_a_proposal_the_stream_already_refused():
    from brian.cli import _skip_refused
    from brian.evolve import Candidate

    proposed = [Candidate("events-11", "h", "llm", "max_context_events", 11),
                Candidate("events-9", "h", "llm", "max_context_events", 9)]

    kept, skipped = _skip_refused(proposed, ["events-11"], named=False)

    assert [candidate.name for candidate in kept] == ["events-9"]
    assert [candidate.name for candidate in skipped] == ["events-11"]


def test_a_named_candidate_is_measured_even_when_a_refusal_covers_it():
    from brian.cli import _skip_refused
    from brian.evolve import Candidate

    named = [Candidate("events-11", "h", "llm", "max_context_events", 11)]

    kept, skipped = _skip_refused(named, ["events-11"], named=True)

    assert kept == named
    assert skipped == named


def test_history_still_hides_retired_records_from_the_plan(tmp_path):
    retired = _round_file(tmp_path, "round-one.json", stream=SMD, verdicts={"inherited": {}})
    retire(tmp_path, retired, re_measured_as="round-two.json")

    assert history(tmp_path) == []
    assert [row["round"] for row in history(tmp_path, include_retired=True)] == ["round-one.json"]
    assert refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=SMD)["inherited"] == [
        "inherited"]
