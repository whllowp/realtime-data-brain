"""Round 174 (item 160): a payload's key set is declared, rendered from the declaration, and refused when it drifts.

The `{...}` column was prose for the first twenty-two rounds of the inventory: a row could name a key nothing writes
or omit one every run writes, and every check stayed green. Three payloads now declare their keys, the page renders
those rows from the declaration, and `_write_payload` refuses a payload that disagrees with it.
"""

import json

import pytest

from brian import cli


def _row_text(command):
    page = cli.format_payload_writers()
    for line in page.splitlines():
        if line.strip().startswith(command + " "):
            return line
    raise AssertionError("no row for %s" % command)


def test_a_matching_payload_is_written_and_says_where(tmp_path, capsys):
    target = tmp_path / "ok.json"

    assert cli._write_payload(str(target), {"a": 1, "b": 2}, keys=("a", "b")) is True

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert "wrote %s" % target in capsys.readouterr().out


def test_a_declared_key_the_payload_lacks_is_refused(tmp_path, capsys):
    target = tmp_path / "missing.json"

    assert cli._write_payload(str(target), {"a": 1}, keys=("a", "b")) is False

    printed = capsys.readouterr().out
    assert "the payload this command built does not match the inventory" in printed
    assert "declared but missing: b" in printed
    assert not target.exists()


def test_a_key_the_declaration_never_mentions_is_refused(tmp_path, capsys):
    target = tmp_path / "extra.json"

    assert cli._write_payload(str(target), {"a": 1, "c": 2}, keys=("a",)) is False

    printed = capsys.readouterr().out
    assert "built but not declared: c" in printed
    assert not target.exists()


def test_an_undeclared_payload_is_written_as_before(tmp_path):
    target = tmp_path / "prose.json"

    assert cli._write_payload(str(target), {"whatever": True}) is True

    assert json.loads(target.read_text(encoding="utf-8")) == {"whatever": True}


@pytest.mark.parametrize("command", sorted(cli.PAYLOAD_KEYS))
def test_the_page_renders_a_declared_row_from_the_declaration(command):
    rendered = _row_text(command)
    quoted = [token for token in rendered.split('"')[1::2] if token.strip()]

    assert quoted == list(cli.PAYLOAD_KEYS[command])


def test_the_page_says_which_rows_are_declared_and_which_are_prose():
    page = cli.format_payload_writers()

    assert "%d row(s)" % len(cli.PAYLOAD_KEYS) in page
    for command in cli.PAYLOAD_KEYS:
        assert command in page
    assert "`--campaign` alone writes far more keys" in page


def _ledger(root):
    """One recorded round, in both books: a round file alone is invisible to every page (round 153's lesson)."""
    store = root / "var" / "evolve"
    store.mkdir(parents=True, exist_ok=True)
    record = {
        "round": "round-a.json",
        "started": "2026-11-18T00:00:00Z",
        "finished": "2026-11-18T00:05:00Z",
        "source": "SMD",
        "stream_path": "data/smd/stream-one.json",
        "rules": {},
        "accepted": [],
        "trials": [],
        "baseline": {"name": "baseline", "recall": 0.5, "fpr": 0.1, "reports": 4, "tokens_per_report": 100.0},
    }
    (store / "round-a.json").write_text(json.dumps(record), encoding="utf-8")
    (store / "ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


@pytest.mark.parametrize("flag", ["--unrecorded", "--history", "--audit"])
def test_each_declared_page_writes_exactly_its_declared_keys(tmp_path, monkeypatch, capsys, flag):
    monkeypatch.chdir(tmp_path)
    _ledger(tmp_path)
    out = tmp_path / "payload.json"

    code = cli.main(["evolve", flag, "--json-out", str(out)])

    assert code in (0, 1), capsys.readouterr().out
    written = json.loads(out.read_text(encoding="utf-8"))
    assert sorted(written) == sorted(cli.PAYLOAD_KEYS[flag])


def test_a_declared_key_the_source_never_mentions_is_a_problem(monkeypatch):
    broken = dict(cli.PAYLOAD_KEYS)
    broken["--audit"] = cli.PAYLOAD_KEYS["--audit"] + ("a_key_no_branch_writes",)
    monkeypatch.setattr(cli, "PAYLOAD_KEYS", broken)

    problems = cli.payload_writer_problems()

    assert any("appears nowhere in this source" in problem for problem in problems)


def test_the_check_agrees_with_the_real_source():
    assert cli.payload_writer_problems() == []


def test_the_declaration_is_not_empty():
    """A rule that checks three rows and calls itself the inventory must say how many it covers."""
    assert len(cli.PAYLOAD_KEYS) >= 3
    assert all(isinstance(keys, tuple) and keys for keys in cli.PAYLOAD_KEYS.values())
