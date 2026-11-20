"""Round 178 (item 164): `--check` builds the three payloads whose keys come from code, not from a literal.

A declaration is only checked when its branch runs: `--sensitivity` writes a payload nobody in CI drives, and
`RcaReport.to_dict()` is a class that changes without touching a single string in `cli.py`. So the three
declarations that are "a class plus the field the branch adds" are compared against the object itself
(boundaries 416/426) -- and `var/prove_178.py` shows the check firing on a real edit to `contracts.py`.
"""
import pytest

from brian import cli


def test_the_check_passes_on_the_real_tree_and_names_the_third_kind(capsys):
    assert cli.main(["evolve", "--payload-writers", "--check"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "4 verified shape(s)" in printed


@pytest.mark.parametrize("command", sorted(cli.PAYLOAD_BUILDERS))
def test_every_builder_agrees_with_its_declaration(command):
    """The builder is the second statement about how the payload is made; both must name the same keys."""
    built = set(cli.PAYLOAD_BUILDERS[command]())

    assert built == set(cli.PAYLOAD_KEYS[command])


def test_a_key_the_class_builds_and_the_declaration_omits_is_a_problem(monkeypatch):
    trimmed = tuple(key for key in cli.PAYLOAD_KEYS["--sensitivity"] if key != "gate")
    monkeypatch.setitem(cli.PAYLOAD_KEYS, "--sensitivity", trimmed)

    problems = cli.payload_writer_problems()

    assert any("--sensitivity has the key(s) gate" in problem for problem in problems)


def test_a_declared_key_nothing_builds_is_a_problem(monkeypatch):
    # The row must be one with a builder: a declared key on a row nobody builds is caught by the source scan,
    # which is a different check (and says "appears nowhere in this source" instead).
    monkeypatch.setitem(cli.PAYLOAD_KEYS, "--sensitivity",
                        tuple(cli.PAYLOAD_KEYS["--sensitivity"]) + ("ghost",))

    problems = cli.payload_writer_problems()

    assert any("lists the key(s) ghost and no source here has them" in problem for problem in problems)


def test_a_builder_that_cannot_run_is_a_finding_not_a_crash(monkeypatch):
    def boom():
        raise RuntimeError("the constructor moved")

    monkeypatch.setitem(cli.PAYLOAD_BUILDERS, "evaluate", boom)

    problems = cli.payload_writer_problems()

    assert any("cannot be read here (the constructor moved)" in problem for problem in problems)


def test_a_builder_without_a_declaration_is_a_problem(monkeypatch):
    monkeypatch.setitem(cli.PAYLOAD_BUILDERS, "--ghost-command", lambda: ("x",))

    problems = cli.payload_writer_problems()

    assert any("--ghost-command has a builder and no declared shape" in problem for problem in problems)


def test_the_page_says_which_rows_are_verified_by_building_them(capsys):
    assert cli.main(["evolve", "--payload-writers"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "also verifies 4 of them" in printed
    for command in cli.PAYLOAD_BUILDERS:
        assert command in printed
