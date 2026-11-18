# -*- coding: utf-8 -*-
"""Round 50 tests: the gate charges only the unattributable rounds this build wrote.

Round 49 printed how much of the campaign cannot be placed on a sampling (98% here) and left it there. A
number no exit code reads is decoration, but charging all of it would leave the gate red until forty rounds
have been re-run -- the failure round 33 already fixed once. The seam is the field: `persist` has written
`stream_path` since round 47, so a record without it is history, and a record with it that is still empty is
a debt this build can pay. The second half of the round is the list that makes that debt shrinkable: the
unattributable rounds whose numbers still decide something.
"""
from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign

LEDGER = Path("var") / "evolve" / "ledger.jsonl"


def _round_file(root: Path, name: str, *, stream_path="absent", applied=False,
                accepted=(), refused=(), source: str = "SMD") -> Path:
    """A round record plus its ledger line; `stream_path="absent"` writes a record from before the field."""
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    trials = [
        {"candidate": {"name": candidate, "changes": [["llm", "max_context_events", 8]]},
         "measurement": {"recall_at_budget": 0.3, "recall": 0.3, "fpr": 0.0, "reports": 1},
         "verdict": {"accepted": True, "rule": "cost", "reason": "cheaper"}}
        for candidate in accepted
    ]
    trials += [
        {"candidate": {"name": candidate, "changes": [["digest", "host_features", "cpu_r"]]},
         "measurement": {"recall_at_budget": 0.3, "recall": 0.3, "fpr": 0.0, "reports": 1},
         "verdict": {"accepted": False, "rule": "replicate", "reason": "not replicated"}}
        for candidate in refused
    ]
    payload = {
        "round": name,
        "started_at": "2026-11-18T00:00:00+00:00",
        "finished_at": "2026-11-18T00:01:00+00:00",
        "stream": {"source": source, "windows": 1355, "scored": 1235},
        "rules": {},
        "baseline": {"candidate": "baseline", "recall_at_budget": 0.297, "tokens_per_report": 3356},
        "trials": trials,
        "accepted": list(accepted),
        "applied": applied,
    }
    if stream_path != "absent":
        payload["stream_path"] = stream_path
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    line = {"round": name, "started_at": payload["started_at"], "finished_at": payload["finished_at"],
            "accepted": list(accepted), "rejected": list(refused), "applied": applied, "rules_hash": "x",
            "baseline_tokens_per_report": 3356}
    with (root / LEDGER).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return path


def test_the_key_is_the_seam_between_history_and_this_build(tmp_path):
    _round_file(tmp_path, "round-legacy.json")                                  # no key: history
    _round_file(tmp_path, "round-nofile.json", stream_path=None)                # key present, no file
    _round_file(tmp_path, "round-demo.json", stream_path="")                    # written empty
    _round_file(tmp_path, "round-named.json", stream_path="data/smd/stream.json")

    missing, legacy, rounds, share = cli._unrecorded_debt(tmp_path)

    assert (missing, legacy, rounds) == (2, 1, 4)
    assert 0.7 < share < 0.8


def test_a_round_this_build_wrote_without_a_file_is_named_but_not_charged(capsys):
    # A failing test taught this: `brian evolve` with no --stream-file measures the generated demo stream,
    # which has no file to name. Charging it made the demo domain permanently red -- the failure round 33
    # fixed for comparisons. So this build's share is a warning; the cap below is the gate.
    assert cli._strict_exit(True, 0, 0, 0, unrecorded=1) == 0
    assert "measured on the generated stream has none to name" in capsys.readouterr().out


def test_history_is_a_warning_not_a_failure(capsys):
    # Legacy rounds cannot be back-filled without inventing a measurement; failing on them forever is how a
    # gate stops being read (boundary 73).
    assert cli._strict_exit(True, 0, 0, 0, legacy_unrecorded=53) == 0
    text = capsys.readouterr().out
    assert "53 round(s) predate the stream-file field" in text
    assert "--unrecorded lists the ones that still decide something" in text


def test_a_share_cap_fails_the_gate_above_it():
    assert cli._strict_exit(True, 0, 0, 0, unrecorded_share=0.98, max_unrecorded=0.25) == 1
    assert cli._strict_exit(True, 0, 0, 0, unrecorded_share=0.10, max_unrecorded=0.25) == 0


def test_the_campaign_splits_its_unknown_column(tmp_path):
    _round_file(tmp_path, "round-legacy.json")
    _round_file(tmp_path, "round-nofile.json", stream_path=None)
    _round_file(tmp_path, "round-named.json", stream_path="data/smd/stream.json")

    result = campaign(tmp_path)

    assert (result.unattributable, result.unattributable_legacy, result.unattributable_missing) == (2, 1, 1)
    assert result.to_dict()["rounds_without_file_this_build"] == 1
    text = format_campaign(result)
    assert "2 of 3 round(s) do not say which file they measured" in text
    # "1 of them written by this build": the reader needs to know which half of the debt is still open.
    assert "1 of them written by this build" in text


def test_the_list_names_the_rounds_that_still_decide_something(tmp_path):
    _round_file(tmp_path, "round-wrote-versions.json", applied=True, accepted=("events-8",))
    _round_file(tmp_path, "round-refused-something.json", refused=("host-features-3",))
    _round_file(tmp_path, "round-answered-nothing.json")
    _round_file(tmp_path, "round-named.json", stream_path="data/smd/stream.json")

    listed = cli._unrecorded_matters(tmp_path)

    why = {name: reason for _domain, name, reason, _files in listed}
    # A round whose numbers still decide something: it wrote a version, or a candidate it refused is still
    # held. The rest of the legacy ledger is a question nobody asks again.
    assert set(why) == {"round-wrote-versions.json", "round-refused-something.json"}
    assert "wrote 1 version(s) still on the page" in why["round-wrote-versions.json"]
    assert "1 refusal(s) still carried" in why["round-refused-something.json"]
    assert all(domain == "SMD" for domain, _name, _why, _files in listed)


def test_an_attributable_round_is_not_listed(tmp_path):
    _round_file(tmp_path, "round-named.json", stream_path="data/smd/stream.json", applied=True,
                accepted=("events-8",))

    assert cli._unrecorded_matters(tmp_path) == []
