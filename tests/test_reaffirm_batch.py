# -*- coding: utf-8 -*-
"""The two kinds of stale debt, and one command that pays a whole file (round 122, item 108).

A file can owe two different jobs. A decision nobody has ever re-taken needs a first reading of the books; one that
was re-taken and has moved again needs the books repaired first, because re-stamping it would cover the new claim
while looking like upkeep. And a file with several stale decisions is one job with one command -- pasting eleven
pairs is how the reason drifts and how one gets skipped.
"""
import json
from datetime import datetime, timedelta, timezone

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import history, load_settlements, reaffirm_link, reaffirm_plan, settle_link

from test_provenance_block import _round
from test_settlement_reaffirm import LATER, RETIRED, SUCCESSOR, _later_claim, _settled

FILE = "data/smd/stream-one.json"


def _clamp(root):
    """Move every recorded moment into the real past, keeping their order.

    The fixtures date their books with `NOW` (2026-12-01), which is after this machine's clock: a claim dated then
    arrives *after* any re-affirmation the CLI takes now, so a batch that re-takes everything leaves the plan saying
    everything moved again (round 119's trap). Clamping makes "later" mean later than the decision and earlier than
    the re-take, which is the situation the command exists for.
    """
    directory = root / "var" / "evolve"
    moments = set()
    store = directory / "settlements.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    for item in data.get("settlements") or []:
        moments.add(str(item.get("at") or ""))
        for entry in item.get("reaffirmations") or []:
            moments.add(str(entry.get("at") or ""))
    rounds = []
    for path in sorted(directory.glob("round-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        retired = payload.get("retired")
        if isinstance(retired, dict) and retired.get("at"):
            moments.add(str(retired["at"]))
        rounds.append((path, payload))
    ordered = sorted(moment for moment in moments if moment)
    mapping = {moment: (datetime.now(timezone.utc) - timedelta(days=2 * (len(ordered) - index))).isoformat()
               for index, moment in enumerate(ordered)}

    def move(value):
        return mapping.get(str(value or ""), value)

    for item in data.get("settlements") or []:
        item["at"] = move(item.get("at"))
        for entry in item.get("reaffirmations") or []:
            entry["at"] = move(entry.get("at"))
    store.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    for path, payload in rounds:
        retired = payload.get("retired")
        if isinstance(retired, dict) and retired.get("at"):
            retired["at"] = move(retired["at"])
            path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    return root


def _plan(root):
    return reaffirm_plan(history(root, include_retired=True), load_settlements(root), [])


def _never_read(root):
    """One stale decision nobody has re-taken; returns the plan row."""
    _settled(root)
    _later_claim(root)
    return _plan(root)[0]


def _read_then_moved(root):
    """One decision that *was* re-taken and has moved again since."""
    _settled(root)
    _later_claim(root)
    reaffirm_link(root, RETIRED, SUCCESSOR, why="read the books", by="alice", now=LATER + timedelta(hours=1))
    _later_claim(root, name="round-fourth.json", at=LATER + timedelta(days=2))
    return _plan(_clamp(root))[0]


def _two_stale(root):
    """Two stale decisions on the same file: one never re-taken, one re-taken and moved again.

    Returns the root (not the plan row): callers build on it and read the plan themselves.
    """
    _settled(root)
    _later_claim(root)
    # The second decision: a record that retires into the same successor, settled when it arrived.
    _round(root, "round-second.json", retired={"re_measured_as": SUCCESSOR, "at": LATER.isoformat()})
    settle_link(root, "round-second.json", SUCCESSOR, why="decided when it arrived", by="alice", now=LATER)
    reaffirm_link(root, RETIRED, SUCCESSOR, why="read the books", by="alice", now=LATER + timedelta(hours=1))
    _later_claim(root, name="round-fourth.json", at=LATER + timedelta(days=2))
    return _clamp(root)


def test_a_file_nobody_has_read_owes_a_first_reading(tmp_path):
    row = _never_read(tmp_path)
    assert row["never"] == 1
    assert row["again"] == 0
    assert row["stale"] == 1


def test_a_file_that_was_read_and_moved_again_owes_a_repair(tmp_path):
    row = _read_then_moved(tmp_path)
    # One decision was re-read and has moved again; the arriving claim is itself a decision nobody has read for, so
    # the two kinds sit on one file -- which is exactly why one command has to name both counts.
    assert row["again"] == 1
    assert row["never"] == 1
    assert row["again"] + row["never"] == row["stale"]


def test_the_row_carries_its_pairs_and_one_batch_command(tmp_path):
    row = _plan(_two_stale(tmp_path))[0]
    assert row["never"] + row["again"] == row["stale"]
    assert [item["record"] for item in row["pairs"]] == sorted(item["record"] for item in row["pairs"])
    assert row["batch"].startswith("brian evolve --settle-reaffirm-file " + FILE)


def test_the_page_names_the_batch_when_a_file_holds_several(tmp_path):
    _two_stale(tmp_path)
    page = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "2 never re-taken" in page or "1 never re-taken" in page
    # Two stale decisions on one file: one command, not two pasted pairs (boundary 282).
    assert "brian evolve --settle-reaffirm-file " + FILE in page


def test_the_page_names_the_pair_when_a_file_holds_one(tmp_path):
    _never_read(tmp_path)
    page = " ".join(format_campaign(campaign(tmp_path)).split())
    assert "brian evolve --settle-reaffirm " + RETIRED + "=" + SUCCESSOR in page


def test_the_batch_re_takes_every_stale_decision_on_the_file(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    # The capture is whitespace-normalised (one space per run), so the expectation is written that way too.
    assert ("re-taking 3 decision(s) on %s in one command (2 never re-taken, 1 re-taken before and moved again)"
            % FILE) in printed
    assert "re-taken 3 of 3 decision(s), 0 kept its date (already re-taken), 0 refused" in printed
    assert _plan(root) == []


def test_the_batch_leaves_out_a_pair_it_cannot_re_take(tmp_path, monkeypatch, capsys):
    root = _two_stale(tmp_path)
    # Retarget the second decision's record, so re-affirming it would assert something the ledger denies.
    from test_provenance_block import _round

    _round(root, "round-second.json", retired={"re_measured_as": "round-moved-away.json",
                                               "at": LATER.isoformat()})
    # Round 133: the plan stops offering it, so the batch never fails pair by pair (boundaries 261/301 at the plan
    # layer) -- the page counts it apart, with the command that can do something about it.
    page = " ".join(format_campaign(campaign(root)).split())
    assert "cannot be re-taken at all" in page
    assert "round-moved-away.json" in page
    assert "--settle-drop round-second.json=%s" % SUCCESSOR in page
    monkeypatch.chdir(root)
    code = cli.main(["evolve", "--settle-reaffirm-file", FILE, "--settle-why", "read today's books",
                     "--settle-by", "alice"])
    printed = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "cannot re-affirm that decision:" not in printed
    assert "0 refused" in printed
    # Nothing payable is left, but the file still appears: the unpairable pair is debt the page must keep showing
    # (round 133), and its remedy is `--settle-drop`, not another re-take.
    left = _plan(root)
    assert [row["stale"] for row in left] == [0]
    assert left[0]["unpayable"] == 1


def test_a_file_with_no_stale_decision_is_refused(tmp_path, monkeypatch, capsys):
    _settled(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm-file", "data/smd/stream-nine.json",
                     "--settle-why", "read today's books"]) == 2
    assert "no stale decision is recorded against data/smd/stream-nine.json" in capsys.readouterr().out


def test_an_empty_file_argument_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--settle-reaffirm-file", "  "]) == 2
    assert "--settle-reaffirm-file takes the stream file" in capsys.readouterr().out
