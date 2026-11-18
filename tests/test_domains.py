"""Round 22: a deployment state per stream, and a proposer that will not measure its own baseline."""
from __future__ import annotations

import json

from brian import propose as propose_module
from brian.config import load_settings
from brian.evolve import Candidate
from brian.promote import (
    apply_promotion,
    current_state,
    domain_slug,
    in_force_overrides,
    legacy_state_note,
    load_promotions,
    promotions_for_domain,
    state_path,
)
from brian.propose import Mode, Proposal, describe_proposal, format_proposal, propose


def _write_version(directory, number: int, env: str, source: str = "SMD"):
    path = directory / "var" / "evolve" / "versions" / ("v%03d.json" % number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": number,
        "created_at": "2026-11-17T20:00:00+00:00",
        "candidate": {"name": "c%d" % number, "hypothesis": "h",
                      "changes": [["digest", "scoring", "deviation"]], "veto": None},
        "change": "digest.scoring = 'deviation'",
        "env": env,
        "measurement": {},
        "stream": {"source": source},
        "rollback": "unset X",
    }, indent=2) + "\n", encoding="utf-8")
    return path


def test_a_domain_slug_is_something_a_path_can_hold():
    assert domain_slug("RCAEval RE1-OB") == "rcaeval-re1-ob"
    assert domain_slug("") == "default"
    assert "/" not in domain_slug("a/b c")
    assert domain_slug("SMD") == "smd"


def test_two_domains_do_not_share_a_deployment_state(tmp_path):
    # Round 21 showed the same global file handing an RCAEval bar of 0.87 to SMD, where every answer
    # sits near 0.33: the setting was in force somewhere it had never been measured.
    _write_version(tmp_path, 1, "BRIAN_GATE_ANOMALY_P=0.87", source="RCAEval RE1-OB")
    _write_version(tmp_path, 2, "BRIAN_GATE_RANK_P=0.85", source="SMD")
    rcaeval, smd = load_promotions(tmp_path)

    apply_promotion(tmp_path, rcaeval, domain="RCAEval RE1-OB")
    apply_promotion(tmp_path, smd, domain="SMD")

    assert current_state(tmp_path, "RCAEval RE1-OB")["env"] == {"BRIAN_GATE_ANOMALY_P": "0.87"}
    assert current_state(tmp_path, "SMD")["env"] == {"BRIAN_GATE_RANK_P": "0.85"}
    assert current_state(tmp_path, "some-other-domain")["env"] == {}


def test_a_domain_state_file_says_which_domain_it_is(tmp_path):
    _write_version(tmp_path, 1, "BRIAN_GATE_RANK_P=0.85", source="SMD")
    promotion = load_promotions(tmp_path)[0]

    apply_promotion(tmp_path, promotion, domain="SMD")

    path = state_path(tmp_path, "SMD")
    assert path == tmp_path / "var" / "evolve" / "domains" / "smd" / "current.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["domain"] == "smd"
    assert payload["source"] == "SMD"


def test_promotions_are_filtered_by_the_stream_they_were_measured_on(tmp_path):
    _write_version(tmp_path, 1, "BRIAN_GATE_ANOMALY_P=0.87", source="RCAEval RE1-OB")
    _write_version(tmp_path, 2, "BRIAN_GATE_RANK_P=0.85", source="SMD")

    names = [promotion.version for promotion in promotions_for_domain(load_promotions(tmp_path), "SMD")]

    assert names == [2]


def test_the_legacy_global_state_is_named_rather_than_adopted(tmp_path):
    _write_version(tmp_path, 1, "BRIAN_GATE_ANOMALY_P=0.87", source="RCAEval RE1-OB")
    apply_promotion(tmp_path, load_promotions(tmp_path)[0])  # the pre-round-22 global file

    note = legacy_state_note(tmp_path, "SMD")

    assert note is not None
    assert "v001" in note
    assert "RCAEval RE1-OB" in note
    assert "not adopted" in note
    assert current_state(tmp_path, "SMD")["env"] == {}


def test_a_domain_with_its_own_state_has_no_note(tmp_path):
    _write_version(tmp_path, 1, "BRIAN_GATE_RANK_P=0.85", source="SMD")
    apply_promotion(tmp_path, load_promotions(tmp_path)[0], domain="SMD")

    assert legacy_state_note(tmp_path, "SMD") is None


