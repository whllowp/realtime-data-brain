# -*- coding: utf-8 -*-
"""Round 58 tests: a domain's unrecorded share can be required — but only where a command can lower it.

Round 57 printed each domain's debt before a round decided what to measure. The number was still not
chargeable, so a page where a domain's totals are mostly unattributable passed `--strict` as long as its
*verdicts* happened to survive. The charge that follows is deliberately narrower than the report: a share
is printed for every domain, and the exit code moves only for a domain whose rounds a re-run can pay,
because a gate that fails on a debt no command can lower is red forever (round 33's rule).
"""
import json
from pathlib import Path

from brian import cli
from brian.streams import load_stream_file, summarise

WORST_PAYABLE = {"domain": "SMD", "rounds": 4, "without": 4, "share": 1.0, "ready": 4}
WORST_STUCK = {"domain": "(unattributed)", "rounds": 1, "without": 1, "share": 1.0, "ready": 0}
PAYABLE = {"domain": "SMD", "rounds": 4, "without": 4, "share": 1.0, "ready": 4}


def _stream_file(root: Path) -> Path:
    """A three-window SMD stream whose summary matches what the record will claim."""
    target = root / "data" / "smd" / "stream-one.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": "SMD",
        "streams": [{
            "name": "smd-machine-1-6",
            "windows": [
                {"start": "2026-11-17T00:00:00+00:00", "service": "host", "scored": True, "anomaly": False},
                {"start": "2026-11-17T00:05:00+00:00", "service": "host", "scored": True, "anomaly": True},
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return target


def _round(root: Path, name: str, *, source: str, stream_path, applied) -> None:
    """One recorded round whose ledger line and file agree, written the way `persist` writes them."""
    evolve = root / "var" / "evolve"
    evolve.mkdir(parents=True, exist_ok=True)
    stream = summarise(load_stream_file(_stream_file(root)).streams) if source == "SMD" else {}
    stamp = "2026-11-18T04:00:00+00:00"
    (evolve / name).write_text(json.dumps({
        "round": name, "started_at": stamp, "finished_at": stamp,
        "stream": dict(stream, source=source), "stream_path": stream_path,
        "trials": [], "baseline": None, "rules": {},
    }), encoding="utf-8", newline="\n")
    line = {"round": name, "started_at": stamp, "finished_at": stamp, "accepted": 0, "rejected": 0,
            "applied": applied, "rules_hash": "test", "stream_path": stream_path,
            "baseline_tokens_per_report": 3358.0}
    with (evolve / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def test_a_domain_share_alone_does_not_fail_the_check(capsys):
    # The paragraph is information; `--max-debt-share` is the request to be charged for it.
    assert cli._strict_exit(True, 0, 0, 0, worst=WORST_PAYABLE, payable=PAYABLE, max_debt=None) == 0
    assert "above the" not in capsys.readouterr().out


def test_a_payable_share_above_the_cap_fails_the_check(capsys):
    assert cli._strict_exit(True, 0, 0, 0, worst=WORST_PAYABLE, payable=PAYABLE, max_debt=0.5) == 1
    printed = capsys.readouterr().out
    assert "SMD has 100% of its rounds unrecorded (4 of 4), above the 50% asked for:" in printed
    assert "cannot be read as a measurement, so --strict exits 1." in printed
    assert "4 of those round(s) can be paid by re-running them on the file they measured." in printed


def test_a_share_nobody_can_pay_is_printed_and_not_charged(capsys):
    # The worst row is `(unattributed)`; the payable debt is SMD's. The share is reported, the exit code is
    # not moved by it: charging a share no re-run can touch would leave the check red forever.
    assert cli._strict_exit(True, 0, 0, 0, worst=WORST_STUCK, payable=PAYABLE, max_debt=0.5) == 0
    printed = capsys.readouterr().out
    assert "(unattributed) has 100% of its rounds unrecorded (1 of 1), above the 50% asked for:" in printed
    assert "no recorded round on it can be re-run" in printed
    assert "a gate that can never be cleared is a gate nobody runs." in printed


def test_a_share_under_the_cap_says_nothing(capsys):
    small = dict(WORST_PAYABLE, share=0.25, without=1, rounds=4)
    assert cli._strict_exit(True, 0, 0, 0, worst=small, payable=dict(small), max_debt=0.5) == 0
    assert "unrecorded (" not in capsys.readouterr().out


def test_the_audit_charges_a_payable_share(tmp_path, monkeypatch, capsys):
    # End to end: the debt is on the row a reader faces, and the round that owes it can be re-run, so asking
    # for the charge is answered.
    _round(tmp_path, "round-smd-unrecorded.json", source="SMD", stream_path=None, applied=["v020"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-debt-share", "0.5"]) == 1
    printed = capsys.readouterr().out
    assert "SMD has 100% of its rounds unrecorded (1 of 1), above the 50% asked for:" in printed


def test_the_audit_does_not_charge_the_share_without_the_flag(tmp_path, monkeypatch):
    _round(tmp_path, "round-smd-unrecorded.json", source="SMD", stream_path=None, applied=["v020"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict"]) == 0


def test_a_domain_that_named_its_file_owes_nothing(tmp_path, monkeypatch):
    name = "round-smd-named.json"
    _round(tmp_path, name, source="SMD", stream_path="data/smd/stream-one.json", applied=["v020"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--audit", "--strict", "--max-debt-share", "0.0"]) == 0
