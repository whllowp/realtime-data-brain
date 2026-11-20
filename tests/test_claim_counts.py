"""Round 160: one function counts the claim rows, and both pages print its sentence.

Round 158 marked the rows, round 159 counted them on the history page, round 158/159's campaign page counted its
own in its own words. Two readings of one quantity drift -- the history page said "row(s)" and split retired rows
out, the campaign page said "round(s)" and could not. Same function, same sentence, and each page names the
population it counted (boundaries 385/386).
"""

import json

from brian import cli
from brian.campaign import ClaimCounts, claim_counts
from brian.evolve import EVOLVE_DIR, history

STREAM = {"source": "SMD", "streams": 1, "windows": 3, "scored": 2, "anomalous": 1}
ENTRY = {"from": "", "to": "data/smd/stream-one.json", "at": "2026-11-20T08:00:00+00:00",
         "geometry": "disagrees", "why": "claimed by hand"}


def _write(root, name, *, started, entry=None, retired=None, stream_path="data/smd/stream-one.json"):
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": name, "started_at": started, "finished_at": started, "stream": dict(STREAM),
               "stream_path": stream_path, "trials": [], "accepted": [], "rejected": [], "applied": ["v020"]}
    if entry is not None:
        payload["stream_path_history"] = [dict(entry)]
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


def _plain_ledger(root):
    """One claimed row and one plain row, nothing retired: both pages count the same population."""
    _write(root, "round-claimed.json", started="2026-11-20T08:00:00+00:00", entry=ENTRY)
    _write(root, "round-plain.json", started="2026-11-20T09:00:00+00:00",
           stream_path="data/smd/stream-two.json")


def _retirement_ledger(root):
    _plain_ledger(root)
    _write(root, "round-origin.json", started="2026-11-20T07:00:00+00:00",
           entry=dict(ENTRY, claim_origin="round-origin", claim_since=ENTRY["at"], claim_hops=0,
                      claim_since_source="derived", backfilled=True),
           retired={"at": "2026-11-20T07:30:00+00:00", "re_measured_as": "round-claimed.json"})


def _claims_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("claims")]


def test_the_counts_split_rows_from_the_people_who_asserted_things(tmp_path):
    _retirement_ledger(tmp_path)
    rounds = history(tmp_path, include_retired=True)

    counts = claim_counts(rounds)

    assert (counts.rows, counts.total, counts.inherited, counts.retired) == (2, 3, 0, 1)
    # One of the two claimed rows had its claim columns written by a repair.
    assert counts.repaired_columns == 1


def test_the_sentence_names_the_population_it_counted():
    sentence = ClaimCounts(rows=1, total=3, inherited=1, repaired_columns=1,
                           population="live round(s)").sentence()

    assert sentence.startswith("1 of 3 live round(s) rest on a file a person claimed")
    assert "the geometry never confirmed it" in sentence
    # A count of rows is not a count of assertions: the retired sentence exists exactly when rows were retired.
    assert ClaimCounts(rows=1, total=3).retired_sentence() is None
    assert ClaimCounts(rows=2, total=3, retired=1).retired_sentence() == (
        "1 of them were retired into a successor, which carries the assertion on")


def test_the_payload_carries_the_population_too():
    payload = ClaimCounts(rows=1, total=2, population="row(s)").to_dict()

    assert payload["population"] == "row(s)" and payload["rows"] == 1 and payload["total"] == 2


def test_both_pages_print_one_sentence_for_one_population(tmp_path, monkeypatch, capsys):
    _plain_ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0
    from_history = _claims_lines(capsys)[0]
    assert cli.main(["evolve", "--campaign"]) == 0
    from_campaign = _claims_lines(capsys)[0]

    # Nothing is retired here, so both pages counted the same rows: the sentences must differ only in the word
    # that names the population, never in the numbers or in the rest of the reading (boundary 385).
    assert from_history.startswith("claims            1 of 2 row(s)")
    assert from_campaign.startswith("claims            1 of 2 live round(s)")
    assert from_campaign.replace("live round(s)", "row(s)") == from_history


def test_each_page_names_the_population_where_they_differ(tmp_path, monkeypatch, capsys):
    _retirement_ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history"]) == 0
    history_line = _claims_lines(capsys)[0]
    assert cli.main(["evolve", "--campaign"]) == 0
    campaign_line = _claims_lines(capsys)[0]

    # The history page lists every row (2 of 3); the campaign counts live rounds only (1 of 2 live). Two
    # questions, two totals -- and each line says which one it answered (boundary 386).
    assert history_line.startswith("claims            2 of 3 row(s)")
    assert campaign_line.startswith("claims            1 of 2 live round(s)")


def test_the_payload_carries_the_counts_the_page_printed(tmp_path, monkeypatch, capsys):
    """Round 161 (item 147): a script should not have to recount what the page already counted."""
    _retirement_ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--campaign", "--json-out", "campaign.json"]) == 0
    printed = _claims_lines(capsys)[0]
    payload = json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8"))["claim_counts"]

    assert payload["population"] == "live round(s)"
    assert (payload["rows"], payload["total"], payload["retired"]) == (1, 2, 0)
    # The payload's counts rebuild the printed sentence exactly: one computation, two readers (boundary 385).
    assert ClaimCounts(**payload).sentence() == printed.split("claims            ", 1)[1]


def test_the_history_payload_carries_the_counts_its_header_printed(tmp_path, monkeypatch, capsys):
    _retirement_ledger(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--history", "--json-out", "history.json"]) == 0
    printed = _claims_lines(capsys)
    payload = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))["claim_counts"]

    assert payload["population"] == "row(s)"
    assert ClaimCounts(**payload).sentence() == printed[0].split("claims            ", 1)[1]
    # The retired sentence is the header's second line, and the payload says how many rows it is about.
    assert payload["retired"] == 1
    assert ClaimCounts(**payload).retired_sentence() == printed[1].split("claims            ", 1)[1]
