"""Round 179 (item 165): `--campaign`'s 68 keys come from two sources, and neither may be skipped.

`Campaign.to_dict()` decides 59 of them; the branch in `cmd_evolve` assigns 9 more. A verifier that only read
the class would look complete -- it names 59 keys and compares them key for key -- while the nine that the
branch adds would be checked by nothing at all (boundary 428). `var/prove_179.py` shows the check firing when
a field is added to the class.
"""
from pathlib import Path

import pytest

from brian import cli

BRANCH_KEYS = ("budget", "ceiling_drift", "claim_counts", "claims", "debt_plan", "gate",
               "limits", "origin_queue", "rerun_offer")


def test_the_campaign_declaration_matches_what_the_two_sources_decide():
    resolved = set(cli._campaign_keys())
    declared = set(cli.PAYLOAD_KEYS["--campaign"])

    assert len(declared) == 68
    assert resolved == declared


@pytest.mark.parametrize("key", BRANCH_KEYS)
def test_every_key_the_branch_assigns_is_in_the_builder(key):
    # The class does not know these keys: if the builder ever fell back to `Campaign.to_dict()` alone, the
    # declaration would still list them and nothing would compare against them.
    source = Path("src/brian/cli.py").read_text(encoding="utf-8")
    class_source = Path("src/brian/campaign.py").read_text(encoding="utf-8")

    assert 'payload["%s"] = ' % key in source
    assert '"%s": self.' % key not in class_source
    assert key in set(cli._campaign_keys())


def test_a_branch_key_the_declaration_omits_is_a_problem(monkeypatch):
    trimmed = tuple(key for key in cli.PAYLOAD_KEYS["--campaign"] if key != "gate")
    monkeypatch.setitem(cli.PAYLOAD_KEYS, "--campaign", trimmed)

    problems = cli.payload_writer_problems()

    assert any("--campaign has the key(s) gate" in problem for problem in problems)


def test_a_declared_key_no_source_has_is_a_problem(monkeypatch):
    monkeypatch.setitem(cli.PAYLOAD_KEYS, "--campaign",
                        tuple(cli.PAYLOAD_KEYS["--campaign"]) + ("ghost",))

    problems = cli.payload_writer_problems()

    assert any("lists the key(s) ghost and no source here has them" in problem for problem in problems)


def test_a_verifier_that_cannot_read_the_sources_is_a_finding_not_a_crash(monkeypatch):
    def boom():
        raise OSError("the source moved")

    monkeypatch.setitem(cli.PAYLOAD_BUILDERS, "--campaign", boom)

    problems = cli.payload_writer_problems()

    assert any("the keys of --campaign cannot be read here (the source moved)" in problem
               for problem in problems)


def test_the_page_says_the_campaign_row_is_read_rather_than_built(capsys):
    assert cli.main(["evolve", "--payload-writers"]) == 0

    printed = " ".join(capsys.readouterr().out.split())
    assert "`--campaign` is read from `Campaign.to_dict()`" in printed
    assert "constructing one needs twelve arguments" in printed
