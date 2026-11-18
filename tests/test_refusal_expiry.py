"""Round 43: a refusal that is skipped is never tested again, so it must expire.

A `skip` refusal stops the mechanism from proposing the candidate (round 37 moved that filter into the
generator), which means the candidate is never measured, which means the refusal can never be falsified:
the verdict is self-sealing. These tests pin the two halves of the cure — a stale refusal is re-opened by
the plan, and a re-measurement is written back into the record that refused the candidate.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brian.evolve import (
    EVOLVE_DIR,
    REFUSAL_EXPIRY_DAYS,
    Candidate,
    EvolveRules,
    Measurement,
    RoundResult,
    Verdict,
    Withheld,
    _refusal_age_days,
    _write_withheld,
    format_withheld,
    record_withheld_check,
    record_withheld_checks,
    refusal_plan,
    rejection_provenance,
    withheld_records,
    withheld_staleness,
)

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
STREAM = {"source": "SMD", "windows": 1355, "scored": 1235, "cadence_s": 2100.0}
CANDIDATE = {"name": "host-features-3", "hypothesis": "three features",
             "changes": [["digest", "host_features", ["cpu_r", "load_1", "mem_free"]]]}
LOSS = {"recording": "smd-machine-1-6", "before": 0.4909, "after": 0.3818, "drop": 0.1091,
        "tolerance": 0.05, "pooled_before": 0.2969, "pooled_after": 0.3125, "budget": 0.1}
REASON = ("recall at the 10% budget rises 0.297 -> 0.312 pooled, but smd-machine-1-6 falls 0.491 -> 0.382: "
          "the gain belongs to the recordings that rose, so this is not a replicated one")


def _measurement(name: str = "host-features-3", recall: float = 0.3125) -> Measurement:
    return Measurement(
        candidate=name, recall=recall, fpr=0.097, precision=0.5, reports=290,
        tokens_per_report=2908.0, tokens_per_window=100.0, latency_p95_ms=5.0, coverage={"log": 1.0},
        confusion={"tp": 100, "fp": 50, "tn": 500, "fn": 50},
        recall_at_budget=recall, fpr_at_budget=0.097, threshold_at_budget=0.9, budget=0.1,
        thresholds_at_budget={"smd-machine-1-6": 0.9, "smd-machine-2-2": 0.9},
        recall_by_recording={"smd-machine-1-6": 0.3818, "smd-machine-2-2": 0.220},
        fpr_by_recording={"smd-machine-1-6": 0.08, "smd-machine-2-2": 0.09},
    )


def _baseline() -> Measurement:
    baseline = _measurement("baseline", 0.2969)
    baseline.recall_by_recording = {"smd-machine-1-6": 0.4909, "smd-machine-2-2": 0.037}
    return baseline


def _verdict(detail: dict = LOSS, reason: str = REASON) -> Verdict:
    return Verdict(candidate="host-features-3", accepted=False, rule="replicate", reason=reason,
                   detail=dict(detail))


def _round(when: str = "2026-11-18T02:40:00+00:00") -> RoundResult:
    return RoundResult(
        started_at=when, finished_at=when, stream=dict(STREAM), rules=EvolveRules(),
        baseline=_baseline(), trials=[(Candidate.from_dict(CANDIDATE), _measurement(), _verdict())],
        ceiling=None, proposal={}, references={}, claim={})


def _withheld_file(root: Path, *, number: int = 1, when: str = "2026-11-18T02:40:00+00:00",
                   checks: list | None = None, round_name: str = "round-20261118T024000Z.json") -> Path:
    folder = root / EVOLVE_DIR / "withheld"
    folder.mkdir(parents=True, exist_ok=True)
    payload = Withheld(
        number=number, round=round_name, created_at=when, stream=dict(STREAM),
        candidate=dict(CANDIDATE), changes=[list(change) for change in CANDIDATE["changes"]],
        bar=0.05, loss=dict(LOSS), measured=_measurement().to_dict(), reason=REASON, rule="replicate",
        checks=list(checks or []),
    ).to_dict()
    path = folder / ("w%03d.json" % number)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _round_file(root: Path, name: str, *, when: str, accepted: bool = False,
                candidate: dict | None = None, reason: str = "no gain") -> str:
    """Write the shape `rejection_provenance` reads: a round file plus its ledger line."""
    directory = root / EVOLVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    candidate = dict(candidate or CANDIDATE)
    record = {
        "started_at": when,
        "finished_at": when,
        "stream": dict(STREAM),
        "rules": EvolveRules().to_dict(),
        "baseline": _baseline().to_dict(),
        "trials": [{
            "candidate": candidate,
            "measurement": _measurement(candidate["name"]).to_dict(),
            "verdict": {"candidate": candidate["name"], "accepted": accepted, "rule": "gain",
                        "reason": reason, "detail": {}},
        }],
        "ceiling": None,
        "proposal": {},
    }
    (directory / name).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"round": name, "started_at": when, "finished_at": when,
                                 "accepted": [], "rejected": [candidate["name"]], "applied": [],
                                 "rules_hash": "abc", "baseline_tokens_per_report": 3356.0}) + "\n")
    return name


def test_a_refusal_carries_the_moment_it_was_made(tmp_path: Path) -> None:
    # Without the timestamp the plan cannot tell a refusal measured this morning from one measured before
    # the deployment changed, and every refusal would have to be skipped forever.
    _round_file(tmp_path, "round-a.json", when="2026-11-18T02:40:00+00:00")
    provenance = rejection_provenance(tmp_path)
    assert provenance["host-features-3"]["when"] == "2026-11-18T02:40:00+00:00"


def test_a_fresh_refusal_is_skipped_and_an_old_one_is_measured_again(tmp_path: Path) -> None:
    _round_file(tmp_path, "round-a.json", when="2026-11-18T02:40:00+00:00")
    provenance = rejection_provenance(tmp_path)
    fresh = refusal_plan(provenance, source="SMD", shape=STREAM, now=NOW)
    assert fresh["skip"] == ["host-features-3"]
    assert fresh["expired"] == []
    old = refusal_plan(provenance, source="SMD", shape=STREAM,
                       now=NOW + timedelta(days=REFUSAL_EXPIRY_DAYS + 1))
    assert old["skip"] == []
    assert old["expired"] == ["host-features-3"]


def test_no_refusal_is_skipped_when_its_evidence_has_aged_out(tmp_path: Path) -> None:
    _round_file(tmp_path, "round-a.json", when="2026-11-18T02:40:00+00:00")
    plan = refusal_plan(rejection_provenance(tmp_path), source="SMD", shape=STREAM, now=NOW,
                        expiry_days=0.0)
    assert plan["expired"] == ["host-features-3"]
    # The point of the group: an expired refusal is not handed to `propose` as refused, so the mechanism
    # proposes the candidate and measures it again.
    assert plan["skip"] == []


def test_the_age_of_a_refusal_is_read_like_a_claim_s_age() -> None:
    assert _refusal_age_days("2026-11-18T02:40:00+00:00", NOW) == pytest.approx((7 + 20 / 60) / 24)
    # A stamp without a zone is UTC, not "now": guessing a date is how a stale check starts looking fresh.
    assert _refusal_age_days("2026-11-16T10:00:00", NOW) == 2.0
    assert _refusal_age_days("", NOW) is None
    assert _refusal_age_days("not a stamp", NOW) is None


def test_a_re_measurement_is_written_back_once(tmp_path: Path) -> None:
    _withheld_file(tmp_path)
    check = {"checked_at": "2026-11-18T02:51:00+00:00", "stream": dict(STREAM), "bar": 0.05,
             "round": "round-b.json", "verdict": "still", "loss": dict(LOSS), "reason": REASON,
             "recall_at_budget": 0.3125, "tokens_per_report": 2908.0}
    assert record_withheld_check(tmp_path, "host-features-3", check) is not None
    record = withheld_records(tmp_path)[0]
    assert record.checks == [check]
    # The same measurement again is the same fact: it must not pile up (round 28's "4 earlier checks").
    assert record_withheld_check(tmp_path, "host-features-3", check) is None
    assert len(withheld_records(tmp_path)[0].checks) == 1


def test_a_different_verdict_is_kept_as_its_own_check(tmp_path: Path) -> None:
    _withheld_file(tmp_path)
    still = {"checked_at": "2026-11-18T02:51:00+00:00", "verdict": "still", "loss": dict(LOSS),
             "reason": REASON}
    gone = {"checked_at": "2026-11-19T02:51:00+00:00", "verdict": "no longer", "loss": {},
            "reason": "no recording fell by more than 0.050"}
    record_withheld_check(tmp_path, "host-features-3", still)
    record_withheld_check(tmp_path, "host-features-3", gone)
    assert [check["verdict"] for check in withheld_records(tmp_path)[0].checks] == ["still", "no longer"]


def test_the_round_that_created_the_record_does_not_count_as_its_re_check(tmp_path: Path) -> None:
    _withheld_file(tmp_path)
    own = "round-20261118T024000Z.json"
    written = record_withheld_checks(tmp_path, _round(), EvolveRules(), skip_round=own)
    # The round named in the record is its own measurement, not a later one.
    assert written == []
    later = record_withheld_checks(tmp_path, _round("2026-11-18T03:10:00+00:00"), EvolveRules(),
                                   skip_round="round-20261118T031000Z.json")
    assert [item["verdict"] for item in later] == ["still"]
    assert later[0]["label"] == "w001"


def test_a_second_measurement_does_not_mint_a_second_record(tmp_path: Path) -> None:
    _withheld_file(tmp_path)
    written = _write_withheld(tmp_path, tmp_path / EVOLVE_DIR, "round-20261118T025200Z.json", _round())
    assert written == []
    assert [record.label for record in withheld_records(tmp_path)] == ["w001"]


def test_the_record_judges_itself_by_its_latest_measurement(tmp_path: Path) -> None:
    gone = {"checked_at": "2026-11-19T02:51:00+00:00", "verdict": "no longer", "loss": {},
            "reason": "no recording fell by more than 0.050 at the 10% budget"}
    record = Withheld.from_dict(json.loads(_withheld_file(tmp_path, checks=[gone]).read_text()))
    state = withheld_staleness(record, EvolveRules())
    assert state["still"] is False
    assert "re-measured 2026-11-19" in state["note"]
    assert "no recording fell" in state["note"]


def test_a_re_measurement_that_still_loses_keeps_the_refusal_standing(tmp_path: Path) -> None:
    again = {"checked_at": "2026-11-19T02:51:00+00:00", "verdict": "still", "loss": dict(LOSS),
             "reason": REASON}
    record = Withheld.from_dict(json.loads(_withheld_file(tmp_path, checks=[again]).read_text()))
    state = withheld_staleness(record, EvolveRules())
    assert state["still"] is True
    assert "re-measured 2026-11-19" in state["note"]


def test_the_page_says_how_many_refusals_were_measured_again(tmp_path: Path) -> None:
    check = {"checked_at": "2026-11-19T02:51:00+00:00", "verdict": "still", "loss": dict(LOSS),
             "reason": REASON}
    _withheld_file(tmp_path, number=1, checks=[check])
    _withheld_file(tmp_path, number=2, round_name="round-20261118T030000Z.json")
    text = format_withheld(withheld_records(tmp_path), EvolveRules())
    assert "re-measured       1 of them have been measured again" in text
    # The unmeasured one keeps the plain arithmetic sentence, and does not borrow the other's credibility.
    assert "the loss was 0.109 against a bar of 0.050" in text


def test_a_record_without_checks_reads_as_before(tmp_path: Path) -> None:
    record = Withheld.from_dict(json.loads(_withheld_file(tmp_path).read_text()))
    assert record.checks == []
    assert record.last_check is None
    state = withheld_staleness(record, EvolveRules())
    assert state == {"still": True, "comparable": True,
                     "note": "the loss was 0.109 against a bar of 0.050; today's bar is 0.050, "
                             "so it still holds"}
