"""Promotion: a version file's promise, the state it puts in force, and the revert when it stops paying.

The unit under test is deliberately small. `brian evolve` decides; `promote` is the part that can fail
*after* the decision, because it is the part that is exposed to data that did not exist yet. What is worth
asserting: the promise is read from the file rather than restated, a revert removes exactly one version's
variables, and a claim whose numbers cannot be compared is reported as unmeasurable rather than as a pass.
"""
from __future__ import annotations

import json

import pytest

from brian.evolve import Candidate, EvolveRules, Measurement, RoundResult, Verdict
from brian.promote import (
    FAILED,
    UNMEASURABLE,
    VERIFIED,
    Claim,
    apply_promotion,
    claim_check,
    combined_versions,
    current_state,
    format_promotions,
    load_promotions,
    revert_promotion,
    write_verification,
)


def measurement(*, recall: float | None = 0.224, fpr: float | None = 0.091,
                budget_recall: float | None = 0.224, budget_fpr: float | None = 0.091,
                tokens: float | None = 3356.0) -> Measurement:
    return Measurement(
        candidate="host-rolling-rank",
        recall=recall,
        fpr=fpr,
        precision=0.27,
        reports=138,
        tokens_per_report=tokens,
        tokens_per_window=488.5,
        latency_p95_ms=1.0,
        recall_at_budget=budget_recall,
        fpr_at_budget=budget_fpr,
    )


def round_result(*, tokens: float = 3356.0) -> RoundResult:
    candidate = Candidate(name="host-rolling-rank", hypothesis="the level drifts, the rank does not",
                          section="digest", field="scoring", value="deviation",
                          also=(("digest", "rolling_window", 100),))
    trial = measurement(tokens=tokens)
    return RoundResult(
        started_at="2026-11-18T00:00:00+00:00",
        finished_at="2026-11-18T00:01:00+00:00",
        stream={"source": "SMD", "windows": 474, "scored": 414},
        rules=EvolveRules(),
        baseline=measurement(recall=1.0, fpr=1.0, budget_recall=0.060, budget_fpr=0.072,
                             tokens=3350.0),
        trials=[(candidate, trial, Verdict("host-rolling-rank", True, "quality", "found more"))],
    )


def seed_versions(tmp_path, count: int = 1):
    from brian.evolve import persist

    written = []
    for _index in range(count):
        written.append(persist(tmp_path, round_result()))
    return written


def test_a_claim_is_read_from_the_measurement_that_justified_the_version():
    original = measurement()

    claim = Claim.from_measurement(original)
    restored = Claim.from_dict(claim.to_dict())

    assert restored == claim
    assert claim.recall_at_budget == original.recall_at_budget
    assert claim.tokens_per_report == original.tokens_per_report


def test_a_version_file_loads_with_its_assignments_and_promise(tmp_path):
    seed_versions(tmp_path)

    promotions = load_promotions(tmp_path)

    assert len(promotions) == 1
    promotion = promotions[0]
    assert promotion.change == "digest.scoring = 'deviation', digest.rolling_window = 100"
    assert promotion.env == {"BRIAN_DIGEST_SCORING": "deviation",
                             "BRIAN_DIGEST_ROLLING_WINDOW": "100"}
    assert promotion.claim.recall_at_budget == pytest.approx(0.224)
    assert promotion.stream["source"] == "SMD"
    assert promotion.pending


