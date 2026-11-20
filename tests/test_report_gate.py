"""Round 171 (item 157): every report page says what it did about the gate, and `--history` names the check.

The premise of item 157 was that only `--audit`/`--campaign` carry the footer. `--sensitivity` already did (it
calls `_strict_exit`); `--history` never ran the gate at all and its payload had no `gate` key, so a script could
not tell "no charges" from "no check". These tests pin the three states a report can be in.
"""
import json

from brian import cli
from brian.evolve import EVOLVE_DIR


def _round(root):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": "round-a.json", "started_at": "2026-11-20T08:00:00+00:00",
               "finished_at": "2026-11-20T08:05:00+00:00",
               "stream": {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1},
               "stream_path": "data/smd/stream-one.json", "trials": [], "accepted": [], "rejected": [],
               "applied": ["v020"]}
    (ledger / payload["round"]).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line["rules_hash"] = "abc"
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return root


def test_the_history_payload_says_the_page_did_not_check(tmp_path, monkeypatch):
    _round(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "history.json"

    assert cli.main(["evolve", "--history", "--json-out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"] == {"asked": False, "checked": False, "command": "brian evolve --campaign --strict"}


def test_the_history_page_prints_the_line_that_names_the_check(tmp_path, monkeypatch, capsys):
    _round(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0
    flat = " ".join(capsys.readouterr().out.split())

    assert ("gate this page reads the ledger and charges nothing; "
            "`brian evolve --campaign --strict` is the page that does") in flat


def test_strict_on_the_history_page_is_acknowledged_not_ignored(tmp_path, monkeypatch, capsys):
    """`--history --strict` stays 0: the caps on this page mark rows, and a reader expects the marks.

    What it may not do is stay silent about the flag -- so the line names `--audit --strict`, the command that
    turns the marks into an exit, and the payload records that the reader asked.
    """
    _round(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "strict.json"

    assert cli.main(["evolve", "--history", "--strict", "--json-out", str(out)]) == 0
    flat = " ".join(capsys.readouterr().out.split())

    assert ("--strict this page reads the ledger and charges nothing; "
            "`brian evolve --audit --strict` turns the marks above into an exit") in flat
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["asked"] is True
    assert payload["gate"]["checked"] is False
    assert payload["gate"]["command"] == "brian evolve --audit --strict"


def test_a_checked_report_records_that_it_checked(tmp_path, monkeypatch):
    _round(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "campaign.json"

    assert cli.main(["evolve", "--campaign", "--json-out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["checked"] is True
    assert payload["gate"]["would_charge"] == []


def test_the_footer_helper_agrees_with_the_gate_dict():
    """The checked form carries no command: a page that charged does not need to be told where charging happens."""
    assert cli.report_footer(True) == {"asked": False, "checked": True}
    unchecked = cli.report_footer(False)
    assert unchecked["checked"] is False
    assert unchecked["asked"] is False
    assert "campaign --strict" in unchecked["command"]
