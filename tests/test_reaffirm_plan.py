# -*- coding: utf-8 -*-
"""Round 120: the moved decisions, grouped by the file they are about, and several pairs in one command.

Round 119 gave the debt a command, one pair at a time, and the campaign page said nothing at all about it -- the
count lived in the JSON. "Which file do I open first, and how many decisions does it owe" is the question that
follows "the books moved under this decision", and it now has a sentence and a command.
"""
import json
from datetime import timedelta

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import load_settlements, reaffirm_link, reaffirm_plan, settle_link, settlement_summary

from test_provenance_block import RETIRED, SUCCESSOR, _stream
from test_settlement_reaffirm import _ago, _backdate_decision, _later_claim, _records, _settled
from test_untested_answer import NOW


def _settled_two_files(tmp_path):
    """One decision on each of two stream files, both staled by a claim that arrives after them."""
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _stream(tmp_path, "data/smd/stream-two.json")
    path = tmp_path / "var" / "evolve" / "round-other.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stream_path"] = "data/smd/stream-two.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    # The ledger line overrides the round file, so the move has to be written in both books (rounds 34 and 47).
    ledger = tmp_path / "var" / "evolve" / "ledger.jsonl"
    lines = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if str(entry.get("round") or "") == "round-other.json":
            entry["stream_path"] = "data/smd/stream-two.json"
            line = json.dumps(entry)
        lines.append(line)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    # Dated in the past, so the claim three days ago is *after* it -- a decision taken now would simply cover it.
    settle_link(tmp_path, "round-other.json", SUCCESSOR, why="the successor names this one", by="bob", now=_ago(10))
    _later_claim(tmp_path, at=_ago(3))


def test_the_plan_groups_the_debt_by_the_file_the_decision_is_about(tmp_path):
    _settled(tmp_path)
    _backdate_decision(tmp_path)
    _later_claim(tmp_path, at=_ago(3))
    rows = reaffirm_plan(_records(tmp_path), load_settlements(tmp_path))
    assert [row["origin"] for row in rows] == ["data/smd/stream-one.json"]
    assert rows[0]["stale"] == 1
    # `round-third.json`'s own decision is dated when it arrived, so it is not stale -- the plan counts what is owed.
    assert rows[0]["decisions"] == 2
    assert rows[0]["commands"] == ["brian evolve --settle-reaffirm round-old.json=round-new.json"]


def test_a_file_with_nothing_stale_is_not_in_the_plan(tmp_path):
    _settled(tmp_path)
    assert reaffirm_plan(_records(tmp_path), load_settlements(tmp_path)) == []


def test_a_re_taken_decision_still_counts_as_stale_until_the_claim_is_covered(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)                     # dated NOW + 2 days, i.e. after any re-affirmation taken now
    reaffirm_link(tmp_path, RETIRED, SUCCESSOR, why="read today's books", now=NOW - timedelta(days=1))
    rows = reaffirm_plan(_records(tmp_path), load_settlements(tmp_path))
    assert rows[0]["stale"] == 1 and rows[0]["retaken"] == 1


def test_the_plan_sorts_by_how_much_is_owed_then_by_file(tmp_path):
    _settled_two_files(tmp_path)
    rows = reaffirm_plan(_records(tmp_path), load_settlements(tmp_path))
    assert len(rows) == 2
    assert [row["origin"] for row in rows] == ["data/smd/stream-one.json", "data/smd/stream-two.json"]
    assert all(row["stale"] == 1 for row in rows)


def test_the_campaign_page_says_the_debt_and_the_first_command(tmp_path):
    _settled(tmp_path)
    _later_claim(tmp_path)
    result = campaign(tmp_path)
    assert result.settlement_moved, "the campaign reads the same two files as the audit"
    assert result.to_dict()["reaffirm_plan"][0]["origin"] == "data/smd/stream-one.json"

    page = " ".join(format_campaign(result).split())
    # Round 118 computed this and printed it only in the JSON; the page is what a reader opens (boundary 264).
    assert "decision(s) rest on books that have moved since they were taken: round-old.json -> round-new.json" in page
    # Round 121: the line now says when the file was last re-taken and how many walks followed. This ledger has no
    # re-takes and no walks, so it says "never re-taken" and "0 walk(s) since" -- zero recorded walks is a fact,
    # while `unknown` is reserved for a plan built without the walk store (boundary 280).
    assert ("re-take next: data/smd/stream-one.json -- 1 stale of 2 recorded decision(s), never re-taken, "
            "0 walk(s) since -- "
            "brian evolve --settle-reaffirm round-old.json=round-new.json") in page


