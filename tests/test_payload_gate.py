"""Round 166 (item 152): the gate reads the payload page, so a stale table cannot pass a green `--strict`.

Round 165 gave the inventory a command that checks it (`--payload-writers --check`), and round 165's own first run
of that command refused the change that introduced it. But a command only speaks to whoever remembers to run it: a
table that no longer describes the source could still ride a fully green `--audit --strict` into the trunk. This
round puts the same question in the gate, where the other build defects are charged (round 33/81's exit 2).

The tests are written to show the charge fire, not to describe it: a `_strict_exit` that returns 2 only because a
constant is 0 would be a declaration (boundary 359).
"""

from brian import cli


def test_the_gate_charges_a_page_that_does_not_describe_the_source(capsys):
    assert cli._strict_exit(True, 0, 0, page_problems=1) == 2

    printed = capsys.readouterr().out
    assert "payload page      1 problem(s): the --payload-writers table does not describe this source" in printed
    assert "run `brian evolve --payload-writers --check` for the list (boundary 397)" in printed


def test_a_page_that_agrees_charges_nothing(capsys):
    assert cli._strict_exit(True, 0, 0, page_problems=0) == 0

    assert "payload page" not in capsys.readouterr().out


def test_without_strict_the_page_is_not_read(capsys):
    """`--audit` is a report unless `--strict` asks it to be a check (round 15's rule)."""
    assert cli._strict_exit(False, 0, 0, page_problems=1) == 0

    assert "payload page" not in capsys.readouterr().out


def test_a_build_defect_outranks_a_moved_verdict(capsys):
    """Exit 2 for "cannot check", exit 1 for "would decide differently" (round 32's ordering)."""
    assert cli._strict_exit(True, 3, 0, page_problems=2) == 2


def test_the_audit_gate_reads_the_page_it_is_given(tmp_path, monkeypatch, capsys):
    """The gate must call the same check the command prints, or the two disagree about the same table."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["the page lists 0 payload writer(s)"])

    assert cli.main(["evolve", "--audit", "--strict"]) == 2

    printed = capsys.readouterr().out
    assert "payload page      1 problem(s)" in printed


def test_the_campaign_gate_reads_it_too(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["the page does not name --settle-x"])

    assert cli.main(["evolve", "--campaign", "--strict"]) == 2

    assert "payload page      1 problem(s)" in capsys.readouterr().out


def test_a_report_is_not_a_check(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "payload_writer_problems", lambda source=None: ["the page lists 0 payload writer(s)"])

    assert cli.main(["evolve", "--audit"]) == 0

    assert "payload page" not in capsys.readouterr().out


def test_the_real_page_passes_the_real_gate():
    """The deployment's own table against its own source: the check must be silent here, or every gate run is red."""
    assert cli.payload_writer_problems() == []
