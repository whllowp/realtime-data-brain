# -*- coding: utf-8 -*-
"""Round 110: what a refusal has cost, and which origin file to open first."""
import json

from brian import cli
from brian.campaign import campaign
from brian.promote import blocked_versions, repair_plan

from test_blocked_dossier import _blocked
from test_clearing_evidence_file import FILE
from test_origin_queue import _at_origin, _elsewhere_only
from test_provenance_block import _mismatch
from test_queue_history import _walk


def _refused_twice(tmp_path):
    _blocked(tmp_path)
    why = blocked_versions(tmp_path)[20]["why"]
    _walk(tmp_path, attempted=[{"version": 20, "code": 2, "why": why}])


def test_a_clean_ledger_has_nothing_to_repair(tmp_path):
    _elsewhere_only(tmp_path)
    assert repair_plan(tmp_path) == []


def test_the_plan_names_the_file_the_cost_and_the_remedy(tmp_path):
    _refused_twice(tmp_path)
    plan = repair_plan(tmp_path)
    assert len(plan) == 1
    row = plan[0]
    assert row["origin"] == FILE
    assert row["records"] == [20]
    assert row["walks_blocked"] == 2
    assert row["walks"] == 2
    assert row["held"] == 1
    assert row["cost"] == 2
    # Round 112: the refusing book has a command, so the plan names the decision rather than the diagnostic --
    # a plan whose remedy only diagnoses ranks work nobody can do (boundary 250).
    assert row["pairs"] == [{"record": "round-old.json", "successor": "round-new.json",
                            "withdrawn": False}]
    assert row["remedy"] == "brian evolve --settle-link round-old.json=round-new.json"


def test_a_file_that_holds_nothing_still_costs_the_walks_it_spent(tmp_path):
    # A promise that was checked where it was born is not waiting behind the file any more, so the cost is the walk
    # that was spent rather than zero.
    _at_origin(tmp_path)
    _mismatch(tmp_path)
    _walk(tmp_path, attempted=[{"version": 20, "code": 2}])
    plan = repair_plan(tmp_path)
    assert plan[0]["held"] == 0
    assert plan[0]["cost"] == plan[0]["walks_blocked"]


def test_the_ranking_is_by_cost_then_walks_then_name(monkeypatch, tmp_path):
    entries = {
        "data/smd/b.json": {"records": [21], "refused_walks": [1], "walks": 3, "last_walk": 1},
        "data/smd/a.json": {"records": [20], "refused_walks": [1, 2, 3], "walks": 3, "last_walk": 3},
        "data/smd/c.json": {"records": [22], "refused_walks": [1, 2, 3], "walks": 3, "last_walk": 3},
    }
    monkeypatch.setattr("brian.promote.blocked_dossier", lambda root: entries)
    monkeypatch.setattr("brian.promote.origin_queue", lambda root: [{"version": version} for version in (20, 21, 22)])
    plan = repair_plan(tmp_path)
    # Equal cost and equal walks fall back to the name, so two runs print the same order.
    assert [row["origin"] for row in plan] == ["data/smd/a.json", "data/smd/c.json", "data/smd/b.json"]
    assert [row["cost"] for row in plan] == [3, 3, 1]


def test_the_plan_page_ends_with_the_repair_order(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--verify", "--from-queue", "0"]) == 0
    printed = capsys.readouterr().out
    assert "repair order     %s costs 1 walk(s) of refusals and holds 1 promise(s) behind it" % FILE in printed
    assert "(brian evolve --settle-link round-old.json=round-new.json)" in printed


def test_a_clean_queue_page_does_not_print_a_repair_order(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--verify", "--from-queue", "0"]) == 0
    assert "repair order" not in capsys.readouterr().out


def test_the_campaign_names_the_file_to_open_first(tmp_path, monkeypatch, capsys):
    _blocked(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign"]) == 0
    printed = capsys.readouterr().out
    assert "repair next: %s -- 1 walk(s) of refusals, 1 promise(s) held behind it" % FILE in printed


def test_the_campaign_json_carries_the_plan(tmp_path, monkeypatch):
    _blocked(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "campaign.json"
    assert cli.main(["evolve", "--campaign", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["repair_plan"][0]["origin"] == FILE
    assert payload["repair_plan"][0]["cost"] == 1


def test_the_page_and_the_json_agree_about_the_order(tmp_path):
    _refused_twice(tmp_path)
    plan = campaign(tmp_path).repair_plan
    assert plan == repair_plan(tmp_path)
    assert plan[0]["origin"] == FILE
