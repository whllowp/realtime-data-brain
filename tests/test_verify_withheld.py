# -*- coding: utf-8 -*-
"""Round 44 tests: the refusal family is re-measured on request, not by luck.

Round 43 let a `skip` refusal expire, but the re-test still depended on a round happening to propose the
candidate again — and round 37 made the plan stop proposing settled ones. So an objection could outlive every
check that could have retired it, which is the same self-sealing loop one layer further out. `--verify-withheld`
asks the question directly: which refusals on this stream have not been measured again inside the horizon, and
what do the numbers say today.

The tests below cover the two halves that decide the answer — when a refusal is *due*, and what a
re-measurement is allowed to conclude — because both were wrong in the round where they were invented: a
measurement without per-recording numbers must say `unknown` rather than announce that an objection died.
"""
from datetime import datetime, timedelta, timezone

import pytest

from brian.campaign import campaign, format_campaign
from brian.evolve import (Measurement, Withheld, EvolveRules, REFUSAL_EXPIRY_DAYS, withheld_check,
                          withheld_due, withheld_evidence_age, withheld_records)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
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
STREAM = {"source": "SMD", "windows": 1355, "scored": 1235, "anomalous": 192}


def _stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _measurement(name, *, recall_at_budget=0.312, split=None):
    return Measurement.from_dict({
        "candidate": name,
        "recall": recall_at_budget,
        "fpr": 0.097,
        "precision": 0.32,
        "reports": 261,
        "tokens_per_report": 2908.0,
        "tokens_per_window": 560.0,
        "windows": 1235,
        "recall_at_budget": recall_at_budget,
        "fpr_at_budget": 0.097,
        "budget": 0.10,
        "window": 200,
        "recall_by_recording": dict(split or {"smd-machine-1-6": 0.3818, "smd-machine-2-2": 0.2195}),
        "fpr_by_recording": {"smd-machine-1-6": 0.097, "smd-machine-2-2": 0.097},
    })


def _baseline():
    return _measurement("baseline", recall_at_budget=0.297,
                        split={"smd-machine-1-6": 0.4909, "smd-machine-2-2": 0.0374})


def _withheld_file(root, number=1, *, created_days_ago=30.0, checks=None, source="SMD",
                   name="host-features-3"):
    import json

    folder = root / "var" / "evolve" / "withheld"
    folder.mkdir(parents=True, exist_ok=True)
    payload = Withheld(
        number=number,
        round="round-20261118T024405Z.json",
        created_at=_stamp(created_days_ago),
        stream={"source": source} if source else {},
        candidate={"name": name},
        changes=[["digest", "host_features", "cpu_r,load_1,mem_free"]],
        bar=0.05,
        loss=dict(LOSS),
        measured=_measurement(name).to_dict(),
        reason="not a replicated gain",
        checks=list(checks or []),
    ).to_dict()
    path = folder / ("w%03d.json" % number)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _check(days_ago, verdict="still", *, reason="", loss=None):
    return {
        "checked_at": _stamp(days_ago),
        "verdict": verdict,
        "reason": reason or "recorded",
        "loss": dict(LOSS if loss is None else loss),
        "bar": 0.05,
        "round": "verify",
    }


def test_the_age_is_the_age_of_the_latest_evidence_not_of_the_refusal(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=30.0, checks=[_check(1.0)])
    record = withheld_records(tmp_path)[0]
    assert withheld_evidence_age(record, NOW) == pytest.approx(1.0, abs=0.01)


def test_a_refusal_nobody_measured_again_is_due(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=30.0)
    due = withheld_due(withheld_records(tmp_path), NOW, REFUSAL_EXPIRY_DAYS)
    assert [record.label for record in due] == ["w001"]
    # A horizon longer than the record's age leaves nothing due: the number is a horizon, not a trigger.
    assert withheld_due(withheld_records(tmp_path), NOW, 60.0) == []


def test_a_recent_check_makes_it_not_due(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=30.0, checks=[_check(2.0)])
    assert withheld_due(withheld_records(tmp_path), NOW, REFUSAL_EXPIRY_DAYS) == []


def test_an_objection_that_already_fell_is_not_due_again(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=400.0, checks=[_check(200.0, "no longer")])
    # Re-testing a retired objection would keep asking a question the record already answered; what remains
    # is a candidate for a round to propose, not a refusal to check.
    assert withheld_due(withheld_records(tmp_path), NOW, REFUSAL_EXPIRY_DAYS) == []


