"""Round 96: a repair can stop being necessary, and the pages have to say so.

Rounds 93-95 made a hand repair visible, recorded and cited. The missing fact is the one that makes it
actionable: when the file the promise was originally measured on is back in this checkout, the record still
points at the substitute, and a reader who is told only "the path was rewritten" cannot tell whether the repair
is still needed. So `repaired_paths` reports `origin_exists`, the pages name the record, the file and the
command that moves it back, and a verdict taken on the original file is marked apart from one taken on the
substitute -- the same word ("failed") means different things in the two places (boundaries 209, 210).
"""

from __future__ import annotations

from pathlib import Path

from brian.campaign import campaign, format_campaign
from brian.promote import (format_promotions, load_promotions, repaired_paths, same_stream_path,
                           write_verification)

from test_clearing_evidence_file import FILE
from test_provenance_block import _stream
from test_repaired_check import _repaired


def _repaired_with_origin_gone(root: Path) -> None:
    """A repaired record whose original file really is missing: what an operator deleting or moving it looks like."""
    _repaired(root)
    target = root / FILE
    if target.exists():
        target.unlink()


def _restore_origin(root: Path) -> None:
    """The file the promise was originally measured on is back: an operator restored or re-fetched it."""
    if not (root / FILE).exists():
        _stream(root, FILE)


def _page(root: Path) -> str:
    return format_promotions(load_promotions(root), None, domain="SMD")


def test_the_original_file_is_missing_after_a_repair(tmp_path):
    _repaired_with_origin_gone(tmp_path)
    rows = repaired_paths(tmp_path)
    assert rows and rows[0]["origin_exists"] is False


def test_the_original_file_is_reported_when_it_comes_back(tmp_path):
    _repaired_with_origin_gone(tmp_path)
    _restore_origin(tmp_path)
    rows = repaired_paths(tmp_path)
    assert rows[0]["origin_exists"] is True


def test_the_history_marks_a_row_whose_original_file_is_back(tmp_path):
    from brian import cli

    _repaired(tmp_path)
    _restore_origin(tmp_path)
    lines = cli.format_history_paths(repaired_paths(tmp_path))
    assert ("(the original file is back: %s)" % FILE) in "\n".join(lines)


def test_the_history_says_nothing_when_the_original_file_is_still_gone(tmp_path):
    from brian import cli

    _repaired_with_origin_gone(tmp_path)
    assert "the original file is back" not in "\n".join(cli.format_history_paths(repaired_paths(tmp_path)))


def test_the_campaign_names_the_record_the_file_and_the_command(tmp_path):
    _repaired_with_origin_gone(tmp_path)
    _restore_origin(tmp_path)
    page = format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": repaired_paths(tmp_path)}))
    assert ("v020's original file %s is back in this checkout: the record can be pointed at it again and "
            "measured there -- brian evolve --set-stream-path v020=%s" % (FILE, FILE)) in page


def test_the_campaign_does_not_offer_a_command_while_the_original_is_gone(tmp_path):
    _repaired_with_origin_gone(tmp_path)
    page = format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": repaired_paths(tmp_path)}))
    assert "is back in this checkout" not in page


def test_the_table_marks_a_verdict_taken_on_the_original_file(tmp_path):
    _repaired_with_origin_gone(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion, {"checked_at": "2026-11-20T00:00:00+00:00", "verdict": "failed",
                                             "reason": "recall fell", "stream_path_moves": 1,
                                             "on_original_path": True})
    page = _page(tmp_path)
    assert "[measured on the original path]" in page
    assert "[measured on the repaired path]" not in page


def test_same_stream_path_ignores_separators_and_dot_prefix():
    assert same_stream_path("data/smd/stream-one.json", "data\\smd\\stream-one.json")
    assert same_stream_path("./data/smd/stream-one.json", "data/smd/stream-one.json")
    assert not same_stream_path("", "data/smd/stream-one.json")
    assert not same_stream_path("data/smd/stream-one.json", "data/smd/other.json")
