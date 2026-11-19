"""Round 98: a verdict names the file it stood on, and the pages split the evidence by file.

Round 96 separated "measured on the repair" from "measured on the original"; round 97 said when a record came
back. What stayed folded together is *how many* verdicts came from each place: `stream_path_moves` says a check
stood somewhere other than the record's first file, but not which file, so after a round trip the page could
only repeat "1 check was taken after the repair" -- a count with no denominator and no location.

Two sources of truth, and they are not equal. A check written from this round on records `checked_path` itself.
An older check recorded only the move count, from which the file is deducible exactly (the k-th move's target) --
so it is reported as deduced, not as written (boundary 187). A check that can be placed neither way is counted as
unplaceable instead of being folded into a side (boundary 166).
"""

from __future__ import annotations

import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (evidence_split, load_promotions, repaired_paths, set_stream_path,
                           verification_evidence)

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_repaired_check import _repaired, _verify
from test_return_trip import OTHER, _repaired_then_back
from test_untested_answer import NOW

THIRD = "data/smd/third.json"


def _payload(*checks: dict, moves: tuple = ()) -> dict:
    history = [{"from": FILE, "to": OTHER, "at": NOW.isoformat(), "why": "moved"}]
    if len(moves) > 1:
        history.append({"from": OTHER, "to": THIRD, "at": NOW.isoformat(), "why": "moved again"})
    document = {"checks": list(checks[:-1]), "verification": checks[-1], "stream_path_history": history}
    return document


def test_a_check_that_records_its_path_is_a_written_citation():
    rows = verification_evidence(_payload({"checked_at": "t1", "verdict": "holds", "checked_path": OTHER}))
    assert rows == [{"at": "t1", "verdict": "holds", "path": OTHER, "source": "written"}]


def test_an_older_check_is_placed_by_the_move_it_recorded():
    # `stream_path_moves = 1` means "the record had moved once when this ran", i.e. the check stood on move 1's
    # target -- exact, but deduced from the record rather than written by the check itself.
    rows = verification_evidence(_payload({"checked_at": "t1", "verdict": "failed", "stream_path_moves": 1}))
    assert rows[0]["path"] == OTHER and rows[0]["source"] == "deduced"


def test_a_check_that_can_be_placed_neither_way_is_counted_as_unplaced():
    rows = verification_evidence(_payload({"checked_at": "t1", "verdict": "holds", "stream_path_moves": 9}))
    assert rows[0] == {"at": "t1", "verdict": "holds", "path": "", "source": ""}


def test_a_check_on_the_original_before_any_move_is_unplaced_not_original():
    # Nothing in that payload says which file was measured; assuming the original because no move is recorded
    # would turn a missing fact into evidence (boundary 166).
    rows = verification_evidence(_payload({"checked_at": "t1", "verdict": "holds"}))
    assert rows[0]["path"] == ""
    assert evidence_split(rows, FILE)["unplaced"] == 1


def test_the_split_names_the_files_the_evidence_stands_on():
    rows = [{"path": FILE, "source": "deduced"},
            {"path": OTHER, "source": "deduced"},
            {"path": THIRD, "source": "written"},
            {"path": "", "source": ""}]
    split = evidence_split(rows, FILE)
    assert split == {"original": 1, "elsewhere": 2, "unplaced": 1, "total": 4,
                     "elsewhere_files": [OTHER, THIRD]}


def test_a_check_written_now_records_the_file_it_stood_on(tmp_path, monkeypatch, capsys):
    _repaired(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=OTHER)
    capsys.readouterr()
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["checked_path"] == OTHER
    assert payload["verification"]["prior_evidence_paths"] == []


def test_the_second_check_records_what_the_record_held_before_it(tmp_path, monkeypatch, capsys):
    _repaired(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=OTHER)
    capsys.readouterr()
    # Point the record back at the original without rebuilding the fixture: a rebuild would recreate the
    # version file and wipe the check this test is about.
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    _verify(tmp_path, monkeypatch, path=FILE)
    capsys.readouterr()
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    prior = payload["verification"]["prior_evidence_paths"]
    # The check about to be written says where the evidence stood before it -- so a reader never has to
    # reconstruct the order from the file's keys. This prior check was itself written by this round, so its
    # citation is `written`; a check predating round 98 would be `deduced` (see the unit tests above).
    assert prior and prior[0]["path"] == OTHER and prior[0]["source"] == "written"


def test_a_record_with_no_verdicts_has_nothing_to_split(tmp_path):
    _repaired_then_back(tmp_path)
    row = repaired_paths(tmp_path)[0]
    assert row["returned"] is True
    assert row["evidence"]["total"] == 0
    page = format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": repaired_paths(tmp_path)}))
    # "0 verdict(s) on file" would be a line about nothing; the absence is the honest statement.
    assert "verdict(s) on file" not in page


def test_the_campaign_counts_verdicts_taken_on_the_substitute(tmp_path):
    _repaired_then_back(tmp_path, on_substitute=True)
    rows = repaired_paths(tmp_path)
    assert rows[0]["evidence"]["elsewhere"] == 1
    assert rows[0]["evidence"]["elsewhere_files"] == [OTHER]
    page = "\n".join(format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": rows})).split("\n"))
    assert ("v020 holds 1 verdict(s) on file: 0 on its original file, 1 on another file (%s), 0 it cannot place"
            % OTHER) in page


def test_a_record_with_no_moves_has_nothing_to_split(tmp_path):
    from test_repaired_check import _clean
    _clean(tmp_path)
    assert cli.format_history_paths(repaired_paths(tmp_path)) == []


def test_a_verdict_taken_after_a_third_move_is_placed_on_the_third_file(tmp_path):
    _repaired(tmp_path)
    set_stream_path(tmp_path, 20, THIRD, now=NOW)
    promotion = load_promotions(tmp_path)[0]
    assert promotion.path_moves == 2
    payload = json.loads(promotion.path.read_text(encoding="utf-8"))
    payload["verification"] = {"checked_at": NOW.isoformat(), "verdict": "holds", "stream_path_moves": 2}
    promotion.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    row = repaired_paths(tmp_path)[0]
    # `stream_path_moves = 2` means the check ran when the record had moved twice, i.e. it stood on the second
    # move's target -- the third file, not both of them.
    assert row["evidence"]["elsewhere_files"] == [THIRD]
