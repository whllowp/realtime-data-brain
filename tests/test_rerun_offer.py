"""The campaign page says what the re-run queue could pay today, not only how the last payments were made.

Round 147 read the ledger's retirement marks back into the store, and the page printed `0 by the queue, 17 typed by
hand` from then on: true, and useless to a reader. Round 149 adds the count the queue could pay and the command that
pays it, plus the one sentence an operator needs when the store holds hand payments and no queued walk.
"""

from __future__ import annotations

import pytest

from brian import cli
from brian.campaign import campaign, format_campaign


def _offer(monkeypatch: pytest.MonkeyPatch, *, file_rounds: int = 0, ceiling_rounds: int = 0,
           hand: int = 0, queued: int = 0) -> dict:
    monkeypatch.setattr(cli, "_unrecorded_matters",
                        lambda root, library=None: [("d%d" % index, "round-%d.json" % index, "why", [])
                                                    for index in range(file_rounds)])
    monkeypatch.setattr(cli, "_ceiling_matters",
                        lambda root: [("d%d" % index, "round-c%d.json" % index, "why", "data/x.json")
                                      for index in range(ceiling_rounds)])
    monkeypatch.setattr(cli, "rerun_walks", lambda root: {"walks": hand + queued, "hand": hand, "queued": queued})
    return cli._rerun_offer(None)


def test_a_paid_off_ledger_says_nothing_is_payable(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch)
    assert offer["rounds"] == 0
    assert offer["command"] == ""
    assert offer["text"].startswith("nothing for the queue to pay today")


def test_the_file_queue_names_the_plain_command(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, file_rounds=2)
    assert offer["rounds"] == 2
    assert "(2 from the file queue)" in offer["text"]
    assert offer["command"] == "brian evolve --rerun-unrecorded --rerun-limit 0"
    assert "--with-ceilings" not in offer["text"]


def test_the_named_file_family_names_the_command_with_ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, ceiling_rounds=1)
    assert "(1 that named their file but never recorded their ceilings)" in offer["text"]
    assert offer["command"] == "brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0"


def test_both_families_are_counted_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, file_rounds=1, ceiling_rounds=2)
    assert offer["rounds"] == 3
    assert "(1 from the file queue, 2 that named their file but never recorded their ceilings)" in offer["text"]


def test_a_store_of_hand_payments_says_the_queue_has_never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, ceiling_rounds=1, hand=17, queued=0)
    assert "the queue has paid none of this store's 17 payment(s)" in offer["text"]
    assert "every one was typed by hand" in offer["text"]


def test_a_store_with_a_queued_walk_does_not_say_that(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, ceiling_rounds=1, hand=17, queued=3)
    assert "typed by hand" not in offer["text"]


def test_nothing_payable_does_not_carry_the_explanation(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, hand=17, queued=0)
    assert "typed by hand" not in offer["text"]


def test_the_offer_and_the_debt_line_name_the_same_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # One builder (`_rerun_command`), two lines: if either drifts this fails rather than printing two commands.
    named = _offer(monkeypatch, ceiling_rounds=1)["command"]
    assert named == cli._ceiling_command(payable=0, named=1)
    plain = _offer(monkeypatch, file_rounds=1)["command"]
    assert plain == cli._ceiling_command(payable=1, named=0)


def test_the_page_prints_the_offer_under_the_walks_line(tmp_path) -> None:
    result = campaign(tmp_path)
    printed = format_campaign(result, rerun_offer={"text": "2 round(s) the queue could pay today: do it"})
    lines = printed.splitlines()
    offers = [index for index, line in enumerate(lines) if line.startswith("re-run offer")]
    assert offers, printed
    assert "2 round(s) the queue could pay today: do it" in lines[offers[0]]
    walks = [index for index, line in enumerate(lines) if line.startswith("re-run walks")]
    if walks:
        assert walks[0] < offers[0]


def test_a_page_without_an_offer_prints_no_offer_line(tmp_path) -> None:
    printed = format_campaign(campaign(tmp_path))
    assert "re-run offer" not in printed
