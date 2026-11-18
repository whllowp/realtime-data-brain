"""The ceiling debt is counted per domain, and the page says whether it can be paid (round 71).

Round 70 put each round's ceilings on its history line and counted the ledger's shortfall in the header. A
reader could see "38 do not say" and still not know which stream owed them — nor whether any command could
change the number. Both answers are one per-domain count plus the queue's reach.
"""

from __future__ import annotations

import json

from brian import cli
from brian.campaign import Campaign, CampaignRow, format_campaign
from brian.evolve import ceiling_shortfall, format_ceiling_shortfall


def _round(name, *, source="SMD", limits=None, retired=None, accepted=None, started="2026-11-18T06:00:00+00:00"):
    record = {
        "round": name,
        "started_at": started,
        "finished_at": started,
        "stream": {"source": source, "windows": 3, "scored": 2},
        "trials": [],
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.297},
    }
    if limits is not None:
        record["limits"] = limits
    if retired is not None:
        record["retired"] = retired
    if accepted is not None:
        record["accepted"] = accepted
    return record


def test_the_shortfall_is_counted_per_domain():
    rows = ceiling_shortfall([
        _round("a.json", limits={"max_minutes": 4.0}),
        _round("b.json"),
        _round("c.json"),
        _round("d.json", source="RCAEval RE1-OB"),
    ])
    by_domain = {row["domain"]: row for row in rows}
    assert by_domain["SMD"]["rounds"] == 3
    assert by_domain["SMD"]["missing"] == 2
    assert by_domain["RCAEval RE1-OB"]["missing"] == 1
    # Sorted by share, so the reader sees the worst row first.
    assert rows[0]["domain"] == "RCAEval RE1-OB"
    assert rows[0]["share"] == 1.0


def test_a_retired_round_owes_nothing_and_a_streamless_one_is_named():
    rows = ceiling_shortfall([
        _round("a.json", retired={"at": "2026-11-18T07:00:00+00:00", "re_measured_as": "b.json"}),
        _round("b.json", source=None),
    ])
    by_domain = {row["domain"]: row for row in rows}
    assert "SMD" not in by_domain  # the only SMD round is retired, so the domain owes nothing
    assert by_domain["(unattributed)"]["rounds"] == 1


def test_nothing_missing_says_nothing():
    assert format_ceiling_shortfall([{"domain": "SMD", "rounds": 3, "missing": 0, "share": 0.0}]) is None
    assert format_ceiling_shortfall([]) is None


def test_a_debt_nobody_can_pay_is_reported_as_a_standing_warning():
    text = format_ceiling_shortfall([{"domain": "SMD", "rounds": 5, "missing": 5, "share": 1.0}], payable=0)
    assert text == ("5 of 5 round(s) do not record the ceilings they were judged under, and none of them "
                    "can be re-run, so the default --max-limit-drift cap stays a warning")


def test_a_debt_the_queue_can_pay_says_the_cap_would_arm():
    text = format_ceiling_shortfall([{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}], payable=2)
    assert "re-running 2 of them would leave none" in text
    assert "would arm" in text
    assert "stays a warning" not in text


def test_a_partly_payable_debt_says_how_much_would_be_left():
    text = format_ceiling_shortfall([{"domain": "SMD", "rounds": 5, "missing": 4, "share": 0.8}], payable=1)
    assert "re-running 1 of them would leave 3 without one" in text
    assert "stays a warning" in text


def test_payable_is_never_rounded_up_past_the_debt():
    text = format_ceiling_shortfall([{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}], payable=9)
    assert "re-running 2 of them" in text
    assert "would arm" in text


def _campaign_row():
    return CampaignRow(domain="SMD", rounds=3, trials=3, first="a", last="c", verdict="holds",
                       in_force="20", env={"BRIAN_DIGEST_ROLLING_WINDOW": "200"})


def test_the_page_prints_the_ceiling_debt():
    page = format_campaign(Campaign(rows=[_campaign_row()], rounds=3, trials=3),
                           ceiling_note="2 of 3 round(s) do not record the ceilings they were judged under")
    assert "ceiling debt      2 of 3 round(s) do not record the ceilings they were judged under" in page


def test_the_page_says_nothing_when_there_is_no_debt():
    page = format_campaign(Campaign(rows=[_campaign_row()], rounds=3, trials=3), ceiling_note=None)
    assert "ceiling debt" not in page


def _write_round(root, name, *, source="SMD", limits=None, accepted=None):
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    record = _round(name, source=source, limits=limits, accepted=accepted)
    (directory / name).write_text(json.dumps(record), encoding="utf-8")
    line = {"round": name, "started_at": record["started_at"], "accepted": 1, "rejected": 0,
            "applied": [], "rules_hash": "abc123abc123", "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_history_page_names_the_domain_that_owes(tmp_path, monkeypatch, capsys):
    _write_round(tmp_path, "round-a.json")
    _write_round(tmp_path, "round-b.json", limits={"max_minutes": 4.0})
    _write_round(tmp_path, "round-c.json", source="RCAEval RE1-OB")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "ceilings          1 of 3 round(s) record the wait ceilings they were judged under; 2 do not" in text
    assert "  ceiling debt    SMD              1 of 2 round(s) do not record them" in text
    assert "  ceiling debt    RCAEval RE1-OB   1 of 1 round(s) do not record them" in text


def test_the_history_page_survives_a_record_that_kept_a_count(tmp_path, monkeypatch, capsys):
    """Found by running: the ledger's `accepted` is a count, the round file's is a list of names."""
    _write_round(tmp_path, "round-a.json", accepted=1)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--history"]) == 0
    text = capsys.readouterr().out
    assert "accepted=-" in text
