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


# Round 164 (item 150): the count above keeps the rule honest for whoever runs the suite; the page keeps it honest
# for the reader. They have to be the same inventory, or the test proves a number about a table nobody can see.


def test_the_page_and_the_count_are_the_same_inventory():
    calls = [line for line in SOURCE.splitlines()
             if "_write_payload(" in line and "def _write_payload" not in line]
    assert len(cli.PAYLOAD_WRITERS) == EXPECTED_PAYLOAD_WRITERS
    assert len(cli.PAYLOAD_WRITERS) == len(calls), (
        "the page lists %d writer(s) and the source has %d: the table must move with the calls"
        % (len(cli.PAYLOAD_WRITERS), len(calls)))


def test_the_table_names_every_previewable_batch_command():
    """A family member that `--dry-run` accepts must be on the page: as a row, a file-level form, or a named gap."""
    named = " ".join(command for command, _payload, _kind in cli.PAYLOAD_WRITERS)
    named += " " + " ".join(cli.PAYLOAD_BATCH_FORMS) + " " + " ".join(cli.PAYLOAD_NONE)
    missing = [flag for flag, _attribute in cli.BATCH_PREVIEWS if flag not in named]
    assert missing == [], "the inventory does not name %s" % missing


def test_the_page_names_the_batch_command_that_exports_nothing():
    """Writing the table is what found it: `--backfill-walks` has no `--json-out`, unlike `--backfill-claims`."""
    printed = cli.format_payload_writers()

    assert cli.PAYLOAD_NONE == ("--backfill-walks",)
    assert "--backfill-walks takes --dry-run and exports nothing" in printed
    assert "script it the way it can script --backfill-claims" in printed


def test_every_row_says_whether_the_command_records_something():
    """`recorded=` decides which of the two failure sentences is true (boundaries 390/393)."""
    kinds = {kind for _command, _payload, kind in cli.PAYLOAD_WRITERS}
    assert kinds == {"records", "reports", "records, unless --dry-run"}
    assert sum(1 for _c, _p, kind in cli.PAYLOAD_WRITERS if kind == "reports") == 7


def test_the_page_says_what_the_writer_says_when_it_cannot_write():
    printed = cli.format_payload_writers()

    assert "brian payload writers: %d command(s), one writer" % len(cli.PAYLOAD_WRITERS) in printed
    assert "what this command recorded is already in the store: only the payload is missing" in printed
    assert "nothing was recorded: this command only reports, so only its export is missing" in printed
    assert "wrote <path>" in printed
    for boundary in ("336", "390", "392", "394", "395"):
        assert "boundar" in printed and boundary in printed


def test_the_page_needs_no_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--payload-writers"]) == 0

    printed = capsys.readouterr().out
    assert "brian payload writers: 22 command(s), one writer" in printed
    assert "--settle-reaffirm" in printed
    assert not (tmp_path / "var").exists(), "the page is a report: it must not create a book"


def test_a_preview_of_a_repairing_command_says_it_recorded_nothing(tmp_path, monkeypatch, capsys):
    """Round 164: a dry run of a repairing command records nothing, so the *report* sentence is the true one."""
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--claim-round-file", "nope.json=data/smd/stream-one.json",
                     "--dry-run", "--json-out", "."]) == 2

    printed = capsys.readouterr().out
    assert "nothing was written; drop --dry-run to record the claim" in printed
    assert "nothing was recorded: this command only reports" in printed
    assert "what this command recorded is already in the store" not in printed
