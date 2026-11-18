"""The queue can reach the rounds that named their file (round 73).

Round 71 counted two families of ceiling debt in one column: rounds whose stream file was unknown, and rounds
that named their file but never recorded the ceilings they were judged under. Only the first family was
reachable by `--rerun-unrecorded`, so round 72's gate correctly refused to charge the second: a debt nothing
could lower. This round gives the queue the second path -- `--with-ceilings` -- and with it the charge.

The collision below is the other half of the same run: `persist` named the record from `started_at`, and two
re-runs inside one second wrote one file, so the first round was retired naming evidence that was the second
round's measurement. A record is identified by its content, not by its timestamp (boundary 157).
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import budget_text
from brian.evolve import EvolveRules, Measurement, RoundResult, format_ceiling_shortfall, persist
from brian.streams import load_stream_file, summarise


def _stream(root: Path, relative: str, dataset: str, windows: int, step_hours: int) -> dict:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "format": "brian.stream/1",
        "dataset": dataset,
        "streams": [{
            "name": "host-1",
            "windows": [
                {"start": "2026-11-1%dT00:00:00+00:00" % (index * step_hours), "service": "host",
                 "scored": True, "anomaly": index == 0}
                for index in range(windows)
            ],
        }],
    }), encoding="utf-8", newline="\n")
    return summarise(load_stream_file(target).streams)


def _write_round(root, name, *, source="SMD", limits=None, applied=None, shape=None, stream_path=None):
    directory = root / "var" / "evolve"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "round": name,
        "started_at": "2026-11-18T06:00:00+00:00",
        "finished_at": "2026-11-18T06:05:00+00:00",
        "stream": dict(shape or {"source": source, "windows": 3, "scored": 2}, source=source),
        "trials": [],
        "baseline": {"tokens_per_report": 3356.0, "recall_at_budget": 0.297},
        "accepted": [],
        "applied": list(applied or []),
    }
    if stream_path is not None:
        record["stream_path"] = stream_path
    if limits is not None:
        record["limits"] = limits
    (directory / name).write_text(json.dumps(record), encoding="utf-8")
    line = {"round": name, "started_at": record["started_at"], "accepted": 0, "rejected": 0,
            "applied": list(applied or []), "rules_hash": "abc123abc123",
            "baseline_tokens_per_report": 3356.0}
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line) + "\n")


def _measurement() -> Measurement:
    return Measurement(candidate="baseline", recall=0.30, fpr=0.10, precision=0.70, reports=10,
                       tokens_per_report=3356.0, tokens_per_window=900.0, latency_p95_ms=12.0,
                       recall_at_budget=0.297, fpr_at_budget=0.092)


def test_the_named_family_is_listed_as_reachable(tmp_path):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    matters = cli._ceiling_matters(tmp_path)
    assert [entry[1] for entry in matters] == ["round-named.json"]
    assert matters[0][3] == "data/smd/stream-one.json"


def test_a_round_with_its_ceilings_or_with_nothing_at_stake_is_not_listed(tmp_path):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-recorded.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json", limits={"max_minutes": 4.0})
    _write_round(tmp_path, "round-idle.json", applied=[], shape=shape,
                 stream_path="data/smd/stream-one.json")
    assert cli._ceiling_matters(tmp_path) == []


def test_the_page_names_how_many_are_reachable():
    rows = [{"domain": "SMD", "rounds": 3, "missing": 2, "share": 2 / 3}]
    text = format_ceiling_shortfall(rows, payable=1, named=1)
    assert "2 of 3 round(s) do not record the ceilings they were judged under" in text
    assert "1 of them name their file, so --with-ceilings can re-measure them" in text


def test_nothing_payable_but_something_reachable_says_so():
    rows = [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=0, named=2)
    assert "none of them can be re-run from the record as it stands" in text
    assert "2 of them name their file" in text


def test_named_is_clamped_to_what_payable_left():
    rows = [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=1, named=5)
    assert "1 of them name their file" in text


def test_two_rounds_in_one_second_are_two_records(tmp_path):
    rules = EvolveRules()
    stream = {"source": "SMD", "windows": 3, "scored": 2}
    first = RoundResult(started_at="2026-11-18T06:15:36+00:00", finished_at="2026-11-18T06:15:41+00:00",
                        stream=dict(stream), rules=rules, baseline=_measurement())
    second = RoundResult(started_at="2026-11-18T06:15:36+00:00", finished_at="2026-11-18T06:15:36+00:00",
                         stream=dict(stream), rules=rules, baseline=_measurement())
    written_first = persist(tmp_path, first, write_versions=False)
    written_second = persist(tmp_path, second, write_versions=False)
    directory = tmp_path / "var" / "evolve"
    first_name = Path(str(written_first["round"])).name
    second_name = Path(str(written_second["round"])).name
    assert first_name != second_name
    assert sorted(path.name for path in directory.glob("round-*.json")) == sorted([first_name, second_name])
    assert json.loads((directory / first_name).read_text(encoding="utf-8"))["finished_at"] == first.finished_at
    assert json.loads((directory / second_name).read_text(encoding="utf-8"))["finished_at"] == second.finished_at


def test_a_single_step_run_mentions_the_named_family_without_paying_it(tmp_path, monkeypatch, capsys):
    """Round 74: a step measures one round of one family; the default must not double its own cost."""
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "1 round(s) name their file and never recorded their ceilings; add --with-ceilings" in text
    assert not list((tmp_path / "var" / "evolve").glob("round-2026*.json"))


def test_a_drain_pays_the_named_family_by_default(tmp_path, monkeypatch, capsys):
    """A drain means "pay everything payable", so the second family comes with it -- and says so."""
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "0"]) == 0
    text = capsys.readouterr().out
    assert "re-measuring them here (a drain pays this family too)" in text
    # The fixture keeps no candidate definitions, so the round cannot be measured here; what this test pins is
    # that the drain reached for it at all, which is the decision the round shipped.
    assert "---- re-running round-named.json on data/smd/stream-one.json" in text


def test_the_drain_rule_itself():
    from argparse import Namespace

    assert cli._wants_ceilings(Namespace(rerun_limit=0)) is True
    assert cli._wants_ceilings(Namespace(rerun_limit=1)) is False
    assert cli._wants_ceilings(Namespace(rerun_limit=3, with_ceilings=True)) is True
    assert cli._wants_ceilings(Namespace(rerun_limit=0, without_ceilings=True)) is False


def test_without_ceilings_stops_a_drain_from_paying_that_family(tmp_path, monkeypatch, capsys):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "0", "--without-ceilings"]) == 0
    text = capsys.readouterr().out
    assert "add --with-ceilings to re-measure them" in text
    assert not list((tmp_path / "var" / "evolve").glob("round-2026*.json"))


def test_the_command_named_on_the_page_pays_the_family_that_owes():
    assert cli._ceiling_command(0, 2) == "brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0"
    assert cli._ceiling_command(3, 0) == "brian evolve --rerun-unrecorded --rerun-limit 0"
    assert cli._ceiling_command(0, 0) is None


def test_the_page_sentence_names_the_command_when_one_can_pay():
    rows = [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=0, named=2, command=cli._ceiling_command(0, 2))
    assert text.endswith("measure them with brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0")
    silent = format_ceiling_shortfall(rows, payable=0, named=0, command=cli._ceiling_command(0, 0))
    assert "measure them with" not in silent


def test_the_two_families_are_counted_separately_on_the_page():
    """Round 74's defect: adding the families into one parameter made the reachable clause vanish."""
    rows = [{"domain": "SMD", "rounds": 3, "missing": 3, "share": 1.0}]
    text = format_ceiling_shortfall(rows, payable=1, named=2)
    assert "re-running 1 of them would leave 2 without one" in text
    assert "2 of them name their file" in text


