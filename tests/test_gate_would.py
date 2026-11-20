"""Round 169 (item 155): a report knows what a check would charge, and is still not a check.

Round 168 made the exit code the list of charges, but the list was only built when `--strict` was given: a report
said `charges: []`, which was true about what it charged and silent about what a check would find. The flag decides
the return code, not the facts the function already holds (boundary 401's other half) -- so the same table is now
built in both modes and stored under `would_charge` (boundary 406).
"""

import json

from brian import cli


def _box() -> dict:
    return {"asked": True, "page_problems": []}


def test_a_report_keeps_the_table_and_charges_nothing():
    box = _box()
    assert cli._strict_exit(False, 3, 0, into=box) == 0

    assert box["charges"] == []
    assert box["would_charge"] == [{"rule": "moved verdict", "count": 3, "code": 1}]


def test_both_modes_read_the_same_facts():
    """The two fields may not be two readings of different flags (round 160's rule, one level down)."""
    report, check = _box(), _box()
    cli._strict_exit(False, 2, 0, broken_links=1, into=report)
    cli._strict_exit(True, 2, 0, broken_links=1, into=check)

    assert report["would_charge"] == check["charges"]
    assert report["charges"] == []


def test_a_report_prints_one_line_not_the_checks_paragraphs(capsys):
    """Round 170: the report names the rule and the command; the check's paragraphs stay the check's voice."""
    box = _box()
    assert cli._strict_exit(False, 4, 0, into=box) == 0

    printed = capsys.readouterr().out
    assert "next check        would charge 1 item(s): moved verdict (4)" in printed
    assert "run the same command with --strict to charge it" in printed
    assert "would be decided differently now" not in printed
    assert box["would_charge"]


def test_a_check_charge_mode_prints_no_would_charge_line(capsys):
    """Under `--strict` the line would be a second account of the paragraph printed below it."""
    box = _box()
    assert cli._strict_exit(True, 4, 0, into=box) == 1

    assert "next check" not in capsys.readouterr().out


def test_a_report_with_nothing_to_charge_prints_no_line(capsys):
    box = _box()
    assert cli._strict_exit(False, 0, 0, into=box) == 0

    assert "next check" not in capsys.readouterr().out


def test_one_charge_prints_its_rule_without_a_count(capsys):
    box = _box()
    cli._strict_exit(False, 1, 0, into=box)

    assert "would charge 1 item(s): moved verdict\n" in capsys.readouterr().out


def test_the_report_payload_carries_would_charge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "campaign.json"
    assert cli.main(["evolve", "--campaign", "--json-out", str(out)]) == 0

    gate = json.loads(out.read_text(encoding="utf-8"))["gate"]
    assert gate["asked"] is False
    assert gate["exit"] == 0
    assert gate["charges"] == []
    assert isinstance(gate["would_charge"], list)


def test_a_charged_check_carries_the_same_list_in_both_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["the page lists 0 writer(s)"])

    out = tmp_path / "audit.json"
    assert cli.main(["evolve", "--audit", "--strict", "--json-out", str(out)]) == 2

    gate = json.loads(out.read_text(encoding="utf-8"))["gate"]
    assert gate["charges"] == gate["would_charge"]
    assert {"rule": "payload page", "count": 1, "code": 2} in gate["charges"]
