"""Round 168 (item 154): the gate keeps the list of what it charged, so the report can explain its own code.

Round 167 put the payload page's problems in the report. The other seventeen reasons the gate can charge a ledger
still lived in the paragraphs it printed, so a script with exit 1 knew that something was wrong and not what. The
list below is also the code: `_done()` derives 2 / 1 / 0 from it, so the explanation and the exit cannot be two
readings of different flags (boundary 404).
"""

import json

from brian import cli


def _box() -> dict:
    return {"asked": True, "page_problems": []}


def test_a_moved_verdict_records_its_charge_and_returns_one():
    box = _box()
    assert cli._strict_exit(True, 1, 0, into=box) == 1

    assert box["charges"] == [{"rule": "moved verdict", "count": 1, "code": 1}]


def test_a_build_defect_outranks_a_debt_and_both_are_listed():
    box = _box()
    assert cli._strict_exit(True, 3, 0, broken_links=1, into=box) == 2

    rules = {item["rule"]: item["code"] for item in box["charges"]}
    assert rules == {"replacement link": 2, "moved verdict": 1}


def test_the_code_is_derivable_from_the_list_alone():
    """`exit == 2 if any charge is a defect else 1 if any else 0` -- the reader needs no other field."""
    for kwargs in ({}, {"moved": 2}, {"broken_links": 1}, {"due": 1, "page_problems": 1},
                   {"ceiling_missing": 4, "ceiling_rounds": 4, "ceiling_payable": 4,
                    "max_ceiling_debt": 0.5}):
        box = _box()
        code = cli._strict_exit(True, kwargs.pop("moved", 0), 0, into=box, **kwargs)
        charges = box["charges"]
        expected = 2 if any(item["code"] == 2 for item in charges) else (1 if charges else 0)
        assert code == expected, (kwargs, charges)


def test_a_report_records_no_charge():
    """Without `--strict` the gate returns before reading anything: a report is not a check (round 15)."""
    box = _box()
    assert cli._strict_exit(False, 4, 2, broken_links=1, into=box) == 0

    assert box["charges"] == []


def test_the_payload_carries_the_code_the_command_returns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "audit.json"
    code = cli.main(["evolve", "--audit", "--strict", "--json-out", str(out)])

    payload = json.loads(out.read_text(encoding="utf-8"))
    gate = payload["gate"]
    expected = 2 if any(item["code"] == 2 for item in gate["charges"]) else (
        1 if gate["charges"] else 0)
    assert gate["exit"] == code == expected


def test_the_campaign_payload_carries_it_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "campaign.json"
    code = cli.main(["evolve", "--campaign", "--json-out", str(out)])

    gate = json.loads(out.read_text(encoding="utf-8"))["gate"]
    assert gate["exit"] == code


def test_a_charged_gate_names_the_rule_in_the_payload(tmp_path, monkeypatch):
    """One problem on the payload page, and the report says which rule charged for it."""
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["the page lists 0 writer(s)"])

    box = _box()
    assert cli._strict_exit(True, 0, 0, page_problems=1, into=box) == 2

    assert box["charges"] == [{"rule": "payload page", "count": 1, "code": 2}]
