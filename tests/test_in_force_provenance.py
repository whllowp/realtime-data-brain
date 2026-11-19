"""Round 85: the provenance question asked about the versions already in force.

Round 84 refused to *stack* a promise on a disputed record. That refusal only exists at the moment of stacking.
A version can be applied and then have its record's link broken by a later retirement, or copied by a repair:
it keeps running, and nothing asks again. The deployment states are the only place that records what a process
would take at start-up (`sources` maps environment variable -> owning version), so the same question is asked
of them -- on the audit page, in the campaign, and in the strict gate.

The split is the same as round 84's, for the same reason: a contradiction is a defect (`--strict` exits 2);
a copied end is a debt, reported and never charged, because no command can rewrite that end while the version
keeps running and a gate nobody can clear is a gate nobody runs (boundaries 171, 179).
"""

from __future__ import annotations

import json
from pathlib import Path

from brian import cli
from brian.campaign import campaign, format_campaign
from brian.evolve import audit, format_audit, repair_legacy
from brian.promote import LINK_BACKFILLED, LINK_BROKEN, in_force_disputes, in_force_promotions
from test_provenance_block import _legacy, _mismatch, _round, _stream, _version


def _state(root: Path, *, slug="smd", sources=None, env=None) -> None:
    """A deployment state: `sources` is what `in_force_promotions` reads, `env` what the process would run."""
    folder = root / "var" / "evolve" / "domains" / slug
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "domain": slug,
        "source": "SMD",
        "updated_at": "2026-11-18T06:10:00+00:00",
        "env": {"BRIAN_DIGEST_ROLLING_WINDOW": "200"} if env is None else env,
        "sources": {"BRIAN_DIGEST_ROLLING_WINDOW": "20"} if sources is None else sources,
    }
    (folder / "current.json").write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _legacy_state(root: Path, *, version="20") -> None:
    """The pre-round-22 global state, still read as the empty domain slug."""
    target = root / "var" / "evolve" / "current.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "env": {"BRIAN_GATE_RANK_P": "0.85"},
        "sources": {"BRIAN_GATE_RANK_P": version},
        "updated_at": "2026-11-18T06:10:00+00:00",
    }), encoding="utf-8", newline="\n")


def test_a_version_nobody_deployed_is_not_in_force(tmp_path):
    _round(tmp_path, "round-plain.json")
    assert in_force_promotions(tmp_path) == []
    assert in_force_disputes(tmp_path) == []


def test_the_states_name_the_versions_they_run(tmp_path):
    _round(tmp_path, "round-plain.json")
    _state(tmp_path, slug="smd")
    _state(tmp_path, slug="rcaeval-re1-ob", sources={"BRIAN_GATE_ANOMALY_P": "19"})
    _legacy_state(tmp_path)
    pairs = in_force_promotions(tmp_path)
    assert pairs == [("", 20), ("rcaeval-re1-ob", 19), ("smd", 20)]


def test_a_variable_owned_by_one_version_counts_once(tmp_path):
    # Two variables owned by the same version are one deployed version, not two: the question is what is
    # running, not how many knobs each version happens to own.
    _round(tmp_path, "round-plain.json")
    _state(tmp_path, sources={"BRIAN_DIGEST_ROLLING_WINDOW": "20", "BRIAN_GATE_RANK_P": "20"})
    assert in_force_promotions(tmp_path) == [("smd", 20)]


def test_a_source_that_is_not_a_version_number_is_left_to_the_other_reader(tmp_path):
    # `in_force_overrides` reports a source that is not a version; this reader only answers "which versions".
    _state(tmp_path, sources={"BRIAN_DIGEST_ROLLING_WINDOW": "tomorrow"})
    assert in_force_promotions(tmp_path) == []
    assert in_force_disputes(tmp_path) == []


def test_a_clean_ledger_has_nothing_to_report(tmp_path):
    _round(tmp_path, "round-plain.json")
    _state(tmp_path)
    assert in_force_disputes(tmp_path) == []


