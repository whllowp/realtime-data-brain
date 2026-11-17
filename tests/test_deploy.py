"""Reloading a running pipeline from the deployment state file.

These tests are the evidence for one claim and only one: a configuration change written by
``brian evolve --apply`` / ``--verify`` reaches a pipeline that is **already running**, without anybody
restarting it. That is the last human step between this repo and the D2I.ai ladder's L3, so it is
tested behaviourally — the second batch of windows is judged with the new threshold, and the decision
it produces differs — rather than by asserting that a settings object was replaced.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from brian.bus import MemoryBus
from brian.config import CalibrationSettings, DeploySettings, Settings, load_settings
from brian.contracts import Routing
from brian.demo import DEFAULT_SEQUENCE, flatten, generate
from brian.deploy import (
    RELOADABLE_FIELDS,
    DeploymentState,
    coerce,
    overrides_from_state,
    reload_capable,
)
from brian.laya.client import LayaResult
from brian.pipeline import Pipeline


class _FakeLaya:
    """Answers from a table: these tests are about reloading, not about model quality."""

    questions_hash = "sha256:fake"
    cache_stats = type("S", (), {"hits": 0, "misses": 0, "hit_rate": 0.0})()
    cache_size = 0

    def __init__(self, probability: float = 0.9) -> None:
        self.probability = probability
        self.calls = 0

    def decide(self, state: Any, questions: Any = None, model: Optional[str] = None,
               use_cache: bool = True, task: Optional[str] = None) -> LayaResult:
        self.calls += 1
        return LayaResult(
            answers={
                "is_anomaly": {"type": "noul", "noul": self.probability, "confidence": 0.9},
                "category": {"type": "choice", "choice": "resource_exhaustion",
                             "probabilities": {"resource_exhaustion": 0.9}, "confidence": 0.9},
                "severity": {"type": "score", "score": 2.0, "confidence": 0.9},
                "needs_human": {"type": "noul", "noul": 0.1, "confidence": 0.9},
            },
            routing=Routing(model="fake"),
            latency_ms=1.0,
        )


def _write_state(path, env: dict, stamp: str) -> None:
    path.write_text(json.dumps({"env": env, "sources": {name: "1" for name in env},
                                "updated_at": stamp}, indent=1), encoding="utf-8")


def _settings(state_file: str = "", **gate: Any) -> Settings:
    base = load_settings()
    return base.with_overrides(
        gate=dataclasses_replace_gate(base, **gate),
        calibration=CalibrationSettings(path=base.calibration.path, enabled=False),
        deploy=DeploySettings(state_file=state_file),
    )


def dataclasses_replace_gate(base: Settings, **values: Any):
    from dataclasses import replace

    return replace(base.gate, **values)


def _pipeline(settings: Settings, laya: Optional[_FakeLaya] = None) -> Pipeline:
    from brian.llm.rca import OfflineReasoner, RcaEngine

    return Pipeline(settings, MemoryBus(), laya=laya or _FakeLaya(),
                    rca=RcaEngine(settings.llm, offline_reasoner=OfflineReasoner()))


# --------------------------------------------------------------------------------------
# coercion: a reload must not change a value's type behind the gate's back
# --------------------------------------------------------------------------------------
def test_a_flag_is_read_as_a_flag_not_as_the_integer_zero():
    # bool is a subclass of int: without an explicit branch, "false" would become 0 and a gate that was
    # meant to stop analysing alerts would keep analysing them.
    assert coerce(True, "false") is False
    assert coerce(True, "1") is True
    assert coerce(True, "on") is True


def test_each_field_keeps_the_type_it_had():
    assert coerce(20, "8") == 8
    assert isinstance(coerce(20, "8"), int)
    assert coerce(0.5, "0.25") == 0.25
    assert isinstance(coerce(0.5, "0.25"), float)
    assert coerce("model", "auto") == "auto"


# --------------------------------------------------------------------------------------
# what a live process may take, and what it may not
# --------------------------------------------------------------------------------------
def test_a_state_file_splits_into_reloadable_refused_and_unknown():
    settings = _settings()
    state = {"env": {"BRIAN_GATE_ANOMALY_P": "0.25",
                     "BRIAN_DIGEST_SCORING": "auto",
                     "BRIAN_WINDOW_SIZE_S": "120",
                     "BRIAN_DEPLOY_STATE": "var/evolve/current.json",
                     "SOMETHING_FROM_THE_FUTURE": "1"}}

    overrides, refused, unknown = overrides_from_state(state, settings)

    assert overrides == {"gate": {"anomaly_probability": 0.25}, "digest": {"scoring": "auto"}}
    assert refused == ["BRIAN_DEPLOY_STATE", "BRIAN_WINDOW_SIZE_S"]
    assert unknown == ["SOMETHING_FROM_THE_FUTURE"]


def test_every_reloadable_field_has_an_environment_variable():
    from brian.config import ENV_VARS

    known = {pair for pair in ENV_VARS}
    assert RELOADABLE_FIELDS <= known


# --------------------------------------------------------------------------------------
# the poll: silent until the file moves
# --------------------------------------------------------------------------------------
def test_a_poll_is_silent_until_the_state_file_moves(tmp_path):
    path = tmp_path / "current.json"
    _write_state(path, {"BRIAN_GATE_ANOMALY_P": "0.25"}, "2026-01-01T00:00:00+00:00")
    watcher = DeploymentState(path)
    settings = _settings()

    first = watcher.poll(settings)
    assert first["stamp"] == "2026-01-01T00:00:00+00:00"
    assert first["overrides"] == {"gate": {"anomaly_probability": 0.25}}
    assert watcher.poll(settings) is None

    _write_state(path, {"BRIAN_GATE_ANOMALY_P": "0.35"}, "2026-01-01T00:05:00+00:00")
    second = watcher.poll(settings)
    assert second["overrides"] == {"gate": {"anomaly_probability": 0.35}}


def test_a_missing_state_file_is_not_a_change(tmp_path):
    watcher = DeploymentState(tmp_path / "absent.json")

    assert watcher.poll(_settings()) is None
    assert watcher.stamp() is None


# --------------------------------------------------------------------------------------
# the behaviour: a new threshold changes what the *next* window does
# --------------------------------------------------------------------------------------
def test_a_running_pipeline_takes_a_new_threshold_without_a_restart(tmp_path):
    path = tmp_path / "current.json"
    _write_state(path, {"BRIAN_GATE_ANOMALY_P": "0.95"}, "2026-01-01T00:00:00+00:00")
    pipeline = _pipeline(_settings(state_file=str(path)))
    windows = generate(sequence=DEFAULT_SEQUENCE[:2], warmup_minutes=2)
    batches = [flatten(windows[:2]), flatten(windows[2:4])]

    for event in batches[0]:
        pipeline.ingest(event)
    quiet = [trace for trace in pipeline.drain_ready(final=True) if trace.decision is not None]
    assert pipeline.counters.settings_reloads == 1
    assert pipeline.settings.gate.anomaly_probability == 0.95

    # The loop reverts or re-applies while the pipeline is running.
    _write_state(path, {"BRIAN_GATE_ANOMALY_P": "0.20"}, "2026-01-01T00:05:00+00:00")
    for event in batches[1]:
        pipeline.ingest(event)
    loud = [trace for trace in pipeline.drain_ready(final=True) if trace.decision is not None]

    assert pipeline.counters.settings_reloads == 2
    assert pipeline.reloads[-1]["changed"] == ["gate.anomaly_probability"]
    assert pipeline.settings.gate.anomaly_probability == 0.20
    # Same windows, same engine answer (0.9), different threshold — so the actions must differ.
    assert [trace.decision.action.value for trace in quiet] != \
           [trace.decision.action.value for trace in loud]
    assert "page_human" in [trace.decision.action.value for trace in loud]


def test_a_window_geometry_change_is_refused_while_the_gate_change_lands(tmp_path):
    path = tmp_path / "current.json"
    _write_state(path, {"BRIAN_GATE_ANOMALY_P": "0.20",
                        "BRIAN_WINDOW_SIZE_S": "600"}, "2026-01-01T00:00:00+00:00")
    pipeline = _pipeline(_settings(state_file=str(path)))
    before = pipeline.settings.window.size_s

    for event in flatten(generate(sequence=DEFAULT_SEQUENCE[:1], warmup_minutes=2)):
        pipeline.ingest(event)
    pipeline.drain_ready(final=True)

    assert pipeline.settings.gate.anomaly_probability == 0.20
    assert pipeline.settings.window.size_s == before
    assert pipeline.reloads[-1]["refused"] == ["BRIAN_WINDOW_SIZE_S"]


def test_a_pipeline_without_a_state_file_never_reloads(tmp_path):
    pipeline = _pipeline(_settings())

    assert pipeline.deploy is None
    assert pipeline.reload_from_deployment() is None
    assert pipeline.counters.settings_reloads == 0


def test_the_grader_is_told_that_this_build_can_reload():
    assert reload_capable() is True
