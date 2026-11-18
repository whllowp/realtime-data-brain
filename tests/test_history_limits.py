"""The history page names the ceilings each round was judged under (round 70).

`--audit` and `--campaign` compare recorded ceilings against today's and print how many rounds moved. Neither
answers the smaller question a reader has while looking at one row: what was this round allowed to wait for?
The ledger listed promises, files and verdicts without the limits, so two rows measured ten minutes apart in
policy looked like one continuous experiment.
"""

from __future__ import annotations

import json

from brian import cli
from brian.evolve import limit_drift, round_limits_text


def _limits(run=None, run_domains=None, drain=None, drain_domains=None):
    return {
        "max_minutes": run,
        "max_minutes_by_domain": dict(run_domains or {}),
        "max_drain_minutes": drain,
        "max_drain_by_domain": dict(drain_domains or {}),
    }


def test_no_ceilings_is_said_not_left_blank():
    assert round_limits_text(None) == "no ceilings recorded"
    assert round_limits_text({}) == "no ceilings recorded"
    # A payload that carries only origins and no numbers is still "nothing recorded": the origins say who
    # decided a ceiling that is not there.
    assert round_limits_text({"max_minutes_origin": {"smd": "state"}}) == "no ceilings recorded"


def test_every_kind_of_ceiling_appears():
    text = round_limits_text(_limits(run=12.0, run_domains={"smd": 4.0}, drain=30.0,
                                     drain_domains={"SMD": 3.0, "rcaeval-re1-ob": 6.0}))
    assert "one run 12.0 minute(s) for every domain" in text
    assert "one run 4.0 minute(s) on smd" in text
    assert "a drain of 30.0 minute(s) for every domain" in text
    assert "a drain of 3.0 minute(s) on SMD" in text
    assert "a drain of 6.0 minute(s) on rcaeval-re1-ob" in text


def test_the_domains_are_named_in_a_stable_order():
    one = round_limits_text(_limits(run_domains={"smd": 4.0, "rcaeval-re1-ob": 6.0}))
    other = round_limits_text(_limits(run_domains={"rcaeval-re1-ob": 6.0, "smd": 4.0}))
    assert one == other
    assert one.index("rcaeval") < one.index("on smd")


def _round(root, name, *, limits=None, started="2026-11-18T06:00:00+00:00"):
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": name,
        "started_at": started,
        "finished_at": started,
        "trials": [],
        "accepted": [],
        "rejected": [],
        "applied": [],
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.297},
        "stream": {"source": "SMD", "windows": 948, "scored": 828},
    }
    if limits is not None:
        payload["limits"] = limits
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    line = {"round": name, "started_at": started, "accepted": [], "rejected": [],
            "applied": [], "rules_hash": "abc123abc123", "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


def test_the_history_page_counts_the_rounds_that_say(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-one.json", limits=_limits(run_domains={"smd": 4.0}))
    _round(tmp_path, "round-two.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings          1 of 2 round(s) record the wait ceilings they were judged under; 1 do not" in text


def test_a_round_names_the_ceiling_it_was_judged_under(tmp_path, monkeypatch, capsys):
    """Nothing is in force today, so a round that ran under a 10-minute ceiling was not judged like today's."""
    _round(tmp_path, "round-one.json", limits=_limits(run=10.0))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings  one run 10.0 minute(s) for every domain  (differs from today's)" in text


def test_the_same_ceiling_in_force_is_reported_as_such(tmp_path, monkeypatch, capsys):
    state = tmp_path / "var" / "evolve" / "domains" / "smd"
    state.mkdir(parents=True)
    (state / "current.json").write_text(json.dumps({
        "env": {}, "sources": {}, "budget": {"max_minutes": 10.0},
        "updated_at": "2026-11-18T06:00:00+00:00", "domain": "smd", "source": "SMD",
    }), encoding="utf-8")
    _round(tmp_path, "round-one.json", limits=_limits(run_domains={"smd": 10.0}))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings  one run 10.0 minute(s) on smd  (as in force now)" in text


def test_a_global_ceiling_is_not_the_same_promise_as_one_domains(tmp_path):
    """A ceiling for every domain also constrains the domains this round did not touch, so the two differ."""
    today = _limits(run=10.0)
    judged = _round(tmp_path, "round-one.json", limits=_limits(run_domains={"smd": 10.0}))
    assert limit_drift([judged], today)["differ"] == ["round-one.json"]


def test_a_round_judged_under_another_ceiling_says_so(tmp_path, monkeypatch, capsys):
    """The point of the per-round line: today's state is 4 minutes, this round ran under 10."""
    state = tmp_path / "var" / "evolve" / "domains" / "smd"
    state.mkdir(parents=True)
    (state / "current.json").write_text(json.dumps({
        "env": {}, "sources": {}, "budget": {"max_minutes": 4.0},
        "updated_at": "2026-11-18T06:00:00+00:00", "domain": "smd", "source": "SMD",
    }), encoding="utf-8")
    _round(tmp_path, "round-one.json", limits=_limits(run=10.0))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings  one run 10.0 minute(s) for every domain  (differs from today's)" in text


def test_a_round_without_ceilings_gets_no_line(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings          0 of 1 round(s)" in text
    assert "      ceilings  " not in text


def test_the_line_and_the_comparison_agree(tmp_path):
    """The per-round note is the same comparison the campaign's header counts, so the two cannot disagree."""
    today = _limits(run=4.0)
    judged = _round(tmp_path, "round-one.json", limits=_limits(run=10.0))
    assert limit_drift([judged], today)["differ"] == ["round-one.json"]
    same = _round(tmp_path, "round-two.json", limits=_limits(run=4.0))
    assert limit_drift([same], today)["differ"] == []
