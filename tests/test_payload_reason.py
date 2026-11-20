"""Round 167 (item 153): the reason for a non-zero exit travels with the machine-readable report.

Round 166 made the gate charge a payload page that no longer describes the source. The charge is printed, and the
report it is about is written to `--json-out` *before* the gate runs -- so a script that saw exit 2 had a file whose
every field said "nothing is wrong" (boundary 402). These tests read the payload back and ask it why.
"""

import json
from pathlib import Path

from brian import cli


def _gate(tmp_path: Path, *argv: str) -> dict:
    """Run one report command and return the `gate` block it wrote."""
    out = tmp_path / "gate.json"
    cli.main(["evolve", *argv, "--json-out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    return payload["gate"]


def test_the_audit_payload_says_the_gate_was_asked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gate = _gate(tmp_path, "--audit", "--strict")

    assert gate == {"asked": True, "page_problems": []}


def test_the_campaign_payload_says_when_no_verdict_was_asked_for(tmp_path, monkeypatch):
    """`asked` is not the verdict: a report command runs the same code without asking to be one (boundary 401)."""
    monkeypatch.chdir(tmp_path)
    gate = _gate(tmp_path, "--campaign")

    assert gate["asked"] is False


def test_a_problem_appears_verbatim_while_the_gate_exits_two(tmp_path, monkeypatch, capsys):
    """The payload must carry the items, not a count of them: they are what the reader has to fix (boundary 403)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems",
                        lambda source=None: ["the page lists 0 payload writer(s) and this source has 24"])

    out = tmp_path / "audit.json"
    assert cli.main(["evolve", "--audit", "--strict", "--json-out", str(out)]) == 2

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["page_problems"] == ["the page lists 0 payload writer(s) and this source has 24"]
    printed = capsys.readouterr().out
    assert "payload page      1 problem(s)" in printed
    assert "wrote " in printed


def test_the_campaign_payload_carries_the_items_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems",
                        lambda source=None: ["the page does not name --settle-x"])

    gate = _gate(tmp_path, "--campaign", "--strict")

    assert gate["page_problems"] == ["the page does not name --settle-x"]


def test_the_two_payloads_agree_about_the_same_page(tmp_path, monkeypatch):
    """One computation, two reports: the blocks may not be assembled twice (round 160's rule)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["a problem"])

    audit = _gate(tmp_path / "audit", "--audit")
    campaign = _gate(tmp_path / "campaign", "--campaign")

    assert audit["page_problems"] == campaign["page_problems"] == ["a problem"]
