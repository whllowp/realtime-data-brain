"""Round 171 (item 157), part two: the four pages that only read the ledger all say so.

`--history`, `--sensitivity`, `--withheld` and `--unrecorded` print a summary and charge nothing. None of them
carried a `gate` key, so a consumer could not tell "nothing charged" from "this build never checked" -- and a
terminal reader got silence, which reads as good news (boundary 406).

The checked pages (`--audit`, `--campaign`) are covered in `tests/test_report_gate.py`.
"""
import json

from brian import cli


def _empty(root):
    (root / "var" / "evolve").mkdir(parents=True, exist_ok=True)
    return root


def _gate(path):
    return json.loads(path.read_text(encoding="utf-8"))["gate"]


def test_the_sensitivity_payload_says_the_page_did_not_check(tmp_path, monkeypatch):
    _empty(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "sensitivity.json"

    assert cli.main(["evolve", "--sensitivity", "--json-out", str(out)]) == 0

    assert _gate(out)["checked"] is False
    assert _gate(out)["command"] == "brian evolve --campaign --strict"


def test_the_sensitivity_page_prints_the_line(tmp_path, monkeypatch, capsys):
    _empty(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--sensitivity"]) == 0

    flat = " ".join(capsys.readouterr().out.split())
    assert "gate this page reads the ledger and charges nothing" in flat


def test_the_withheld_payload_and_page_say_the_same(tmp_path, monkeypatch, capsys):
    _empty(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "withheld.json"

    assert cli.main(["evolve", "--withheld", "--json-out", str(out)]) == 0

    flat = " ".join(capsys.readouterr().out.split())
    assert "gate this page reads the ledger and charges nothing" in flat
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["checked"] is False
    assert payload["withheld"] == []


def test_the_inventory_page_names_the_key_these_pages_now_write():
    """The rows are the page's description of each payload; a new key that the table omits is a stale description."""
    rows = {name: keys for name, keys, kind in cli.PAYLOAD_WRITERS}

    assert '"gate"' in rows["--history"]
    assert '"gate"' in rows["--unrecorded"]
    assert '"gate"' in rows["--sensitivity"]
    assert '"gate"' in rows["--withheld"]
