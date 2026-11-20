# -*- coding: utf-8 -*-
"""Round 136: the what-if answers a plan, in an order, because each answer changes the next question.

The three batch commands move debt between two books: dropping turns "no re-take can pay this" into "nobody has
taken it up again", restoring turns "nobody has taken it up again" back into a settled pair. Three separate
after-states therefore do not add up to one plan -- and a reader who pastes them in some order gets books nobody
predicted (boundaries 321/322).
"""
from brian import cli
from brian.evolve import load_settlements, reaffirm_plan, withdraw_settlement, withdrawal_plan
from test_drop_batch import _two_unpayable
from test_reaffirm_batch import _two_stale
from test_restore_file import ONE


def _plan(*steps):
    return cli.main(["evolve", "--campaign", "--what-if-records", ONE, "--plan", ",".join(steps)])


def _printed(capsys):
    return " ".join(capsys.readouterr().out.split())


def test_the_plan_prints_every_step(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert _plan("restore", "drop", "retake") == 0
    printed = _printed(capsys)
    assert "in the order: restore, drop, retake" in printed
    assert "after restore" in printed and "after drop" in printed and "after retake" in printed
    assert "unanswered withdrawal(s)" in printed and "cannot be re-taken" in printed
    assert "nothing here is written: every step runs on a copy of the store" in printed


def _withdrawn_but_restorable(root):
    """A withdrawn decision whose record still names its successor, on a pair that has since gone stale."""
    rows = [row for row in reaffirm_plan(cli.history(root, include_retired=True), load_settlements(root))
            if row["origin"] == ONE and row.get("pairs")]
    assert rows, "the fixture must owe at least one payable re-take"
    pair = rows[0]["pairs"][0]
    withdraw_settlement(root, str(pair["record"]), str(pair["successor"]), why="the earlier call was wrong",
                        by="bob")
    return pair


def test_the_orders_agree_where_both_doors_refuse(tmp_path, monkeypatch, capsys):
    """Round 137: on 136's own fixture the orders now agree, because the restore door refuses what the re-take
    door refuses. A page that stayed silent about the order is telling the truth, not forgetting the feature."""
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert _plan("restore", "drop", "retake") == 0
    printed = _printed(capsys)
    assert "the order matters:" not in printed
    assert "in the order: restore, drop, retake" in printed


def test_the_order_matters_when_one_step_unlocks_the_next(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    _withdrawn_but_restorable(root)
    rounds_all = cli.history(root, include_retired=True)
    runs = cli.queue_runs(root)["runs"]
    stored = load_settlements(root)
    forward = cli._run_steps(rounds_all, stored, runs, ONE, ["restore", "retake"])[1][-1][2]
    backward = cli._run_steps(rounds_all, stored, runs, ONE, ["retake", "restore"])[1][-1][2]
    assert forward != backward, (forward, backward)
    assert forward[1] <= backward[1], "restoring before re-taking pays the stale decision it revives"
    assert _plan("restore", "retake") == 0
    printed = _printed(capsys)
    assert "the order matters:" in printed
    assert "done the other way round it would end at" in printed


def test_one_step_does_not_claim_an_order(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert _plan("retake") == 0
    printed = _printed(capsys)
    assert "in the order: retake" in printed
    assert "after retake" in printed
    assert "the order matters:" not in printed


def test_the_plan_writes_nothing(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    before = load_settlements(root)
    plans_before = (withdrawal_plan(cli.history(root, include_retired=True), before),
                    reaffirm_plan(cli.history(root, include_retired=True), before))
    assert _plan("restore", "drop", "retake") == 0
    capsys.readouterr()
    after = load_settlements(root)
    assert after == before
    assert (withdrawal_plan(cli.history(root, include_retired=True), after),
            reaffirm_plan(cli.history(root, include_retired=True), after)) == plans_before


def test_a_plan_without_the_question_is_refused(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--campaign", "--plan", "restore"]) == 2
    printed = _printed(capsys)
    assert "--plan steps --campaign --what-if-records through the batch commands" in printed


def test_a_step_nobody_has_is_refused(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--campaign", "--what-if-records", ONE, "--plan", "restore,retreat"]) == 2
    printed = _printed(capsys)
    assert "retreat is not one of them" in printed
    assert "restore, drop, retake" in printed


def test_the_numbers_agree_with_the_plans_they_come_from(tmp_path, monkeypatch, capsys):
    root = _two_unpayable(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    stored = load_settlements(root)
    rounds_all = cli.history(root, include_retired=True)
    runs = cli.queue_runs(root)["runs"]
    counts = cli._plan_counts(rounds_all, stored, runs, ONE)
    assert _plan("retake") == 0
    printed = _printed(capsys)
    assert ("owes %d unanswered withdrawal(s), %d stale decision(s) and %d decision(s) no re-take can pay"
            % counts) in printed
