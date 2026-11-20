"""Round 159: the history header counts the rows that rest on a person's claim (and names the parts).

Round 158 put the mark on each row; a reader who has to scroll a 74-row list to count them has been given a
detail and not a fact. Two numbers to keep apart: rows that rest on a claim, and the people's assertions those
rows carry -- a retired row's assertion lives on in its successor (boundary 383).
"""

import json

from brian import cli
from brian.evolve import EVOLVE_DIR

STREAM = {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1}
PLAIN = {"from": "", "to": "data/smd/stream-one.json", "at": "2026-11-20T08:00:00+00:00",
         "geometry": "disagrees", "why": "claimed by hand"}


def _write(root, name, *, started, history=None, retired=None, stream_path="data/smd/stream-one.json"):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": name, "started_at": started, "finished_at": started, "stream": dict(STREAM),
               "stream_path": stream_path, "trials": [], "accepted": [], "rejected": [], "applied": ["v020"]}
    if history is not None:
        payload["stream_path_history"] = history
    if retired:
        payload["retired"] = dict(retired)
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line["rules_hash"] = "abc"
    if retired:
        line["retired"] = dict(retired)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _chain(root):
    """One assertion carried through two rows: the origin was retired into a successor that inherited it."""
    _write(root, "round-origin.json", started="2026-11-20T08:00:00+00:00",
           history=[dict(PLAIN, claim_origin="round-origin", claim_since=PLAIN["at"], claim_hops=0,
                         claim_since_source="derived", backfilled=True)],
           retired={"at": "2026-11-20T09:00:00+00:00", "re_measured_as": "round-successor.json"})
    _write(root, "round-successor.json", started="2026-11-20T09:00:00+00:00",
           history=[dict(PLAIN, inherited_from="round-origin.json", claim_origin="round-origin",
                         claim_since=PLAIN["at"], claim_hops=1, claim_since_source="backfilled",
                         backfilled=True)])
    _write(root, "round-plain.json", started="2026-11-20T10:00:00+00:00", stream_path="data/smd/stream-two.json")


def _header(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("claims")]


def test_the_header_counts_the_rows_that_rest_on_a_claim(tmp_path, monkeypatch, capsys):
    _chain(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0

    lines = _header(capsys)
    assert lines[0].startswith("claims            2 of 3 row(s) rest on a file a person claimed")
    assert "1 inherited at least once" in lines[0]


def test_the_header_names_how_many_were_retired_into_a_successor(tmp_path, monkeypatch, capsys):
    _chain(tmp_path)
    monkeypatch.chdir(tmp_path)

    cli.main(["evolve", "--history"])

    lines = _header(capsys)
    # Two rows, one assertion: without the second line a reader would count the same claim twice.
    assert lines[1] == ("claims            1 of them were retired into a successor, "
                        "which carries the assertion on")


def test_a_claim_column_a_repair_wrote_is_counted_as_that_and_not_as_a_repaired_age(tmp_path, monkeypatch,
                                                                                   capsys):
    _chain(tmp_path)
    monkeypatch.chdir(tmp_path)

    cli.main(["evolve", "--history"])

    line = _header(capsys)[0]
    # `backfilled` means the claim's columns were written by a repair. It does not mean the date came from one:
    # the origin record's date is its own, and this count must not claim otherwise (boundary 384).
    assert "whose claim columns a repair wrote" in line
    assert "completed by a repair" not in line


def test_a_ledger_with_no_claim_prints_no_header_line(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "round-plain.json", started="2026-11-20T10:00:00+00:00",
           stream_path="data/smd/stream-two.json")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0

    assert _header(capsys) == []
