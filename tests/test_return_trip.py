"""Round 97: a move back to the original file is a return, not another repair.

Round 96 taught the pages that the original file can come back and named the command that moves the record
back. What it could not say is what happened once the operator ran it: `--set-stream-path` recorded the trip
back as one more move, so `--history` printed a second "move" and `--campaign` still described the record as
repaired. Worse, a record pointed back at its original file reads as if the promise had been answered there
again, while the evidence it holds was almost all taken on the substitute (boundary 210: the same word means
different things in the two places).

So the command writes its own `why` on a return, the history reads the whole trip as one line, and the campaign
separates "the location is restored" from "the evidence is not" (boundary 211).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import load_promotions, repaired_paths, set_stream_path, write_verification

from test_clearing_evidence_file import FILE
from test_repaired_check import OTHER, _clean, _repaired, _add_files
from test_untested_answer import NOW

THIRD = "data/smd/third.json"


def _repaired_then_back(root: Path, *, checked: bool = False, on_substitute: bool = False) -> None:
    """A record repaired away from `FILE`, then pointed back at it once the file returned."""
    _clean(root)
    _add_files(root)
    set_stream_path(root, 20, OTHER, now=NOW)
    set_stream_path(root, 20, FILE, now=NOW + timedelta(days=1))
    if on_substitute:
        promotion = load_promotions(root)[0]
        write_verification(root, promotion,
                           {"checked_at": (NOW + timedelta(days=1, hours=1)).isoformat(),
                            "verdict": "failed", "reason": "recall fell", "stream_path_moves": 1,
                            "checked_path": OTHER, "on_original_path": False})
    if checked:
        promotion = load_promotions(root)[0]
        # `stream_path_moves` is the number of moves the record had when the check ran, which is 2 once the trip
        # back has been written -- and `checked_path` is the file itself, so the two agree (round 98).
        write_verification(root, promotion,
                           {"checked_at": (NOW + timedelta(days=2)).isoformat(),
                            "verdict": "holds", "reason": "measured again", "stream_path_moves": 2,
                            "checked_path": FILE, "on_original_path": True})


def _history(root: Path) -> str:
    return "\n".join(cli.format_history_paths(repaired_paths(root)))


def _campaign(root: Path) -> str:
    return format_campaign(campaign(root, in_force_history={"repaired_paths": repaired_paths(root)}))


def test_a_move_back_to_the_original_is_recorded_as_a_return(tmp_path):
    _clean(tmp_path)
    _add_files(tmp_path)
    set_stream_path(tmp_path, 20, OTHER, now=NOW)
    result = set_stream_path(tmp_path, 20, FILE, now=NOW + timedelta(days=1))
    assert result["returned"] is True and result["changed"] is True
    payload = json.loads((tmp_path / "var/evolve/versions/v020.json").read_text(encoding="utf-8"))
    entry = payload["stream_path_history"][-1]
    assert entry["return"] is True and entry["to"] == FILE
    # The command knows the trip's shape, so it writes the reason itself rather than trusting the caller to.
    assert "returns the promise to it" in entry["why"]


def test_a_first_move_is_not_a_return(tmp_path):
    _clean(tmp_path)
    _add_files(tmp_path)
    result = set_stream_path(tmp_path, 20, OTHER, now=NOW)
    assert result["returned"] is False
    payload = json.loads((tmp_path / "var/evolve/versions/v020.json").read_text(encoding="utf-8"))
    assert "return" not in payload["stream_path_history"][-1]


def test_a_move_to_a_third_file_is_not_a_return(tmp_path):
    _repaired(tmp_path)
    result = set_stream_path(tmp_path, 20, THIRD, now=NOW + timedelta(days=1))
    assert result["returned"] is False


def test_the_record_points_at_the_original_again(tmp_path):
    _repaired_then_back(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    assert promotion.path_moves == 2
    assert promotion.path_current == FILE and promotion.path_origin == FILE


def test_the_history_reads_the_whole_trip_as_one_line(tmp_path):
    _repaired_then_back(tmp_path)
    page = _history(tmp_path)
    assert "%s -> %s -> %s" % (FILE, OTHER, FILE) in page
    assert "round trip: 2 move(s), returned %s" % (NOW + timedelta(days=1)).isoformat() in page
    # Once returned, "the original file is back" is no longer a fact about the future: it is what the record says.
    assert "the original file is back" not in page


def test_a_returned_record_is_not_described_as_a_repair(tmp_path):
    _repaired_then_back(tmp_path)
    page = _campaign(tmp_path)
    assert "and are back on the file the promise was made on: v020 (round trip of 2 move(s))" in page
    # The page must not call a closed trip a repair in the same breath: it argued with itself in the first
    # version of this line (round 60's rule).
    assert "the location is a repair, not the original measurement" not in page


def test_the_check_after_the_return_says_where_it_stands(tmp_path, monkeypatch, capsys):
    from test_repaired_check import _verify
    from test_clearing_recorded import _measurement

    _repaired_then_back(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    code = _verify(tmp_path, monkeypatch, path=FILE)
    out = capsys.readouterr().out
    assert code in (0, 1), out
    # Round 97: "the record still points at the repair" is false once the trip is closed, and printing
    # "origin -> origin" for the move is not a description of anything.
    assert ("path rewritten 2 time(s) by hand and back on the original: %s -> %s -> %s" % (FILE, OTHER, FILE)) in out
    assert "the verdicts it already holds were taken on the substitute (boundary 212)" in out
    assert "the record still points at the repair" not in out


def test_the_campaign_separates_the_location_from_the_evidence(tmp_path):
    _repaired_then_back(tmp_path)
    page = _campaign(tmp_path)
    assert ("v020's record is back on its original file %s, but no check has stood there since the return: the "
            "location is restored, the evidence is not -- brian evolve --verify --verify-version 020 --stream-file %s"
            % (FILE, FILE)) in page


def test_the_campaign_says_when_a_check_stood_there_since(tmp_path):
    _repaired_then_back(tmp_path, checked=True)
    page = _campaign(tmp_path)
    assert "is back on its original file" in page
    # Round 99: this one check is also the first verdict ever taken where the promise was made, and saying
    # "another check since the return" would hide that the promise had never been tested there before.
    assert "this was the first verdict ever taken there (%s)" % (NOW + timedelta(days=2)).isoformat() in page
    assert "the evidence is not" not in page


def test_a_second_check_after_the_return_is_not_called_the_first(tmp_path):
    from brian.promote import write_verification

    _repaired_then_back(tmp_path, checked=True)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion,
                       {"checked_at": (NOW + timedelta(days=3)).isoformat(), "verdict": "failed",
                        "reason": "recall fell", "stream_path_moves": 2, "on_original_path": True,
                        "checked_path": FILE})
    page = _campaign(tmp_path)
    assert "2 verdict(s) have been taken there since the return (latest %s)" % (
        NOW + timedelta(days=3)).isoformat() in page
    assert "first verdict ever taken there" not in page


def test_a_check_taken_on_the_substitute_is_still_counted_apart(tmp_path):
    _repaired_then_back(tmp_path, on_substitute=True)
    rows = repaired_paths(tmp_path)
    assert rows[0]["returned"] is True
    assert rows[0]["checks_on_repair"] == 1
    assert rows[0]["checked_after_return"] == ""
    page = _campaign(tmp_path)
    assert "1 check(s) were taken after the repair" in page


def test_a_clean_record_is_not_a_round_trip(tmp_path):
    _clean(tmp_path)
    assert cli.format_history_paths(repaired_paths(tmp_path)) == []


def test_the_cli_records_the_return(tmp_path, monkeypatch):
    _repaired(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    target = tmp_path / "var/evolve/report.json"
    code = cli.main(["evolve", "--set-stream-path", "v020=%s" % FILE, "--json-out", str(target)])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["stream_path"]["returned"] is True
    promotion = load_promotions(tmp_path)[0]
    assert promotion.path_current == FILE
