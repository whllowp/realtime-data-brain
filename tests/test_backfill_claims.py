"""Round 156: give a pre-155 inherited record its chain back, and say where each date came from."""

import json

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import EVOLVE_DIR, backfill_claims, claimed_rounds

ORIGIN_AT = "2026-11-20T08:38:50+00:00"
MEASURED_AT = "2026-11-20T09:06:13+00:00"


def _record(name, *, history, stream_path="data/smd/stream-one.json"):
    return {"round": name, "started_at": ORIGIN_AT, "finished_at": MEASURED_AT, "stream": {"source": "SMD"},
            "stream_path": stream_path, "stream_path_history": history, "applied": ["v020"],
            "trials": [], "accepted": [], "rejected": []}


def _write(root, record):
    directory = root / EVOLVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / record["round"]).write_bytes(json.dumps(record, indent=2).encode("utf-8"))
    ledger = directory / "ledger.jsonl"
    existing = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    ledger.write_bytes((existing + json.dumps({"round": record["round"], "started_at": record["started_at"]})
                        + "\n").encode("utf-8"))


def _pre155(root):
    """A round-154 record: one hop, no chain fields, and an `at` that is the later measurement."""
    _write(root, _record("round-origin.json", history=[
        {"from": "", "to": "data/smd/stream-one.json", "at": ORIGIN_AT, "geometry": "disagrees",
         "why": "claimed by hand"}]))
    _write(root, _record("round-successor.json", history=[
        {"from": "data/smd/stream-one.json", "to": "data/smd/stream-one.json", "at": MEASURED_AT,
         "geometry": "disagrees", "why": "claimed by hand", "inherited_from": "round-origin.json"}]))
    return root


def test_the_backfill_fills_the_chain_and_derives_the_first_moment(tmp_path):
    rows = backfill_claims(_pre155(tmp_path))

    # Both rounds predate the fields: the origin gets hops 0 (its own `at` is the moment), the successor hops 1
    # (its moment is derived from the record it points at).
    assert [(row["round"], row["entries"], row["derived"], row["unknown"]) for row in rows] == [
        ("round-origin.json", 1, 1, 0), ("round-successor.json", 1, 1, 0)]
    stored = json.loads((tmp_path / EVOLVE_DIR / "round-successor.json").read_text(encoding="utf-8"))
    entry = stored["stream_path_history"][0]
    assert entry["claim_origin"] == "round-origin.json"
    # Derived from the record the entry points at -- not from the re-measurement's own `at`.
    assert entry["claim_since"] == ORIGIN_AT
    assert entry["claim_since_source"] == "derived"
    assert entry["claim_hops"] == 1
    assert entry["backfilled"] is True


def test_a_missing_origin_is_reported_as_unknown_not_guessed(tmp_path):
    _write(tmp_path, _record("round-orphan.json", history=[
        {"from": "data/smd/stream-one.json", "to": "data/smd/stream-one.json", "at": MEASURED_AT,
         "geometry": "disagrees", "why": "", "inherited_from": "round-gone.json"}]))

    rows = backfill_claims(tmp_path)

    assert rows[0]["unknown"] == 1 and rows[0]["derived"] == 0
    entry = json.loads((tmp_path / EVOLVE_DIR / "round-orphan.json").read_text(encoding="utf-8"))[
        "stream_path_history"][0]
    assert entry["claim_since"] == ""
    assert entry["claim_since_source"] == "unknown"


def test_a_hand_recorded_entry_is_its_own_origin_with_no_hops(tmp_path):
    rows = backfill_claims(_pre155(tmp_path))

    # `round-origin.json` had no `inherited_from`, so it is a claim recorded on the round itself: hops 0.
    origin = json.loads((tmp_path / EVOLVE_DIR / "round-origin.json").read_text(encoding="utf-8"))
    assert rows and origin["stream_path_history"][0]["claim_hops"] == 0
    assert origin["stream_path_history"][0]["claim_since"] == ORIGIN_AT


def test_a_second_run_has_nothing_to_do(tmp_path):
    _pre155(tmp_path)

    assert backfill_claims(tmp_path)
    assert backfill_claims(tmp_path) == []


def test_the_preview_writes_nothing_but_says_what_it_would_do(tmp_path):
    root = _pre155(tmp_path)
    before = (root / EVOLVE_DIR / "round-successor.json").read_bytes()

    rows = backfill_claims(root, dry_run=True)

    assert rows and rows[0]["derived"] == 1
    assert (root / EVOLVE_DIR / "round-successor.json").read_bytes() == before


def test_the_page_stops_calling_a_repaired_chain_hopless(tmp_path):
    root = _pre155(tmp_path)
    backfill_claims(root)

    rows = claimed_rounds([json.loads((root / EVOLVE_DIR / "round-successor.json").read_text(encoding="utf-8"))])

    assert rows[0]["hops"] == 1
    assert rows[0]["origin"] == "round-origin.json"
    assert rows[0]["since"] == ORIGIN_AT
    page = format_campaign(campaign(root), claims=rows)
    assert "was inherited 1 time(s) and was first recorded on round-origin.json at " + ORIGIN_AT in page


def test_the_command_reports_what_it_filled(tmp_path, monkeypatch, capsys):
    root = _pre155(tmp_path / "repo")
    monkeypatch.chdir(root)

    assert cli.main(["evolve", "--backfill-claims"]) == 0
    printed = capsys.readouterr().out
    assert "filled           round-successor.json  1 entr(y/ies): 1 dated from the record they point at, 0 unknown" in printed
    assert "each entry says backfilled" in printed
    assert cli.main(["evolve", "--backfill-claims"]) == 0
    assert "nothing to backfill: every claim chain already carries its origin" in capsys.readouterr().out


def test_the_preview_is_reachable_through_the_batch_table(tmp_path, monkeypatch, capsys):
    root = _pre155(tmp_path / "repo")
    monkeypatch.chdir(root)

    assert cli.main(["evolve", "--backfill-claims", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "would fill       round-successor.json" in printed
    assert "nothing was written" in printed
    assert "--backfill-claims" in cli.previews_sentence()
