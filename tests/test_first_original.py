"""Round 99: was anything ever measured where the promise was made?

Round 98 made every verdict name the file it stood on, and divided the record's evidence between the file the
promise was born with and everywhere else. That split makes a question askable that the pages had no way to
answer: has this promise *ever* been tested where it was made? A record repaired away, verified twice on the
substitute and pointed back holds two verdicts and not one of them says anything about its own file.

So the check says it when it is the first one, records that fact with the verdict, and the campaign stops calling
it "another check since the return". A verdict the record cannot place is named with its time and word instead of
being left as a bare count.
"""

from __future__ import annotations

import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.promote import load_promotions, repaired_paths, set_stream_path, write_verification

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_evidence_paths import THIRD
from test_repaired_check import _clean, _repaired, _verify
from test_untested_answer import NOW


def _campaign(root) -> str:
    return format_campaign(campaign(root, in_force_history={"repaired_paths": repaired_paths(root)}))


def _only_elsewhere(root) -> None:
    """A record whose only verdict was taken on the substitute, still repaired away from its original."""
    _repaired(root)
    promotion = load_promotions(root)[0]
    write_verification(root, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "failed", "reason": "recall fell",
                        "stream_path_moves": 1, "checked_path": "data/smd/other.json",
                        "on_original_path": False})


def test_the_check_says_when_it_is_the_first_one_on_the_original(tmp_path, monkeypatch, capsys):
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=FILE)
    out = capsys.readouterr().out
    assert "this is the first verdict taken on the file the promise was born with" in out
    assert "all 1 it holds were measured elsewhere" in out
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["first_original_measurement"] is True
    assert payload["verification"]["prior_evidence_split"]["original"] == 0
    assert payload["verification"]["prior_evidence_split"]["elsewhere"] == 1


def test_a_second_check_on_the_original_is_not_called_the_first(tmp_path, monkeypatch, capsys):
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=FILE)
    capsys.readouterr()
    _verify(tmp_path, monkeypatch, path=FILE)
    out = capsys.readouterr().out
    assert "this is the first verdict taken on the file the promise was born with" not in out
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["first_original_measurement"] is False
    assert payload["verification"]["prior_evidence_split"]["original"] == 1


def test_a_check_on_the_substitute_never_claims_to_be_the_first_on_the_original(tmp_path, monkeypatch, capsys):
    _only_elsewhere(tmp_path)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path="data/smd/other.json")
    out = capsys.readouterr().out
    assert "first verdict taken on the file the promise was born with" not in out
    payload = json.loads((tmp_path / "var" / "evolve" / "versions" / "v020.json").read_text(encoding="utf-8"))
    assert payload["verification"]["first_original_measurement"] is False


def test_the_record_knows_when_the_only_original_verdict_is_the_first(tmp_path):
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion,
                       {"checked_at": (NOW + __import__("datetime").timedelta(days=1)).isoformat(),
                        "verdict": "holds", "reason": "measured again", "stream_path_moves": 2,
                        "checked_path": FILE, "on_original_path": True})
    row = repaired_paths(tmp_path)[0]
    assert row["returned"] is True
    assert row["first_on_original"] is True
    assert row["evidence"]["original"] == 1 and row["evidence"]["elsewhere"] == 1


def test_the_campaign_calls_it_the_first_verdict_taken_there(tmp_path):
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion,
                       {"checked_at": (NOW + __import__("datetime").timedelta(days=1)).isoformat(),
                        "verdict": "holds", "reason": "measured again", "stream_path_moves": 2,
                        "checked_path": FILE, "on_original_path": True})
    page = _campaign(tmp_path)
    assert "this was the first verdict ever taken there (%s)" % (
        NOW + __import__("datetime").timedelta(days=1)).isoformat() in page
    assert "verdict(s) have been taken there since the return" not in page


def test_an_unplaceable_verdict_is_named_with_its_time_and_word(tmp_path):
    _repaired(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    # `stream_path_moves = 7` with a two-move history cannot be placed: it is reported, not assigned to a side.
    write_verification(tmp_path, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "unmeasurable",
                        "reason": "no recall at the budget", "stream_path_moves": 7})
    page = _campaign(tmp_path)
    assert "holds a verdict it cannot place: %s (unmeasurable)" % NOW.isoformat() in page


def test_the_page_counts_the_verdicts_taken_here_when_there_are_some(tmp_path, monkeypatch, capsys):
    """Round 97 said "the verdicts it already holds were taken on the substitute" -- untrue once one was here."""
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, FILE, now=NOW)
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    _verify(tmp_path, monkeypatch, path=FILE)
    capsys.readouterr()
    _verify(tmp_path, monkeypatch, path=FILE)
    out = capsys.readouterr().out
    assert "1 of the 2 verdict(s) it already holds were taken here, 1 elsewhere" in out
    assert "the verdicts it already holds were taken on the substitute" not in out


def test_an_untouched_record_says_nothing_about_first_verdicts(tmp_path):
    _clean(tmp_path)
    assert "first verdict" not in _campaign(tmp_path)
    assert "cannot place" not in _campaign(tmp_path)


def test_a_third_move_does_not_make_the_first_original_verdict_untrue(tmp_path):
    _only_elsewhere(tmp_path)
    set_stream_path(tmp_path, 20, THIRD, now=NOW)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "holds", "reason": "measured again",
                        "stream_path_moves": 2, "checked_path": THIRD, "on_original_path": False})
    row = repaired_paths(tmp_path)[0]
    # The record never returned, so nothing here is "the first verdict on the original".
    assert row["returned"] is False
    assert row["first_on_original"] is False