def test_a_version_in_force_on_a_broken_link_is_named_with_its_domain(tmp_path):
    _mismatch(tmp_path)
    _state(tmp_path, slug="smd")
    _stream(tmp_path)
    rows = in_force_disputes(tmp_path)
    assert len(rows) == 1
    assert rows[0]["domain"] == "smd" and rows[0]["version"] == 20
    assert rows[0]["kind"] == LINK_BROKEN
    assert "mismatch" in rows[0]["detail"]


def test_a_version_in_force_on_a_copied_end_is_a_debt_not_a_defect(tmp_path):
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _state(tmp_path)
    rows = in_force_disputes(tmp_path)
    assert [row["kind"] for row in rows] == [LINK_BACKFILLED]


def test_the_legacy_global_state_reports_under_the_default_domain(tmp_path):
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _legacy_state(tmp_path)
    rows = in_force_disputes(tmp_path)
    assert rows and rows[0]["domain"] == "default"


def test_the_audit_page_says_which_version_inherits_the_contradiction(tmp_path):
    _mismatch(tmp_path)
    _stream(tmp_path)
    _state(tmp_path)
    rows = in_force_disputes(tmp_path)
    result = audit(tmp_path, in_force=rows)
    text = format_audit(result)
    assert result.in_force_broken == 1 and result.in_force_copied == 0
    assert "1 version(s) in force sit on a record whose provenance the books dispute:" in text
    assert "v020" in text
    assert "A version is deployed, so refusing to stack it is no longer available" in text


def test_the_audit_does_not_charge_a_copied_end_it_reports(tmp_path):
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _state(tmp_path)
    rows = in_force_disputes(tmp_path)
    result = audit(tmp_path, in_force=rows)
    assert result.in_force_copied == 1 and result.in_force_broken == 0


def test_the_campaign_names_the_versions_in_force_on_disputed_records(tmp_path):
    _mismatch(tmp_path)
    _stream(tmp_path)
    _state(tmp_path)
    rows = in_force_disputes(tmp_path)
    result = campaign(tmp_path, in_force=rows)
    text = format_campaign(result)
    assert "1 version(s) in force sit on a record the books dispute: v020" in text
    assert result.to_dict()["in_force_disputed"][0]["kind"] == LINK_BROKEN


def test_strict_audit_exits_two_when_a_deployed_version_inherits_a_contradiction(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _mismatch(tmp_path)
    _stream(tmp_path)
    _version(tmp_path)
    _state(tmp_path)
    code = cli.main(["evolve", "--audit", "--strict"])
    out = capsys.readouterr().out
    assert code == 2
    assert "1 version(s) in force sit on a replacement link whose two ends contradict each other" in out
    assert "smd" in out and "v020" in out


def test_strict_audit_does_not_fail_on_a_deployed_version_whose_end_was_copied(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _legacy(tmp_path)
    repair_legacy(tmp_path)
    _version(tmp_path)
    _stream(tmp_path)
    _state(tmp_path)
    code = cli.main(["evolve", "--audit", "--strict"])
    out = capsys.readouterr().out
    assert code == 0
    assert "backfilled end" in out
    assert "a copied end is a debt, named here, not charged" in out


def test_the_strict_charge_is_the_one_the_link_already_makes(tmp_path, monkeypatch, capsys):
    # One defect, one exit code: a version in force on a contradiction is always also a recorded contradiction,
    # so `--strict` must not count the same fact twice -- it must name the deployed version that inherits it.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIAN_STATE_ROOT", str(tmp_path))
    _mismatch(tmp_path)
    _stream(tmp_path)
    _version(tmp_path)
    _state(tmp_path)
    rows = in_force_disputes(tmp_path)
    result = audit(tmp_path, in_force=rows)
    assert result.in_force_broken == 1
    assert all(row["broken"] for row in rows)
    assert result.broken_links >= 1
