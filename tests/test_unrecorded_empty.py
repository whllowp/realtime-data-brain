"""Round 172 (item 158): three empty ledgers are three facts, and each still hands over a payload.

`--unrecorded` used to return before its write when nothing was unrecorded, so a script asking "is there work?"
got no file at all -- and a missing file reads exactly like a failed run. It also printed one sentence for three
different situations: no round at all, every round placed, and rounds that name no file but decide nothing. The
payload now carries `why_empty`, so a consumer does not have to infer the answer from three empty lists.
"""

import json

from brian import cli

ROUND = {"round": "round-placed.json", "started_at": "2026-11-18T05:00:00+00:00",
         "accepted": [], "rejected": [], "applied": ["v020"], "rules_hash": "abc",
         "stream": {"source": "SMD", "windows": 3, "scored": 2, "cadence_s": 300.0},
         "stream_path": "data/smd/stream-one.json"}


def _record(root, payload):
    ledger = root / "var" / "evolve"
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / payload["round"]).write_text(json.dumps(payload), encoding="utf-8")
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _payload(root, capsys, *args):
    out = root / "page.json"
    code = cli.main(["evolve", "--unrecorded", "--json-out", str(out), *args])
    printed = capsys.readouterr().out
    return code, printed, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None)


def test_an_empty_ledger_still_hands_over_a_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    code, printed, payload = _payload(tmp_path, capsys)

    assert code == 0
    assert "nothing recorded: this ledger holds no round yet" in printed
    assert payload is not None, "a script asking for the answer got no file at all"
    assert payload["why_empty"] == "no rounds"
    assert payload["matters"] == [] and payload["payable"] == [] and payload["out_of_reach"] == []


def test_every_round_placed_is_its_own_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _record(tmp_path, ROUND)

    code, printed, payload = _payload(tmp_path, capsys)

    assert code == 0
    assert "Every round that still decides something says which file it measured." in printed
    assert payload["why_empty"] == "all placed"


def test_rounds_that_decide_nothing_are_a_third_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # A record with no file, no applied version and no refusal: it is unplaceable and it decides nothing.
    _record(tmp_path, {key: value for key, value in ROUND.items()
                       if key not in ("applied", "stream_path")} )

    code, printed, payload = _payload(tmp_path, capsys)

    assert code == 0
    assert "nothing to re-run: 1 round(s) name no file, and none of them still decides anything" in printed
    assert payload["why_empty"] == "none deciding"
    assert payload["unattributed"] == 1


def test_a_page_with_work_says_why_empty_is_not_a_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _record(tmp_path, {key: value for key, value in ROUND.items() if key != "stream_path"})

    code, printed, payload = _payload(tmp_path, capsys)

    assert code == 0
    assert "round-placed.json" in printed
    assert payload["why_empty"] is None, "null means 'there is work', not 'no reason recorded'"
    assert payload["matters"]


def test_the_empty_page_also_says_it_charged_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    _code, _printed, payload = _payload(tmp_path, capsys)

    assert payload["gate"]["checked"] is False
    assert payload["gate"]["command"]
