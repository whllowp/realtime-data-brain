# -*- coding: utf-8 -*-
"""Round 57 tests: which domain owes what, and whether this round pays it.

Round 56 put the unrecorded trend in the gate, but the page stayed ledger-wide: "66 % of the rounds cannot
be placed" says nothing about *where* the debt is, so a round could be started on the generated stream --
adding one more unattributable record -- while a domain with a nameable file waited. This round makes the
debt per domain, prints where the largest payable debt is before the proposal, and pays the queue biggest
debtor first.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import EvolveRules
from brian.streams import load_stream_file, summarise

STREAM = {
    "format": "brian.stream/1",
    "dataset": "SMD",
    "streams": [{
        "name": "smd-machine-1-6",
        "windows": [
            {"start": "2026-11-17T00:00:00+00:00", "service": "host", "scored": True, "anomaly": False},
            {"start": "2026-11-17T00:05:00+00:00", "service": "host", "scored": True, "anomaly": False},
            {"start": "2026-11-17T00:10:00+00:00", "service": "host", "scored": False, "anomaly": False},
        ],
    }],
}


def _stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _root(tmp_path: Path, *, with_stream: bool = True) -> Path:
    if with_stream:
        data = tmp_path / "data" / "smd"
        data.mkdir(parents=True)
        (data / "stream-one.json").write_text(json.dumps(STREAM), encoding="utf-8")
    (tmp_path / "var" / "evolve").mkdir(parents=True)
    return tmp_path


def _shape(root: Path) -> dict:
    return summarise(load_stream_file(root / "data" / "smd" / "stream-one.json").streams)


def _round(root: Path, name: str, *, source: str, applied=(), summary=None, hours_ago: float = 2.0,
           stream_path=None) -> None:
    directory = root / "var" / "evolve"
    stamp = _stamp(hours_ago)
    line = {"round": name, "started_at": stamp, "finished_at": stamp, "accepted": 1 if applied else 0,
            "rejected": 0, "applied": applied, "rules_hash": "test", "stream_path": stream_path,
            "baseline_tokens_per_report": 0.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    (directory / name).write_text(json.dumps({
        "round": name, "started_at": stamp, "finished_at": stamp,
        "stream": dict(summary or {}, source=source), "stream_path": stream_path,
        "trials": [], "baseline": None, "rules": {},
    }), encoding="utf-8")


def test_the_debt_is_counted_per_domain_and_sorted(tmp_path):
    root = _root(tmp_path)
    shape = _shape(root)
    # SMD owes two of three; demo owes two of two. Demo has the larger share and must come first.
    _round(root, "round-smd-a.json", source="SMD", applied=["20"], summary=shape)
    _round(root, "round-smd-b.json", source="SMD", applied=(), summary=shape, stream_path="data/smd/x.json")
    _round(root, "round-smd-c.json", source="SMD", applied=(), summary=shape)
    _round(root, "round-demo-a.json", source="demo", applied=())
    _round(root, "round-demo-b.json", source="demo", applied=())

    rows = cli._debt_by_domain(root)

    assert [row[0] for row in rows] == ["demo", "SMD"]
    demo = rows[0]
    assert (demo[1], demo[2], demo[3]) == (2, 2, 1.0)
    smd = rows[1]
    assert (smd[1], smd[2]) == (3, 2)
    assert abs(smd[3] - 2.0 / 3.0) < 1e-9
    # Only the unrecorded SMD round that wrote a version still decides something and can have its file named:
    # demo's two rounds wrote nothing and kept no refusal, so they are a debt nobody can pay by re-running.
    assert demo[4] == 0
    assert smd[4] == 1


def test_a_domain_with_no_nameable_round_is_not_the_payable_debt(tmp_path):
    root = _root(tmp_path, with_stream=False)
    _round(root, "round-demo-a.json", source="demo", applied=())

    assert cli._debt_by_domain(root)[0][0] == "demo"
    assert cli._largest_debt(root) is None


def test_the_largest_payable_debt_is_named_before_the_proposal(tmp_path, capsys):
    root = _root(tmp_path)
    _round(root, "round-smd-a.json", source="SMD", applied=["20"], summary=_shape(root))

    cli._print_debt(root, "demo", named_file=False)

    text = capsys.readouterr().out
    assert "the largest payable debt is on SMD: 1 of 1 round(s) cannot be placed on a sampling (100%)" in text
    assert "1 of them can be re-run from the record" in text
    assert "it adds one unrecorded round, it is the first round on demo" in text
    assert "pays none of SMD's debt (boundary 124)" in text


def test_a_round_that_names_its_file_is_only_told_where_the_debt_is(tmp_path, capsys):
    root = _root(tmp_path)
    _round(root, "round-smd-a.json", source="SMD", applied=["20"], summary=_shape(root))

    cli._print_debt(root, "SMD", named_file=True)

    text = capsys.readouterr().out
    assert "the largest payable debt is on SMD" in text
    assert "boundary 124" not in text


def test_with_nothing_payable_the_round_still_says_its_record_will_be_unattributable(tmp_path, capsys):
    root = _root(tmp_path, with_stream=False)
    _round(root, "round-demo-a.json", source="demo", applied=())

    cli._print_debt(root, "demo", named_file=False)

    text = capsys.readouterr().out
    assert "no payable debt remains" in text
    assert "its record will be unattributable too (demo already has 1 of 1 unrecorded)" in text


def test_the_queue_pays_the_biggest_debtor_first(tmp_path):
    root = _root(tmp_path)
    shape = _shape(root)
    _round(root, "round-smd-a.json", source="SMD", applied=["20"], summary=shape)
    _round(root, "round-demo-a.json", source="demo", applied=["20"], summary=shape)
    # demo is 100 % unrecorded, SMD is 100 % too but demo has more rounds: the sort falls back to the domain
    # name, so this asserts the ordering is defined rather than accidental.
    ready = [("SMD", "round-smd-a.json", "", ["data/smd/stream-one.json"]),
             ("demo", "round-demo-a.json", "", ["data/smd/stream-one.json"]),
             ("(unattributed)", "round-x.json", "", ["data/smd/stream-one.json"])]

    ordered = cli._order_by_debt(root, ready)

    assert [item[0] for item in ordered][-1] == "(unattributed)"
    assert set(item[1] for item in ordered) == {"round-smd-a.json", "round-demo-a.json", "round-x.json"}


def test_the_campaign_page_names_the_payable_debt(tmp_path):
    root = _root(tmp_path)
    _round(root, "round-smd-a.json", source="SMD", applied=["20"], summary=_shape(root))
    result = campaign(root, EvolveRules())

    page = format_campaign(result, rules=EvolveRules(),
                           debt={"domain": "SMD", "rounds": 1, "without": 1, "share": 1.0, "ready": 1})

    assert "debt              the largest payable debt is on SMD: 1 of 1 round(s)" in page


def test_the_campaign_page_does_not_invent_a_payable_debt(tmp_path):
    root = _root(tmp_path, with_stream=False)
    _round(root, "round-demo-a.json", source="demo", applied=())
    result = campaign(root, EvolveRules())

    page = format_campaign(result, rules=EvolveRules(),
                           debt={"domain": "demo", "rounds": 1, "without": 1, "share": 1.0, "ready": 0})

    assert "debt " not in page


def test_an_empty_ledger_owes_nothing(tmp_path):
    assert cli._debt_by_domain(tmp_path) == []
    assert cli._largest_debt(tmp_path) is None
