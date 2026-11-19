"""Round 102: a run through the queue leaves a mark, and the queue can be counted before it runs.

Round 101 made the queue executable. That created a new fact the store could not express: was this clearing earned by
a command that walked the queue, or by someone who copied a version label and a path off a page? The verdict is the
same either way; what differs is whether anyone retyped an identifier, which is the habit the queue exists to remove
(boundary 221).

So a clearing records `queued`, `--history` marks it, `--campaign` counts the two apart (with "written before this
round" as its own state), and `--from-queue 0` prints the plan without measuring anything.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import cleared_summary, in_force_history, record_clearing, set_stream_path

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_from_queue import _second_record
from test_repaired_check import OTHER, _add_files
from test_untested_answer import NOW, _copied, _observed


def _queued_record(root: Path) -> None:
    """A deployed record whose measured file was moved, with an observation to clear: it owes a check at its origin."""
    _copied(root)
    _add_files(root)
    set_stream_path(root, 20, OTHER, now=NOW)
    _observed(root)


def _run(tmp_path, monkeypatch, *extra):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    return cli.main(["evolve", "--verify"] + list(extra))


def test_a_clearing_records_how_the_check_was_started(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    queued = record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE, queued=True)[0]
    # A clearing removes the observation it answered (round 90), so re-observe before the second check.
    _observed(tmp_path)
    typed = record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE)[0]
    assert queued["queued"] is True
    # The default is "a command was typed", not "unknown": a run that goes through this function without the flag
    # is a caller who named the version and the file itself.
    assert typed["queued"] is False


def test_the_summary_counts_the_three_states_apart():
    entries = [
        {"by": "measurement", "queued": True, "domain": "smd"},
        {"by": "measurement", "queued": False, "domain": "smd"},
        {"by": "measurement", "domain": "smd"},  # written before round 102: the field is absent
    ]
    summary = cleared_summary(entries)
    assert summary["queued"] == 1
    assert summary["unstated_queue"] == 1
    assert summary["total"] - summary["queued"] - summary["unstated_queue"] == 1


def test_the_history_marks_a_clearing_the_queue_produced(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE, queued=True)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    out = capsys.readouterr().out
    assert "[from the queue]" in out


def test_an_ordinary_clearing_carries_no_such_mark(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE)
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_evolve(argparse.Namespace(history=True), None) == 0
    out = capsys.readouterr().out
    assert "clearings" in out and "[from the queue]" not in out


def _campaign_page(tmp_path) -> str:
    entries = in_force_history(tmp_path)[0]
    return format_campaign(campaign(tmp_path, in_force_history=cleared_summary(entries)))


def test_the_campaign_counts_the_queue_and_the_typed_command_apart(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE, queued=True)
    page = _campaign_page(tmp_path)
    assert "1 clearing(s) were produced by the queue, 0 by a hand-typed command" in page


def test_the_campaign_names_the_records_written_before_it_records_that(tmp_path):
    _copied(tmp_path)
    _observed(tmp_path)
    path = tmp_path / "var" / "evolve" / "in_force.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cleared"] = [{"by": "measurement", "age_days": 1.0, "domain": "smd", "tested": True}]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    page = _campaign_page(tmp_path)
    assert "0 clearing(s) were produced by the queue, 0 by a hand-typed command" in page
    assert "1 were written before the store recorded it" in page


def test_the_campaign_says_zero_rather_than_nothing_when_no_clearing_came_from_the_queue(tmp_path):
    """A missing line reads as "no such fact", not as "none came from the queue" (boundary 162)."""
    _copied(tmp_path)
    _observed(tmp_path)
    record_clearing(tmp_path, 20, now=NOW, verdict="holds", stream_path=FILE)
    page = _campaign_page(tmp_path)
    assert "0 clearing(s) were produced by the queue, 1 by a hand-typed command" in page


def test_from_queue_zero_lists_the_queue_and_measures_nothing(tmp_path, monkeypatch, capsys):
    _queued_record(tmp_path)
    original = cli.cmd_evolve
    inner: list = []

    def recorder(args, settings):
        if getattr(args, "from_queue", None) is not None:
            return original(args, settings)
        inner.append(args)
        return 1

    monkeypatch.setattr(cli, "cmd_evolve", recorder)
    code = _run(tmp_path, monkeypatch, "--from-queue", "0")
    out = capsys.readouterr().out
    assert code == 0
    assert inner == []
    assert "--from-queue 0 only lists them" in out
    assert "v020  <-  %s  (never measured where this promise was made)" % FILE in out
    assert "nothing was measured" in out


def test_from_queue_zero_counts_what_it_cannot_reach(tmp_path, monkeypatch, capsys):
    _queued_record(tmp_path)
    _second_record(tmp_path)
    (tmp_path / "data/smd/third.json").unlink()
    code = _run(tmp_path, monkeypatch, "--from-queue", "0")
    out = capsys.readouterr().out
    assert code == 0
    assert "1 of 1 runnable promise(s)" in out
    assert "1 left out: the file they were born with is not in this checkout" in out


def test_a_queued_run_writes_a_clearing_that_says_so(tmp_path, monkeypatch, capsys):
    _queued_record(tmp_path)
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--accept-copied")
    assert code in (0, 1)
    store = json.loads((tmp_path / "var" / "evolve" / "in_force.json").read_text(encoding="utf-8"))
    cleared = store["cleared"]
    assert cleared, "the queued run measured a deployed record with an open observation"
    assert cleared[-1]["queued"] is True
    assert "[from the queue]" in capsys.readouterr().out or cleared[-1]["queued"] is True


def test_a_typed_run_writes_a_clearing_that_says_that(tmp_path, monkeypatch):
    _queued_record(tmp_path)
    code = _run(tmp_path, monkeypatch, "--verify-version", "20", "--stream-file", FILE, "--accept-copied")
    assert code in (0, 1)
    store = json.loads((tmp_path / "var" / "evolve" / "in_force.json").read_text(encoding="utf-8"))
    assert store["cleared"]
    assert store["cleared"][-1]["queued"] is False
