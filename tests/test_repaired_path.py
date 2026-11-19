"""Round 94: a promise whose measured file was rewritten by hand is not an original record.

Round 93 added `--set-stream-path` to repair a record whose file is gone. The repair leaves the promise pointing at a
file it was not born with, and nothing printed that: `--history` listed versions, clearings and ceilings, and
`--campaign` counted broken links, backfilled ends and dead paths. So a repaired record read exactly like one
measured there all along -- the same class as round 83's `backfilled` end, one field over.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import cleared_summary, repaired_paths, set_stream_path
from test_clearing_evidence_file import FILE
from test_untested_answer import NOW, _copied

OTHER = "data/smd/other.json"
THIRD = "data/smd/third.json"


def _add_files(tmp_path: Path) -> None:
    for name in (OTHER, THIRD):
        (tmp_path / name).write_text("{}", encoding="utf-8", newline="\n")


def _payload(root: Path) -> dict:
    return json.loads((root / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))


def _record(root: Path) -> dict:
    return dict(cleared_summary([]), repaired_paths=repaired_paths(root))


def test_a_record_nobody_rewrote_is_not_listed(tmp_path):
    _copied(tmp_path)
    assert repaired_paths(tmp_path) == []


def test_a_repair_is_listed_with_the_move_it_made(tmp_path):
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    rows = repaired_paths(tmp_path)
    assert len(rows) == 1
    assert rows[0]["version"] == 20
    assert rows[0]["first_from"] == FILE
    assert rows[0]["to"] == OTHER
    assert rows[0]["at"] == NOW.isoformat()
    assert rows[0]["in_force"] is True          # the fixture deploys v020
    assert len(rows[0]["moves"]) == 1


def test_every_move_is_kept_so_a_single_pair_cannot_be_mistaken_for_the_chain(tmp_path):
    """One `from` cannot tell 'moved here once' from 'moved three times, ending here'."""
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    set_stream_path(tmp_path, 20, THIRD, now=NOW)
    rows = repaired_paths(tmp_path)
    assert len(rows[0]["moves"]) == 2
    assert [move["to"] for move in rows[0]["moves"]] == [OTHER, THIRD]
    assert rows[0]["first_from"] == FILE        # the file the promise was actually measured on
    assert rows[0]["to"] == THIRD


def test_the_history_page_prints_the_moves(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--history"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "stream paths     1 record(s) have had their measured file rewritten by hand:" in printed
    assert "v020  %s -> %s  (1 move(s), last %s)  [in force]" % (FILE, OTHER, NOW.isoformat()) in printed


def test_the_history_page_says_nothing_when_nothing_moved(tmp_path, monkeypatch, capsys):
    _copied(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--history"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "rewritten by hand" not in printed


def test_the_history_json_carries_the_moves(tmp_path, monkeypatch):
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "var" / "history.json"
    assert cli.main(["evolve", "--history", "--json-out", str(out)]) == 0
    payload = json.loads(io.open(out, encoding="utf-8").read())
    assert payload["stream_path_moves"][0]["to"] == OTHER
    assert payload["stream_path_moves"][0]["moves"][0]["from"] == FILE


def test_the_campaign_counts_a_repaired_record_apart(tmp_path):
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    page = format_campaign(campaign(tmp_path, in_force_history=_record(tmp_path)))
    assert ("1 record(s) have a rewritten stream path: v020 moved 1 time(s) -- the location is a repair, not "
            "the original measurement") in page
    assert "`brian evolve --history` prints each move" in page


def test_the_campaign_counts_the_moves_not_the_versions(tmp_path):
    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    set_stream_path(tmp_path, 20, THIRD, now=NOW)
    page = format_campaign(campaign(tmp_path, in_force_history=_record(tmp_path)))
    assert "v020 moved 2 time(s)" in page


def test_the_campaign_says_nothing_when_no_record_was_rewritten(tmp_path):
    _copied(tmp_path)
    page = format_campaign(campaign(tmp_path, in_force_history=_record(tmp_path)))
    assert "rewritten stream path" not in page


def test_a_repaired_record_is_not_reported_as_a_dead_path(tmp_path):
    """The two facts are different: a dead path names a file nobody can open, a repair says the location changed."""
    from brian.promote import dead_record_paths

    _copied(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    assert dead_record_paths(tmp_path) == []
    assert len(repaired_paths(tmp_path)) == 1