def test_a_drain_prices_the_ceiling_family_it_opens(tmp_path):
    """Round 75: a drain pays both families, so a budget that prices one quotes a smaller command.

    The sentence is the reader's only warning about how long the command they were told to type will keep
    them waiting; counting one family while opening two understates the wait by exactly the work the command
    is about to do (boundary 160).
    """
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-unrecorded.json", applied=["v020"], shape=shape)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    # `ceiling_limit=0` means "drain this family too"; None means "this command does not touch it at all".
    one_family = cli._queue_budget(tmp_path, limit=0, ceiling_limit=None)
    both = cli._queue_budget(tmp_path, limit=0, ceiling_limit=0)
    assert one_family["ceiling_rounds"] == 0
    assert one_family["families"] == 1
    assert both["ceiling_rounds"] == 1
    assert both["families"] == 2
    assert both["estimated_minutes"] > one_family["estimated_minutes"]
    assert "it also re-measures 1 round(s) that named their file" in budget_text(both)
    assert "it also re-measures" not in budget_text(one_family)


def test_the_page_budget_opens_the_family_only_when_the_command_would(tmp_path, monkeypatch, capsys):
    """Round 75's live defect: `0` means drain-the-family, so "leave it alone" must be `None`.

    The first version passed `0` for a family the recommended command would not touch, and the page priced
    nine rounds of work it had just told the reader to skip.
    """
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-unrecorded.json", applied=["v020"], shape=shape)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--campaign", "--rerun-limit", "0", "--without-ceilings"])
    without = capsys.readouterr().out
    cli.main(["evolve", "--campaign", "--rerun-limit", "0"])
    with_ceilings = capsys.readouterr().out
    without_budget = next(line for line in without.splitlines() if line.startswith("budget"))
    assert "it also re-measures" not in without_budget
    assert "it also re-measures 1 round(s) that named their file" in with_ceilings
    assert "--with-ceilings" in with_ceilings
    assert "--with-ceilings" not in without_budget


