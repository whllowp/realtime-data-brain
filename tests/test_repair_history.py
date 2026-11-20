# -*- coding: utf-8 -*-
"""Round 111: a repair leaves a record of what it changed about the plan to repair the books.

The plan is recomputed every run, so "did last time's repair help" can only be answered by a comparison captured
when the repair happened. These tests pin the record, its window, and the two pages that read it.
"""
import pathlib

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import repair_legacy
from brian.promote import (REPAIR_FORMAT, REPAIR_LIMIT, _plan_totals, record_repair, repair_history,
                           set_stream_path)

from test_clearing_evidence_file import FILE
from test_untested_answer import NOW, _copied

OTHER = "data/smd/other.json"


def _row(origin, cost, held=0, walks_blocked=1):
    return {"origin": origin, "records": [20], "cost": cost, "held": held, "walks_blocked": walks_blocked,
            "walks": 3, "last_walk": 2, "remedy": "brian evolve --audit"}


def _before():
    return [_row(FILE, 3, held=1), _row(OTHER, 1)]


def _after():
    return [_row(OTHER, 1)]


def test_a_repair_records_what_it_changed(tmp_path):
    entry = record_repair(tmp_path, "set-stream-path", _before(), _after(), now=NOW)
    assert entry["at"] == NOW.isoformat()
    assert entry["action"] == "set-stream-path"
    assert entry["freed"] == [FILE]
    assert entry["before"]["cost"] == 4
    assert entry["after"]["cost"] == 1
    assert entry["cost_fell"] == 3


def test_the_record_survives_a_reload(tmp_path):
    record_repair(tmp_path, "repair-links", _before(), _after(), now=NOW)
    stored = repair_history(tmp_path)
    assert stored["format"] == REPAIR_FORMAT
    assert stored["runs"][-1]["action"] == "repair-links"
    assert stored["last"]["freed"] == [FILE]


def test_the_totals_sum_the_plan(tmp_path):
    assert _plan_totals(_before()) == {"files": 2, "cost": 4, "held": 1, "walks_blocked": 2}
    assert _plan_totals([]) == {"files": 0, "cost": 0, "held": 0, "walks_blocked": 0}


def test_a_repair_that_frees_nothing_says_so(tmp_path):
    entry = record_repair(tmp_path, "set-stream-path", _after(), _after(), now=NOW)
    assert entry["freed"] == []
    assert entry["cost_fell"] == 0


def test_a_cheaper_file_that_stayed_is_not_freed(tmp_path):
    entry = record_repair(tmp_path, "repair-links", [_row(FILE, 3)], [_row(FILE, 1)], now=NOW)
    assert entry["freed"] == []
    assert entry["cost_fell"] == 2


def test_the_store_is_a_window(tmp_path):
    for index in range(REPAIR_LIMIT + 2):
        record_repair(tmp_path, "repair-%d" % index, _before(), _after(), now=NOW)
    stored = repair_history(tmp_path)
    assert len(stored["runs"]) == REPAIR_LIMIT
    assert stored["trimmed"] == 2
    assert stored["last"]["action"] == "repair-%d" % (REPAIR_LIMIT + 1)


def test_a_corrupt_store_reads_empty(tmp_path):
    path = tmp_path / "var" / "evolve"
    path.mkdir(parents=True)
    (path / "repairs.json").write_text("{not json", encoding="utf-8")
    stored = repair_history(tmp_path)
    assert stored["runs"] == [] and stored["last"] == {}


def test_an_unrepaired_tree_has_no_history(tmp_path):
    stored = repair_history(tmp_path)
    assert stored["runs"] == [] and stored["trimmed"] == 0


def _campaign(root):
    return format_campaign(campaign(root))


def test_the_campaign_names_what_left_the_table(tmp_path):
    record_repair(tmp_path, "set-stream-path", _before(), _after(), now=NOW)
    text = _campaign(tmp_path)
    assert "the last repair (%s, set-stream-path) took 1 file(s) off the table: %s -- cost 4 -> 1" % (
        NOW.isoformat(), FILE) in text


def test_the_campaign_does_not_invent_a_repair(tmp_path):
    assert "the last repair" not in _campaign(tmp_path)


def test_the_campaign_says_a_repair_changed_nothing(tmp_path):
    record_repair(tmp_path, "repair-links", _after(), _after(), now=NOW)
    text = _campaign(tmp_path)
    assert "changed nothing in the plan: cost 1 -> 1, 1 file(s) still owing" in text
    assert "off the table" not in text


def test_the_payload_carries_the_history(tmp_path):
    record_repair(tmp_path, "repair-links", _before(), _after(), now=NOW)
    payload = campaign(tmp_path).to_dict()
    assert payload["repair_history"]["runs"][-1]["freed"] == [FILE]


def test_set_stream_path_records_its_repair(tmp_path, monkeypatch):
    _copied(tmp_path)
    pathlib.Path(tmp_path / OTHER).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(tmp_path / OTHER).write_text(
        pathlib.Path(tmp_path / FILE).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--set-stream-path", "v020=%s" % OTHER]) == 0
    stored = repair_history(tmp_path)
    assert stored["last"]["action"] == "set-stream-path"
    assert stored["last"]["after"]["files"] == 0


def test_a_move_that_changes_nothing_records_nothing(tmp_path, monkeypatch):
    _copied(tmp_path)
    monkeypatch.chdir(tmp_path)
    record = set_stream_path(tmp_path, 20, FILE, now=NOW)
    assert record["changed"] is False
    assert cli.main(["evolve", "--set-stream-path", "v020=%s" % FILE]) == 0
    assert repair_history(tmp_path)["runs"] == []


def test_repair_links_records_its_repair(tmp_path, monkeypatch):
    from test_link_repair import _legacy

    _legacy(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["evolve", "--repair-links", "--apply"]) == 0
    stored = repair_history(tmp_path)
    assert stored["last"]["action"] == "repair-links"
    assert repair_legacy(tmp_path, dry_run=True) == []