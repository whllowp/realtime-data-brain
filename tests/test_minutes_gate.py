# -*- coding: utf-8 -*-
"""Round 62 tests: a queue's wait is priced per domain, and a wait can be too long to ask for.

Round 61 priced the queue from one median over every recorded round. A queue that pays SMD (two hosts over a
long recording) and RCAEval (fifteen cases) is not "about six minutes each": the reader deciding whether to
wait is deciding about the sum, and the sum only comes out right if each domain is priced by its own rounds.
"""
import json
from pathlib import Path

from brian import cli
from brian.campaign import budget_text
from brian.evolve import Candidate
from brian.streams import load_stream_file, summarise

SMD_MINUTES = (4.0, 5.0)
RCAEVAL_MINUTES = (6.0,)


def _stream(root: Path, relative: str, dataset: str, windows: int, step_hours: int) -> dict:
    """A stream file whose geometry is unique among the files in this root.

    Two files with the same geometry make every record match both of them, so nothing is payable — the mistake
    round 51 found between the two RCAEval files and round 61 made again in a demo fixture.
    """
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": dataset,
        "streams": [{
            "name": "host-1",
            "windows": [
                {"start": "2026-11-1%dT00:00:00+00:00" % (index * step_hours), "service": "host",
                 "scored": True, "anomaly": index == 0}
                for index in range(windows)
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _timed_stamp(root: Path, name: str, *, source: str, shape: dict, minutes: float,
                 stream_path=None) -> None:
    """One recorded round with an exact duration, written the way `persist` writes it."""
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone

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


def _two_domain_root(root: Path) -> None:
    smd = _stream(root, "data/smd/stream-one.json", "SMD", 3, 1)
    rcaeval = _stream(root, "data/rcaeval/stream-one.json", "RCAEval RE1-OB", 4, 2)
    # Every named round is recorded; the payable one in each domain is unrecorded.
    for index, minutes in enumerate(SMD_MINUTES):
        _timed_stamp(root, "round-smd-%d.json" % index, source="SMD", shape=smd, minutes=minutes,
                     stream_path="data/smd/stream-one.json")
    for index, minutes in enumerate(RCAEVAL_MINUTES):
        _timed_stamp(root, "round-rcaeval-%d.json" % index, source="RCAEval RE1-OB", shape=rcaeval,
                     minutes=minutes, stream_path="data/rcaeval/stream-one.json")
    # Timed at their domain's own median, so the payable rounds do not move the rate they are priced with.
    _timed_stamp(root, "round-smd-unrecorded.json", source="SMD", shape=smd, minutes=4.5)
    _timed_stamp(root, "round-rcaeval-unrecorded.json", source="RCAEval RE1-OB", shape=rcaeval,
                 minutes=6.0)


def test_each_domain_is_priced_by_its_own_rounds(tmp_path):
    _two_domain_root(tmp_path)
    rates = cli._round_minutes_by_domain(tmp_path)
    assert rates["SMD"] == 4.5  # median of 4 and 5
    assert rates["RCAEval RE1-OB"] == 6.0


def test_the_budget_adds_up_the_domains_it_pays(tmp_path):
    _two_domain_root(tmp_path)
    budget = cli._queue_budget(tmp_path, limit=0)
    assert budget["rounds"] == 2
    assert budget["minutes_by_domain"] == {"SMD": 4.5, "RCAEval RE1-OB": 6.0}
    assert budget["estimated_minutes_by_domain"] == 10.5
    # The queue-wide rate is still reported, but it is not what prices the sentence any more.
    assert budget["minutes_each"] == 5.0


def test_a_domain_without_a_timed_round_borrows_the_queue_median(tmp_path):
    smd = _stream(tmp_path, "data/smd/stream-one.json", "SMD", 3, 1)
    _timed_stamp(tmp_path, "round-smd-0.json", source="SMD", shape=smd, minutes=4.0)
    evolve = tmp_path / "var" / "evolve"
    (evolve / "round-rcaeval-0.json").write_text(json.dumps({
        "round": "round-rcaeval-0.json", "started_at": "", "finished_at": "",
        "stream": dict(shape_for_rcaeval(tmp_path), source="RCAEval RE1-OB"), "stream_path": None,
        "trials": [{"candidate": Candidate(name="events-4", hypothesis="h", section="llm",
                                           field="max_context_events", value=4).to_dict(),
                    "measurement": None, "verdict": None}],
        "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"round": "round-rcaeval-0.json", "started_at": "", "finished_at": "",
                                 "accepted": 1, "rejected": 0, "applied": ["v099"], "rules_hash": "test",
                                 "stream_path": None, "baseline_tokens_per_report": 0.0}) + "\n")
    (evolve / "versions").mkdir(parents=True, exist_ok=True)
    (evolve / "versions" / "v099.json").write_text(json.dumps({
        "version": 99, "created_at": "2026-11-18T06:00:00+00:00", "candidate": {"name": "events-4"},
        "change": "events-4", "env": {"BRIAN_LLM_MAX_CONTEXT_EVENTS": "4"}, "measurement": {},
        "stream": dict(shape_for_rcaeval(tmp_path), source="RCAEval RE1-OB"), "stream_path": None,
        "verdict": {"accepted": True, "rule": "cost", "reason": "fixture"},
        "rollback": "unset BRIAN_LLM_MAX_CONTEXT_EVENTS",
    }), encoding="utf-8", newline="\n")
    rates = cli._round_minutes_by_domain(tmp_path)
    assert "RCAEval RE1-OB" not in rates
    assert rates["SMD"] == 4.0


def shape_for_rcaeval(root: Path) -> dict:
    return _stream(root, "data/rcaeval/stream-one.json", "RCAEval RE1-OB", 4, 2)


def test_the_sentence_shows_the_breakdown_only_when_two_domains_pay(tmp_path):
    _two_domain_root(tmp_path)
    budget = cli._queue_budget(tmp_path, limit=0)
    text = budget_text(budget)
    assert "about 10 minute(s)" in text
    assert "(RCAEval RE1-OB 6, SMD 4.5)" in text
    single = budget_text(dict(budget, domains=["SMD"], minutes_by_domain={"SMD": 4.5},
                             estimated_minutes_by_domain=4.5))
    assert "about 4 minute(s)" in single
    assert "SMD 4.5)" not in single


def test_a_wait_longer_than_asked_for_fails_the_check(capsys):
    assert cli._strict_exit(True, 0, 0, 0, budget_minutes=10.5, max_minutes=5.0) == 1
    out = capsys.readouterr().out
    assert "about 10.5 minute(s), above the 5.0 asked for" in out
    assert "--rerun-limit" in out


def test_a_wait_inside_the_limit_passes(capsys):
    assert cli._strict_exit(True, 0, 0, 0, budget_minutes=10.5, max_minutes=30.0) == 0
    assert "minute" not in capsys.readouterr().out


def test_a_queue_with_nothing_payable_is_never_charged_for_time(capsys):
    # Nothing will run, so there is no wait to charge: an unpayable budget must not fail the gate.
    assert cli._strict_exit(True, 0, 0, 0, budget_minutes=None, max_minutes=0.0) == 0
    assert "minute" not in capsys.readouterr().out


def test_the_cap_is_read_in_both_modes(tmp_path, monkeypatch, capsys):
    _two_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-minutes", "5",
                     "--rerun-limit", "0"]) == 1
    assert "above the 5.0 asked for" in capsys.readouterr().out
    assert cli.main(["evolve", "--audit", "--strict", "--max-minutes", "30",
                     "--rerun-limit", "0"]) == 0
    capsys.readouterr()


def test_the_audit_prices_the_same_budget_the_page_does(tmp_path, monkeypatch, capsys):
    _two_domain_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--campaign", "--rerun-limit", "0"])
    page = capsys.readouterr().out
    cli.main(["evolve", "--audit", "--strict", "--max-minutes", "5", "--rerun-limit", "0"])
    audit = capsys.readouterr().out
    assert "about 10 minute(s)" in page
    assert "about 10.5 minute(s)" in audit
