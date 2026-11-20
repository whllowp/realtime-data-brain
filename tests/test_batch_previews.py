"""The batch commands `--dry-run` can preview are a family, and a family is one table (round 148).

Rounds 135 and 147 each added a batch command and updated one of two hand-written lists -- the guard's condition, or
the sentence it printed -- so the guard refused the preview the change had just made possible. The help text was a
third list and named two of five. These tests hold the one table to the parser, in both directions, so a sixth
file-level command cannot be half-added.
"""

from __future__ import annotations

import argparse

import pytest

from brian import cli


def _evolve_options() -> set[str]:
    """Every option string the `evolve` subcommand accepts, read from the parser itself."""
    parser = cli.build_parser()
    subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    assert subparsers, "the parser has no subcommands"
    return {option for action in subparsers[0].choices["evolve"]._actions for option in action.option_strings}


def _namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def test_the_sentence_names_every_command_once_in_table_order() -> None:
    sentence = cli.previews_sentence()
    flags = [flag for flag, _attribute in cli.BATCH_PREVIEWS]
    assert sentence == ", ".join(flags[:-1]) + " or " + flags[-1]
    for flag in flags:
        assert sentence.count(flag) == 1


def test_one_row_reads_as_one_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "BATCH_PREVIEWS", (("--settle-drop-file", "settle_drop_file"),))
    assert cli.previews_sentence() == "--settle-drop-file"


def test_no_row_does_not_read_as_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "BATCH_PREVIEWS", ())
    assert cli.previews_sentence() == "no command"


def test_the_flags_a_run_asked_for_come_back_in_the_tables_order() -> None:
    asked = _namespace(settle_drop_file="data/x.json", backfill_walks=True)
    assert cli.previewing_flags(asked) == ["--settle-drop-file", "--backfill-walks"]
    assert cli.previewing_flags(_namespace()) == []


@pytest.mark.parametrize("flag,attribute", cli.BATCH_PREVIEWS)
def test_the_guard_accepts_every_row(flag: str, attribute: str) -> None:
    # This is the defect itself: rounds 135/147 shipped a command whose own preview the guard refused.
    assert cli.previewing_flags(_namespace(**{attribute: flag if attribute.endswith("_file") else True})) == [flag]


def test_the_table_holds_every_file_level_command_the_parser_offers() -> None:
    offered = {option for option in _evolve_options() if option.startswith("--settle-") and option.endswith("-file")}
    # `--backfill-walks` is a batch preview too, and the only one the naming rule does not cover.
    offered.add("--backfill-walks")
    assert {flag for flag, _attribute in cli.BATCH_PREVIEWS} == offered


def test_every_row_names_a_flag_the_parser_really_has() -> None:
    offered = _evolve_options()
    for flag, _attribute in cli.BATCH_PREVIEWS:
        assert flag in offered, "%s is in the table but the evolve command has no such option" % flag


def test_the_help_text_is_the_same_sentence() -> None:
    parser = cli.build_parser()
    subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    evolve = subparsers[0].choices["evolve"]
    help_text = next(action.help for action in evolve._actions if "--dry-run" in action.option_strings)
    assert cli.previews_sentence() in help_text


def test_using_the_flag_without_a_batch_command_still_refuses(capsys: pytest.CaptureFixture[str],
                                                              tmp_path) -> None:
    code = cli.main(["evolve", "--audit", "--dry-run"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 2
    assert "--dry-run previews %s" % cli.previews_sentence() in printed
