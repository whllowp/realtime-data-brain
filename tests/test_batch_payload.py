# -*- coding: utf-8 -*-
"""Round 140: a batch writes the payload its single form writes, and says so when it writes none.

Round 139 closed with four file-level settlement commands that answered `--json-out` in two different ways:
`--settle-reaffirm-file` and `--settle-restore-file` wrote the list, `--settle-drop-file` and
`--settle-reanchor-file` set `inner.json_out = None` and wrote nothing -- in silence. A flag that produces nothing
and says nothing is indistinguishable from a flag that was ignored, which is what it was.
"""
import json

from brian import cli
from test_reaffirm_batch import FILE, _two_stale
from test_restore_guard import _retarget, _withdrawn

WHY = "the books moved"
BY = "carol"


def _payload(root, name="out.json"):
    with open(root / name, encoding="utf-8") as handle:
        return json.load(handle)


def _retargeted(root, count=2):
    pairs = [(str(item["record"]), str(item["successor"]))
             for row in _plan_rows(root) for item in row["pairs"]][:count]
    assert len(pairs) == count, "the fixture must owe %d payable decision(s)" % count
    for record, _successor in pairs:
        _retarget(root, record)
    return pairs


def _plan_rows(root):
    from test_reaffirm_batch import _plan
    return [row for row in _plan(root) if row["origin"] == FILE and row.get("pairs")]


def test_the_reanchor_batch_writes_the_payload_it_paid(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    capsys.readouterr()
    assert code == 0
    payload = _payload(root)
    assert [item["record"] for item in payload["reanchored"]] == [record for record, _s in pairs]
    assert {item["to"] for item in payload["reanchored"]} == {"round-moved-away.json"}
    assert payload["reanchor_refused"] == 0


def test_the_drop_batch_writes_the_payload_it_paid(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _retargeted(root)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-drop-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    capsys.readouterr()
    assert code == 0
    payload = _payload(root)
    assert [item["record"] for item in payload["withdrawn"]] == [record for record, _s in pairs]
    assert payload["drop_refused"] == 0


def test_the_restore_batch_writes_the_payload_it_paid(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    pairs = _withdrawn(root, count=2)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-restore-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    capsys.readouterr()
    assert code == 0
    payload = _payload(root)
    assert [item["record"] for item in payload["restored"]] == [record for record, _s in pairs]
    assert payload["restore_refused"] == 0


def test_the_reaffirm_batch_writes_the_payload_it_paid(tmp_path, monkeypatch, capsys):
    # The two file commands that already forwarded the flag are here so one family cannot drift apart again: the
    # test names all four, not just the two round 140 repaired.
    root = _two_stale(tmp_path)
    pairs = _plan_rows(root)[0]["pairs"]
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    capsys.readouterr()
    assert code == 0
    payload = _payload(root)
    assert [item["record"] for item in payload["reaffirmed"]] == [str(item["record"]) for item in pairs]
    assert payload["reaffirm_refused"] == 0


def test_every_batch_names_the_list_it_wrote(tmp_path, monkeypatch, capsys):
    # One shape, four names: the payload is always {"<family>": [...], "<family>_refused": N}, and the list comes
    # first. A reader writing a script against one file command must not have to special-case the others.
    for index, (command, key) in enumerate((("--settle-reanchor-file", "reanchored"),
                                            ("--settle-drop-file", "withdrawn"))):
        # One root per family: paying the first decision leaves the file owing nothing, so reusing a root would
        # measure the second command against a file it has no work for (and read the first payload back).
        root = _two_stale(tmp_path / ("file-%d" % index))
        _retargeted(root)
        monkeypatch.chdir(root)
        assert cli.main(["evolve", command, FILE, "--settle-why", WHY, "--settle-by", BY,
                         "--json-out", "out.json"]) in (0, 2)
        payload = _payload(root)
        assert list(payload)[0] == key
        assert isinstance(payload[key], list)
        capsys.readouterr()


def test_a_dry_run_says_it_wrote_no_payload(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    _retargeted(root)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json", "--dry-run"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert ("no payload a preview writes nothing, not even the file --json-out names (boundary 304)"
            in printed)
    assert not (root / "out.json").exists()


def test_a_batch_that_pays_nothing_writes_no_payload(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reanchor-file", FILE, "--settle-why", WHY, "--settle-by", BY,
                     "--json-out", "out.json"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "no decision on data/smd/stream-one.json still names a successor its record has left" in printed
    # A refusal is not a payment: an empty payload would read as "the batch ran and paid nothing to nobody".
    assert not (root / "out.json").exists()
