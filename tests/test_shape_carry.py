# -*- coding: utf-8 -*-
"""Round 29, part 2: a plain re-check must not delete the shape an earlier check measured."""
import json
from datetime import datetime, timedelta, timezone

from brian.promote import UNMEASURABLE, load_promotions, shape_note, write_verification

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
SHAPE = {"window": 286, "recall_at_budget": 0.427, "claimed_recall": 0.507, "reports": 219}


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _with_shape(tmp_path):
    directory = tmp_path / "var" / "evolve" / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "v020.json"
    path.write_text(json.dumps({
        "version": 20,
        "candidate": {"name": "host-rolling-rank-0-85-200", "hypothesis": "h",
                      "changes": [["digest", "rolling_window", 200]], "veto": None},
        "change": "digest.rolling_window = 200",
        "env": "BRIAN_DIGEST_ROLLING_WINDOW=200",
        "rollback": "unset BRIAN_DIGEST_ROLLING_WINDOW",
        "claim": {"recall_at_budget": 0.507},
        "created_at": _stamp(2),
        "stream": {"source": "SMD", "windows": 948, "scored": 828},
        "verification": {"checked_at": _stamp(1), "stream": "SMD", "verdict": UNMEASURABLE,
                         "reason": "the re-check measured 1235 scored windows against the 828",
                         "shape": dict(SHAPE)},
    }), encoding="utf-8")
    return path


def test_a_plain_recheck_carries_the_shape_and_says_when_it_was_measured(tmp_path):
    path = _with_shape(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, {
        "checked_at": _stamp(0), "stream": "SMD", "verdict": UNMEASURABLE,
        "reason": "the re-check measured 1235 scored windows against the 828",
    })

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["verification"]["shape"] == SHAPE
    # The number keeps the date of the check that measured it, not the date of the re-check that did not.
    assert saved["verification"]["shape_from"] == _stamp(1)
    note = shape_note(saved["verification"])
    assert "recalls 0.427" in note
    assert "measured 2026-11-17" in note


def test_a_recheck_that_measures_its_own_shape_is_not_dated_from_the_earlier_one(tmp_path):
    path = _with_shape(tmp_path)
    promotion = load_promotions(tmp_path)[0]

    write_verification(tmp_path, promotion, {
        "checked_at": _stamp(0), "stream": "SMD", "verdict": UNMEASURABLE,
        "reason": "the re-check measured 1235 scored windows against the 828",
        "shape": {"window": 286, "recall_at_budget": 0.5, "claimed_recall": 0.507},
    })

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["verification"]["shape"]["recall_at_budget"] == 0.5
    assert "shape_from" not in saved["verification"]
