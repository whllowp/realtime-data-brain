"""Round 103: a queue run is an event, and the events page must not read like a state.

`--from-queue` measured promises on the origins their records were born with and then forgot that it had run. So a
campaign page could say which promises still owe a check where they were made and never when anyone last walked
the queue -- a ledger that had never been queued and one queued an hour ago read the same. The second half is the
exit code: exit 2 means the books for that record contradict each other, which is not a defect the next promise in
the queue measures away, so the walk stops and names what it left.

These tests pin the record's own numbers, the stop, and the two campaign sentences.
"""

from __future__ import annotations

import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import QUEUE_RUN_LIMIT, last_queue_run, queue_runs, record_queue_run

from test_evidence_paths import THIRD
from test_from_queue import _elsewhere_only, _run, _second_record
from test_untested_answer import NOW


def test_a_tree_nobody_queued_has_no_run_to_report(tmp_path):
    assert queue_runs(tmp_path)["runs"] == []
    assert last_queue_run(tmp_path) is None


def test_a_run_records_its_own_numbers(tmp_path):
    entry = record_queue_run(tmp_path, {"limit": 2, "runnable": 3, "measured": 2, "skipped": 1,
                                        "skipped_origins": [THIRD], "cleared": 1, "worst": 1,
                                        "stopped_at": 0}, now=NOW)
    assert entry["at"] == NOW.isoformat()
    assert entry["measured"] == 2 and entry["runnable"] == 3 and entry["skipped"] == 1
    # Measuring a promise and answering a dispute are two facts: a check can run and settle nothing.
    assert entry["cleared"] == 1
    stored = last_queue_run(tmp_path)
    assert stored["skipped_origins"] == [THIRD]
    assert json.loads((tmp_path / "var" / "evolve" / "queue_runs.json").read_text(encoding="utf-8"))[
        "format"] == "brian.queue/1"


def test_the_newest_run_is_the_one_the_page_reads(tmp_path):
    record_queue_run(tmp_path, {"measured": 1}, now=NOW)
    record_queue_run(tmp_path, {"measured": 2}, now=NOW)
    assert [run["measured"] for run in queue_runs(tmp_path)["runs"]] == [1, 2]
    assert last_queue_run(tmp_path)["measured"] == 2


def test_the_store_keeps_a_window_and_counts_what_it_dropped(tmp_path):
    for index in range(QUEUE_RUN_LIMIT + 2):
        record_queue_run(tmp_path, {"measured": index}, now=NOW)
    held = queue_runs(tmp_path)
    assert len(held["runs"]) == QUEUE_RUN_LIMIT
    assert held["runs"][-1]["measured"] == QUEUE_RUN_LIMIT + 1
    # The campaign can say "the last run" without claiming it is the whole history.
    assert held["trimmed"] == 2


def test_a_corrupt_store_reads_as_empty_rather_than_raising(tmp_path):
    path = tmp_path / "var" / "evolve" / "queue_runs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert queue_runs(tmp_path)["runs"] == []
    assert last_queue_run(tmp_path) is None


def test_the_campaign_says_when_the_queue_last_ran(tmp_path):
    result = campaign(tmp_path, queue_run={"at": NOW.isoformat(), "limit": 1, "runnable": 2, "measured": 1,
                                           "skipped": 1, "cleared": 1, "worst": 1, "stopped_at": 0})
    page = format_campaign(result)
    assert ("the last queue run was %s: measured 1 of 2 runnable promise(s) on their origins, left 1 out, and 1 "
            "of those measurements left a clearing" % NOW.isoformat()) in page
    assert result.to_dict()["queue_run"]["measured"] == 1


def test_a_run_that_settled_nothing_does_not_claim_a_clearing(tmp_path):
    result = campaign(tmp_path, queue_run={"at": NOW.isoformat(), "runnable": 1, "measured": 1, "worst": 1})
    page = format_campaign(result)
    assert "left 0 out" in page
    assert "left a clearing" not in page


def test_a_run_that_stopped_says_why(tmp_path):
    result = campaign(tmp_path, queue_run={"at": NOW.isoformat(), "runnable": 2, "measured": 1, "worst": 2,
                                           "stopped_at": 20})
    page = format_campaign(result)
    assert ("it stopped at v020, which exited 2: a build defect is not something the next promise in the queue "
            "measures away") in page


def test_a_campaign_without_a_run_does_not_invent_one(tmp_path):
    assert "the last queue run" not in format_campaign(campaign(tmp_path))


def test_a_queue_stops_at_the_first_contradiction_and_names_what_it_left(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    original = cli.cmd_evolve
    seen: list = []

    def contradiction_first(args, settings):
        if int(getattr(args, "from_queue", 0) or 0):
            return original(args, settings)
        seen.append(int(getattr(args, "verify_version", 0) or 0))
        return 2

    monkeypatch.setattr(cli, "cmd_evolve", contradiction_first)
    code = _run(tmp_path, monkeypatch, "--from-queue", "2")
    out = capsys.readouterr().out
    assert code == 2
    # The second promise was never measured: a broken ledger is not measured away by walking further.
    assert seen == [20]
    assert "stopped          v020 exited 2" in out
    assert "1 promise(s) were left in the queue: v021" in out
    stored = last_queue_run(tmp_path)
    assert stored["measured"] == 1
    assert stored["stopped_at"] == 20
    assert stored["worst"] == 2
    assert stored["runnable"] == 2


def test_the_run_record_says_which_origins_were_left_out(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    (tmp_path / THIRD).unlink()
    _run(tmp_path, monkeypatch, "--from-queue", "2")
    stored = last_queue_run(tmp_path)
    assert stored["skipped"] == 1
    assert stored["skipped_origins"] == [THIRD]
    assert stored["measured"] == 1


def test_a_finished_walk_records_the_versions_it_measured(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    _run(tmp_path, monkeypatch, "--from-queue", "1")
    stored = last_queue_run(tmp_path)
    assert stored["measured"] == 1
    assert stored["stopped_at"] == 0
    assert stored["limit"] == 1


def test_the_queue_flag_refuses_to_be_ignored_without_the_mode_that_reads_it(tmp_path, monkeypatch, capsys):
    """A queue walk that is not requested as a check silently measures a round instead (found by running it)."""
    _elsewhere_only(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    code = cli.main(["evolve", "--from-queue", "1", "--accept-copied"])
    out = capsys.readouterr().out
    assert code == 2
    assert "--from-queue walks the origin queue through --verify" in out
    assert last_queue_run(tmp_path) is None

