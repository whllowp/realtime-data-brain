"""Round 101: the queue the campaign prints is the queue a command runs.

Round 100 could count the promises that hold no verdict on the file they were born with and name the first
command to answer one. Running that command still meant copying a version label and a path off the page, and a
hand-copied path is exactly how a check lands somewhere the promise was never made (round 99).

So `--verify --from-queue N` walks the queue in its own order, verifying each record on the file it was born
with, and `--campaign --json-out` carries the same list for a script. These tests pin the wiring: which records
are runnable, which are counted out, that the inner check is told the origin rather than whatever `--stream-file`
said, and that several checks report the worst exit code rather than the last.
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.promote import load_promotions, origin_queue, write_verification

from test_clearing_evidence_file import FILE
from test_clearing_recorded import _measurement
from test_evidence_paths import THIRD
from test_repaired_check import OTHER, _repaired
from test_untested_answer import NOW


def _elsewhere_only(root: Path) -> None:
    """A repaired record whose only verdict was taken on the substitute: it owes a check at its origin."""
    _repaired(root)
    promotion = load_promotions(root)[0]
    write_verification(root, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "failed", "reason": "recall fell",
                        "stream_path_moves": 1, "checked_path": OTHER, "on_original_path": False})


def _second_record(root: Path) -> None:
    """A second promise, born with a third file and never measured there either."""
    versions = root / "var" / "evolve" / "versions"
    payload = json.loads((versions / "v020.json").read_text(encoding="utf-8"))
    payload["version"] = 21
    payload["stream_path_history"] = [{"from": THIRD, "to": OTHER, "at": NOW.isoformat(),
                                       "why": "the file the record named does not exist in this checkout"}]
    payload["stream_path"] = OTHER
    for key in ("verification", "checks", "reverted_at"):
        payload.pop(key, None)
    (versions / "v021.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run(tmp_path, monkeypatch, *extra):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "measure", lambda *args, **kwargs: _measurement())
    return cli.main(["evolve", "--verify"] + list(extra))


def test_the_queue_lists_the_promises_never_measured_where_they_were_made(tmp_path):
    _elsewhere_only(tmp_path)
    queue = origin_queue(tmp_path)
    assert [(row["version"], row["origin"], row["origin_exists"]) for row in queue] == [(20, FILE, True)]


def test_a_record_measured_at_its_origin_leaves_the_queue(tmp_path):
    _elsewhere_only(tmp_path)
    promotion = load_promotions(tmp_path)[0]
    write_verification(tmp_path, promotion,
                       {"checked_at": NOW.isoformat(), "verdict": "holds", "reason": "measured there",
                        "stream_path_moves": 1, "checked_path": FILE, "on_original_path": True})
    assert origin_queue(tmp_path) == []


def test_the_queue_cannot_be_run_together_with_the_refusal_check(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--verify-withheld")
    out = capsys.readouterr().out
    assert code == 2
    assert "they answer different questions about different records" in out


def test_the_queue_refuses_to_be_told_which_version_to_check(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--verify-version", "20",
                "--stream-file", FILE)
    out = capsys.readouterr().out
    assert code == 2
    assert "drop --verify-version so the queue decides which promises are measured" in out


def test_a_queue_whose_files_are_all_gone_says_so_and_measures_nothing(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    (tmp_path / FILE).unlink()
    code = _run(tmp_path, monkeypatch, "--from-queue", "1")
    out = capsys.readouterr().out
    assert code == 0
    assert "no promise in the queue can be measured here" in out
    assert "none of those files is in this checkout" in out


def test_the_inner_check_is_told_the_origin_not_the_stream_file_the_caller_passed(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    original = cli.cmd_evolve
    inner: list = []

    def recorder(args, settings):
        if int(getattr(args, "from_queue", 0) or 0):
            return original(args, settings)
        inner.append(args)
        return 1

    monkeypatch.setattr(cli, "cmd_evolve", recorder)
    code = _run(tmp_path, monkeypatch, "--from-queue", "1", "--stream-file", OTHER)
    out = capsys.readouterr().out
    assert code == 1
    assert len(inner) == 1
    assert inner[0].verify_version == 20
    assert inner[0].stream_file == FILE
    assert inner[0].from_queue == 0
    assert "---- v020  <-  %s  (never measured where this promise was made)" % FILE in out


def test_the_limit_runs_only_that_many_and_says_how_many_it_left(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    code = _run(tmp_path, monkeypatch, "--from-queue", "1")
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "from queue       1 of 2 runnable promise(s)" in out
    assert "---- v020" in out and "---- v021" not in out


def test_the_whole_queue_runs_in_version_order(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    code = _run(tmp_path, monkeypatch, "--from-queue", "2")
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert out.index("---- v020") < out.index("---- v021")


def test_a_record_whose_origin_is_gone_is_counted_out_not_dropped(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    (tmp_path / THIRD).unlink()
    code = _run(tmp_path, monkeypatch, "--from-queue", "2")
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "1 of 1 runnable promise(s)" in out
    assert "1 left out: the file they were born with is not in this checkout" in out


def test_the_worst_result_of_several_checks_is_the_one_reported(tmp_path, monkeypatch):
    """Two promises, and the second is a contradiction: the caller must not read the queue as a pass."""
    _elsewhere_only(tmp_path)
    _second_record(tmp_path)
    original = cli.cmd_evolve
    seen: list = []

    def orderly(args, settings):
        if int(getattr(args, "from_queue", 0) or 0):
            return original(args, settings)
        seen.append(int(getattr(args, "verify_version", 0) or 0))
        return 0 if seen[-1] == 20 else 2

    monkeypatch.setattr(cli, "cmd_evolve", orderly)
    code = _run(tmp_path, monkeypatch, "--from-queue", "2")
    assert seen == [20, 21]
    # 2 beats 1 beats 0: a queue whose second check found a contradiction cannot report the first one's pass.
    assert code == 2


def test_the_campaign_json_carries_the_queue_a_script_would_run(tmp_path, monkeypatch, capsys):
    _elsewhere_only(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    target = tmp_path / "queue.json"
    code = cli.main(["evolve", "--campaign", "--json-out", str(target)])
    assert code in (0, 1, 2)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["origin_queue"]["records"] == 1
    assert payload["origin_queue"]["versions"][0]["version"] == 20
    assert payload["origin_queue"]["versions"][0]["origin"] == FILE
