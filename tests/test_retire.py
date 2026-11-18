# -*- coding: utf-8 -*-
"""Round 34 tests: a superseded record is retired, not edited, and the audit belongs to a domain.

Round 31's two dry runs wrote records that predate the geometry field but postdate the reference field, so
`--audit` cannot tell which reference each trial used and replays nothing for 23 comparisons. The honest cure
is not to back-fill a reference that experiment never used: it is to re-run the round, which writes a readable
record, and retire the old one from the checks. The same round gives each domain row its own audit counts,
because every other number on the campaign page already belonged to a domain.
"""
import json

import pytest

from brian.campaign import campaign, format_campaign
from brian.evolve import EvolveRules, audit, history, previously_rejected, retire

RULES = EvolveRules()

CANDIDATE = {
    "name": "events-16",
    "hypothesis": "a longer context window finds more of the signatures",
    "changes": [["pipeline", "context_events", 16]],
    "veto": None,
}

WINDOWED = {
    "name": "host-rolling-rank-0-85-200@hint286",
    "hypothesis": "the same configuration at the window that covers the span its claim was made over",
    "changes": [["digest", "rolling_window", 286]],
    "veto": None,
}


def _measurement(name, *, window=None, recall_at_budget=0.30, tokens=3200.0):
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


def _round_file(root, name, *, source="SMD", candidate=None, trial_recall=0.30, window=None,
                references=None, stored=None, candidates=None):
    """Write a round file and its ledger line the way `run_round` and `persist` do."""
    chosen = [dict(item) for item in (candidates or [candidate or CANDIDATE])]
    payload = {
        "round": name,
        "started_at": "2026-11-18T01:00:00+00:00",
        "finished_at": "2026-11-18T01:01:00+00:00",
        "stream": {"source": source, "windows": 1355, "scored": 1235},
        "rules": RULES.to_dict(),
        "baseline": _measurement("baseline", window=window, recall_at_budget=0.30),
        "trials": [{
            "candidate": item,
            "measurement": _measurement(item["name"], window=None, recall_at_budget=trial_recall),
            "verdict": dict(stored or {"candidate": item["name"], "accepted": False,
                                       "rule": "gain", "reason": "recall at the budget did not rise"}),
        } for item in chosen],
        "references": dict(references or {}),
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


def test_a_record_that_cannot_name_its_reference_is_not_replayed(tmp_path):
    """The round-31 shape: it stored references, but not which trial used which."""
    name = _round_file(tmp_path, "round-20261118T012841Z.json", window=None,
                       references={"286": _measurement(WINDOWED["name"], window=286,
                                                       recall_at_budget=0.43)})
    result = audit(tmp_path, RULES)
    assert result.rounds == 1
    assert result.cross == 1
    assert result.entries[0]["cross"] == [{"candidate": CANDIDATE["name"], "kind": "legacy"}]
    assert result.entries[0]["round"] == name


def test_retiring_takes_its_comparisons_out_of_the_check(tmp_path):
    _round_file(tmp_path, "round-20261118T012841Z.json", window=None,
                references={"286": _measurement(WINDOWED["name"], window=286, recall_at_budget=0.43)})
    retire(tmp_path, "round-20261118T012841Z.json", re_measured_as="round-20261118T020000Z.json")
    result = audit(tmp_path, RULES)
    assert result.cross == 0
    assert result.rounds == 0
    assert result.retired == 1


def test_retiring_does_not_edit_what_the_round_measured(tmp_path):
    name = _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    path = tmp_path / "var" / "evolve" / name
    before = path.read_bytes()
    retire(tmp_path, name, re_measured_as="round-20261118T020000Z.json")
    assert path.read_bytes() == before
    lines = (tmp_path / "var" / "evolve" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    mark = json.loads(lines[0])["retired"]
    assert mark["re_measured_as"] == "round-20261118T020000Z.json"
    assert mark["at"].endswith("Z")


def test_history_hides_a_retired_round_unless_asked(tmp_path):
    name = _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    retire(tmp_path, name, re_measured_as="round-20261118T020000Z.json")
    assert history(tmp_path) == []
    kept = history(tmp_path, include_retired=True)
    assert [row["round"] for row in kept] == [name]
    assert kept[0]["retired"]["re_measured_as"] == "round-20261118T020000Z.json"


def test_retiring_twice_keeps_the_first_mark(tmp_path):
    name = _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    first = retire(tmp_path, name, re_measured_as="round-20261118T020000Z.json", at="2026-11-18T03:00:00Z")
    ledger = tmp_path / "var" / "evolve" / "ledger.jsonl"
    after_first = ledger.read_bytes()
    second = retire(tmp_path, name, re_measured_as="round-20261118T030000Z.json")
    assert second == first
    assert ledger.read_bytes() == after_first


def test_retiring_an_unknown_round_says_so(tmp_path):
    _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    with pytest.raises(ValueError, match="no recorded round matches"):
        retire(tmp_path, "round-20260101T000000Z.json")


def test_a_bare_stem_names_the_round_file(tmp_path):
    _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    mark = retire(tmp_path, "round-20261118T012841Z", re_measured_as="round-20261118T020000Z.json")
    assert mark["re_measured_as"] == "round-20261118T020000Z.json"
    assert history(tmp_path) == []


def test_the_campaign_counts_retired_rounds_separately(tmp_path):
    retired = _round_file(tmp_path, "round-20261118T012841Z.json", window=None,
                          references={"286": _measurement(WINDOWED["name"], window=286)})
    _round_file(tmp_path, "round-20261118T020000Z.json", window=None)
    retire(tmp_path, retired, re_measured_as="round-20261118T020000Z.json")
    result = campaign(tmp_path)
    assert result.rounds == 1
    assert result.retired == 1
    assert result.to_dict()["rounds_retired"] == 1
    text = format_campaign(result)
    assert "retired           1 round(s) were re-measured" in text


def test_the_audit_belongs_to_the_domain_that_owes_it(tmp_path):
    """A verdict that would move today, recorded on one stream, must not show up on another row."""
    _round_file(tmp_path, "round-20261118T010000Z.json", source="SMD", trial_recall=0.60)
    _round_file(tmp_path, "round-20261118T011000Z.json", source="demo")
    result = campaign(tmp_path)
    rows = {row.domain: row for row in result.rows}
    assert rows["SMD"].audit_moved == 1
    assert rows["demo"].audit_moved == 0
    text = format_campaign(result)
    assert "  audit             1 moved, 0 relabelled" in text


def test_each_row_carries_its_audit_counts(tmp_path):
    _round_file(tmp_path, "round-20261118T010000Z.json", source="SMD", trial_recall=0.60)
    result = campaign(tmp_path)
    row = result.rows[0].to_dict()
    assert row["audit_moved"] == 1
    assert row["audit_relabelled"] == 0
    assert row["audit_unreconstructable"] == 0


def test_a_clean_domain_prints_no_audit_line(tmp_path):
    _round_file(tmp_path, "round-20261118T010000Z.json", source="demo")
    text = format_campaign(campaign(tmp_path))
    assert "  audit             " not in text


def test_a_retirement_says_how_much_of_it_the_successor_repeats(tmp_path):
    """One shared candidate must not launder the rest of the record."""
    old = _round_file(tmp_path, "round-20261118T012841Z.json", window=None,
                      candidates=[CANDIDATE, WINDOWED])
    new = _round_file(tmp_path, "round-20261118T020000Z.json", candidates=[CANDIDATE])
    mark = retire(tmp_path, old, re_measured_as=new)
    assert mark["overlap"] == {"matches": [CANDIDATE["name"]],
                               "not_repeated": [WINDOWED["name"]],
                               "successor_readable": True}


def test_a_successor_nobody_can_read_is_not_an_empty_overlap(tmp_path):
    old = _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    mark = retire(tmp_path, old, re_measured_as="round-20261118T020000Z.json")
    assert mark["overlap"]["successor_readable"] is False
    assert mark["overlap"]["not_repeated"] == [CANDIDATE["name"]]


def test_retiring_a_refusal_does_not_unlearn_it(tmp_path):
    """The checks stop asking; the loop must not forget what the stream already refused."""
    old = _round_file(tmp_path, "round-20261118T012841Z.json", window=None)
    assert CANDIDATE["name"] in previously_rejected(tmp_path)
    retire(tmp_path, old, re_measured_as="round-20261118T020000Z.json")
    assert CANDIDATE["name"] in previously_rejected(tmp_path)
