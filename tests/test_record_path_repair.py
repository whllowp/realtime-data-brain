"""Round 93: a promise that names a file nobody can open, and the command that fixes it.

Round 92 taught `--verify` to run on the recorded file when the one it was asked for is gone, and the pages to mark
a clearing whose evidence has aged out. Neither could change the record, so a promise could keep naming a file this
checkout does not have: every check then substitutes or refuses, and the record never learns. This round lists those
promises against the checkout (computed, never stored), names the repair beside them, and adds the repair itself --
after checking that the new path points at a file a reader can open, and keeping the old value in
`stream_path_history` so the move survives in the promise rather than in a log line.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (cleared_summary, dead_record_paths, in_force_history, record_clearing,
                           set_stream_path, stream_path_history)
from test_clearing_evidence_file import FILE
from test_untested_answer import NOW, _copied, _observed

MISSING = "data/smd/missing.json"
OTHER = "data/smd/other.json"


def _substituted(root: Path, *, named: str = MISSING, path: str = FILE) -> list:
    return record_clearing(root, 20, now=NOW, verdict="failed", reason="measured again by --verify",
                           stream_path=path, substituted_from=named)


def _version_payload(root: Path) -> dict:
    return json.loads((root / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))


def test_a_substitution_is_counted_by_the_path_that_was_asked_for(tmp_path):
    """Substituting is the caller naming a file the record knows better; it is a fact, not a defect."""
    _copied(tmp_path)
    for _index in range(2):
        _observed(tmp_path)
        _substituted(tmp_path)
    entries, trimmed = in_force_history(tmp_path)
    summary = cleared_summary(entries, trimmed=trimmed)
    assert summary["substitutions"] == {MISSING: 2}
    assert summary["by_file"] == {FILE: 2}          # the file that answered, counted apart
    assert "stale_paths" not in summary             # the object recut: a record's path, not a caller's habit


def test_a_record_that_names_a_file_that_is_there_is_not_listed(tmp_path):
    _copied(tmp_path)
    assert dead_record_paths(tmp_path) == []


def _dead_record(tmp_path: Path) -> None:
    """Make v020 name a file this checkout does not have (the fixture otherwise names an existing one)."""
    payload = _version_payload(tmp_path)
    payload["stream_path"] = "data/smd/stream-real.json"
    (tmp_path / "var" / "evolve" / "versions" / "v020.json").write_text(
        json.dumps(payload), encoding="utf-8", newline="\n")


def _withdraw_the_deployment(tmp_path: Path) -> None:
    """The fixture deploys v020; a promise nobody runs is housekeeping, so the state's sources are emptied."""
    state = tmp_path / "var" / "evolve" / "domains" / "smd" / "current.json"
    body = json.loads(state.read_text(encoding="utf-8"))
    body["sources"] = {}
    state.write_text(json.dumps(body), encoding="utf-8", newline="\n")


def test_a_record_that_names_a_file_this_checkout_lacks_is_listed(tmp_path):
    _copied(tmp_path)
    _dead_record(tmp_path)
    assert dead_record_paths(tmp_path) == [{"version": 20, "stream_path": "data/smd/stream-real.json",
                                            "in_force": True}]        # the fixture's v020 is deployed


def test_a_promise_that_is_not_deployed_is_listed_as_such(tmp_path):
    """A dead path on a promise nobody runs is housekeeping; on a live one it is a check that cannot be repeated."""
    _copied(tmp_path)
    _dead_record(tmp_path)
    _withdraw_the_deployment(tmp_path)
    assert dead_record_paths(tmp_path)[0]["in_force"] is False


def test_a_deployed_promise_is_marked_as_such(tmp_path):
    _copied(tmp_path)
    _dead_record(tmp_path)
    record = dict(cleared_summary([]), dead_record_paths=dead_record_paths(tmp_path))
    page = format_campaign(campaign(tmp_path, in_force_history=record))
    assert "data/smd/stream-real.json (in force), which this checkout cannot open" in page


