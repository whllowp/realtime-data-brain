"""Round 180 (item 166): one resolver for the sources a check reads, and a failed read that names the file.

Round 179's check read two files with two addresses, one of them relative to the caller's working directory; from
a fixture directory that turned 43 gate tests red with a false reason. The address and the failure message are
now one helper each (boundaries 430/431).
"""
from pathlib import Path

import pytest

from brian import cli


def test_the_resolver_points_beside_this_module_not_at_the_working_directory():
    resolved = cli._source_file("campaign.py")

    assert resolved.is_absolute()
    assert resolved.parent == Path(cli.__file__).resolve().parent
    assert resolved.exists()


def test_sources_are_read_with_universal_newlines():
    # src/brian/cli.py is the one tracked file stored with CRLF; a byte read would keep the \r and every anchor
    # written with \n would miss (round 179).
    assert "\r" not in cli._read_source("cli.py")


def test_a_failed_read_names_the_file_it_could_not_read():
    with pytest.raises(OSError) as caught:
        cli._read_source("nope.py")

    assert "src/brian/nope.py" in str(caught.value)


def test_the_inventory_still_checks_from_another_directory(tmp_path, monkeypatch):
    # The regression round 179 shipped: every gate runs this check, and the whole suite chdirs into a fixture.
    monkeypatch.chdir(tmp_path)

    assert cli.payload_writer_problems() == []


def test_a_verifier_that_loses_its_source_is_reported_with_its_path(monkeypatch):
    monkeypatch.setitem(cli.PAYLOAD_BUILDERS, "--campaign", lambda: cli._read_source("nope.py"))

    problems = cli.payload_writer_problems()

    assert any("the keys of --campaign cannot be read here (src/brian/nope.py" in problem
               for problem in problems)


def test_the_check_passes_on_the_real_tree(capsys):
    assert cli.main(["evolve", "--payload-writers", "--check"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "4 verified shape(s)" in printed
