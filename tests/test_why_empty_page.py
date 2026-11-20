"""Round 173 (item 159): the three `why_empty` values are documented and checked, not just named.

Round 172 added the field and one word in the table's payload string. A value a script branches on needs two more
things: a page saying what each value means, and a check that the values the page lists are the values the command
can produce -- once each, because a reason assigned in two places is two statements about the same nothing.
"""

from pathlib import Path

import pytest

from brian import cli

SOURCE = Path(cli.__file__).read_bytes().decode("utf-8")


def test_the_page_says_what_each_empty_page_means():
    page = cli.format_payload_writers()

    for reason, meaning in cli.WHY_EMPTY_REASONS:
        assert reason in page, reason
        assert meaning in page, meaning
    assert "`null` means" in page


def test_the_three_reasons_are_the_three_the_command_can_report(tmp_path, monkeypatch, capsys):
    """The page's list and the branch's assignments are the same three strings."""
    observed = []
    for record in ([], [{"round": "round-placed.json", "stream_path": "data/smd/stream-one.json"}],
                   [{"round": "round-lost.json"}]):
        root = tmp_path / str(len(observed))
        root.mkdir()
        ledger = root / "var" / "evolve"
        ledger.mkdir(parents=True)
        for item in record:
            (ledger / item["round"]).write_text(__import__("json").dumps(dict(item, applied=[], rejected=[])),
                                                encoding="utf-8")
            with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
                handle.write(__import__("json").dumps(item) + "\n")
        monkeypatch.chdir(root)
        out = root / "p.json"
        assert cli.main(["evolve", "--unrecorded", "--json-out", str(out)]) == 0
        observed.append(__import__("json").loads(out.read_text(encoding="utf-8"))["why_empty"])
        capsys.readouterr()

    assert observed == [reason for reason, _meaning in cli.WHY_EMPTY_REASONS]


def test_a_documented_reason_the_source_never_assigns_is_a_problem():
    broken = SOURCE.replace('why = "all placed"', 'why = "everything placed"')

    problems = cli.payload_writer_problems(broken)

    assert any("documents the why_empty value 'all placed' and no branch of this source assigns it" in problem
               for problem in problems)


def test_a_reason_assigned_twice_is_a_problem():
    broken = SOURCE.replace('why = "all placed"', 'why = "all placed"\n                why = "all placed"')

    problems = cli.payload_writer_problems(broken)

    assert any("'all placed' in 2 place(s)" in problem for problem in problems)


def test_a_reason_the_source_reports_and_the_page_never_documents_is_a_problem(monkeypatch):
    """The page loses an entry: the source still assigns it, so the page is the one that is wrong."""
    monkeypatch.setattr(cli, "WHY_EMPTY_REASONS", cli.WHY_EMPTY_REASONS[:2])

    problems = cli.payload_writer_problems()

    assert any("the source can report why_empty = 'none deciding' and the page does not say what it means" in problem
               for problem in problems)


def test_a_documented_reason_whose_meaning_the_page_drops_is_a_problem(monkeypatch):
    """The entry survives but its meaning does not reach the page -- a name alone leaves the caller guessing."""
    page = cli.format_payload_writers()
    assert "the ledger holds no round yet" in page
    monkeypatch.setattr(cli, "format_payload_writers", lambda: page.replace("the ledger holds no round yet", "..."))

    problems = cli.payload_writer_problems()

    assert any("names 'no rounds' without saying what it means" in problem for problem in problems)


def test_the_check_says_how_many_values_it_read(capsys):
    assert cli.main(["evolve", "--payload-writers", "--check"]) == 0

    printed = capsys.readouterr().out
    assert "the page and the source agree" in printed
    assert "%d why_empty value(s)" % len(cli.WHY_EMPTY_REASONS) in printed


@pytest.mark.parametrize("reason", [reason for reason, _meaning in cli.WHY_EMPTY_REASONS])
def test_every_reason_is_assigned_once_in_the_source(reason):
    assert SOURCE.count('why = "%s"' % reason) == 1
