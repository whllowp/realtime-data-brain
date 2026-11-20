"""Round 181 (item 167): the page names the source files `--check` parses, and `--check` confirms both sides.

"Which files does this check read?" had no page: a source that moved showed up as the first failed read, in a
sentence about a payload shape rather than about a file that is gone (boundaries 431/432).
"""
import pytest

from brian import cli


def test_the_page_names_the_sources_the_check_parses(capsys):
    assert cli.main(["evolve", "--payload-writers"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "parsed from 2 source file(s)" in printed
    for name in cli.PAYLOAD_SOURCE_FILES:
        assert "src/brian/" + name in printed
    assert "every `_read_source` call names a listed file" in printed


def test_the_check_counts_the_sources(capsys):
    assert cli.main(["evolve", "--payload-writers", "--check"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "2 source file(s)" in printed


def test_every_listed_source_is_parsed_and_every_parsed_source_is_listed():
    text = cli._read_source("cli.py")
    parsed = set(cli._read_source_names(text))

    assert parsed == set(cli.PAYLOAD_SOURCE_FILES)


def test_a_listed_source_that_cannot_be_read_is_a_finding(monkeypatch):
    monkeypatch.setattr(cli, "PAYLOAD_SOURCE_FILES", ("cli.py", "nope.py"))

    problems = cli.payload_writer_problems()

    assert any("a source this check parses cannot be read" in problem and "src/brian/nope.py" in problem
               for problem in problems)


def test_a_listed_source_nothing_parses_is_a_problem(monkeypatch):
    monkeypatch.setattr(cli, "PAYLOAD_SOURCE_FILES", ("cli.py", "campaign.py", "evolve.py"))

    problems = cli.payload_writer_problems()

    assert any("lists src/brian/evolve.py as a source of this check and nothing parses it" in problem
               for problem in problems)


def test_a_parsed_source_the_page_forgets_is_a_problem(monkeypatch):
    monkeypatch.setattr(cli, "PAYLOAD_SOURCE_FILES", ("cli.py",))

    problems = cli.payload_writer_problems()

    assert any("parses src/brian/campaign.py and the page does not list it as a source" in problem
               for problem in problems)


def test_the_reader_finds_calls_and_not_its_own_definition():
    names = cli._read_source_names(cli._read_source("cli.py"))

    assert "cli.py" in names  # `_read_source("cli.py")` is a real call
    assert "campaign.py" in names
    assert "name" not in names  # `def _read_source(name: str)` is not a call


@pytest.mark.parametrize("name", ("cli.py", "campaign.py"))
def test_each_listed_source_is_readable(name):
    assert cli._read_source(name).strip()
