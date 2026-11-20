"""Round 151: the part of the ceiling debt no command can pay is named, not left to subtraction.

A round that recorded neither its file nor its ceilings is not one debt but two: the queue can re-measure a
round that names exactly one candidate file, and cannot touch one that names none or several. The debt line
said "47 of 56 ... and none of them can be re-run", which is true and leaves the reader to work out which
rounds are which; `--unrecorded` printed one table for both, so a row invited a command that could not help.
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR, format_ceiling_shortfall
from brian.streams import FORMAT

ROWS = [{"domain": "SMD", "rounds": 3, "missing": 2, "share": 2 / 3}]


def _stream(root, name, *, windows, scored, cadence_s=300):
    minutes = [index * (cadence_s // 60) for index in range(windows)]
    moments = [{"start": "2026-01-01T%02d:%02d:00+00:00" % (offset // 60, offset % 60),
                "service": "host-1", "scored": index < scored, "anomaly": False}
               for index, offset in enumerate(minutes)]
    path = root / "data" / "smd" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "dataset": "SMD", "window_s": 300,
                                "overrides": {}, "streams": [{"name": "host-1", "windows": moments}]}),
                    encoding="utf-8")
    return path


def _record(root, name, shape):
    """A round that recorded a geometry and no file -- the shape both debts start from."""
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": name, "started_at": "2026-11-18T05:00:00+00:00", "accepted": [], "rejected": [],
               "applied": ["v020"], "rules_hash": "abc", "stream": {"source": "SMD", **shape}}
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8")
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _root_with_both_debts(root):
    """One round the queue can re-run and one it never can, in the same table."""
    _stream(root, "stream-one.json", windows=3, scored=2)
    _record(root, "round-reachable.json", {"windows": 3, "scored": 2, "cadence_s": 300.0})
    _record(root, "round-reachless.json", {"windows": 7, "scored": 5, "cadence_s": 600.0})
    return root


def test_the_debt_line_names_the_part_no_command_can_pay():
    note = format_ceiling_shortfall(ROWS, payable=1, unpayable=1)

    assert "2 of 3 round(s) do not record the ceilings they were judged under" in note
    assert "1 of them are out of reach: no command can pay a round that names no single file" in note


def test_a_debt_nobody_can_pay_still_says_which_part_is_unreachable():
    note = format_ceiling_shortfall(ROWS, payable=0, unpayable=2)

    # The old sentence stopped at "none of them can be re-run", which says nothing about which rows need a
    # repaired record rather than a queue run (boundary 366).
    assert "and none of them can be re-run" in note
    assert "2 of them are out of reach" in note
    assert note.endswith("cap stays a warning")


def test_a_payable_debt_with_nothing_unreachable_reads_exactly_as_before():
    note = format_ceiling_shortfall(ROWS, payable=2, unpayable=0)

    assert "out of reach" not in note


def test_the_unreachable_count_cannot_exceed_the_debt():
    note = format_ceiling_shortfall(ROWS, payable=1, named=0, unpayable=99)

    # 2 missing, 1 payable -- at most one can be out of reach; clamping keeps the sentence arithmetic sound.
    assert "1 of them are out of reach" in note


def test_the_page_prints_the_two_debts_apart_and_the_payload_splits_them(tmp_path, monkeypatch, capsys):
    _root_with_both_debts(tmp_path)
    monkeypatch.chdir(tmp_path)

    code = cli.main(["evolve", "--unrecorded", "--json-out", str(tmp_path / "u.json")])
    printed = capsys.readouterr().out

    assert code in (0, 2)
    assert "payable           1 round(s) name exactly one candidate file" in printed
    assert "out of reach      1 round(s) name no single file" in printed
    assert printed.index("payable ") < printed.index("out of reach ")
    payload = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    assert [row["round"] for row in payload["payable"]] == ["round-reachable.json"]
    assert [row["round"] for row in payload["out_of_reach"]] == ["round-reachless.json"]
    assert [row["round"] for row in payload["matters"]] == ["round-reachable.json", "round-reachless.json"]
    assert payload["ceilings"]["unpayable"] == 1
