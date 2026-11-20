"""Round 165 (item 151): the table becomes checkable by a command, and the gap it found gets its payload.

Round 164 put the payload inventory on a page and, while writing it, found that `--backfill-walks` had no
`--json-out` at all while its sibling `--backfill-claims` did. Two things follow, and they are the same thing seen
from two sides: a page whose claim only `pytest` can check speaks to whoever runs the suite (so `--check` evaluates
it and exits 2), and a batch command that cannot hand its result over cannot be scripted (so it now writes one).

The first run of `--check` refused the change that added it -- 24 calls against a 22-row table -- which is exactly
what the rule is for: the table moves with the calls, or the page lies (boundary 392).
"""

import json
from pathlib import Path

from brian import cli

SHAPE = {"streams": 1, "windows": 3, "scored": 2, "anomalous": 1, "events": 2, "cadence_s": 300.0}


def _round(root: Path, name: str, *, retired=None) -> dict:
    ledger = root / "var" / "evolve"
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": name,
        "started_at": "2026-11-18T04:00:00+00:00",
        "finished_at": "2026-11-18T04:05:00+00:00",
        "stream": dict(SHAPE, source="SMD"),
        "trials": [], "accepted": [], "rejected": [], "applied": [], "rules_hash": "abc",
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.25, "fpr_at_budget": 0.12},
    }
    if retired is not None:
        payload["retired"] = dict(retired)
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    line = {key: payload[key] for key in ("round", "started_at", "finished_at", "accepted", "rejected",
                                          "applied", "stream")}
    line.update({"rules_hash": "abc", "baseline_tokens_per_report": 3356.0})
    if retired is not None:
        line["retired"] = dict(retired)
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return payload


# --- the claim on the page, evaluated by the command ------------------------

def test_the_page_and_the_source_agree(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--payload-writers", "--check"]) == 0

    printed = capsys.readouterr().out
    assert "checked           the page and the source agree" in printed
    assert "%d writer(s)" % len(cli.PAYLOAD_WRITERS) in printed
    assert not (tmp_path / "var").exists(), "checking reads the source, it does not touch a ledger"


def test_check_without_the_page_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--check"]) == 2

    printed = capsys.readouterr().out
    assert "--check reads the --payload-writers page" in printed


def test_a_source_with_more_calls_than_rows_is_a_problem():
    """The first run of `--check` refused this very change: two new calls against a two-dozen-row table."""
    problems = cli.payload_writer_problems(source="x = _write_payload(a)\ny = _write_payload(b)\n")

    assert len(problems) == 1
    assert "the page lists" in problems[0]
    assert "this source has 2" in problems[0]


def test_a_previewable_flag_the_table_does_not_name_is_a_problem(monkeypatch):
    monkeypatch.setattr(cli, "BATCH_PREVIEWS", tuple(cli.BATCH_PREVIEWS) + (("--settle-new-file", "settle_new"),))
    source = Path(cli.__file__).read_bytes().decode("utf-8")

    problems = cli.payload_writer_problems(source=source)

    assert problems == ["the page does not name --settle-new-file, which --dry-run accepts"]


def test_a_consistent_source_reports_nothing():
    source = Path(cli.__file__).read_bytes().decode("utf-8")

    assert cli.payload_writer_problems(source=source) == []


# --- the gap the table found, closed ---------------------------------------

def test_a_dry_run_hands_over_what_it_would_backfill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-a.json", retired={"at": "2026-11-18T04:39:39Z",
                                              "re_measured_as": "round-x.json"})

    assert cli.main(["evolve", "--backfill-walks", "--dry-run", "--json-out", "dry.json"]) == 0

    payload = json.loads((tmp_path / "dry.json").read_text(encoding="utf-8"))
    assert payload["backfilled"] == []
    assert payload["dry_run"] is True
    assert payload["would_backfill"] == [{"round": "round-a.json", "successor": "round-x.json",
                                          "at": "2026-11-18T04:39:39Z"}]
    # A preview records nothing, so a failed export must say so (boundary 395).
    assert not (tmp_path / "var" / "evolve" / "rerun_runs.json").exists()


def test_the_walks_it_wrote_are_the_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-a.json", retired={"at": "2026-11-18T04:39:39Z",
                                              "re_measured_as": "round-x.json"})

    assert cli.main(["evolve", "--backfill-walks", "--json-out", "real.json"]) == 0

    payload = json.loads((tmp_path / "real.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] is False and payload["would_backfill"] == []
    assert len(payload["backfilled"]) == 1
    assert payload["backfilled"][0]["rounds"] == ["round-x.json"]
    stored = json.loads((tmp_path / "var" / "evolve" / "rerun_runs.json").read_text(encoding="utf-8"))
    assert stored["runs"][0]["rounds"] == payload["backfilled"][0]["rounds"]


def test_a_run_with_nothing_to_do_hands_over_nothing(tmp_path, monkeypatch, capsys):
    """Round 142's rule: a command that recorded nothing must not pretend to hand over an empty thing."""
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-a.json", retired={"at": "2026-11-18T04:39:39Z",
                                              "re_measured_as": "round-x.json"})
    assert cli.main(["evolve", "--backfill-walks"]) == 0

    assert cli.main(["evolve", "--backfill-walks", "--json-out", "second.json"]) == 0

    printed = capsys.readouterr().out
    assert "nothing to backfill: every payment the ledger names already has a walk in the store" in printed
    assert "no payload        nothing was backfilled, so there is no walk to hand over (boundary 339)." in printed
    assert not (tmp_path / "second.json").exists()


def test_the_preview_counts_only_what_the_run_would_write(tmp_path, monkeypatch, capsys):
    """Round 165's own defect: the preview named every retirement, the run wrote none (all backfilled already).

    On this machine that was 27 promised payments against a run that would write nothing, which is why the
    preview and the run now ask one function (boundary 364).
    """
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-a.json", retired={"at": "2026-11-18T04:39:39Z",
                                              "re_measured_as": "round-x.json"})
    assert cli.main(["evolve", "--backfill-walks"]) == 0
    _round(tmp_path, "round-b.json", retired={"at": "2026-11-18T04:44:00Z",
                                              "re_measured_as": "round-y.json"})

    assert cli.main(["evolve", "--backfill-walks", "--dry-run", "--json-out", "dry.json"]) == 0

    printed = capsys.readouterr().out
    assert "would backfill    round-b.json -> round-y.json" in printed
    assert "round-a.json" not in printed, "a payment the store already holds is not one this run would make"
    payload = json.loads((tmp_path / "dry.json").read_text(encoding="utf-8"))
    assert [row["successor"] for row in payload["would_backfill"]] == ["round-y.json"]


def test_two_different_nothings_say_two_different_things(tmp_path, monkeypatch, capsys):
    """A ledger naming no payment and a store holding them all are different states (boundary 367)."""
    monkeypatch.chdir(tmp_path)
    _round(tmp_path, "round-a.json")

    assert cli.main(["evolve", "--backfill-walks"]) == 0
    printed = capsys.readouterr().out
    assert "nothing to backfill: no round in the ledger names the re-run that replaced it" in printed
