"""A bounded operator note — the SRN analogue from "The RSI Tax".

What SRN is, and what it is not
--------------------------------
In arXiv:2611.13524v1, Simple Reflection with Notes keeps **one** note capped at 2,048 tokens, revises it
only from *failed* tasks, only at update boundaries, and performs no candidate search, no extra sampling and
no verification. It matched or beat three costlier self-improvement methods while using 65.6-71.0% fewer
tokens, because those methods spent 74.8-93.6% of their budget on verification.

This system is already at SRN's end of that spectrum: one LLM call per report, no candidates, no re-scoring.
So a note here cannot save what is not being spent. What it can do is **substitute** — carry the standing
lessons so the per-alert prompt does not have to re-derive them from a full metric dump every time.

That distinction decides how this module is measured: not "how much does the note save" (it costs tokens on
every prompt it enters) but "does note + smaller evidence beat no-note + full evidence at equal or lower
cost". Anything else would be reporting a saving that comes from somewhere else.

Design choices, each following the paper:
  * hard 2,048-token cap, enforced at write time rather than hoped for at read time;
  * revised only from alerts with a *failure* signal, since a note distilled from successes learns nothing;
  * revision happens on an interval (every N alerts), not on every alert — SRN updates every four tasks;
  * bounded growth: the note is rewritten, never appended, so its cost cannot creep.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Hard cap from the paper. Enforced on write.
NOTE_MAX_TOKENS = 2048

#: SRN revises every four completed tasks; the same interval is used here so the note cannot become a
#: per-alert cost.
UPDATE_EVERY = 4


def _approx_tokens(text: str) -> int:
    """Cheap token estimate: words plus punctuation lumps.

    Deliberately rough and clearly labelled as such. The prompt builder has a real estimator for messages;
    this one exists so the note cannot grow unbounded while still being cheap to check on every write.
    """
    if not text:
        return 0
    words = text.split()
    return int(len(words) * 1.35) + text.count("\n")


@dataclass
class OperatorNote:
    """One bounded note: standing lessons, not a log."""

    text: str = ""
    revisions: int = 0
    updated_at: Optional[str] = None
    #: Which alerts the last revision drew from, for auditability.
    sources: List[str] = field(default_factory=list)

    def tokens(self) -> int:
        return _approx_tokens(self.text)

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "revisions": self.revisions,
                "updated_at": self.updated_at, "sources": list(self.sources),
                "tokens": self.tokens()}


class NoteBook:
    """Holds the note, decides when to revise it, and enforces the cap."""

    def __init__(self, path: Optional[Path] = None, max_tokens: int = NOTE_MAX_TOKENS,
                 update_every: int = UPDATE_EVERY) -> None:
        self.path = path
        self.max_tokens = max_tokens
        self.update_every = max(1, update_every)
        self.note = OperatorNote()
        self._seen = 0
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            self.note = OperatorNote(
                text=str(raw.get("text") or ""),
                revisions=int(raw.get("revisions") or 0),
                updated_at=raw.get("updated_at"),
                sources=list(raw.get("sources") or []),
            )
            self._seen = int(raw.get("seen") or 0)

    # -- lifecycle -----------------------------------------------------------
    def observe(self) -> bool:
        """Record one completed alert. Returns True when this is an update boundary."""
        self._seen += 1
        return self._seen % self.update_every == 0

    def should_revise(self, failed: Sequence[str]) -> bool:
        """Revise only when there is something to learn from.

        A note updated from a run of successes has nothing to add and would still cost tokens on every
        later prompt, which is the "unused artifact" failure the paper measures from the other side.
        """
        return bool(failed)

    def revise(self, new_text: str, sources: Sequence[str] = ()) -> bool:
        """Replace the note. Returns False when the revision exceeds the cap and is rejected.

        Rejected rather than truncated: a note cut mid-sentence is worse than the previous note, and
        silently truncating would hide that the generator is over budget.
        """
        candidate = new_text.strip()
        if _approx_tokens(candidate) > self.max_tokens:
            return False
        self.note = OperatorNote(
            text=candidate,
            revisions=self.note.revisions + 1,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            sources=list(sources)[:12],
        )
        self._persist()
        return True

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({**self.note.to_dict(), "seen": self._seen}, indent=2),
                             encoding="utf-8")

    # -- read ----------------------------------------------------------------
    def render(self) -> str:
        """The block to place in a prompt. Empty when there is no note."""
        if not self.note.text:
            return ""
        return self.note.text.strip()

    def stats(self) -> Dict[str, Any]:
        return {"tokens": self.note.tokens(), "max_tokens": self.max_tokens,
                "revisions": self.note.revisions, "seen": self._seen,
                "update_every": self.update_every, "updated_at": self.note.updated_at}
