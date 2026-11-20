# -*- coding: utf-8 -*-
"""Round 141: a payload is an artefact, so writing one is announced and failing to write one is said.

Eight call sites wrote a `--json-out` payload with a bare `open(...)`: they printed nothing, and when the path could
not be written they raised a traceback -- *after* the decisions had been recorded. `--json-out nodir\\p.json` gave
the caller a FileNotFoundError in a run that had already paid, which is the worst of both answers: nothing to read,
and a half-done command. One helper now creates the parents, renames the file into place and prints `wrote <path>`,
the same words the non-settlement commands have always used.
"""
import json

from brian import cli
from test_reaffirm_batch import FILE, _two_stale
from test_restore_guard import _retarget, _withdrawn

WHY = "the books moved"
BY = "carol"


def _retargeted(root, count=2):
    from test_reaffirm_batch import _plan
    pairs = [(str(item["record"]), str(item["successor"]))
             for row in _plan(root) if row["origin"] == FILE for item in row["pairs"]][:count]
    assert len(pairs) == count, "the fixture must owe %d payable decision(s)" % count
    for record, _successor in pairs:
        _retarget(root, record)
    return pairs


def test_a_written_payload_is_announced(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "wrote out.json" in printed


def test_the_payload_directory_is_created(tmp_path, monkeypatch, capsys):
    # `--json-out deep/nested/p.json` used to die on a missing directory; a payload path is a path, not a promise
    # that the reader already made the folders (round 113's rule for the round record, applied to every payload).
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "deep/nested/p.json"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "wrote deep/nested/p.json" in printed
    with open(root / "deep" / "nested" / "p.json", encoding="utf-8") as handle:
        assert len(json.load(handle)["reanchored"]) == 2


def test_a_payload_that_cannot_be_written_is_a_sentence_not_a_traceback(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    # The cwd is a directory: the write fails at the rename, whichever way the platform words it.
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "."])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "cannot write the payload to ." in printed
    # and the run says what it did manage: the decisions are in the store, only the payload is missing (boundary 337)
    assert "what this command recorded is already in the store: only the payload is missing" in printed
    assert len(json.loads((root / "var" / "evolve" / "settlements.json").read_text(encoding="utf-8"))
               ["settlements"]) >= 2


def test_no_temporary_file_is_left_behind(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"]) == 0
    capsys.readouterr()
    assert sorted(name for name in ("out.json", "out.json.tmp") if (root / name).exists()) == ["out.json"]


def test_the_single_record_commands_announce_their_payload_too(tmp_path, monkeypatch, capsys):
    # Three more call sites in the same family: `--settle-link`, `--settle-note` and `--settle-drop` wrote silently.
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-note", "round-third.json=round-new.json",
                     "--settle-why", WHY, "--settle-by", BY, "--json-out", "note.json"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "wrote note.json" in printed
    with open(root / "note.json", encoding="utf-8") as handle:
        assert "noted" in json.load(handle)


def test_a_refused_batch_says_wrote_nothing(tmp_path, monkeypatch, capsys):
    # The announcement is evidence, not decoration: a command that wrote no file must not have the line, or the
    # line stops meaning anything (boundary 336).
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "wrote" not in printed
    assert not (root / "out.json").exists()


def test_the_restore_batch_announces_its_payload(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _withdrawn(root, count=2)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-restore-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "restored.json"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "wrote restored.json" in printed
