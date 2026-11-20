# -*- coding: utf-8 -*-
"""Round 128: the withdrawal debt is paid per stream file, so the plan counts files.

Round 127 charged the debt per decision and per age. A cap in decisions answers "is the debt large", which is not
what a reader with three afternoons can act on: eleven withdrawals on eleven files is eleven of them, eleven on one
file is one (boundary 279, applied to this debt). `withdrawal_plan` groups the same rows by their origin and spells
the command that pays a whole file.
"""
import json

from brian import cli
from brian.campaign import campaign
from brian.evolve import (history, load_settlements, restore_settlement, settle_link,
                          unanswered_withdrawals, withdrawal_plan, withdraw_settlement)

from test_provenance_block import RETIRED, SUCCESSOR, _round
from test_settlement_reaffirm import _ago

ONE = "data/smd/stream-one.json"
THIRD = "data/smd/third.json"


def _origins(root, names, origin):
    """Stamp the round files and their ledger lines with the stream path the plan groups by."""
    for name in names:
        path = root / "var" / "evolve" / name
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["stream_path"] = origin
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    for ledger in (root / "var" / "evolve" / "ledger.jsonl",):
        if not ledger.exists():
            continue
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            if str(row.get("round")) in names:
                row["stream_path"] = origin
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _pair(root, record, successor, *, origin=ONE, days_ago=2.0, by="alice"):
    """Settle then withdraw one pair: no contradiction is left, so the debt is the quiet kind round 127 charges."""
    _round(root, record, retired={"re_measured_as": successor})
    _round(root, successor, queue={"paid": "SMD", "paid_round": record, "family": "file"})
    _origins(root, [record, successor], origin)
    settle_link(root, record, successor, why="the retirement mark is the surviving claim", by=by,
                now=_ago(days_ago + 1))
    withdraw_settlement(root, record, successor, why="the books have said otherwise three times", by=by,
                        now=_ago(days_ago))


def _rounds(root):
    return history(root, include_retired=True)


def test_the_plan_groups_two_decisions_on_one_file(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=3.0)
    _pair(tmp_path, "round-fourth.json", "round-fifth.json", days_ago=1.0, by="bob")
    rows = withdrawal_plan(_rounds(tmp_path), load_settlements(tmp_path))
    assert len(rows) == 1
    assert rows[0]["origin"] == ONE and rows[0]["decisions"] == 2
    # Round 129: a plan grouped by file prints the command that pays the file; the single pair is kept beside it.
    assert rows[0]["batch"] == ("brian evolve --settle-restore-file data/smd/stream-one.json "
                                "--settle-why TEXT --settle-by NAME")
    assert rows[0]["pair"] == ("brian evolve --settle-restore round-old.json=round-new.json "
                               "--settle-why TEXT --settle-by NAME")
    assert rows[0]["more"] == 1


def test_the_plan_splits_two_files_and_puts_the_heaviest_first(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=2.0)
    _pair(tmp_path, "round-sixth.json", "round-seventh.json", origin=THIRD, days_ago=2.0, by="bob")
    _pair(tmp_path, "round-eighth.json", "round-ninth.json", origin=THIRD, days_ago=1.0, by="bob")
    rows = withdrawal_plan(_rounds(tmp_path), load_settlements(tmp_path))
    assert [row["origin"] for row in rows] == [THIRD, ONE]
    assert [row["decisions"] for row in rows] == [2, 1]


def test_a_restored_pair_is_not_in_the_plan(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=2.0)
    restore_settlement(tmp_path, RETIRED, SUCCESSOR, why="the third claim is dated after the decision", by="bob",
                       now=_ago(1))
    assert withdrawal_plan(_rounds(tmp_path), load_settlements(tmp_path)) == []


def test_the_rows_carry_the_origin_only_when_the_rounds_are_given(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=2.0)
    settlements = load_settlements(tmp_path)
    assert unanswered_withdrawals(settlements)[0]["origin"] == ""
    assert unanswered_withdrawals(settlements, rounds=_rounds(tmp_path))[0]["origin"] == ONE


def test_the_oldest_withdrawal_on_the_file_is_named(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=9.0)
    _pair(tmp_path, "round-tenth.json", "round-eleventh.json", days_ago=2.0, by="bob")
    row = withdrawal_plan(_rounds(tmp_path), load_settlements(tmp_path))[0]
    assert row["decisions"] == 2
    assert row["oldest_age"] > 8.0
    assert row["oldest_at"] <= row["records"][-1]["at"]


def test_the_gate_charges_a_file_carrying_more_than_the_cap(tmp_path, capsys, monkeypatch):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=1.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-per-file", "0"]) == 1
    printed = " ".join(capsys.readouterr().out.split())
    assert "1 withdrawal(s) sit on one stream file, above the 0 asked for for a single file" in printed
    assert "the file is data/smd/stream-one.json" in printed


def test_a_cap_of_one_does_not_charge_a_single_file(tmp_path, monkeypatch):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=1.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-withdrawn-per-file", "1"]) == 0


def test_the_per_file_cap_is_armed_like_the_other_caps(tmp_path, capsys, monkeypatch):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=1.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--max-withdrawn-per-file", "0"]) == 2
    assert "--max-withdrawn-per-file caps a --strict charge" in " ".join(capsys.readouterr().out.split())


def test_the_campaign_carries_the_plan(tmp_path):
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=2.0)
    result = campaign(tmp_path)
    assert result.withdrawal_plan[0]["origin"] == ONE
    assert result.to_dict()["withdrawal_plan"][0]["decisions"] == 1


def test_the_campaign_names_the_files_when_there_is_more_than_one(tmp_path):
    from brian.campaign import format_campaign
    _pair(tmp_path, RETIRED, SUCCESSOR, days_ago=2.0)
    _pair(tmp_path, "round-sixth.json", "round-seventh.json", origin=THIRD, days_ago=1.0, by="bob")
    printed = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "withdrawn and waiting, by stream file:" in printed