def test_applying_a_promotion_puts_exactly_its_variables_in_force(tmp_path):
    seed_versions(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    apply_promotion(tmp_path, promotion)

    state = current_state(tmp_path)
    assert state["env"] == {"BRIAN_DIGEST_SCORING": "deviation",
                            "BRIAN_DIGEST_ROLLING_WINDOW": "100"}
    assert state["sources"] == {"BRIAN_DIGEST_SCORING": "1", "BRIAN_DIGEST_ROLLING_WINDOW": "1"}


def test_reverting_one_promotion_leaves_a_later_one_in_force(tmp_path):
    seed_versions(tmp_path, count=2)
    first, second = load_promotions(tmp_path)
    apply_promotion(tmp_path, first)
    apply_promotion(tmp_path, second)

    revert_promotion(tmp_path, first)

    state = current_state(tmp_path)
    # Both versions set the same two variables, so after reverting the first the second still owns them.
    assert state["sources"] == {"BRIAN_DIGEST_SCORING": "2", "BRIAN_DIGEST_ROLLING_WINDOW": "2"}
    assert state["env"]["BRIAN_DIGEST_SCORING"] == "deviation"


def test_reverting_the_last_owner_removes_the_variable_instead_of_inventing_one(tmp_path):
    seed_versions(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion)

    revert_promotion(tmp_path, promotion)

    assert current_state(tmp_path)["env"] == {}


def test_a_claim_holds_when_the_re_measurement_still_pays_out():
    claim = Claim.from_measurement(measurement())

    verdict, reason = claim_check(claim, measurement(), EvolveRules())

    assert verdict == VERIFIED, reason
    assert "0.224" in reason


def test_a_claim_fails_when_recall_drops_past_the_tolerance():
    claim = Claim.from_measurement(measurement())

    verdict, reason = claim_check(claim, measurement(budget_recall=0.150), EvolveRules())

    assert verdict == FAILED
    assert "promised recall 0.224" in reason and "0.150" in reason


def test_a_claim_tolerates_a_shortfall_inside_the_operational_tolerance():
    claim = Claim.from_measurement(measurement())

    verdict, _reason = claim_check(claim, measurement(budget_recall=0.210), EvolveRules())

    assert verdict == VERIFIED


def test_a_claim_fails_when_the_configuration_leaves_the_false_positive_budget():
    claim = Claim.from_measurement(measurement())

    verdict, reason = claim_check(claim, measurement(budget_fpr=0.30), EvolveRules())

    assert verdict == FAILED
    assert "false-positive rate 0.300" in reason


def test_a_claim_fails_when_the_bill_grows_past_the_cost_tolerance():
    claim = Claim.from_measurement(measurement())

    verdict, reason = claim_check(claim, measurement(tokens=5000.0), EvolveRules())

    assert verdict == FAILED
    assert "promised 3356 tokens per report" in reason


def test_a_claim_without_a_budget_recall_is_unmeasurable_not_a_pass():
    claim = Claim.from_measurement(measurement(budget_recall=None))

    verdict, reason = claim_check(claim, measurement(), EvolveRules())

    assert verdict == UNMEASURABLE
    assert "cannot be checked either way" in reason


def test_verifying_a_version_file_keeps_the_rollback_that_makes_it_reversible(tmp_path):
    seed_versions(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, {"verdict": VERIFIED, "reason": "still pays out"})

    document = json.loads(promotion.path.read_text(encoding="utf-8"))
    assert document["rollback"].startswith("unset BRIAN_DIGEST_SCORING")
    assert document["verification"]["verdict"] == VERIFIED
    assert load_promotions(tmp_path)[0].verification["reason"] == "still pays out"


def test_a_version_file_that_does_not_name_its_change_still_loads_but_cannot_be_measured(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True)
    (directory / "v007.json").write_text(json.dumps({
        "version": 7, "change": "hand-written", "env": "BRIAN_GATE_RANK_P=0.9",
        "measurement": {"recall_at_budget": 0.2},
    }), encoding="utf-8")

    promotions = load_promotions(tmp_path)

    assert len(promotions) == 1
    assert promotions[0].candidate is None
    assert promotions[0].env == {"BRIAN_GATE_RANK_P": "0.9"}


def test_an_unreadable_version_file_is_skipped_rather_than_guessed_at(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True)
    (directory / "v001.json").write_text("{not json", encoding="utf-8")
    seed_versions(tmp_path)

    promotions = load_promotions(tmp_path)

    # persist() skips the unreadable v001, so the round it just wrote is v002; the broken file stays on
    # disk untouched instead of being silently replaced.
    assert [item.version for item in promotions] == [2]


def test_the_table_says_what_is_in_force_and_whether_a_claim_was_rechecked(tmp_path):
    seed_versions(tmp_path)
    promotions = load_promotions(tmp_path)
    apply_promotion(tmp_path, promotions[0])
    write_verification(tmp_path, promotions[0], {"verdict": FAILED, "reason": "promised recall 0.224"})

    table = format_promotions(load_promotions(tmp_path), current_state(tmp_path))

    assert "v001" in table
    assert "recall 0.224" in table
    assert "failed: promised recall 0.224" in table
    assert "in force  BRIAN_DIGEST_ROLLING_WINDOW=100" in table
    assert "BRIAN_DEPLOY_STATE" in table
    assert "follows them between windows" in table


def _write_version(directory, number: int, env: str, change: str):
    """A version file with the two fields a merge test needs, and nothing that has to be measured."""
    import json

    path = directory / "var" / "evolve" / "versions" / ("v%03d.json" % number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": number,
        "created_at": "2026-11-17T20:00:00+00:00",
        "candidate": {"name": "c%d" % number, "hypothesis": "h",
                      "changes": [["digest", "rolling_window", 100]], "veto": None},
        "change": change,
        "env": env,
        "measurement": {},
        "stream": {"source": "SMD"},
        "rollback": "unset X",
    }, indent=2) + "\n", encoding="utf-8")
    return path


