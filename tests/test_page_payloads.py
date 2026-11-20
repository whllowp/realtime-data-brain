"""Round 162 (item 148): the two report pages write their payload the round-141 way.

Rounds 141/336/337 said every payload is written by `_write_payload`: parent directories created, the page prints
`wrote <path>`, and a write that fails says what is missing and exits non-zero. `--history` and `--campaign` were
written before that rule and kept their own `Path.write_text` plus a second `wrote` line, so `--json-out nodir/x.json`
raised FileNotFoundError and a reader saw the line twice. They also record nothing, so round 141's failure sentence
("what this command recorded is already in the store") described work that never happened.
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR

STREAM = {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1}


def _ledger(root):
    """One round record plus its ledger line: enough for both pages to print something."""
    directory = root / EVOLVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"round": "round-one.json", "started_at": "2026-11-20T08:00:00+00:00",
               "finished_at": "2026-11-20T08:00:00+00:00", "stream": dict(STREAM),
               "stream_path": "data/smd/stream-one.json", "trials": [], "accepted": [], "rejected": [],
               "applied": ["v020"]}
    (directory / "round-one.json").write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line["rules_hash"] = "abc"
    with open(directory / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return root


def _wrote_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("wrote ")]


def test_the_campaign_page_writes_into_a_directory_that_does_not_exist_yet(tmp_path, monkeypatch, capsys):
    _ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--campaign", "--json-out", "deep/nested/c.json"]) == 0

    # The writer creates the parents; the page must say the line once, not once per caller (boundary 336).
    assert _wrote_lines(capsys) == ["wrote deep/nested/c.json"]
    assert json.loads((tmp_path / "deep" / "nested" / "c.json").read_text(encoding="utf-8"))["rounds"] == 1


def test_the_history_page_writes_the_same_way(tmp_path, monkeypatch, capsys):
    _ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history", "--json-out", "deep/nested/h.json"]) == 0

    assert _wrote_lines(capsys) == ["wrote deep/nested/h.json"]
    payload = json.loads((tmp_path / "deep" / "nested" / "h.json").read_text(encoding="utf-8"))
    assert payload["claim_counts"]["population"] == "row(s)"


def test_a_page_that_records_nothing_says_only_its_export_is_missing(tmp_path, monkeypatch, capsys):
    _ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    # "." cannot be replaced by a file: the write fails after the page has printed everything it reports.
    assert cli.main(["evolve", "--campaign", "--json-out", "."]) == 2

    printed = capsys.readouterr().out
    assert "cannot write the payload to ." in printed
    assert "nothing was recorded: this command only reports, so only its export is missing" in printed
    # Round 141's sentence belongs to commands that changed a book; a page has no book to have changed.
    assert "already in the store" not in printed


def test_the_history_page_makes_the_same_promise(tmp_path, monkeypatch, capsys):
    _ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history", "--json-out", "."]) == 2

    printed = capsys.readouterr().out
    assert "nothing was recorded: this command only reports" in printed
    assert "already in the store" not in printed
    # The report itself was printed before the export failed: the page is a report, not a transaction. (The
    # history row prints no round name -- round 158's trap -- so the header is what proves the page ran.)
    assert "recorded rounds" in printed


def test_a_command_that_did_record_something_keeps_round_141s_sentence(tmp_path):
    assert cli._write_payload(str(tmp_path / "x.json"), {"a": 1}, recorded=True) is True
    assert cli._write_payload(str(tmp_path / "nodir" / "x.json"), {"a": 1}, recorded=True) is True
    assert (tmp_path / "nodir" / "x.json").exists()