def test_a_withdrawn_check_makes_the_refusal_due_again(tmp_path):
    # Round 44's first live run cleared `host-features-3` at recall 0.146 because it measured on the stream's
    # own settings while the objection had been raised against the deployment in force. A check that compared
    # the wrong configuration is withdrawn, not deleted: the record keeps it, and the refusal is due again.
    _withheld_file(tmp_path, 1, created_days_ago=400.0, checks=[_check(200.0, "withdrawn")])
    assert [record.label for record in withheld_due(withheld_records(tmp_path), NOW,
                                                    REFUSAL_EXPIRY_DAYS)] == ["w001"]


def test_a_record_whose_stamp_cannot_be_read_is_not_due(tmp_path):
    path = _withheld_file(tmp_path, 1, created_days_ago=30.0)
    payload = path.read_text(encoding="utf-8").replace(_stamp(30.0), "")
    path.write_text(payload, encoding="utf-8")
    assert withheld_evidence_age(withheld_records(tmp_path)[0], NOW) is None
    assert withheld_due(withheld_records(tmp_path), NOW, 1.0) == []


def test_a_check_that_cannot_see_each_recording_says_unknown():
    # The measurement pools the recordings, so it cannot test a claim about one of them. Writing "no longer"
    # here would clear a refusal on evidence nobody took, which is the defect round 43 found in the write-back.
    pooled = _measurement("host-features-3")
    pooled.recall_by_recording = {}
    check = withheld_check(_baseline(), pooled, EvolveRules(), checked_at=_stamp(0), stream=STREAM)
    assert check["verdict"] == "unknown"
    assert "does not say how each recording did" in check["reason"]


def test_a_check_that_still_loses_says_still():
    check = withheld_check(_baseline(), _measurement("host-features-3"), EvolveRules(),
                           checked_at=_stamp(0), stream=STREAM)
    assert check["verdict"] == "still"
    assert "smd-machine-1-6 falls 0.491 -> 0.382" in check["reason"]
    assert check["loss"]["drop"] == pytest.approx(0.1091, abs=1e-3)


def test_a_check_with_no_recording_falling_retires_the_objection():
    unchanged = _measurement("host-features-3",
                             split={"smd-machine-1-6": 0.4909, "smd-machine-2-2": 0.0374})
    check = withheld_check(_baseline(), unchanged, EvolveRules(), checked_at=_stamp(0), stream=STREAM)
    assert check["verdict"] == "no longer"
    assert "no recording fell by more than 0.050" in check["reason"]


def test_the_campaign_says_how_old_the_evidence_is(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=30.0)
    _withheld_file(tmp_path, 2, created_days_ago=2.0, name="events-8")
    result = campaign(tmp_path, now=NOW)
    assert result.withheld == 2
    assert result.withheld_due == 1
    assert result.withheld_oldest_days == pytest.approx(30.0, abs=0.01)
    assert result.to_dict()["withheld_due_for_check"] == 1
    assert result.to_dict()["withheld_oldest_evidence_days"] == pytest.approx(30.0, abs=0.01)
    text = format_campaign(result, rules=EvolveRules())
    assert "1 due for a re-check at the 14-day horizon" in text
    assert "(oldest evidence 30 day(s) old)" in text
    row = next(row for row in result.rows if row.domain == "SMD")
    assert row.withheld_due == 1
    assert "  withheld          2 candidate(s) refused for replication, 1 due for a re-check" in text


def test_a_campaign_with_recent_evidence_does_not_claim_a_debt(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=1.0)
    result = campaign(tmp_path, now=NOW)
    assert result.withheld_due == 0
    text = format_campaign(result, rules=EvolveRules())
    assert "due for a re-check" not in text
    assert "1 candidate(s) were measured and refused for replication (oldest evidence 1 day(s) old)" in text


def test_the_horizon_the_caller_passes_is_the_one_reported(tmp_path):
    _withheld_file(tmp_path, 1, created_days_ago=30.0)
    result = campaign(tmp_path, now=NOW, refusal_expiry_days=45.0)
    assert result.withheld_due == 0
    assert "45-day horizon" not in format_campaign(result, rules=EvolveRules())
