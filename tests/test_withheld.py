# -*- coding: utf-8 -*-
"""Round 42 tests: a refusal is a record, not a sentence.

Until this round a `replicate` refusal existed only as a verdict inside one round's file, so "why wasn't this
promoted" required finding that round, and "would it still be blocked" required a hypothetical rejudge. The
refusal is now written down as `w###` next to the versions the loop *did* ship, carrying the bar it was
judged against, the loss it was refused for, and the measurement behind both — which is what lets a later
reader disagree with the bar without measuring anything again.
"""
import json

from brian.campaign import campaign, format_campaign
from brian.evolve import (EvolveRules, Measurement, RoundResult, Verdict, Withheld, format_withheld,
                          persist, withheld_records, withheld_staleness)

LOSS = {
    "recording": "smd-machine-1-6",
    "before": 0.4909,
    "after": 0.3818,
    "drop": 0.1091,
    "tolerance": 0.05,
    "pooled_before": 0.2969,
    "pooled_after": 0.3125,
    "budget": 0.10,
}

CANDIDATE = {
    "name": "host-features-3",
    "hypothesis": "three series rather than eight",
    "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]],
    "veto": None,
}

STREAM = {"source": "SMD", "streams": 2, "windows": 1355, "scored": 1235, "anomalous": 192}


def _measurement(name, *, recall_at_budget=0.312, split=None, tokens=2908.0):
    payload = {
        "candidate": name,
        "recall": recall_at_budget,
        "fpr": 0.097,
        "precision": 0.32,
        "reports": 261,
        "tokens_per_report": tokens,
        "tokens_per_window": 560.0,
        "windows": 1235,
        "recall_at_budget": recall_at_budget,
        "fpr_at_budget": 0.097,
        "budget": 0.10,
        "window": 200,
        "recall_by_recording": dict(split or {"smd-machine-1-6": 0.3818, "smd-machine-2-2": 0.2195}),
        "fpr_by_recording": {"smd-machine-1-6": 0.097, "smd-machine-2-2": 0.097},
    }
    return Measurement.from_dict(payload)


def _result(*, verdicts):
    trials = []
    for candidate, verdict in verdicts:
        built = candidate if not isinstance(candidate, dict) else _candidate(candidate["name"],
                                                                            candidate["changes"])
        trials.append((built, _measurement(built.name), verdict))
    return RoundResult(
        started_at="2026-11-18T02:44:00+00:00",
        finished_at="2026-11-18T02:44:10+00:00",
        stream=dict(STREAM),
        rules=EvolveRules(),
        baseline=_measurement("baseline", recall_at_budget=0.297),
        trials=trials,
    )


def _candidate(name, changes):
    from brian.evolve import Candidate

    section, field, value = changes[0]
    return Candidate(name=name, hypothesis="h", section=section, field=field, value=value)


def _replicate(name="host-features-3"):
    return Verdict(candidate=name, accepted=False, rule="replicate",
                   reason="recall rises pooled, but smd-machine-1-6 falls 0.491 -> 0.382",
                   detail=dict(LOSS))


def _accepted(name="events-8"):
    return Verdict(candidate=name, accepted=True, rule="cost",
                   reason="tokens per report fall 3356 -> 2908")


def _withheld_file(root, number=1, *, bar=0.05, loss=None, source="SMD", name="host-features-3"):
    folder = root / "var" / "evolve" / "withheld"
    folder.mkdir(parents=True, exist_ok=True)
    payload = Withheld(
        number=number,
        round="round-20261118T024405Z.json",
        created_at="2026-11-18T02:44:10+00:00",
        stream={"source": source} if source else {},
        candidate={"name": name},
        changes=[["digest", "host_features", "cpu_r,load_1,mem_free"]],
        bar=bar,
        loss=dict(LOSS if loss is None else loss),
        measured=_measurement(name).to_dict(),
        reason="not a replicated gain",
    ).to_dict()
    path = folder / ("w%03d.json" % number)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_replicate_refusal_is_written_as_its_own_record(tmp_path):
    written = persist(tmp_path, _result(verdicts=[(CANDIDATE, _replicate())]), write_versions=False)
    assert [path.rsplit("\\", 1)[-1] for path in written["withheld"]] == ["w001.json"]
    payload = json.loads((tmp_path / "var" / "evolve" / "withheld" / "w001.json").read_text(encoding="utf-8"))
    assert payload["bar"] == 0.05
    assert payload["loss"]["drop"] == 0.1091
    assert payload["loss"]["recording"] == "smd-machine-1-6"
    assert payload["candidate"]["name"] == "host-features-3"
    assert payload["round"].startswith("round-2026")
    assert payload["stream"]["source"] == "SMD"


