# -*- coding: utf-8 -*-
"""Round 32 tests: a record must survive being read back, and a replay must use the reference the round used.

Round 31 made `run_round` measure a candidate against a baseline at the candidate's own window. The replay
paths (`rejudge`, `audit`) still read only `baseline`, so re-deciding a window change compared 286 against
200 and reported "3 verdict(s) changed" on a round whose rules had not moved at all. The measurement now
carries the window it was taken with, the replay picks the reference the round stored, and a comparison that
cannot be reconstructed is withheld instead of invented.
"""
import json
from dataclasses import fields

from brian.evolve import (EvolveRules, Measurement, audit, decide, format_audit, format_rejudge,
                          rejudge)

RULES = EvolveRules()

CANDIDATE = {
    "name": "host-rolling-rank-0-85-200@hint286",
    "hypothesis": "the same configuration at the window that covers the span its claim was made over",
    "changes": [["digest", "rolling_window", 286], ["digest", "scoring", "auto"]],
    "veto": None,
}


def _measurement(name, *, window=None, recall_at_budget=0.5, recall=None, fpr=0.08, tokens=100.0,
                 **overrides):
    payload = {
        "candidate": name,
        "recall": recall_at_budget if recall is None else recall,
        "fpr": fpr,
        "precision": 0.7,
        "reports": 200,
        "tokens_per_report": tokens,
        "tokens_per_window": 40.0,
        "windows": 1235,
        "recall_at_budget": recall_at_budget,
        "fpr_at_budget": fpr,
        "budget": 0.10,
        "window": window,
    }
    payload.update(overrides)
    return Measurement.from_dict(payload)


def _baseline(window=200, recall_at_budget=0.297):
    return _measurement("baseline", window=window, recall_at_budget=recall_at_budget, candidate="baseline")


def _trial(window=286, recall_at_budget=0.427):
    return _measurement(CANDIDATE["name"], window=window, recall_at_budget=recall_at_budget)


