"""Round 174 (item 160): a payload's key set is declared, rendered from the declaration, and refused when it drifts.

The `{...}` column was prose for the first twenty-two rounds of the inventory: a row could name a key nothing writes
or omit one every run writes, and every check stayed green. Three payloads now declare their keys, the page renders
those rows from the declaration, and `_write_payload` refuses a payload that disagrees with it.
"""

import json

import pytest

from brian import cli


def _row_text(command):
    """The row and its continuation lines, joined (round 175: a declared row wraps)."""
    page = cli.format_payload_writers().splitlines()
    for index, line in enumerate(page):
        if line.strip().startswith(command + " "):
            parts = [line]
            for following in page[index + 1:]:
                if not following.startswith(" " * 60) or following.strip()[:1] == "`":
                    break
                parts.append(following)
            return " ".join(part.strip() for part in parts)
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
    # Round 175 (item 161): the prose rows are counted, not named by hand, so declaring another row
    # changes the sentence without anybody remembering to edit it.
    assert "the %d row(s) not declared here" % (len(cli.PAYLOAD_WRITERS) - len(cli.PAYLOAD_KEYS)) in page


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


@pytest.mark.parametrize("flag", ["--unrecorded", "--history", "--audit", "--campaign"])
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


def test_the_campaign_declaration_comes_from_the_sources_that_build_it():
    """Round 175 (item 161): a run shows the branches it took; the declaration must come from the code.

    Two places build this payload -- `Campaign.to_dict()` in src/brian/campaign.py and the assignments beside the
    write in src/brian/cli.py -- so both are read here, by shape, and every key either one names must be declared.
    """
    import re

    from brian import campaign as campaign_module

    expected = set(cli.PAYLOAD_KEYS["--campaign"])

    source = __import__("pathlib").Path(campaign_module.__file__).read_text(encoding="utf-8")
    class_at = source.index("\nclass Campaign:")
    start = source.index("    def to_dict(self)", class_at)
    literal = source[start:]
    literal = literal[literal.index("return {"):]
    # The literal ends at the first line that closes it (eight spaces and a brace); reading to the next `@` or to
    # the end of the file walked into the helpers below and counted their nested keys as top-level ones.
    literal = literal[: literal.index("\n        }\n") + 1]
    # Top-level keys are the ones indented twelve spaces; a nested dict's keys are deeper, and this payload has
    # several (`limits`, `queue_paid_by_domain`), whose keys are not keys of the payload.
    declared = set(re.findall(r'\n {12}"([a-z_]+)":', literal))

    assert declared, "the parse found no keys in Campaign.to_dict"
    assert declared <= expected, "Campaign.to_dict writes keys the page does not declare: %s" % sorted(declared - expected)

    cli_source = __import__("pathlib").Path(cli.__file__).read_text(encoding="utf-8")
    line_start = cli_source.index("if args.campaign:")
    branch = cli_source[line_start:cli_source.index("\n    if args.sensitivity:", line_start)]
    assigned = set(re.findall(r'\n\s+payload\["([a-z_]+)"\] = ', branch))
    assert assigned, "the parse found no assignments in the campaign branch"
    assert assigned <= expected, "the campaign branch writes keys the page does not declare: %s" % sorted(assigned - expected)


def test_a_declared_row_wraps_instead_of_running_off_the_terminal():
    """A 68-key row on one line is 1,400 characters: the page is the deliverable, so it must fit."""
    page = cli.format_payload_writers().splitlines()

    assert max(len(line) for line in page) <= 170
    start = next(index for index, line in enumerate(page) if line.strip().startswith("--campaign "))
    assert page[start + 1].strip().startswith('"'), "the campaign row does not continue on the next line"
    assert len(cli.PAYLOAD_KEYS["--campaign"]) > 60


def test_an_empty_declaration_prints_instead_of_crashing(monkeypatch):
    """Round 176: doctoring a declaration to `()` used to make the page raise IndexError.

    An empty tuple is a claim ("this payload has no keys"), and the page has to be able to print the claim it
    enforces -- otherwise the command that checks the page is the command that crashes on it (boundary 422).
    """
    monkeypatch.setattr(cli, "PAYLOAD_KEYS", dict(cli.PAYLOAD_KEYS, **{"--settle-link": ()}))

    page = cli.format_payload_writers()
    row = [line for line in page.splitlines() if line.strip().startswith("--settle-link")][0]

    assert "no keys declared" in row


def test_the_declared_rows_come_from_their_write_sites_not_from_a_run():
    """Every declared row's keys must be quoted somewhere in the source: the declaration is read off the writer.

    This is the weak half of the check (round 174's `payload_writer_problems` owns the strong half at write time);
    it exists so a row copied from a run's output with a key no branch builds fails here first.
    """
    import pathlib

    source = pathlib.Path(cli.__file__).read_text(encoding="utf-8")

    assert len(cli.PAYLOAD_KEYS) >= 19
    for command, keys in cli.PAYLOAD_KEYS.items():
        assert keys, command
        for key in keys:
            assert '"%s"' % key in source, "%s declares %r and no line of this source writes it" % (command, key)
