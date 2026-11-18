# -*- coding: utf-8 -*-
"""Round 65 tests: the whole-queue ceiling is a second promise, recorded beside the per-run one.

Round 64 recorded how long one queue run may wait for a domain. Draining that domain's queue is a different
number and a different promise: a reader who can only cap the first learns the second mid-queue, when the money
is already spent.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brian import cli
from brian.evolve import Candidate
from brian.promote import (current_state, format_promotions, load_promotions, set_state_budget,
                           state_budgets, state_drains)
from brian.streams import load_stream_file, summarise

DATASET = "RCAEval RE1-OB"
SLUG = "rcaeval-re1-ob"


def _stream_file(root: Path, relative: str = "data/rcaeval/stream-one.json") -> dict:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": DATASET,
        "streams": [{
            "name": "case-1",
            "windows": [
                {"start": "2026-11-1%dT00:00:00+00:00" % index, "service": "case",
                 "scored": True, "anomaly": index == 0}
                for index in range(4)
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _round(root: Path, name: str, *, shape: dict, minutes: float, stream_path=None) -> None:
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    end = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)
    begin = end - timedelta(minutes=minutes)
    candidate = Candidate(name="events-4", hypothesis="fewer context events", section="llm",
                          field="max_context_events", value=4)
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": begin.isoformat(), "finished_at": end.isoformat(),
        "stream": dict(shape, source=DATASET), "stream_path": stream_path,
        "trials": [{"candidate": candidate.to_dict(), "measurement": None, "verdict": None}],
        "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    versions = evolve / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    label = "v%03d" % (len(list(evolve.glob("round-*.json"))) + 40)
    (versions / ("%s.json" % label)).write_text(json.dumps({
        "version": int(label[1:]), "created_at": end.isoformat(), "candidate": {"name": "events-4"},
        "change": "events-4", "env": {"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"}, "measurement": {},
        "stream": dict(shape, source=DATASET), "stream_path": stream_path,
        "verdict": {"accepted": True, "rule": "cost", "reason": "fixture"},
        "rollback": "unset BRIAN_LLM_MAX_CONTEXT_EVENTS",
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": begin.isoformat(), "finished_at": end.isoformat(),
            "accepted": 1, "rejected": 0, "applied": [label], "rules_hash": "test",
            "stream_path": stream_path, "baseline_tokens_per_report": 1706.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _root_with_queue(root: Path) -> None:
    """One named round (6 minutes, names its file) and one unrecorded round priced at that median."""
    shape = _stream_file(root)
    _round(root, "round-rcaeval-a.json", shape=shape, minutes=6.0,
           stream_path="data/rcaeval/stream-one.json")
    _round(root, "round-rcaeval-unrecorded.json", shape=shape, minutes=6.0)


def test_both_ceilings_live_side_by_side(tmp_path):
    set_state_budget(tmp_path, DATASET, 10.0)
    set_state_budget(tmp_path, DATASET, drain_minutes=20.0)
    assert state_budgets(tmp_path) == {SLUG: 10.0}
    assert state_drains(tmp_path) == {SLUG: 20.0}
    assert current_state(tmp_path, DATASET)["budget"] == {"max_minutes": 10.0, "drain_minutes": 20.0}


def test_setting_one_ceiling_keeps_the_other(tmp_path):
    set_state_budget(tmp_path, DATASET, drain_minutes=20.0)
    set_state_budget(tmp_path, DATASET, 10.0)
    assert state_drains(tmp_path) == {SLUG: 20.0}
    set_state_budget(tmp_path, DATASET, drain_minutes=30.0)
    assert state_budgets(tmp_path) == {SLUG: 10.0}
    assert state_drains(tmp_path) == {SLUG: 30.0}


def test_set_state_budget_refuses_to_write_nothing(tmp_path):
    with pytest.raises(ValueError):
        set_state_budget(tmp_path, DATASET)
    assert state_budgets(tmp_path) == {}


def test_an_unreadable_drain_ceiling_is_absent_not_fatal(tmp_path):
    set_state_budget(tmp_path, DATASET, 5.0)
    path = tmp_path / "var" / "evolve" / "domains" / SLUG / "current.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["budget"]["drain_minutes"] = "whenever"
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    assert state_budgets(tmp_path) == {SLUG: 5.0}
    assert state_drains(tmp_path) == {}


def test_the_version_page_names_both_ceilings(tmp_path):
    set_state_budget(tmp_path, DATASET, 10.0)
    set_state_budget(tmp_path, DATASET, drain_minutes=20.0)
    text = format_promotions(load_promotions(tmp_path), current_state(tmp_path, DATASET), domain=DATASET)
    assert "at most 10 minute(s) for one queue run" in text
    assert "at most 20 minute(s) to empty the queue" in text


def test_the_writer_records_a_drain_ceiling(tmp_path, monkeypatch, capsys):
    _root_with_queue(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--set-domain-drain", "rcaeval-re1-ob=20"]) == 0
    assert "at most 20 minute(s) to empty the queue" in capsys.readouterr().out
    assert state_drains(tmp_path) == {SLUG: 20.0}


def test_a_malformed_drain_ceiling_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--set-domain-drain", "rcaeval=soon"]) == 2
    assert "--set-domain-drain wants domain=minutes" in capsys.readouterr().out
    assert state_drains(tmp_path) == {}


def test_the_gate_charges_the_drain_not_the_run(tmp_path, monkeypatch, capsys):
    """Six minutes a run is fine at ten; emptying the one-round queue at three is not."""
    _root_with_queue(tmp_path)
    set_state_budget(tmp_path, DATASET, 10.0)
    set_state_budget(tmp_path, DATASET, drain_minutes=3.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0"]) == 1
    out = capsys.readouterr().out
    assert "emptying the queue would spend about 6.0 minute(s) on RCAEval RE1-OB, above the 3.0 asked for" in out
    assert "one queue run" not in out


def test_a_drain_ceiling_inside_the_limit_passes(tmp_path, monkeypatch, capsys):
    _root_with_queue(tmp_path)
    set_state_budget(tmp_path, DATASET, drain_minutes=30.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0"]) == 0
    assert "emptying the queue" not in capsys.readouterr().out


def test_a_drain_flag_can_disagree_with_the_state(tmp_path, monkeypatch, capsys):
    _root_with_queue(tmp_path)
    set_state_budget(tmp_path, DATASET, drain_minutes=3.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0",
                     "--max-drain-minutes", "rcaeval-re1-ob=100"]) == 0
    assert "emptying the queue" not in capsys.readouterr().out


def test_the_json_report_says_where_each_ceiling_came_from(tmp_path, monkeypatch):
    _root_with_queue(tmp_path)
    set_state_budget(tmp_path, DATASET, 10.0)
    set_state_budget(tmp_path, DATASET, drain_minutes=3.0)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "campaign.json"
    assert cli.main(["evolve", "--campaign", "--rerun-limit", "0", "--json-out", str(target)]) == 0
    limits = json.loads(target.read_text(encoding="utf-8"))["limits"]
    assert limits["max_minutes_by_domain"] == {SLUG: 10.0}
    assert limits["max_minutes_origin"] == {SLUG: "state"}
    assert limits["max_drain_by_domain"] == {SLUG: 3.0}
    assert limits["max_drain_origin"] == {SLUG: "state"}


def test_a_flag_that_overrides_a_recorded_ceiling_is_reported_as_a_flag(tmp_path, monkeypatch):
    """A number that matches the policy by accident is not evidence that nobody overrode it."""
    _root_with_queue(tmp_path)
    set_state_budget(tmp_path, DATASET, drain_minutes=3.0)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "campaign.json"
    assert cli.main(["evolve", "--campaign", "--rerun-limit", "0",
                     "--max-drain-minutes", "rcaeval-re1-ob=3", "--json-out", str(target)]) == 0
    limits = json.loads(target.read_text(encoding="utf-8"))["limits"]
    assert limits["max_drain_by_domain"] == {SLUG: 3.0}
    assert limits["max_drain_origin"] == {SLUG: "flag"}
