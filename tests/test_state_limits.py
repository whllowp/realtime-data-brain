# -*- coding: utf-8 -*-
"""Round 64 tests: a domain's wait ceiling lives in its own deployment state, not in the shell history.

Round 63 let an operator cap a queue run per domain, but only on the command line. A ceiling that lives for one
invocation has to be retyped, and the same number can mean two different things on two days; a limit is a
statement about how long one stream may keep the loop waiting, so it belongs with that stream's state.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.evolve import Candidate
from brian.promote import (current_state, format_promotions, load_promotions, set_state_budget,
                           state_budgets, state_path)
from brian.streams import load_stream_file, summarise

STREAM = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 0,
          "names": ["host-1"], "cadence_s": 3600.0, "span_s": 7200.0}


def _stream_file(root: Path, relative: str = "data/smd/stream-one.json", dataset: str = "SMD") -> dict:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": dataset,
        "streams": [{
            "name": "host-1",
            "windows": [
                {"start": "2026-11-1%dT00:00:00+00:00" % (index * 1), "service": "host",
                 "scored": True, "anomaly": index == 0}
                for index in range(3)
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _round(root: Path, name: str, *, source: str, shape: dict, minutes: float, stream_path=None) -> None:
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    end = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    begin = end - timedelta(minutes=minutes)
    candidate = Candidate(name="events-4", hypothesis="fewer context events", section="llm",
                          field="max_context_events", value=4)
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": begin.isoformat(), "finished_at": end.isoformat(),
        "stream": dict(shape, source=source), "stream_path": stream_path,
        "trials": [{"candidate": candidate.to_dict(), "measurement": None, "verdict": None}],
        "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    versions = evolve / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    number = len(list(evolve.glob("round-[a-z0-9-]*.json"))) + 30
    label = "v%03d" % number
    (versions / ("%s.json" % label)).write_text(json.dumps({
        "version": number, "created_at": end.isoformat(), "candidate": {"name": "events-4"},
        "change": "events-4", "env": {"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"}, "measurement": {},
        "stream": dict(shape, source=source), "stream_path": stream_path,
        "verdict": {"accepted": True, "rule": "cost", "reason": "fixture"},
        "rollback": "unset BRIAN_LLM_MAX_CONTEXT_EVENTS",
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": begin.isoformat(), "finished_at": end.isoformat(),
            "accepted": 1, "rejected": 0, "applied": [label], "rules_hash": "test",
            "stream_path": stream_path, "baseline_tokens_per_report": 3358.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _one_domain_root(root: Path) -> None:
    shape = _stream_file(root)
    _round(root, "round-smd-a.json", source="SMD", shape=shape, minutes=4.0,
           stream_path="data/smd/stream-one.json")
    _round(root, "round-smd-b.json", source="SMD", shape=shape, minutes=5.0,
           stream_path="data/smd/stream-one.json")
    _round(root, "round-smd-unrecorded.json", source="SMD", shape=shape, minutes=4.5)


def test_a_recorded_ceiling_is_read_back(tmp_path):
    assert state_budgets(tmp_path) == {}
    set_state_budget(tmp_path, "RCAEval RE1-OB", 4.0)
    assert state_budgets(tmp_path) == {"rcaeval-re1-ob": 4.0}
    assert current_state(tmp_path, "RCAEval RE1-OB")["budget"] == {"max_minutes": 4.0}


def test_writing_a_ceiling_leaves_the_configuration_alone(tmp_path):
    _one_domain_root(tmp_path)
    before = current_state(tmp_path, "SMD")
    set_state_budget(tmp_path, "SMD", 3.0)
    after = current_state(tmp_path, "SMD")
    assert after["env"] == before["env"]
    assert after["sources"] == before["sources"]
    assert after["budget"] == {"max_minutes": 3.0}
    assert after["updated_at"] >= before["updated_at"]


def test_a_promotion_does_not_delete_a_ceiling_that_shares_its_file(tmp_path):
    """The state file holds both a configuration and a policy: writing one must not drop the other."""
    _one_domain_root(tmp_path)
    set_state_budget(tmp_path, "SMD", 3.0)
    from brian.promote import apply_promotion

    promotion = load_promotions(tmp_path)[0]
    apply_promotion(tmp_path, promotion, domain="SMD")
    assert current_state(tmp_path, "SMD")["budget"] == {"max_minutes": 3.0}


def test_an_unreadable_ceiling_is_simply_absent(tmp_path):
    path = state_path(tmp_path, "SMD")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {}, "sources": {}, "updated_at": "",
                                "budget": {"max_minutes": "soon"}}), encoding="utf-8", newline="\n")
    assert state_budgets(tmp_path) == {}
    assert current_state(tmp_path, "SMD")["budget"] == {"max_minutes": "soon"}


def test_the_version_page_names_the_recorded_ceiling(tmp_path):
    _one_domain_root(tmp_path)
    set_state_budget(tmp_path, "SMD", 3.0)
    text = format_promotions(load_promotions(tmp_path), current_state(tmp_path, "SMD"), domain="SMD")
    assert "at most 3 minute(s) for one queue run (from the deployment state)" in text
    plain = format_promotions(load_promotions(tmp_path), current_state(tmp_path, "RCAEval RE1-OB"),
                              domain="RCAEval RE1-OB")
    assert "at most" not in plain


def test_the_writer_records_the_dataset_name_not_the_spelling(tmp_path, monkeypatch, capsys):
    _one_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--set-domain-limit", "smd=3"]) == 0
    assert "recorded          smd: at most 3 minute(s)" in capsys.readouterr().out
    # The state's `source` is the name the ledger used, or the domain would look like a stream nobody measured.
    assert current_state(tmp_path, "SMD")["budget"] == {"max_minutes": 3.0}
    document = json.loads(state_path(tmp_path, "SMD").read_text(encoding="utf-8"))
    assert document["source"] == "SMD"


def test_a_malformed_ceiling_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--set-domain-limit", "oops"]) == 2
    assert "--set-domain-limit wants domain=minutes" in capsys.readouterr().out


def test_the_gate_charges_a_state_ceiling_without_being_told(tmp_path, monkeypatch, capsys):
    _one_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    set_state_budget(tmp_path, "SMD", 4.0)  # the queue's one round is priced at 4.5 minutes
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0"]) == 1
    assert "on SMD, above the 4.0 asked for" in capsys.readouterr().out


def test_the_state_ceiling_can_be_ignored_on_purpose(tmp_path, monkeypatch, capsys):
    _one_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    set_state_budget(tmp_path, "SMD", 4.0)
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0", "--ignore-state-limits"]) == 0
    assert "asked for" not in capsys.readouterr().out


def test_a_flag_for_the_same_domain_wins_over_the_recorded_one(tmp_path, monkeypatch, capsys):
    _one_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    set_state_budget(tmp_path, "SMD", 4.0)
    # Today's operator may disagree with yesterday's policy without editing the deployment state first.
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0",
                     "--max-minutes", "SMD=10"]) == 0
    capsys.readouterr()
