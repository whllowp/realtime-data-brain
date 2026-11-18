# -*- coding: utf-8 -*-
"""Round 44 tests: a verified version does not join one already in force.

`--verify` walks whatever is due and applied each version it verified. An unscoped run over the SMD ledger
therefore verified `v004` while `v020` was in force and merged both into the deployment state — and the
deployment page printed its own warning: "the variables in force come from 2 versions … their sum is a
configuration nobody measured". Each version was measured as a patch against the same baseline, so two
verified patches answer two questions; the combination is a third configuration, and nothing measured it.
"""
import json
from pathlib import Path

from brian.promote import Promotion, Claim, current_state, stacking_note


def _promotion(version=4):
    return Promotion(
        version=version,
        path=Path("var/evolve/versions/v%03d.json" % version),
        candidate={"name": "events-4", "hypothesis": "h", "changes": [["llm", "max_context_events", 4]]},
        change="llm.max_context_events = 4",
        env={"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"},
        claim=Claim(recall_at_budget=0.060),
        created_at="2026-11-18T02:00:00+00:00",
        stream={"source": "SMD"},
    )


def _state(tmp_path, *, env, sources):
    directory = tmp_path / "var" / "evolve" / "domains" / "smd"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "current.json").write_text(json.dumps({
        "domain": "smd", "source": "SMD", "env": dict(env), "sources": dict(sources),
        "updated_at": "2026-11-18T03:31:33+00:00",
    }), encoding="utf-8")


def test_nothing_in_force_means_nothing_to_stack(tmp_path):
    _state(tmp_path, env={}, sources={})
    assert stacking_note(tmp_path, _promotion(), "SMD") is None


def test_a_second_version_is_named_as_already_in_force(tmp_path):
    _state(tmp_path,
           env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
           sources={"BRIAN_DIGEST_ROLLING_WINDOW": "20"})
    note = stacking_note(tmp_path, _promotion(4), "SMD")
    assert note is not None
    assert "v20" in note and "already in force for smd" in note
    assert "the sum of two patches was never measured" in note


def test_a_version_already_in_force_is_not_its_own_obstacle(tmp_path):
    # Re-verifying the version that owns the state must still apply (it is the same configuration), otherwise
    # a re-check would silently stop maintaining the deployment it is checking.
    _state(tmp_path,
           env={"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"},
           sources={"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"})
    assert stacking_note(tmp_path, _promotion(4), "SMD") is None


def test_the_note_does_not_touch_the_state(tmp_path):
    _state(tmp_path,
           env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"},
           sources={"BRIAN_DIGEST_ROLLING_WINDOW": "20"})
    stacking_note(tmp_path, _promotion(4), "SMD")
    state = current_state(tmp_path, "SMD")
    assert state["sources"] == {"BRIAN_DIGEST_ROLLING_WINDOW": "20"}
    assert state["env"] == {"BRIAN_DIGEST_ROLLING_WINDOW": "200"}