def test_the_campaign_names_the_dead_path_and_the_repair(tmp_path):
    _copied(tmp_path)
    _dead_record(tmp_path)
    _withdraw_the_deployment(tmp_path)
    record = dict(cleared_summary([]), dead_record_paths=dead_record_paths(tmp_path))
    page = format_campaign(campaign(tmp_path, in_force_history=record))
    assert ("v020 records data/smd/stream-real.json, which this checkout cannot open: fix the record rather "
            "than the check -- brian evolve --set-stream-path v020=<a file that exists>") in page


def test_set_stream_path_records_the_move(tmp_path):
    _copied(tmp_path)
    (tmp_path / OTHER).write_text("{}", encoding="utf-8", newline="\n")
    change = set_stream_path(tmp_path, 20, OTHER, now=NOW)
    assert change == {"version": 20, "from": FILE, "to": OTHER, "changed": True}
    payload = _version_payload(tmp_path)
    assert payload["stream_path"] == OTHER
    assert payload["stream_path_history"] == [{"from": FILE, "to": OTHER, "at": NOW.isoformat(),
                                               "why": "the file the record named does not exist in this checkout"}]
    assert stream_path_history(tmp_path, 20)[0]["to"] == OTHER


def test_the_repair_clears_the_dead_path_it_fixed(tmp_path):
    """The list is computed against the checkout, so the repair shows up immediately -- nothing to re-record."""
    _copied(tmp_path)
    _dead_record(tmp_path)
    assert len(dead_record_paths(tmp_path)) == 1
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    assert dead_record_paths(tmp_path) == []
    assert stream_path_history(tmp_path, 20)[0]["from"] == "data/smd/stream-real.json"


def test_set_stream_path_refuses_a_file_that_is_not_there(tmp_path):
    """A repair that points at nothing fixes the paperwork and loses the evidence (boundary 195)."""
    _copied(tmp_path)
    try:
        set_stream_path(tmp_path, 20, "data/smd/gone.json", now=NOW)
    except ValueError as error:
        assert "a repair must point at something a reader can open" in str(error)
    else:                                                  # pragma: no cover - the assertion is the point
        raise AssertionError("set_stream_path accepted a file that does not exist")
    assert _version_payload(tmp_path)["stream_path"] == FILE     # the record is untouched


def test_set_stream_path_refuses_an_empty_path(tmp_path):
    _copied(tmp_path)
    try:
        set_stream_path(tmp_path, 20, "   ", now=NOW)
    except ValueError as error:
        assert "would say less than it does now" in str(error)
    else:                                                  # pragma: no cover
        raise AssertionError("set_stream_path accepted an empty path")


def test_set_stream_path_says_nothing_changed_the_second_time(tmp_path):
    _copied(tmp_path)
    (tmp_path / OTHER).write_text("{}", encoding="utf-8", newline="\n")
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    again = set_stream_path(tmp_path, 20, OTHER, now=NOW)
    assert again["changed"] is False
    assert len(stream_path_history(tmp_path, 20)) == 1      # one move, recorded once


def test_set_stream_path_needs_a_version_record(tmp_path):
    try:
        set_stream_path(tmp_path, 77, FILE, now=NOW)
    except ValueError as error:
        assert "no version record for v077" in str(error)
    else:                                                  # pragma: no cover
        raise AssertionError("set_stream_path accepted a version that does not exist")


def test_stream_path_history_is_empty_for_a_version_this_build_never_moved(tmp_path):
    _copied(tmp_path)
    assert stream_path_history(tmp_path, 20) == []


def test_the_command_reports_the_move_and_writes_json(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    (tmp_path / OTHER).write_text("{}", encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "var" / "fix.json"
    code = cli.main(["evolve", "--set-stream-path", "v020=%s" % OTHER, "--json-out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "stream path     v020  %s -> %s" % (FILE, OTHER) in printed
    assert "Nothing was re-measured by this call." in printed
    assert json.loads(io.open(out, encoding="utf-8").read())["stream_path"]["to"] == OTHER


def test_the_command_refuses_a_pair_it_cannot_read(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--set-stream-path", "v020"])
    printed = capsys.readouterr().out
    assert code == 2
    assert "--set-stream-path takes VERSION=FILE" in printed


def test_the_command_refuses_a_file_that_is_not_there(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--set-stream-path", "v020=data/smd/gone.json"])
    printed = capsys.readouterr().out
    assert code == 2
    assert "a repair must point at something a reader can open" in printed