def _record(path, *, with_reference=True, trial=None, baseline=None, verdict=None):
    """Write a round file the way `run_round` does, optionally without the reference it compared against."""
    reference = _trial() if with_reference else None
    payload = {
        "round": path.name,
        "started_at": "2026-11-18T01:00:00+00:00",
        "finished_at": "2026-11-18T01:01:00+00:00",
        "stream": {"source": "SMD", "windows": 1355, "scored": 1235},
        "rules": RULES.to_dict(),
        "baseline": (baseline or _baseline()).to_dict(),
        "trials": [{
            "candidate": dict(CANDIDATE),
            "measurement": (trial or _trial()).to_dict(),
            "verdict": dict(verdict or {"candidate": CANDIDATE["name"], "accepted": False,
                                        "rule": "gain", "reason": "recall at the budget did not rise"}),
        }],
        "references": {"286": reference.to_dict()} if reference is not None else {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_measurement_survives_its_own_record():
    original = _measurement("trial", window=286, recall_at_budget=0.427, prevalence=0.15,
                            alert_budget=0.10, alerts_at_free_cut=123, scored_at_free_cut=1235,
                            recall_at_free_cut=0.269, fpr_at_free_cut=0.066, promise=0.507)

    restored = Measurement.from_dict(original.to_dict())

    # Compared as documents, not as objects: the point is that nothing the round wrote is dropped on the way
    # back in. Round 32 found four fields (`alert_budget`, `prevalence`, `alerts_at_free_cut`,
    # `scored_at_free_cut`) that were written and never read, so a re-decided record lost its label-free cap.
    assert restored.to_dict() == original.to_dict()
    assert restored.label_free_ceiling == original.label_free_ceiling


def test_every_measurement_field_is_written_out():
    written = set(_baseline().to_dict())

    missing = [field.name for field in fields(Measurement) if field.name not in written]

    assert missing == [], "a field the record cannot carry is a field the replay cannot use"


def test_the_geometry_is_restored_and_a_legacy_record_has_none():
    assert Measurement.from_dict(_trial().to_dict()).window == 286

    # Rounds before round 32 do not carry the field at all; inventing a window there would make old records
    # look like they were compared across geometries.
    payload = _trial().to_dict()
    payload.pop("window")
    assert Measurement.from_dict(payload).window is None


def test_the_replay_uses_the_reference_the_round_used(tmp_path):
    path = _record(tmp_path / "round-20261118T010000Z.json")

    result = rejudge(path, RULES)

    assert result.withheld == []
    candidate, measurement, old, new = result.rows[0]
    # The round compared 286 against 286 and refused; replaying it against the 200 baseline would accept the
    # same trial for 0.297 -> 0.427, which is the geometry being credited to the candidate.
    assert old.accepted is False and new.accepted is False
    assert new.rule == "gain"
    assert result.changed == []


def test_ignoring_the_reference_is_what_flipped_the_verdict():
    # The mechanism, stated as two calls: the same trial is a win against the other geometry and a tie against
    # its own. This is why the replay has to ask the record which one the round used.
    assert decide(_baseline(), _trial(), RULES).accepted is True
    assert decide(_trial(), _trial(), RULES).accepted is False


def test_a_trial_without_a_reference_is_not_re_decided(tmp_path):
    path = _record(tmp_path / "round-20261118T010000Z.json", with_reference=False)

    result = rejudge(path, RULES)

    assert result.rows == []
    assert result.withheld == [CANDIDATE["name"]]
    text = format_rejudge(result)
    assert "were not re-decided" in text
    assert "boundary 71" in text
    # Withholding is not "no change": the text must not claim the rules agree with the round.
    assert "no verdict changed" not in text


def test_the_audit_counts_what_it_cannot_reconstruct(tmp_path):
    _record(tmp_path / "var" / "evolve" / "round-20261118T010000Z.json", with_reference=False)
    (tmp_path / "var" / "evolve" / "ledger.jsonl").write_text(
        json.dumps({"round": "round-20261118T010000Z.json", "stream": {"source": "SMD"}}) + "\n",
        encoding="utf-8")

    result = audit(tmp_path, RULES)

    assert result.cross == 1
    assert result.moved == 0
    text = format_audit(result)
    assert "could not be reconstructed" in text
    assert "not reconstructable" in text


def test_a_reference_backed_round_audits_clean(tmp_path):
    _record(tmp_path / "var" / "evolve" / "round-20261118T010000Z.json")
    (tmp_path / "var" / "evolve" / "ledger.jsonl").write_text(
        json.dumps({"round": "round-20261118T010000Z.json", "stream": {"source": "SMD"}}) + "\n",
        encoding="utf-8")

    result = audit(tmp_path, RULES)

    assert result.cross == 0
    assert result.moved == 0
    assert result.entries == []


def test_a_reference_without_a_labelled_trial_is_still_withheld(tmp_path):
    # The shape round 31 actually wrote: the round stored references, but the measurements do not say
    # which one each trial was compared with. Assuming the baseline puts the geometry back into the
    # comparison, and rejudging the round-31 record that way still reports three "changed" verdicts.
    path = _record(tmp_path / "round-20261118T010000Z.json",
                   trial=_measurement(CANDIDATE["name"], window=None, recall_at_budget=0.427))

    result = rejudge(path, RULES)

    assert result.rows == []
    assert result.withheld == [CANDIDATE["name"]]


def test_a_legacy_record_is_still_audited(tmp_path):
    # No window on either side: the record is replayed against its baseline, which is how it was decided.
    # Gating old rounds on a field they could not have carried would throw away every verdict so far.
    path = _record(tmp_path / "round-20261118T010000Z.json", with_reference=False,
                   trial=_measurement(CANDIDATE["name"], recall_at_budget=0.427),
                   baseline=_measurement("baseline", recall_at_budget=0.297, candidate="baseline"))
    trial = Measurement.from_dict(json.loads(path.read_text(encoding="utf-8"))["trials"][0]["measurement"])
    assert trial.window is None

    result = rejudge(path, RULES)

    assert result.withheld == []
    assert [row[2].accepted for row in result.rows] == [False]