def test_the_round_reads_a_domain_state_rather_than_the_global_one(tmp_path):
    _write_version(tmp_path, 1, "BRIAN_GATE_ANOMALY_P=0.87", source="RCAEval RE1-OB")
    _write_version(tmp_path, 2, "BRIAN_GATE_RANK_P=0.85", source="SMD")
    rcaeval, smd = load_promotions(tmp_path)
    apply_promotion(tmp_path, rcaeval, domain="RCAEval RE1-OB")
    apply_promotion(tmp_path, smd, domain="SMD")

    overrides, sources, refused, unknown = in_force_overrides(tmp_path, load_settings(), domain="SMD")

    assert overrides == {"gate": {"rank_probability": 0.85}}
    assert sources == {"BRIAN_GATE_RANK_P": "2"}
    assert (refused, unknown) == ([], [])


def _candidate(name: str, *, scoring: str = "auto", window: int = 100, bar: float = 0.85):
    return Candidate(name=name, hypothesis="h", section="digest", field="scoring", value=scoring,
                     also=(("digest", "rolling_window", window), ("gate", "rank_probability", bar)))


def test_a_neighbour_that_is_already_the_deployment_is_not_proposed():
    # Round 22 found `brian evolve --propose` listing the configuration in force as a candidate: the
    # ledger says its values are measured points, and measuring a point again spends a round learning
    # that a configuration is not better than itself.
    deployed = {"digest": {"scoring": "auto", "rolling_window": 100}, "gate": {"rank_probability": 0.85}}
    identical = _candidate("same", scoring="auto", window=100, bar=0.85)
    moved = _candidate("moved", scoring="auto", window=50, bar=0.85)

    assert propose_module._is_deployed(identical, deployed) is True
    assert propose_module._is_deployed(moved, deployed) is False


def test_a_partial_match_is_still_a_new_configuration():
    deployed = {"digest": {"scoring": "auto", "rolling_window": 100}, "gate": {"rank_probability": 0.85}}
    partial = Candidate(name="p", hypothesis="h", section="gate", field="rank_probability", value=0.85)

    assert propose_module._is_deployed(partial, deployed) is True

    other = Candidate(name="q", hypothesis="h", section="gate", field="rank_probability", value=0.8)
    assert propose_module._is_deployed(other, deployed) is False


def test_nothing_is_dropped_when_no_deployment_is_known():
    assert propose_module._is_deployed(_candidate("x"), None) is False
    assert propose_module._is_deployed(_candidate("x"), {}) is False


def test_the_count_of_dropped_candidates_is_recorded_and_printed():
    proposal = Proposal("expand", "because", [], None, "SMD", deployed=2)

    assert proposal.to_dict()["deployed"] == 2
    assert any("2 neighbour(s) dropped" in line for line in describe_proposal(proposal))
    assert "2 neighbour(s) were dropped" in format_proposal(proposal)


def test_a_catalogue_entry_that_is_the_deployment_is_not_measured_again():
    # Once `reuse-300` has been accepted it is both a hand-written hypothesis and the configuration in
    # force: measuring it again would compare the baseline with itself.
    entry = Candidate(name="reuse-300", hypothesis="h", section="gate", field="reuse_report_within_s",
                      value=300)
    other = Candidate(name="events-8", hypothesis="h", section="llm", field="max_context_events", value=8)
    deployed = {"gate": {"reuse_report_within_s": 300}}

    proposal = propose([], entries=[entry, other], mode=Mode("catalogue", "forced by the test"),
                       source="SMD", deployed=deployed)

    assert [candidate.name for candidate in proposal.candidates] == ["events-8"]
    assert proposal.deployed == 1


def test_a_catalogue_that_is_entirely_in_force_says_the_round_has_nothing_to_measure():
    entry = Candidate(name="reuse-300", hypothesis="h", section="gate", field="reuse_report_within_s",
                      value=300)
    deployed = {"gate": {"reuse_report_within_s": 300}}

    proposal = propose([], entries=[entry], mode=Mode("catalogue", "forced by the test"),
                       source="SMD", deployed=deployed)

    assert proposal.candidates == []
    assert proposal.deployed == 1
    assert "already in force" in proposal.reason
    assert "nothing to measure" in proposal.reason
