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


def test_a_report_that_would_charge_says_nothing_on_the_page(capsys):
    """The paragraphs are the check's voice; a report that printed them would be a report pretending to be one."""
    box = _box()
    cli._strict_exit(False, 4, 0, into=box)

    assert capsys.readouterr().out == ""
    assert box["would_charge"]


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
