"""Round 163 (item 149): inventory the payload writers instead of remembering the rule.

Round 141 collected the settlement commands' eight `--json-out` writers into `_write_payload` and said "this is the
one way". Round 162 then found `--history`/`--campaign` still writing the file themselves, and a grep for the same
helper found **eight more** sites (state ceilings, `--unrecorded`, `--sensitivity`, `--withheld`, `--audit`, the run
summary, `evaluate`, `--summary-out`). A rule stated once per family is violated by every writer outside the family
(boundary 389) -- so the rule is now enforced by counting, and the count is a constant that fails when a new command
adds a payload without the helper.
"""

import json
from pathlib import Path

from brian import cli

SOURCE = Path(cli.__file__).read_bytes().decode("utf-8")

# Every place a command hands a payload to the caller. Adding a command with `--json-out` means adding a call here,
# so bump it in the same change -- the point of the constant is that forgetting is a test failure, not a review note.
EXPECTED_PAYLOAD_WRITERS = 22


def test_no_command_writes_a_payload_by_hand():
    """`_write_payload` creates parents, renames into place, prints `wrote`, and reports a failed write."""
    offenders = [
        line.strip() for line in SOURCE.splitlines()
        if "write_text(_json" in line or "Path(args.json_out).write_text" in line
        or "Path(args.summary_out).write_text" in line
    ]
    assert offenders == []


def test_the_number_of_payload_writers_is_counted():
    calls = [line for line in SOURCE.splitlines()
             if "_write_payload(" in line and "def _write_payload" not in line]
    assert len(calls) == EXPECTED_PAYLOAD_WRITERS, (
        "a command started or stopped writing a payload: %d call(s) now, %d expected"
        % (len(calls), EXPECTED_PAYLOAD_WRITERS))


def test_a_report_payload_says_when_it_could_not_be_written(tmp_path, monkeypatch, capsys):
    """The eight writers round 163 moved were all reports: none of them records anything."""
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit", "--json-out", "."]) == 2

    printed = capsys.readouterr().out
    assert "cannot write the payload to ." in printed
    assert "nothing was recorded: this command only reports" in printed


def test_a_report_payload_finds_its_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--audit", "--json-out", "deep/nested/a.json"]) == 0

    payload = json.loads((tmp_path / "deep" / "nested" / "a.json").read_text(encoding="utf-8"))
    assert "rules_hash" in payload and "moved" in payload
