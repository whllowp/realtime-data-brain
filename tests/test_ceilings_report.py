"""The gate prints the ceilings it judged by, and a withdrawal travels with the command (round 67).

Round 66 put the ceilings on the campaign page and made them writable and withdrawable; the *check* still
charged them without naming them, and a withdrawal existed only in the terminal that typed it. Both are the
same failure: a decision that reaches the exit code but not the report cannot be reviewed by whoever reads the
report later.
"""

from __future__ import annotations

import json

from brian import cli
from brian.promote import set_state_budget, state_budgets, state_drains


def _root_with_ceiling(tmp_path, *, run=10.0, drain=3.0):
    """A workspace whose one domain records both ceilings and whose ledger is empty."""
    set_state_budget(tmp_path, "SMD", run, drain_minutes=drain)
    return tmp_path


def test_the_audit_prints_the_ceilings_it_judged_by(tmp_path, monkeypatch, capsys):
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0"]) == 0
    out = capsys.readouterr().out
    assert "limits            one run 10.0 minute(s) on smd (state)" in out
    assert "a drain of 3.0 minute(s) on smd (state)" in out


def test_the_audit_says_nothing_about_ceilings_when_there_are_none(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0"]) == 0
    assert "limits            " not in capsys.readouterr().out


def test_an_ignored_state_ceiling_is_not_reported_as_in_force(tmp_path, monkeypatch, capsys):
    """`--ignore-state-limits` means the check did not read them, so the page must not claim it did."""
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0", "--ignore-state-limits"]) == 0
    assert "limits            " not in capsys.readouterr().out


def test_the_audit_sentence_is_the_campaign_sentence(tmp_path, monkeypatch, capsys):
    """Two modes reading one ledger must describe the judgement in the same words (boundary 108)."""
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0"]) == 0
    from_audit = [line for line in capsys.readouterr().out.splitlines() if line.startswith("limits ")]
    assert cli.main(["evolve", "--campaign", "--rerun-limit", "0"]) == 0
    from_page = [line for line in capsys.readouterr().out.splitlines() if line.startswith("limits ")]
    assert from_audit == from_page
    assert len(from_audit) == 1


def test_the_audit_json_carries_the_same_limits_it_printed(tmp_path, monkeypatch, capsys):
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "audit.json"
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["limits"]["max_minutes_by_domain"] == {"smd": 10.0}
    assert payload["limits"]["max_drain_by_domain"] == {"smd": 3.0}
    assert payload["limits"]["max_minutes_origin"] == {"smd": "state"}
    assert "one run 10.0 minute(s) on smd (state)" in payload["limits_text"]
    # And the counts the page printed are machine-readable too.
    for key in ("moved", "relabelled", "rounds", "trials", "retired",
                "unreconstructable", "inconsistent", "rules_hash"):
        assert key in payload


def test_a_withdrawal_travels_with_the_command_that_made_it(tmp_path, monkeypatch, capsys):
    """`cleared` used to exist only in stdout; the state file cannot say "withdrawn" vs "never set"."""
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "write.json"
    assert cli.main(["evolve", "--set-domain-drain", "smd=none", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["cleared"] == [{"domain": "smd", "kind": "drain"}]
    assert payload["recorded"] == []
    assert payload["in_force"] == {"run": {"smd": 10.0}, "drain": {}}
    assert payload["files"] and payload["files"][0].endswith("current.json")


def test_a_write_reports_what_it_recorded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "write.json"
    assert cli.main(["evolve", "--set-domain-limit", "smd=4", "--set-domain-drain", "smd=9",
                     "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["recorded"] == [{"domain": "smd", "kind": "run", "minutes": 4.0},
                                   {"domain": "smd", "kind": "drain", "minutes": 9.0}]
    assert payload["cleared"] == []
    assert payload["in_force"] == {"run": {"smd": 4.0}, "drain": {"smd": 9.0}}


def test_withdrawing_both_ceilings_leaves_nothing_in_force(tmp_path, monkeypatch):
    _root_with_ceiling(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "write.json"
    assert cli.main(["evolve", "--set-domain-limit", "smd=none", "--set-domain-drain", "smd=none",
                     "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["cleared"] == [{"domain": "smd", "kind": "run"}, {"domain": "smd", "kind": "drain"}]
    assert payload["in_force"] == {"run": {}, "drain": {}}
    assert state_budgets(tmp_path) == {}
    assert state_drains(tmp_path) == {}


def test_a_malformed_entry_writes_no_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "write.json"
    assert cli.main(["evolve", "--set-domain-limit", "smd=soon", "--json-out", str(target)]) == 2
    assert not target.exists()
