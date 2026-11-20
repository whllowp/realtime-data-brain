# -*- coding: utf-8 -*-
"""Round 133: the plan counts only the work its command can do.

Round 132's what-if predicted that paying three re-takes would leave two stale, and the real run agreed -- by
refusing two pairs, one at a time, because those records now name a different successor and `reaffirm_link`
refuses to assert what the ledger denies (round 119). A page that says "do this" and then declines when the reader
does it is the plan failing at its own job, so an unpairable pair is now counted apart and carries the command that
can actually be run against it (`--settle-drop`).
"""
from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import (history, load_settlements, moved_settlements, reaffirm_plan,
                          withdraw_settlement)
from test_provenance_block import _round
from test_reaffirm_batch import FILE, LATER, RETIRED, SUCCESSOR, _plan, _two_stale


def _retarget(root, name="round-second.json", successor="round-moved-away.json"):
    """Point one settled record at another successor, in both books.

    The ledger line overrides the round file (rounds 34/120/128), so a fixture that rewrites only the file keeps
    telling the old story.
    """
    _round(root, name, retired={"re_measured_as": successor, "at": LATER.isoformat()})
    return root


def test_a_retargeted_pair_is_not_payable_stale_work(tmp_path):
    root = _retarget(_two_stale(tmp_path))
    row = _plan(root)[0]
    assert row["stale"] == 2
    assert row["unpayable"] == 1
    assert [item["record"] for item in row["unpayable_pairs"]] == ["round-second.json"]
    assert row["unpayable_pairs"][0]["names"] == "round-moved-away.json"
    assert "round-second.json" not in [item["record"] for item in row["pairs"]]


def test_the_unpayable_pair_carries_the_command_that_can_be_run(tmp_path):
    root = _retarget(_two_stale(tmp_path))
    row = _plan(root)[0]
    assert row["stuck"] == ["brian evolve --settle-drop round-second.json=%s --settle-why TEXT" % SUCCESSOR]


def test_a_file_owing_only_unpairable_work_still_shows_up(tmp_path):
    root = _two_stale(tmp_path)
    for name in ("round-old.json", "round-second.json", "round-third.json"):
        _round(root, name, retired={"re_measured_as": "round-moved-away.json", "at": LATER.isoformat()})
    rows = _plan(root)
    assert [row["stale"] for row in rows] == [0]
    assert rows[0]["unpayable"] >= 1


def test_the_page_names_the_pairs_no_re_take_can_pay(tmp_path):
    root = _retarget(_two_stale(tmp_path))
    page = " ".join(format_campaign(campaign(root)).split())
    assert "cannot be re-taken at all" in page
    assert "round-second.json -> %s" % SUCCESSOR in page
    assert "round-moved-away.json" in page
    assert "--settle-drop round-second.json=%s" % SUCCESSOR in page


def test_the_page_says_nothing_about_it_when_every_pair_is_payable(tmp_path):
    root = _two_stale(tmp_path)
    page = " ".join(format_campaign(campaign(root)).split())
    assert "cannot be re-taken at all" not in page


def test_the_batch_header_counts_only_what_it_can_pay(tmp_path, monkeypatch, capsys):
    root = _retarget(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    # Three stale decisions as the page counted them last round; two pay, one cannot be re-taken at all.
    assert "re-taking 2 decision(s) on %s in one command" % FILE in printed
    assert "2 of 2 decision(s)" in printed


def test_a_dry_run_previews_only_the_payable_pairs(tmp_path, monkeypatch, capsys):
    root = _retarget(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books",
                     "--dry-run"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    # The preview lists the pairs the real run would hand over, so the unpairable one is not among them.
    assert "round-second.json" not in printed
    assert "nothing was written" in printed


def test_the_per_file_cap_counts_what_a_command_can_pay(tmp_path, monkeypatch, capsys):
    root = _retarget(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    # Two payable pairs and one unpairable one: the cap of 2 is met by the payable count, so the cap paragraph stays
    # quiet. (This ledger still exits 2 -- the retargeted record names a round the ledger does not hold, which is a
    # dangling link. That is the *link* charge, and it must not be confused with the debt charge.)
    cli.main(["evolve", "--audit", "--strict", "--max-moved-per-file", "2"])
    printed = " ".join(capsys.readouterr().out.split())
    assert "above the 2 asked for for a single file" not in printed
    # The page that lists plans is `--campaign`; the audit charges the cap and names the link defect it found.
    assert "dangling" in printed


def test_the_plan_and_the_command_agree_about_what_is_payable(tmp_path):
    root = _retarget(_two_stale(tmp_path))
    row = _plan(root)[0]
    payable = {(item["record"], item["successor"]) for item in row["pairs"]}
    rows = reaffirm_plan([{"round": "round-second.json", "retired": {"re_measured_as": "round-moved-away.json"}}],
                         [{"record": "round-second.json", "successor": SUCCESSOR, "at": LATER.isoformat(),
                           "reaffirmations": [{"at": LATER.isoformat()}]}])
    assert ("round-second.json", SUCCESSOR) not in payable
    assert rows == [] or rows[0]["stale"] == 0


def test_an_unpayable_pair_is_never_counted_as_a_refusal_charge(tmp_path, monkeypatch, capsys):
    root = _retarget(_two_stale(tmp_path))
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "cannot re-affirm that decision:" not in printed


def test_a_withdrawn_decision_is_not_stale_work(tmp_path):
    from test_reaffirm_batch import _later_claim, _settled

    _settled(tmp_path)
    _later_claim(tmp_path)
    assert [row["stale"] for row in _plan(tmp_path)] == [1]
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise", by="alice")
    # Round 125's boundary 293: a withdrawn decision owes nothing. Round 133 found the plan still counting it --
    # work the re-take command refuses, for a reason the plan never printed.
    assert _plan(tmp_path) == []


def test_a_withdrawn_decision_is_not_reported_as_books_that_moved(tmp_path):
    from test_reaffirm_batch import _later_claim, _settled

    _settled(tmp_path)
    _later_claim(tmp_path)
    rounds = history(tmp_path, include_retired=True)
    assert [row["record"] for row in moved_settlements(rounds, load_settlements(tmp_path))] == [RETIRED]
    withdraw_settlement(tmp_path, RETIRED, SUCCESSOR, why="the books have said otherwise", by="alice")
    assert moved_settlements(history(tmp_path, include_retired=True), load_settlements(tmp_path)) == []