def test_a_round_without_a_replicate_refusal_writes_no_record(tmp_path):
    written = persist(tmp_path, _result(verdicts=[(_candidate("events-8", [["events", "limit", 8]]),
                                                   _accepted())]), write_versions=False)
    assert written["withheld"] == []
    assert not (tmp_path / "var" / "evolve" / "withheld").exists()


def test_the_numbers_come_back_out_of_the_file(tmp_path):
    _withheld_file(tmp_path)
    records = withheld_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.label == "w001"
    assert record.name == "host-features-3"
    assert record.domain == "SMD"
    assert record.bar == 0.05
    assert record.loss["drop"] == 0.1091
    assert record.changes == [["digest", "host_features", "cpu_r,load_1,mem_free"]]


def test_numbering_continues_across_rounds(tmp_path):
    persist(tmp_path, _result(verdicts=[(CANDIDATE, _replicate()),
                                        (_candidate("host-features-4", [["digest", "host_features", "cpu_r"]]),
                                         _replicate("host-features-4"))]), write_versions=False)
    persist(tmp_path, _result(verdicts=[(CANDIDATE, _replicate())]), write_versions=False)
    assert [record.label for record in withheld_records(tmp_path)] == ["w001", "w002", "w003"]


def test_an_objection_that_still_holds_says_so(tmp_path):
    _withheld_file(tmp_path)
    state = withheld_staleness(withheld_records(tmp_path)[0], EvolveRules())
    assert state["still"] is True
    assert "0.109" in state["note"] and "still holds" in state["note"]


def test_a_bar_that_outgrew_the_loss_retires_the_objection(tmp_path):
    _withheld_file(tmp_path)
    state = withheld_staleness(withheld_records(tmp_path)[0], EvolveRules(replication_tolerance=0.20))
    assert state["still"] is False
    assert "no longer holds" in state["note"]
    # The default bar is `min_quality_gain`; naming it explicitly must not change the answer.
    assert withheld_staleness(withheld_records(tmp_path)[0],
                              EvolveRules(replication_tolerance=0.05))["still"] is True


def test_a_record_without_numbers_cannot_be_checked(tmp_path):
    _withheld_file(tmp_path, loss={})
    state = withheld_staleness(withheld_records(tmp_path)[0], EvolveRules())
    assert state["comparable"] is False
    assert "does not say how much was lost" in state["note"]


def test_the_table_names_the_recording_whose_gain_it_was(tmp_path):
    _withheld_file(tmp_path)
    text = format_withheld(withheld_records(tmp_path), EvolveRules())
    assert "w001" in text
    assert "host-features-3" in text
    assert "smd-machine-1-6" in text
    assert "0.050" in text and "0.109" in text
    assert "no longer hold    0 of them" in text


def test_an_empty_ledger_does_not_invent_a_refusal():
    text = format_withheld([], EvolveRules())
    assert "no refusal has been written down yet" in text
    assert "records           0 refusal(s) recorded" in text


def test_the_campaign_counts_what_was_measured_and_not_shipped(tmp_path):
    _withheld_file(tmp_path)
    persist(tmp_path, _result(verdicts=[(CANDIDATE, _replicate())]), write_versions=False)
    result = campaign(tmp_path)
    assert result.withheld == 2
    assert result.withheld_stale == 0
    text = format_campaign(result, rules=EvolveRules())
    assert "withheld          2 candidate(s) were measured and refused for replication" in text
    assert "  withheld          2 candidate(s) refused for replication" in text

    looser = campaign(tmp_path, EvolveRules(replication_tolerance=0.20))
    assert looser.withheld_stale == 2
    assert "2 no longer hold at today's bar (0.200)" in format_campaign(
        looser, rules=EvolveRules(replication_tolerance=0.20))


def test_withheld_refusals_are_attributed_to_their_domain(tmp_path):
    _withheld_file(tmp_path, number=1, source="SMD")
    _withheld_file(tmp_path, number=2, source="RCAEval RE1-OB")
    result = campaign(tmp_path)
    by_domain = {row.domain: row.withheld for row in result.rows}
    assert by_domain == {"SMD": 1, "RCAEval RE1-OB": 1}
