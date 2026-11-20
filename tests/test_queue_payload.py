"""Round 142: the queue walk hands over the record it already keeps.

`--verify --from-queue N` measured promises and wrote a walk into `var/evolve/queue_runs.json`, but it nulled
`--json-out` for every inner record -- one file per record would overwrite itself -- and then handed the caller
nothing at all. So the one command that spends the most (it measures every promise in the queue) was the one whose
result a script could not read: the numbers existed in the store and nowhere else.

These tests pin the shape: one payload per walk, the record the store just wrote, a walk that measured nothing
still handing over its own walk, and a plan saying it has nothing to hand over.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli

from test_evidence_paths import THIRD
from test_from_queue import _elsewhere_only, _run, _second_record
from test_return_trip import OTHER
from test_untested_answer import NOW


def _nowhere_only(root: Path) -> None:
    """A promise whose origin file is not in this checkout: the queue lists it and cannot walk it."""
    _elsewhere_only(root)
    path = root / "var" / "evolve" / "versions" / "v020.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stream_path"] = THIRD
    payload["stream_path_history"] = [{"from": OTHER, "to": THIRD, "at": NOW.isoformat(),
                                       "why": "the file the record named is not here"}]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_walk_hands_over_the_record_it_kept(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    target = tmp_path / "walk.json"
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--json-out", str(target))
    out = capsys.readouterr().out
    assert code == 0
    assert "wrote" in out
    walk = _payload(target)["walks"][0]
    # The payload is the store's own record, not a summary written for the occasion.
    assert walk["measured"] == 1 and walk["runnable"] == 1
    assert [item["version"] for item in walk["attempted"]] == [20]
    stored = json.loads((tmp_path / "var" / "evolve" / "queue_runs.json").read_text(encoding="utf-8"))
    assert stored["runs"][-1]["attempted"] == walk["attempted"]


def test_one_payload_covers_the_whole_walk(tmp_path, monkeypatch):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    target = tmp_path / "walk.json"
    assert _run(tmp_path, monkeypatch, "--from-queue", "2", "--json-out", str(target)) == 0
    walks = _payload(target)["walks"]
    # One file per record would have been overwritten by each inner check; one record for the walk is the answer.
    assert len(walks) == 1
    assert [item["version"] for item in walks[0]["attempted"]] == [20, 21]


def test_a_walk_that_measured_nothing_is_still_a_walk(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    # The queue can list a promise whose origin file is not in this checkout: it is counted out, not walked.
    monkeypatch.setattr(cli, "origin_queue",
                        lambda root: [{"version": 20, "origin": THIRD, "origin_exists": False}])
    target = tmp_path / "walk.json"
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--json-out", str(target))
    out = capsys.readouterr().out
    assert code == 0
    assert "none of those files is in this checkout" in out
    walk = _payload(target)["walks"][0]
    assert walk["measured"] == 0 and walk["runnable"] == 0 and walk["attempted"] == []
    assert walk["skipped"] == 1


def test_a_plan_hands_over_nothing_and_says_so(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    target = tmp_path / "plan.json"
    code = _run(tmp_path, monkeypatch, "--from-queue", "0", "--json-out", str(target))
    out = capsys.readouterr().out
    assert code == 0
    # A plan records no walk, so an empty `walks` list would misreport the ledger rather than the run.
    assert not target.exists()
    assert "no payload" in out and "a plan measures nothing" in out


def test_a_walk_whose_payload_cannot_be_written_exits_non_zero(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    target = tmp_path / "walk.json"
    target.mkdir()
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--json-out", str(target))
    out = capsys.readouterr().out
    # Round 141's rule, one command over: the walk happened, so the failure is the payload and the exit code says so.
    assert code == 2
    assert "cannot write the payload" in out
    stored = json.loads((tmp_path / "var" / "evolve" / "queue_runs.json").read_text(encoding="utf-8"))
    assert stored["runs"][-1]["measured"] == 1