def test_a_command_that_leaves_the_family_alone_says_so(tmp_path):
    """Round 76: silence about a family reads as "there is no such debt"."""
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-unrecorded.json", applied=["v020"], shape=shape)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    skipped = cli._queue_budget(tmp_path, limit=1, ceiling_limit=None)
    opened = cli._queue_budget(tmp_path, limit=1, ceiling_limit=0)
    assert skipped["ceiling_pending"] == 1
    assert skipped["ceilings_skipped"] == 1
    assert opened["ceilings_skipped"] == 0
    assert "it leaves 1 round(s) that named their file" in budget_text(skipped)
    assert "it leaves" not in budget_text(opened)


def test_an_empty_file_queue_that_left_the_family_alone_is_not_called_empty(tmp_path):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    budget = cli._queue_budget(tmp_path, limit=1, ceiling_limit=None)
    text = budget_text(budget)
    assert budget["rounds"] == 0
    assert "nothing to spend on the file family" in text
    assert "1 round(s) named their file" in text
    assert text.endswith("(the share stays 0%)")


def test_the_queue_header_names_the_families_and_their_rounds(tmp_path, monkeypatch, capsys):
    shape = _stream(tmp_path, "data/smd/stream-one.json", "smd", 3, 1)
    _write_round(tmp_path, "round-unrecorded.json", applied=["v020"], shape=shape)
    _write_round(tmp_path, "round-named.json", applied=["v020"], shape=shape,
                 stream_path="data/smd/stream-one.json")
    monkeypatch.chdir(tmp_path)
    cli.main(["evolve", "--rerun-unrecorded", "--rerun-limit", "1", "--with-ceilings"])
    out = capsys.readouterr().out
    assert "families          2: 1 round(s) from the file queue, 1 from the ceiling queue" in out
