# -*- coding: utf-8 -*-
"""Round 35 tests: retire several rounds in one command, and say who still carries a refusal.

Round 34 made retirement possible but one round at a time, and the campaign's page said nothing about
the two records it had retired: the header counted them ledger-wide while every other number named a
stream, and the refusals they still supply were counted in the rejected column as if the loop had just
re-measured them. Two questions this file pins down:

* a batch is one command but not one decision — each record reports its own overlap with the successor;
* a refusal inherited from a retired record is a decision the loop still acts on, so the page says how
  many of them are inherited.
"""
import json

from brian.campaign import campaign, format_campaign
from brian.cli import _retire_names
from brian.evolve import (EvolveRules, audit, history, previously_rejected, rejection_provenance,
                          retire)

RULES = EvolveRules()

CANDIDATE = {
    "name": "events-16",
    "hypothesis": "a longer context window finds more of the signatures",
    "changes": [["pipeline", "context_events", 16]],
    "veto": None,
}

OTHER = {
    "name": "host-features-3",
    "hypothesis": "three features beat eight",
    "changes": [["digest", "host_features", ["cpu_r", "load_1", "mem_free"]]],
    "veto": None,
}

REJECT = {"candidate": CANDIDATE["name"], "accepted": False, "rule": "gain",
          "reason": "recall at the budget did not rise"}


def _measurement(name, *, window=200, recall_at_budget=0.30, tokens=3200.0):
    return {
        "candidate": name,
        "recall": recall_at_budget,
        "fpr": 0.09,
        "precision": 0.7,
        "reports": 200,
        "tokens_per_report": tokens,
        "tokens_per_window": 480.0,
        "windows": 1235,
        "recall_at_budget": recall_at_budget,
        "fpr_at_budget": 0.09,
        "budget": 0.10,
        "window": window,
    }


