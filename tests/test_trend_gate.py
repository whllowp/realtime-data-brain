# -*- coding: utf-8 -*-
"""Round 56 tests: the unrecorded share's movement enters the gate.

Round 55 put the trend on the campaign page: whether the share of rounds that cannot be placed on a
sampling had fallen, risen or stood still, recomputed from the ledger's arrival and retirement events.
A number on a page that no exit code reads is decoration (boundary 98), so a rising share -- a debt that
grows while nothing is paid -- has to be chargeable. It is chargeable only on request, because this build
writes nameless rounds on purpose when it measures the generated demo stream: a blanket "a rise fails"
would leave that domain red forever, and the exit code a check can never clear is the check nobody runs.
"""
from brian import cli
from brian.campaign import campaign, format_campaign, span_text
from brian.evolve import EvolveRules, debt_history

RISE = {"points": -12.0, "days": 1.0, "paid": 3, "arrived": 7, "share": 0.66,
        "before_share": 0.54, "truncated": False}


def test_a_rise_is_reported_but_does_not_fail_the_check_by_default(capsys):
    # The demo stream names no file by design, so charging every rise would keep that domain red forever.
    code = cli._strict_exit(True, 0, 0, trend_rise=12.0, trend_days=1.0,
                            trend_paid=3, trend_arrived=7)

    text = capsys.readouterr().out
    assert code == 0
    assert "12.0 point(s) of the unrecorded share came back in the last 1 day(s)" in text
    assert "3 round(s) were re-measured and 7 record(s) arrived" in text
    assert "pass --max-share-rise to make it fail the check" in text


def test_an_asked_for_cap_makes_the_rise_fail(capsys):
    code = cli._strict_exit(True, 0, 0, trend_rise=12.0, trend_days=1.0,
                            trend_paid=3, trend_arrived=7, max_rise=10.0)

    text = capsys.readouterr().out
    assert code == 1
    assert "rose by more than the 10.0 point(s) asked for" in text


def test_a_rise_under_the_cap_passes(capsys):
    code = cli._strict_exit(True, 0, 0, trend_rise=12.0, trend_days=1.0, max_rise=20.0)

    text = capsys.readouterr().out
    assert code == 0
    assert "rose by more than" not in text


def test_a_rise_under_the_tolerance_is_not_worth_a_paragraph(capsys):
    # A third of a point is round-off on a page that prints whole percentages; printing a charge for it
    # would teach the reader that the paragraph means nothing.
    code = cli._strict_exit(True, 0, 0, trend_rise=0.3, trend_days=1.0, max_rise=0.0)

    assert code == 0
    assert "came back in the last" not in capsys.readouterr().out


def test_a_falling_share_prints_nothing(capsys):
    # The caller clamps a fall to zero, so this is the ordinary case: the queue is being paid.
    assert cli._strict_exit(True, 0, 0, trend_rise=0.0, trend_days=7.0) == 0
    assert capsys.readouterr().out.strip() == ""


def test_a_move_verdict_still_fails_without_any_trend():
    assert cli._strict_exit(True, 3, 0) == 1
    assert cli._strict_exit(False, 3, 0) == 0


def test_the_window_is_not_printed_as_zero(capsys):
    # Round 56's first live run printed "in the last 0.0 hour(s)" for a window a few seconds long, which
    # reads as no window at all.
    assert span_text(0.0005) == "43 second(s)"
    assert span_text(0.5) == "12.0 hour(s)"
    assert span_text(30.0) == "30 day(s)"
    assert span_text(0.0) == "0 second(s)"

    cli._strict_exit(True, 0, 0, trend_rise=5.0, trend_days=0.0005)
    text = capsys.readouterr().out
    assert "second(s)" in text
    assert "0.0 hour(s)" not in text


def test_the_gate_and_the_page_read_the_same_window(tmp_path):
    # One window, two readers: the page says how far the share moved and the gate charges it. If the
    # numbers came from different windows, a reader could pass the check and still see a rise.
    rounds = tmp_path / "var" / "evolve"
    rounds.mkdir(parents=True)
    ledger = rounds / "ledger.jsonl"
    lines = [
        {"round": "round-a.json", "started_at": "2026-11-01T00:00:00+00:00", "accepted": 0,
         "rejected": 0, "applied": 0, "rules_hash": "x", "stream_path": None},
        {"round": "round-b.json", "started_at": "2026-11-17T00:00:00+00:00", "accepted": 0,
         "rejected": 0, "applied": 0, "rules_hash": "x", "stream_path": None},
    ]
    ledger.write_text("\n".join(__import__("json").dumps(line) for line in lines) + "\n",
                      encoding="utf-8")
    for name in ("round-a.json", "round-b.json"):
        (rounds / name).write_text("{}", encoding="utf-8")

    trend = debt_history(tmp_path, days=30.0)
    result = campaign(tmp_path, EvolveRules(), refusal_expiry_days=14.0)
    page = format_campaign(result, rules=EvolveRules(), trend=trend,
                           queue={"ready": 0, "paid": 0})

    assert trend["share"] == 1.0
    assert trend["points"] == 0.0
    assert "the unrecorded share has not moved" in page
    assert "100%" in page
    assert "trend_rise" not in page
