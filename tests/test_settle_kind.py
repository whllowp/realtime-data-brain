# -*- coding: utf-8 -*-
"""Round 123: the two kinds of stale work are paid by separate commands, and both are counted per domain.

Round 122 split the counts on the file row but still paid them together, so a reader who only owed the cheap
job (a repair after movement) had to do the expensive one (a first reading) in the same breath. `--settle-kind`
restricts the batch; the run says what it left alone; the JSON says it too; and the domain row, which carries
every other settlement state, now carries both counts.
"""
import json

from brian import cli
from brian.campaign import campaign

from test_reaffirm_batch import FILE, _plan, _two_stale


def _rows(root):
    return {row["origin"]: row for row in _plan(root)}


def test_each_pair_carries_the_kind_of_work_it_owes(tmp_path):
    rows = _rows(_two_stale(tmp_path))
    pairs = [item for row in rows.values() for item in row["pairs"]]
    # One decision was re-read and moved again; two were never read for. The kind must travel with the pair,
    # because the batch filters on it (boundary 285).
    assert sorted(item["kind"] for item in pairs) == ["again", "never", "never"]


def test_never_pays_only_the_first_readings(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    out = root / "again.json"
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-kind", "never",
                     "--settle-why", "first reading", "--settle-by", "alice", "--json-out", str(out)])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "re-taking 2 decision(s) on %s in one command (2 never re-taken, 1 re-taken before and moved again)" % FILE in printed
    assert "leaving 1 decision(s) of the other kind alone" in printed
    # The `again` one is still stale: paying the cheap kind must not silently mark the expensive one read.
    remaining = [item for row in _plan(root) for item in row["pairs"]]
    assert [(item["kind"], item["record"]) for item in remaining] == [("again", "round-old.json")]
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [item["record"] for item in payload["reaffirmed"]] == ["round-second.json", "round-third.json"]
    assert payload["reaffirm_skipped"] == 1


def test_again_pays_only_the_repairs(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-kind", "again",
                     "--settle-why", "the books moved again", "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "leaving 2 decision(s) of the other kind alone" in printed
    remaining = [item for row in _plan(root) for item in row["pairs"]]
    assert [item["kind"] for item in remaining] == ["never", "never"]


def test_all_is_the_default_and_pays_everything(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"]) == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert "leaving" not in printed
    assert _plan(root) == []


def test_an_unknown_kind_is_refused(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-kind", "sometimes",
                     "--settle-why", "read today's books"])
    assert code == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert "--settle-kind takes never, again or all" in printed
    assert len(_plan(root)[0]["pairs"]) == 3


def test_a_file_that_owes_only_the_other_kind_says_so(tmp_path, monkeypatch, capsys):
    root = _never_read_root(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-kind", "again",
                     "--settle-why", "the books moved again"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "no stale decision of the kind `again`" in printed
    assert "without --settle-kind to pay them" in printed


def test_the_domain_row_carries_both_kinds(tmp_path):
    _two_stale(tmp_path)
    result = campaign(tmp_path)
    states = {row.domain: row.settlement_state for row in result.rows}
    # Every stale decision is attributed to the stream of the round it settled (round 117's rule, one field over).
    assert sum(state.get("stale_never", 0) for state in states.values()) == 2
    assert sum(state.get("stale_again", 0) for state in states.values()) == 1
    assert sum(state.get("moved", 0) for state in states.values()) == 3


def test_the_domain_json_carries_the_counts(tmp_path):
    _two_stale(tmp_path)
    payload = campaign(tmp_path).to_dict()
    row = payload["domains"][0]
    assert row["settlements"].get("stale_never", 0) + row["settlements"].get("stale_again", 0) == 3


def test_a_pair_no_command_can_pay_is_counted_under_moved_only(tmp_path):
    from test_settlement_evidence import _drop_the_claim
    from test_settle_note import _settled_without_reason

    _settled_without_reason(tmp_path)
    _drop_the_claim(tmp_path)
    state = campaign(tmp_path).rows[0].settlement_state
    # Stale, chargeable by nothing, and therefore not split into a kind of work: "1 decision to take again" is a
    # different sentence from "1 reading to redo" (boundary 279 applied to a pair).
    assert state == {"moved": 1}


def _never_read_root(root):
    from test_reaffirm_batch import _clamp, _later_claim
    from test_settlement_reaffirm import _settled

    _settled(root)
    _later_claim(root)
    return _clamp(root)
