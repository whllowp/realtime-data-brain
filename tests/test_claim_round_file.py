"""Round 152: a repair command for the rounds no queue can reach.

Round 151 split the ceiling debt by the reader's next step and said the out-of-reach half needs a repaired
record. Nothing could repair one. `--claim-round-file` is that verb; these tests hold it to the three
refusals that keep a "repair" from being a way to move a row into the queue's reach without making it true.
"""

import json

import pytest

from brian import cli
from brian.evolve import EVOLVE_DIR, claim_round_file, history
from brian.streams import FORMAT

SHAPE = {"windows": 3, "scored": 2, "cadence_s": 300.0}


def _stream(root, name="stream-one.json", *, windows=3, scored=2, cadence_s=300):
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


def _record(root, name="round-a.json", *, shape=None, stream_path=None):
    """A round whose record names no file, in both books."""
    ledger = root / EVOLVE_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    payload = {"round": name, "started_at": "2026-11-18T05:00:00+00:00", "accepted": [], "rejected": [],
               "applied": ["v020"], "rules_hash": "abc", "stream": {"source": "SMD", **(shape or SHAPE)},
               "stream_path": stream_path}
    (ledger / name).write_text(json.dumps(payload), encoding="utf-8")
    with open(ledger / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    return ledger / name


def _agrees(_path):
    return "agrees"


def test_a_claim_records_the_file_and_the_history(tmp_path):
    _stream(tmp_path)
    _record(tmp_path)

    done = claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=_agrees)

    assert done[0]["changed"] is True and done[0]["to"] == "data/smd/stream-one.json"
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-a.json").read_text(encoding="utf-8"))
    assert stored["stream_path"] == "data/smd/stream-one.json"
    entry = stored["stream_path_history"][0]
    assert (entry["from"], entry["to"], entry["geometry"]) == ("", "data/smd/stream-one.json", "agrees")
    # Round 44's rule: the repair has to land in the other book too, or every page that reads the ledger keeps
    # saying the round named no file.
    assert history(tmp_path)[0]["stream_path"] == "data/smd/stream-one.json"


def test_a_claim_about_a_round_nobody_recorded_is_refused(tmp_path):
    _stream(tmp_path)

    with pytest.raises(ValueError, match="no round record for round-missing.json"):
        claim_round_file(tmp_path, ["round-missing.json=data/smd/stream-one.json"], geometry_of=_agrees)


def test_a_claim_about_a_file_this_checkout_cannot_open_is_refused(tmp_path):
    _record(tmp_path)

    # The repair would fix the paperwork and lose the evidence (round 92's rule).
    with pytest.raises(ValueError, match="does not exist in this checkout"):
        claim_round_file(tmp_path, ["round-a.json=data/smd/nowhere.json"], geometry_of=_agrees)


def test_a_file_the_recorded_geometry_does_not_point_at_is_refused(tmp_path):
    _stream(tmp_path)
    _record(tmp_path)

    with pytest.raises(ValueError, match="does not agree with the geometry"):
        claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=lambda _p: "disagrees")


def test_force_records_a_file_the_geometry_does_not_point_at(tmp_path):
    _stream(tmp_path)
    _record(tmp_path)

    done = claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], force=True,
                            geometry_of=lambda _p: "disagrees")

    # The claim is recorded with the verdict, so the record says a person overruled the matcher rather than
    # claiming the matcher agreed (boundary 369).
    assert done[0]["geometry"] == "disagrees"
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-a.json").read_text(encoding="utf-8"))
    assert stored["stream_path_history"][0]["geometry"] == "disagrees"


def test_a_record_that_already_names_another_file_is_not_repaired_without_force(tmp_path):
    _stream(tmp_path)
    _stream(tmp_path, "stream-two.json")
    _record(tmp_path, stream_path="data/smd/stream-two.json")

    with pytest.raises(ValueError, match="the record already names data/smd/stream-two.json"):
        claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=_agrees)

    done = claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], force=True, geometry_of=_agrees)
    assert done[0]["from"] == "data/smd/stream-two.json"


def test_claiming_the_file_it_already_names_changes_nothing(tmp_path):
    _stream(tmp_path)
    _record(tmp_path, stream_path="data/smd/stream-one.json")

    done = claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=_agrees)

    assert done[0]["changed"] is False
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-a.json").read_text(encoding="utf-8"))
    assert "stream_path_history" not in stored


def test_a_preview_validates_and_writes_nothing(tmp_path):
    _stream(tmp_path)
    _record(tmp_path)

    done = claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=_agrees, dry_run=True)

    assert done[0]["changed"] is True
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-a.json").read_text(encoding="utf-8"))
    assert stored["stream_path"] is None
    # The preview must run the geometry check too: a plan the real command refuses is the round-135 defect.
    with pytest.raises(ValueError, match="does not agree with the geometry"):
        claim_round_file(tmp_path, ["round-a.json=data/smd/stream-one.json"], geometry_of=lambda _p: "disagrees",
                         dry_run=True)


def test_the_command_moves_the_row_out_of_the_reachless_half(tmp_path, monkeypatch, capsys):
    # Geometry that no file here has: the row is in the half no command can pay, which is the half this
    # command exists for. A row the geometry already matches is the queue's job, not this one's.
    _stream(tmp_path)
    _record(tmp_path, shape={"windows": 7, "scored": 5, "cadence_s": 600.0})
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--unrecorded"]) in (0, 2)
    before = capsys.readouterr().out
    assert "out of reach      1 round(s)" in before

    assert cli.main(["evolve", "--claim-round-file", "round-a.json=data/smd/stream-one.json",
                     "--force-claim"]) == 0
    claimed = capsys.readouterr().out
    assert "claimed          round-a.json  now names data/smd/stream-one.json (geometry disagrees)" in claimed

    assert cli.main(["evolve", "--unrecorded"]) in (0, 2)
    after = capsys.readouterr().out
    # Nothing is left to split: the row has a file now, so it is no longer this page's business at all.
    assert "0 of them still decide something" in after
    assert "Every round that still decides something says which file it measured." in after
    assert "out of reach" not in after


def test_the_command_refuses_a_disagreement_and_exits_two(tmp_path, monkeypatch, capsys):
    _stream(tmp_path, windows=3, scored=2)
    _record(tmp_path, shape={"windows": 7, "scored": 5, "cadence_s": 600.0})
    monkeypatch.chdir(tmp_path)

    assert cli.main(["evolve", "--claim-round-file", "round-a.json=data/smd/stream-one.json"]) == 2
    printed = capsys.readouterr().out
    assert "cannot claim that record: data/smd/stream-one.json does not agree with the geometry" in printed
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-a.json").read_text(encoding="utf-8"))
    assert stored["stream_path"] is None