def _round_file(root, name, *, source="SMD", candidates=None, stored=None, window=200):
    chosen = [dict(item) for item in (candidates or [CANDIDATE])]
    payload = {
        "round": name,
        "started_at": "2026-11-18T01:00:00+00:00",
        "finished_at": "2026-11-18T01:01:00+00:00",
        "stream": {"source": source, "windows": 1355, "scored": 1235},
        "rules": RULES.to_dict(),
        "baseline": _measurement("baseline", window=window),
        "trials": [{
            "candidate": item,
            "measurement": _measurement(item["name"], window=window),
            "verdict": dict(stored or dict(REJECT, candidate=item["name"])),
        } for item in chosen],
        "references": {},
    }
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    line = {"round": name, "started_at": payload["started_at"], "accepted": [],
            "rejected": [item["name"] for item in chosen], "applied": [], "rules_hash": "test",
            "baseline_tokens_per_report": 3200.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return name


def test_the_flag_takes_several_rounds_and_hides_the_duplicates():
    names = _retire_names(["a.json, b.json", "b.json c.json", "", "a.json"])
    assert names == ["a.json", "b.json", "c.json"]


def test_a_batch_is_one_command_but_each_record_names_its_own_overlap(tmp_path):
    first = _round_file(tmp_path, "round-one.json", candidates=[CANDIDATE])
    second = _round_file(tmp_path, "round-two.json", candidates=[OTHER])
    successor = _round_file(tmp_path, "round-three.json", candidates=[CANDIDATE])

    marks = [retire(tmp_path, name, re_measured_as=successor) for name in (first, second)]

    assert [mark["re_measured_as"] for mark in marks] == [successor, successor]
    # The successor repeated one candidate, so the first retirement claims one match and the second
    # claims none: a shared successor does not mean both records were answered.
    assert marks[0]["overlap"]["matches"] == [CANDIDATE["name"]]
    assert marks[1]["overlap"]["matches"] == []
    assert marks[1]["overlap"]["not_repeated"] == [OTHER["name"]]
    assert [row["round"] for row in history(tmp_path)] == ["round-three.json"]
    assert len(history(tmp_path, include_retired=True)) == 3


def test_the_audit_skips_both_records_once(tmp_path):
    first = _round_file(tmp_path, "round-one.json")
    second = _round_file(tmp_path, "round-two.json")
    _round_file(tmp_path, "round-three.json")
    for name in (first, second):
        retire(tmp_path, name, re_measured_as="round-three.json")

    result = audit(tmp_path, RULES)

    assert result.rounds == 1
    assert result.retired == 2


def test_the_campaign_names_the_domain_that_was_re_measured(tmp_path):
    first = _round_file(tmp_path, "round-one.json", source="SMD")
    second = _round_file(tmp_path, "round-two.json", source="SMD")
    _round_file(tmp_path, "round-three.json", source="RCAEval RE1-OB")
    for name in (first, second):
        retire(tmp_path, name, re_measured_as="round-three.json")

    result = campaign(tmp_path, RULES)
    rows = {row.domain: row for row in result.rows}

    assert result.retired == 2
    assert rows["SMD"].retired == 2
    assert rows["SMD"].successors == ["round-three.json"]
    assert rows["RCAEval RE1-OB"].retired == 0
    assert rows["SMD"].to_dict()["rounds_retired"] == 2
    assert rows["SMD"].to_dict()["re_measured_as"] == ["round-three.json"]

    text = format_campaign(result)
    assert "  retired           2 round(s): re-measured as round-three.json" in text
    assert "  retired           0" not in text


def test_a_retired_round_without_a_stream_is_not_attributed_to_a_domain(tmp_path):
    name = _round_file(tmp_path, "round-one.json")
    directory = tmp_path / "var" / "evolve"
    payload = json.loads((directory / name).read_text(encoding="utf-8"))
    payload.pop("stream", None)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    retire(tmp_path, name)

    result = campaign(tmp_path, RULES)
    rows = {row.domain: row for row in result.rows}

    assert result.retired == 1
    assert rows["(unattributed)"].retired == 1
    assert rows["(unattributed)"].rounds == 0


def test_the_page_says_how_much_of_the_settled_list_is_inherited(tmp_path):
    retired_round = _round_file(tmp_path, "round-one.json", candidates=[CANDIDATE])
    _round_file(tmp_path, "round-two.json", candidates=[OTHER])
    retire(tmp_path, retired_round, re_measured_as="round-two.json")

    result = campaign(tmp_path, RULES)

    assert result.refusals == 2
    assert result.refusals_retired == 1
    assert result.to_dict()["refusals_carried"] == 2
    assert result.to_dict()["refusals_from_retired"] == 1
    text = format_campaign(result)
    assert "refusals          2 candidate(s) are still held as rejected, 1 of them on retired record(s)" in text


def test_a_campaign_with_nothing_inherited_does_not_claim_a_share(tmp_path):
    _round_file(tmp_path, "round-one.json")

    text = format_campaign(campaign(tmp_path, RULES))

    assert "refusals          1 candidate(s) are still held as rejected" in text
    assert "on retired record(s)" not in text


def test_provenance_answers_the_same_question_and_adds_who(tmp_path):
    retired_round = _round_file(tmp_path, "round-one.json", candidates=[CANDIDATE])
    fresh = _round_file(tmp_path, "round-two.json", candidates=[OTHER])
    retire(tmp_path, retired_round, re_measured_as=fresh)

    provenance = rejection_provenance(tmp_path, RULES)
    rejected = previously_rejected(tmp_path, RULES)

    assert set(provenance) == set(rejected)
    assert provenance[CANDIDATE["name"]]["round"] == retired_round
    assert provenance[CANDIDATE["name"]]["retired"] is True
    assert provenance[CANDIDATE["name"]]["re_measured_as"] == fresh
    assert provenance[OTHER["name"]]["retired"] is False
    for name, reason in rejected.items():
        assert reason.startswith("on %s: " % provenance[name]["source"])


def test_a_later_verdict_wins_even_when_the_earlier_round_was_retired(tmp_path):
    """Retirement is not a tombstone: a re-measured question can still be re-decided."""
    retired_round = _round_file(tmp_path, "round-one.json", candidates=[CANDIDATE])
    retire(tmp_path, retired_round, re_measured_as="round-two.json")
    _round_file(tmp_path, "round-two.json", candidates=[CANDIDATE],
                stored={"candidate": CANDIDATE["name"], "accepted": True, "rule": "cost",
                        "reason": "the bill per report fell"})

    # No rules: this test asks which *stored* verdict stands (the latest one), not what today's rules
    # would do with it — re-deciding is `--audit`'s question and is covered there.
    provenance = rejection_provenance(tmp_path)

    assert provenance[CANDIDATE["name"]]["accepted"] is True
    assert provenance[CANDIDATE["name"]]["round"] == "round-two.json"
    assert CANDIDATE["name"] not in previously_rejected(tmp_path)
