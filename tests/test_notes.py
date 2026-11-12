"""The bounded note must actually be bounded, and must not pretend to save what it costs.

Round 3 measured what a bounded note (the SRN analogue) does in *this* architecture:

    baseline      no note, 20 events   3,965.6 tokens/alert
    note only     note,    20 events   4,079.2   (+2.9%)
    note + few    note,     4 events   2,619.8  (-33.9%)
    few only      no note,  4 events   2,506.1  (-36.8%)

The note is a net cost: cutting evidence does all the saving, and the note is added on top. That is not a
failure of the implementation — it is the correct result for a system that, unlike the RSI methods the paper
measures, already has no verification or exploration budget to remove. These tests pin the properties that
make that conclusion trustworthy: the cap is enforced on write, the note grows only by replacement, and it
is only revised when there is a failure to learn from.
"""
from __future__ import annotations

from brian.notes import NOTE_MAX_TOKENS, NoteBook


def test_the_cap_is_enforced_on_write_not_hoped_for_on_read() -> None:
    book = NoteBook(max_tokens=50)
    assert book.revise("word " * 200) is False, "an oversized revision must be rejected"
    assert book.note.text == "", "a rejected revision must leave the previous note untouched"


def test_replacement_never_append_so_cost_cannot_creep() -> None:
    """SRN rewrites one note; appending would turn a bounded note into an unbounded log."""
    book = NoteBook(max_tokens=4000)
    assert book.revise("first lesson: pool exhaustion looks like latency first.")
    assert book.revise("second lesson: deploy inside the window explains most 5xx.")
    assert "first lesson" not in book.note.text
    assert book.note.revisions == 2


def test_revision_only_when_there_is_a_failure_to_learn_from() -> None:
    """A note updated from a run of successes adds cost to every later prompt and nothing else."""
    book = NoteBook()
    assert book.should_revise([]) is False
    assert book.should_revise(["alert-1"]) is True


def test_updates_happen_on_an_interval_not_on_every_alert() -> None:
    """SRN updates every four tasks; per-alert revision would make the note a per-alert cost."""
    book = NoteBook(update_every=4)
    boundaries = [book.observe() for _ in range(8)]
    assert boundaries == [False, False, False, True, False, False, False, True]


def test_persistence_round_trip_keeps_the_cap_and_the_provenance(tmp_path) -> None:
    path = tmp_path / "note.json"
    book = NoteBook(path=path, max_tokens=200)
    assert book.revise("lesson one", sources=["alert-a", "alert-b"])
    restored = NoteBook(path=path, max_tokens=200)
    assert restored.note.text == "lesson one"
    assert restored.note.sources == ["alert-a", "alert-b"]
    assert restored.note.revisions == 1


def test_empty_note_renders_nothing_so_no_prompt_grows_by_default() -> None:
    """An untouched note must not add a section header to every prompt."""
    assert NoteBook().render() == ""


def test_default_cap_matches_the_paper() -> None:
    assert NOTE_MAX_TOKENS == 2048
