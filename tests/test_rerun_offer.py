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
           unplaceable: int = 0, hand: int = 0, queued: int = 0) -> dict:
    # Round 150: a round counts as payable only when it has exactly one candidate file, so the stub gives each
    # payable round one and each unplaceable round the two files the queue refuses to choose between.
    monkeypatch.setattr(cli, "_unrecorded_matters",
                        lambda root, library=None: [("d%d" % index, "round-%d.json" % index, "why", ["data/x.json"])
                                                    for index in range(file_rounds)]
                        + [("u%d" % index, "round-u%d.json" % index, "why", ["data/a.json", "data/b.json"])
                           for index in range(unplaceable)])
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


def test_unplaceable_rounds_are_counted_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    # Round 150: a round with two candidate files is measured by no command -- `--rerun-unrecorded` skips it -- so
    # counting it as payable promised work nothing performs (boundary 363).
    offer = _offer(monkeypatch, file_rounds=1, unplaceable=2)
    assert offer["rounds"] == 1
    assert offer["unplaceable"] == 2
    assert "; 2 more keep no unique file, so no command can pay them" in offer["text"]


def test_a_queue_that_can_pay_nothing_says_what_holds_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = _offer(monkeypatch, unplaceable=2)
    assert offer["rounds"] == 0
    assert offer["command"] == ""
    assert offer["text"] == ("nothing for the queue to pay today: 2 round(s) keep no unique file, "
                             "so no command can pay them")


def test_the_queue_command_prints_the_offer_and_what_is_left(tmp_path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    # One function, two moments: the sentence before the work answers "what would you pay", the one after answers
    # "what is left", and each is labelled (boundaries 364/365).
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "0"]) == 0
    text = capsys.readouterr().out
    offer = [line for line in text.splitlines() if line.startswith("offer ")]
    left = [line for line in text.splitlines() if line.startswith("left to pay ")]
    assert offer, text
    assert left, text
    assert "nothing for the queue to pay today" in offer[0]
    assert "nothing for the queue to pay today" in left[0]
    assert text.index(offer[0]) < text.index(left[0])