def test_the_table_says_when_the_configuration_in_force_came_from_several_patches(tmp_path):
    # Round 18 accepted four patches measured against the same baseline; applying them one after
    # another produced `rolling_window` from one version and the bars from another, which is a
    # configuration no round measured. The count is now printed rather than left to the reader.
    _write_version(tmp_path, 1, "BRIAN_DIGEST_ROLLING_WINDOW=100", "digest.rolling_window = 100")
    _write_version(tmp_path, 2, "BRIAN_GATE_RANK_P=0.85", "gate.rank_probability = 0.85")
    for promotion in load_promotions(tmp_path):
        apply_promotion(tmp_path, promotion)
    assert combined_versions(current_state(tmp_path)) == ["v001", "v002"]

    table = format_promotions(load_promotions(tmp_path), current_state(tmp_path))

    assert "come from 2 versions: v001, v002" in table
    assert "configuration nobody measured" in table


def test_one_version_in_force_is_not_a_combination(tmp_path):
    seed_versions(tmp_path)
    apply_promotion(tmp_path, load_promotions(tmp_path)[0])
    assert combined_versions(current_state(tmp_path)) == ["v001"]
    assert "come from" not in format_promotions(load_promotions(tmp_path), current_state(tmp_path))


def test_an_empty_ledger_says_there_is_nothing_to_verify(tmp_path):
    table = format_promotions([])

    assert "no version files yet" in table

def test_applying_a_promotion_reports_which_variables_it_actually_took(tmp_path):
    seed_versions(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    outcome = apply_promotion(tmp_path, promotion)

    assert outcome["applied"] == ["BRIAN_DIGEST_SCORING", "BRIAN_DIGEST_ROLLING_WINDOW"]
    assert outcome["superseded"] == []


def test_an_older_promotion_never_takes_a_variable_from_a_newer_one(tmp_path):
    seed_versions(tmp_path, count=2)
    first, second = load_promotions(tmp_path)
    apply_promotion(tmp_path, second)

    outcome = apply_promotion(tmp_path, first)

    # Verifying an old version must not be a rollback disguised as a verification.
    assert outcome["applied"] == []
    assert outcome["superseded"] == ["BRIAN_DIGEST_SCORING", "BRIAN_DIGEST_ROLLING_WINDOW"]
    assert current_state(tmp_path)["sources"] == {"BRIAN_DIGEST_SCORING": "2",
                                                  "BRIAN_DIGEST_ROLLING_WINDOW": "2"}