def test_the_cli_re_takes_several_pairs_with_one_reason(tmp_path, monkeypatch, capsys):
    _settled_two_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm",
                     "%s=%s" % (RETIRED, SUCCESSOR), "round-other.json=%s" % SUCCESSOR,
                     "--settle-why", "read today's books", "--settle-by", "alice"]) == 0
    flat = " ".join(capsys.readouterr().out.split())
    assert "reaffirmed round-old.json -> round-new.json" in flat
    assert "reaffirmed round-other.json -> round-new.json" in flat
    assert "re-taken 2 of 2 decision(s)" in flat
    stored = {(item["record"], item["successor"]): item for item in load_settlements(tmp_path)}
    # The fixture's third decision (the arriving claim) was not named in the command, so it is untouched.
    assert stored[(RETIRED, SUCCESSOR)]["reaffirmations"]
    assert stored[("round-other.json", SUCCESSOR)]["reaffirmations"]
    assert not stored[("round-third.json", SUCCESSOR)].get("reaffirmations")


def test_one_refused_pair_does_not_stop_the_rest(tmp_path, monkeypatch, capsys):
    _settled_two_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["evolve", "--settle-reaffirm",
                     "round-nowhere.json=%s" % SUCCESSOR, "%s=%s" % (RETIRED, SUCCESSOR),
                     "--settle-why", "read today's books", "--settle-by", "alice"])
    assert code == 2                       # something was refused, and the exit says so
    flat = " ".join(capsys.readouterr().out.split())
    assert "cannot re-affirm that decision" in flat
    assert "reaffirmed round-old.json -> round-new.json" in flat   # the second pair still happened
    assert "1 refused" in flat


def test_a_pair_without_an_equals_sign_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm", "round-old.json", "--settle-why", "x"]) == 2
    assert "has no = in it" in capsys.readouterr().out
    assert load_settlements(tmp_path)[0].get("reaffirmations") in (None, [])


def test_the_json_payload_lists_every_pair(tmp_path, monkeypatch):
    _settled_two_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm", "%s=%s" % (RETIRED, SUCCESSOR),
                     "round-other.json=%s" % SUCCESSOR, "--settle-why", "read today's books",
                     "--settle-by", "alice", "--json-out", "many.json"]) == 0
    payload = json.loads((tmp_path / "many.json").read_text(encoding="utf-8"))
    assert [item["record"] for item in payload["reaffirmed"]] == [RETIRED, "round-other.json"]
    assert payload["reaffirm_refused"] == 0


def test_a_second_reaffirmation_says_that_nothing_had_moved(tmp_path, monkeypatch, capsys):
    """Boundary 276: a decision can be confirmed twice, and the page must say which of the two it did."""
    _settled(tmp_path)
    _backdate_decision(tmp_path)               # into the past, so the claim below can arrive after it
    _later_claim(tmp_path, at=_ago(3))
    monkeypatch.chdir(tmp_path)
    args = ["evolve", "--settle-reaffirm", "%s=%s" % (RETIRED, SUCCESSOR),
            "--settle-why", "read today's books", "--settle-by", "alice"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 0
    flat = " ".join(capsys.readouterr().out.split())
    # The first call re-took a moved decision; the second found nothing moved and says so instead of pretending to be
    # a new re-take -- a decision confirmed twice is two facts, and the reader gets both.
    assert "nothing had moved since" in flat
    assert settlement_summary(load_settlements(tmp_path))["reaffirmed"] == 1
    assert len(load_settlements(tmp_path)[0]["reaffirmations"]) == 2
