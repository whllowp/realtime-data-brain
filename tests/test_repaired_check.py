"""Round 95: a check taken on a repaired path must say so.

Round 94 made a hand repair (`--set-stream-path`) visible on the version table and the campaign page. The
verification a check writes said nothing about it, so a verdict taken on the location the repair picked read
exactly like one taken on the file the promise originally named -- boundary 203 (an edited record must name
itself as edited) one field further out, and boundary 205's distinction ("the path was repaired" is not "the
promise was re-verified") lost at the place where the second thing is decided.

So the check says it before measuring, records `stream_path_moves` in the payload (also when the definition
cannot be re-measured, because the repair is a fact about the record and not about the verdict), the version
table marks the row, and the campaign counts the checks taken after the repair.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import (format_promotions, load_promotions, repaired_paths, set_stream_path,
                           write_verification)

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_provenance_block import _round, _stream, _version

OTHER = "data/smd/other.json"


def _clean(root: Path) -> None:
    """A version that measured `FILE`, produced by a round: nothing disputed, nothing copied."""
    _round(root, "round-new.json", applied=(20,))
    _version(root)
    _stream(root)


def _add_files(root: Path) -> None:
    for relative in (OTHER, "data/smd/third.json"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"format": "brian.stream/1", "dataset": "SMD", "streams": []}),
                          encoding="utf-8", newline="\n")


def _repaired(root: Path) -> None:
    _clean(root)
    _add_files(root)
    set_stream_path(root, 20, OTHER)


def _page(root: Path) -> str:
    return format_promotions(load_promotions(root), None, domain="SMD")


def _verify(tmp_path, monkeypatch, *extra):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    arguments = ["evolve", "--verify", "--verify-version", "20", "--stream-file", FILE]
    return cli.main(arguments + list(extra))


def test_the_promotion_carries_its_path_history(tmp_path):
    _repaired(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    assert promotion.path_moves == 1
    assert promotion.path_origin == FILE
    assert promotion.path_current == OTHER
    assert promotion.to_dict()["stream_path_history"][0]["to"] == OTHER


def test_an_untouched_version_reports_no_move(tmp_path):
    _clean(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    assert promotion.path_moves == 0
    assert promotion.path_origin == "" and promotion.path_current == ""


def test_the_version_table_marks_a_repaired_path(tmp_path):
    _repaired(tmp_path)
    page = _page(tmp_path)
    assert "(path rewritten 1 time(s) by hand)" in page


def test_the_version_table_says_nothing_about_an_untouched_path(tmp_path):
    _clean(tmp_path)
    assert "path rewritten" not in _page(tmp_path)


def test_verify_says_where_the_check_is_standing(tmp_path, monkeypatch, capsys):
    _repaired(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    code = _verify(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert code in (0, 1), out
    assert ("path rewritten 1 time(s) by hand: %s -> %s" % (FILE, OTHER)) in out
    assert "this check measures the repaired location, not the file the promise first named" in out


def test_the_verification_records_the_moves(tmp_path, monkeypatch, capsys):
    _repaired(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch)
    capsys.readouterr()
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["stream_path_moves"] == 1


def test_an_untouched_version_records_zero_moves(tmp_path, monkeypatch, capsys):
    _clean(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "path rewritten" not in out
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["stream_path_moves"] == 0


def test_the_table_says_the_verdict_was_taken_after_the_repair(tmp_path):
    _repaired(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion, {"checked_at": "2026-11-20T00:00:00+00:00", "verdict": "holds",
                                             "reason": "recall holds", "stream_path_moves": 1})
    page = _page(tmp_path)
    assert "[measured on the repaired path]" in page


def test_the_campaign_counts_the_checks_taken_after_the_repair(tmp_path):
    _repaired(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion, {"checked_at": "2026-11-20T00:00:00+00:00", "verdict": "holds",
                                             "reason": "recall holds", "stream_path_moves": 1})
    rows = repaired_paths(tmp_path)
    assert rows and rows[0]["checks_on_repair"] == 1
    page = format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": rows}))
    assert ("1 check(s) were taken after the repair: their verdict describes the repaired location, not the "
            "file the promise first named") in page


def test_the_campaign_says_nothing_when_no_check_came_after_the_repair(tmp_path):
    _repaired(tmp_path)
    page = format_campaign(campaign(tmp_path, in_force_history={"repaired_paths": repaired_paths(tmp_path)}))
    assert "taken after the repair" not in page
