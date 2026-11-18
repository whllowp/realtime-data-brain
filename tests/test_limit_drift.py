"""A ceiling is not in the rulebook, so a rules audit cannot see it move (round 68).

`EvolveRules` decides what "better" means; a wait ceiling decides whether the queue may run at all. The
second lives in a deployment state, so it can be tightened without touching a single rule — the same
measurements, the same ruleset, another verdict. These tests pin the record and the comparison that make
that difference visible.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.evolve import EvolveRules, RoundResult, format_limit_drift, limit_drift
from brian.promote import Measurement

NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 0, "names": ["host-1"],
         "cadence_s": 300.0, "span_s": 600.0}


def _limits(run=None, drain=None, domain="smd", origin="state"):
    # The ceiling is per domain on purpose: a global one applies to every domain at once, and mixing the
    # two shapes in a fixture is how a comparison bug hides.
    payload = {"max_minutes": None, "max_minutes_by_domain": {}, "max_minutes_origin": {},
               "max_drain_minutes": None, "max_drain_by_domain": {}, "max_drain_origin": {}}
    if run is not None:
        payload["max_minutes_by_domain"][domain] = float(run)
        payload["max_minutes_origin"][domain] = origin
    if drain is not None:
        payload["max_drain_by_domain"][domain] = float(drain)
        payload["max_drain_origin"][domain] = origin
    return payload


def _round(root: Path, name: str, *, limits=None, retired=None, domain="SMD") -> None:
    evolve_dir = root / "var" / "evolve"
    evolve_dir.mkdir(parents=True, exist_ok=True)
    document = {"round": name, "started_at": (NOW - timedelta(minutes=9)).isoformat(),
                "finished_at": NOW.isoformat(), "stream": dict(SHAPE, source=domain),
                "stream_path": None, "trials": [], "baseline": None, "rules": {}}
    if limits is not None:
        document["limits"] = limits
    (evolve_dir / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    line = {"round": name, "started_at": document["started_at"], "finished_at": NOW.isoformat(),
            "accepted": 0, "rejected": 0, "applied": [], "rules_hash": "test",
            "stream_path": None, "baseline_tokens_per_report": 3358.0}
    if retired is not None:
        line["retired"] = {"at": NOW.isoformat(), "re_measured_as": retired, "overlap": {}}
    path = evolve_dir / "ledger.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _measurement() -> Measurement:
    return Measurement(candidate="baseline", recall=0.30, fpr=0.09, precision=0.5, reports=10,
                       tokens_per_report=3358.0, tokens_per_window=1100.0, latency_p95_ms=800.0)


# --- the record carries its ceilings ------------------------------------------------------------------------


def test_a_round_records_the_ceilings_it_was_judged_under():
    result = RoundResult(started_at=NOW.isoformat(), finished_at=NOW.isoformat(), stream=SHAPE,
                         rules=EvolveRules(), baseline=_measurement())
    result.limits = _limits(run=10.0)
    payload = result.to_dict()
    assert payload["limits"]["max_minutes_by_domain"] == {"smd": 10.0}


def test_a_round_that_recorded_no_ceiling_says_none_rather_than_empty():
    result = RoundResult(started_at=NOW.isoformat(), finished_at=NOW.isoformat(), stream=SHAPE,
                         rules=EvolveRules(), baseline=_measurement())
    assert result.to_dict()["limits"] is None


# --- the comparison ----------------------------------------------------------------------------------------


def test_a_round_judged_under_another_ceiling_is_named(tmp_path):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    drift = limit_drift([{"round": "round-old.json", "limits": _limits(run=10.0)}], _limits(run=4.0))
    assert drift["differ"] == ["round-old.json"]
    assert drift["unrecorded"] == 0
    assert drift["rounds"] == 1


def test_the_same_ceiling_is_not_a_drift():
    drift = limit_drift([{"round": "round-a.json", "limits": _limits(run=10.0)}], _limits(run=10.0))
    assert drift["differ"] == []


def test_an_origin_is_not_a_ceiling():
    """`(state)` and `(command line)` answer who decided, not what the limit was."""
    recorded = _limits(run=10.0, origin="state")
    today = _limits(run=10.0, origin="flag")
    assert limit_drift([{"round": "round-a.json", "limits": recorded}], today)["differ"] == []


def test_a_round_without_ceilings_is_counted_not_guessed(tmp_path):
    drift = limit_drift([{"round": "round-legacy.json"}], _limits(run=4.0))
    assert drift["differ"] == []
    assert drift["unrecorded"] == 1


def test_a_retired_round_owes_no_comparison():
    drift = limit_drift([{"round": "round-gone.json", "limits": _limits(run=10.0),
                          "retired": {"at": NOW.isoformat()}}], _limits(run=4.0))
    assert drift["rounds"] == 0 and drift["differ"] == []


def test_a_drain_ceiling_counts_as_a_ceiling():
    drift = limit_drift([{"round": "round-a.json", "limits": _limits(drain=3.0)}], _limits(drain=9.0))
    assert drift["differ"] == ["round-a.json"]


# --- the sentence ------------------------------------------------------------------------------------------


def test_the_sentence_names_how_many_and_which():
    text = format_limit_drift({"rounds": 3, "differ": ["round-a.json"], "unrecorded": 1})
    assert "1 of 3 round(s) were judged under ceilings that differ from today's" in text
    assert "round-a.json" in text
    assert "the rules did not change, the limits did" in text
    assert "1 round(s) do not say which ceilings they were judged under" in text


def test_the_sentence_does_not_invent_a_drift():
    text = format_limit_drift({"rounds": 2, "differ": [], "unrecorded": 0})
    assert "every recorded round was judged under today's ceilings" in text


def test_an_empty_ledger_has_nothing_to_compare():
    assert "nothing whose ceilings could differ" in format_limit_drift({"rounds": 0})


# --- the check ---------------------------------------------------------------------------------------------


def test_the_audit_prints_the_ceilings_and_the_drift(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    _round(tmp_path, "round-legacy.json")
    state = tmp_path / "var" / "evolve" / "domains" / "smd"
    state.mkdir(parents=True, exist_ok=True)
    (state / "current.json").write_text(json.dumps({
        "env": {}, "sources": {}, "updated_at": NOW.isoformat(), "domain": "smd", "source": "SMD",
        "budget": {"max_minutes": 4.0},
    }, indent=2) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0"]) == 0
    out = capsys.readouterr().out
    assert "limits            one run 4.0 minute(s) on smd (state)" in out
    assert "ceilings          1 of 2 round(s) were judged under ceilings that differ from today's" in out
    assert "round-old.json" in out
    assert "1 round(s) do not say which ceilings they were judged under" in out


def test_the_cap_makes_the_drift_fail_the_check(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    state = tmp_path / "var" / "evolve" / "domains" / "smd"
    state.mkdir(parents=True, exist_ok=True)
    (state / "current.json").write_text(json.dumps({
        "env": {}, "sources": {}, "updated_at": NOW.isoformat(), "domain": "smd", "source": "SMD",
        "budget": {"max_minutes": 4.0},
    }, indent=2) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0",
                     "--max-limit-drift", "0"]) == 1
    assert "above the 0 asked for" in capsys.readouterr().out
    assert cli.main(["evolve", "--audit", "--strict", "--rerun-limit", "0",
                     "--max-limit-drift", "3"]) == 0


def test_the_drift_cap_demands_the_mode_that_reads_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--max-limit-drift", "0"]) == 2
    assert "pass one of them" in capsys.readouterr().out


# --- the campaign page, and the cap that arms itself (round 69) ---------------------------------------------


def _state(tmp_path, run=4.0):
    state = tmp_path / "var" / "evolve" / "domains" / "smd"
    state.mkdir(parents=True, exist_ok=True)
    (state / "current.json").write_text(json.dumps({
        "env": {}, "sources": {}, "updated_at": NOW.isoformat(), "domain": "smd", "source": "SMD",
        "budget": {"max_minutes": run},
    }, indent=2) + "\n", encoding="utf-8")


def test_the_page_names_the_ceilings_each_row_moved_under(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    _round(tmp_path, "round-legacy.json")
    _state(tmp_path, run=4.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--campaign"]) == 0
    out = capsys.readouterr().out
    assert "limits            one run 4.0 minute(s) on smd (state)" in out
    assert ("ceilings          1 round(s) were judged under ceilings that differ from today's: "
            "the rules did not change, the limits did") in out
    assert ("  ceilings          1 of 2 round(s) were judged under ceilings that differ from today's; "
            "1 round(s) do not say which ceilings they were judged under") in out


def test_the_audit_and_the_campaign_count_the_same_drift(tmp_path, monkeypatch, capsys):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    _round(tmp_path, "round-now.json", limits=_limits(run=4.0))
    _state(tmp_path, run=4.0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--rerun-limit", "0"]) == 0
    audit_out = capsys.readouterr().out
    assert cli.main(["evolve", "--campaign"]) == 0
    page_out = capsys.readouterr().out
    assert "1 of 2 round(s) were judged under ceilings that differ from today's" in audit_out
    assert "ceilings          1 round(s) were judged under ceilings that differ from today's" in page_out


def test_the_cap_arms_itself_once_every_round_records_its_ceilings(tmp_path, monkeypatch, capsys):
    """A default of zero must not fail on history nobody can back-fill (round 33's rule)."""
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    _round(tmp_path, "round-legacy.json")
    _state(tmp_path, run=4.0)
    monkeypatch.chdir(tmp_path)
    # One round predates the field, so the default stays a warning...
    assert cli.main(["evolve", "--campaign", "--strict"]) == 0
    capsys.readouterr()
    # ...and once every live round records its ceilings the same drift fails without any flag.
    assert cli.main(["evolve", "--campaign", "--strict", "--retire", "round-legacy.json",
                     "--re-measured-as", "round-old.json"]) == 0
    capsys.readouterr()
    assert cli.main(["evolve", "--campaign", "--strict"]) == 1
    assert "above the 0 asked for" in capsys.readouterr().out


def test_the_campaign_carries_the_drift_to_json(tmp_path, monkeypatch):
    _round(tmp_path, "round-old.json", limits=_limits(run=10.0))
    _state(tmp_path, run=4.0)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "page.json"
    assert cli.main(["evolve", "--campaign", "--json-out", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["ceiling_drift"]["differ"] == ["round-old.json"]
    assert payload["domains"][0]["ceilings_differ"] == 1
    assert payload["ceilings_differ"] == 1
