"""Round 177 (item 163): the last five payload shapes, each checked against the thing that builds it.

The first three (`--set-domain-limit / --set-domain-drain`, `--sensitivity`, `--withheld`) are driven and the
payload is read back, because `_write_payload` refuses a payload whose keys disagree with the declaration
(boundary 416). The other two (`evaluate`, `dataset --summary-out`) are a report class's own `to_dict()` plus the
field the branch adds, so the check builds the object instead of running the whole pipeline.

The test that matters most is the first one: for twenty-two rounds each batch of declarations covered part of the
table, and nothing checked that the *last* row was ever declared. A table with 24 rows and 19 declarations looks
finished from every row -- only the set difference says otherwise (boundary 425).
"""
import json
from datetime import datetime, timezone

from brian import cli
from brian.contracts import RcaReport
from brian.export import TrainingExample, summarise


def _root(root):
    (root / "var" / "evolve").mkdir(parents=True, exist_ok=True)
    return root


def _keys(path):
    return tuple(sorted(json.loads(path.read_text(encoding="utf-8"))))


def test_every_writer_row_has_a_declared_shape():
    """Completeness is a set difference, not a reading of the page: a row can look declared until you count."""
    rows = {name for name, _payload, _kind in cli.PAYLOAD_WRITERS}

    assert rows == set(cli.PAYLOAD_KEYS), sorted(rows ^ set(cli.PAYLOAD_KEYS))
    assert len(cli.PAYLOAD_KEYS) == len(cli.PAYLOAD_WRITERS) == 24


def test_the_sensitivity_payload_is_exactly_its_declaration(tmp_path, monkeypatch):
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "sensitivity.json"

    assert cli.main(["evolve", "--sensitivity", "--json-out", str(out)]) == 0

    assert _keys(out) == cli.PAYLOAD_KEYS["--sensitivity"]
    # The declaration is not just "some keys": it names the footer the page adds and the seven the class carries.
    assert "gate" in cli.PAYLOAD_KEYS["--sensitivity"]
    assert "recorded" in cli.PAYLOAD_KEYS["--sensitivity"]


def test_the_withheld_payload_is_exactly_its_declaration(tmp_path, monkeypatch):
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "withheld.json"

    assert cli.main(["evolve", "--withheld", "--json-out", str(out)]) == 0

    assert _keys(out) == cli.PAYLOAD_KEYS["--withheld"] == ("bar", "gate", "withheld")


def test_the_domain_limit_payload_is_exactly_its_declaration(tmp_path, monkeypatch):
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "domain.json"

    assert cli.main(["evolve", "--set-domain-limit", "smd=4", "--json-out", str(out)]) == 0

    assert _keys(out) == cli.PAYLOAD_KEYS["--set-domain-limit / --set-domain-drain"]
    assert "in_force" in cli.PAYLOAD_KEYS["--set-domain-limit / --set-domain-drain"]


def test_the_evaluate_declaration_is_the_report_plus_its_recommendation():
    """The payload is `report.to_dict()` plus one field the branch assigns, so the object is the authority."""
    report = RcaReport(alert_id="a1", decision_id="d1", service="checkout", summary="s", root_cause="r")

    built = set(report.to_dict()) | {"recommendation"}

    assert tuple(sorted(built)) == cli.PAYLOAD_KEYS["evaluate"]
    assert "recommendation" in built


def test_the_dataset_declaration_covers_both_shapes_a_summary_has():
    """`summarise` returns `{"n_examples": 0}` for nothing and seven keys otherwise; both are this payload."""
    declared = set(cli.PAYLOAD_KEYS["dataset --summary-out"])
    empty = summarise([])
    one = TrainingExample(state="cpu 1.00", answers={}, window_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                          service="host-1", scenario="steady", digest_id="d1", label_mask={}, label_source={})
    full = summarise([one])

    assert set(empty) <= declared
    assert set(full) == declared, sorted(set(full) ^ declared)


def test_the_page_says_there_is_nothing_left_undeclared(monkeypatch, capsys):
    """Round 177: the old sentence warned about 0 rows not declared, which is a page arguing with itself."""
    monkeypatch.setattr("sys.argv", ["brian"])

    assert cli.main(["evolve", "--payload-writers"]) == 0

    printed = capsys.readouterr().out
    assert "every row is declared above" in printed
    assert "the 0 row(s) not declared here" not in printed
    assert "A payload's keys are declared for 24 row(s)" in printed
