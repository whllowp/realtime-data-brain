# -*- coding: utf-8 -*-
"""The plan's timeline, and a cap in the unit the work is paid in (round 121, item 107).

Two questions a page could not answer before. *When* was this file's debt last looked at, and how many walks have
happened since -- `last_retaken` and `walks_since` on each plan row. And is the debt spread across many files or
concentrated in one -- `--max-moved-per-file`, because eleven stale decisions on eleven files are eleven afternoons
and eleven on one file is one afternoon.
"""
from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import reaffirm_plan
from brian.promote import record_queue_run

from test_reaffirm_plan import _later_claim, _settled
from test_settlement_reaffirm import _ago


def _walk(root, *, at):
    """Record one queue walk, so `walks_since` has something to count."""
    return record_queue_run(root, {"limit": 1, "runnable": 1, "measured": 1, "skipped": 0, "cleared": 0,
                                   "worst": 0, "stopped_at": 0, "attempted": []}, now=at)


def _plan(root, runs=None):
    from brian.evolve import load_settlements

    from brian.promote import history

    return reaffirm_plan(history(root, include_retired=True), load_settlements(root), runs)


def test_a_file_that_was_never_re_taken_counts_from_its_decision(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    rows = _plan(tmp_path, runs=[])
    assert len(rows) == 1
    # No reaffirmations, so the moment is the decision's own -- "how much has happened since anyone last looked".
    assert rows[0]["last_retaken"] == ""
    assert rows[0]["walks_since"] == 0


def test_a_re_taken_file_names_the_newest_confirmation(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    from brian.evolve import load_settlements, reaffirm_link
    from brian.promote import history

    record, successor = rows_origin(tmp_path)
    first = _ago(9)
    second = _ago(2)
    reaffirm_link(tmp_path, record, successor, why="read the books", by="alice", now=first)
    reaffirm_link(tmp_path, record, successor, why="read them again", by="alice", now=second)
    plan = reaffirm_plan(history(tmp_path, include_retired=True), load_settlements(tmp_path), [])
    assert plan[0]["last_retaken"] == second.isoformat()
    assert plan[0]["retaken"] == 1


def test_walks_since_counts_only_the_walks_after_the_confirmation(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    from brian.evolve import load_settlements, reaffirm_link
    from brian.promote import history

    record, successor = rows_origin(tmp_path)
    # `_ago` is a function, not a constant: bind each moment once so the stored value and the assertion agree.
    early, middle, late = _ago(8), _ago(7), _ago(3)
    confirmed = _ago(5)
    _walk(tmp_path, at=early)
    _walk(tmp_path, at=middle)
    reaffirm_link(tmp_path, record, successor, why="read the books", by="alice", now=confirmed)
    _walk(tmp_path, at=late)
    plan = reaffirm_plan(history(tmp_path, include_retired=True), load_settlements(tmp_path),
                         [{"at": early.isoformat()}, {"at": middle.isoformat()}, {"at": late.isoformat()}])
    # Two walks happened before the re-take and one after: only the one after is "since".
    assert plan[0]["last_retaken"] == confirmed.isoformat()
    assert plan[0]["walks_since"] == 1


def test_no_walk_store_means_unknown_not_zero(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    plan = _plan(tmp_path, runs=None)
    # A count nobody computed must print as unknown; `0 walk(s) since` would be a measurement (boundary 280).
    assert plan[0]["walks_since"] is None


def rows_origin(root):
    """The settled pair the fixtures build: the retiring record and the successor it names."""
    from brian.evolve import load_settlements

    item = load_settlements(root)[0]
    return str(item["record"]), str(item["successor"])


def test_the_campaign_page_prints_when_the_file_was_last_looked_at(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    page = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "never re-taken" in page


def test_the_json_carries_the_timeline(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    row = campaign(tmp_path).to_dict()["reaffirm_plan"][0]
    assert "last_retaken" in row and "walks_since" in row
    assert row["walks_since"] == 0


def test_a_per_file_cap_charges_one_file_that_holds_the_debt(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    _later_claim(tmp_path)
    monkeypatch.chdir(tmp_path)
    # One stale decision on one file: a per-file cap of 0 is exceeded, while a total cap of 5 is not.
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved-per-file", "0"]) == 1
    printed = " ".join(capsys.readouterr().out.split())
    assert "decision(s) on one stream file rest on books that have moved, above the 0 asked for" in printed
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved", "5"]) == 0


def test_a_generous_per_file_cap_does_not_charge(tmp_path, monkeypatch):
    _settled(tmp_path)
    _later_claim(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-moved-per-file", "1"]) == 0


def test_the_per_file_cap_without_strict_is_reported(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--max-moved-per-file", "0"]) == 2
    assert "caps a --strict charge; without --strict nothing reads it" in capsys.readouterr().out
