"""Dreaming: an offline pass that organises memory against the transcripts.

Why this module exists
----------------------
"The components of agent memory" (Anthropic) describes a four-layer hierarchy and a background process —
*dreaming* — that maintains it:

    transcripts        a searchable event log                     -> this project's Kafka/journal
    memory notes       salient observations the agent writes       -> brian/notes.py (exists)
    representations    an index/wiki linking what is known         -> MISSING before this module
    injected context   a small block loaded into every session     -> only an unstructured note

The article's findings that apply directly here, each of which this module acts on:

1. **Check notes against transcripts.** A note that drifts becomes a wrong belief carried forward. The
   transcript is ground truth: `dream` re-derives what actually happened and drops claims the log no longer
   supports.
2. **Injected context must stay small and act as an index.** The top layer costs tokens in *every* prompt
   and a wrong entry is carried into every prompt. So the injected block here is capped and consists of
   pointers (service, category, count, last seen), not prose.
3. **Memory is a prediction about what the future needs.** Writing down a rule predicts it will recur.
   A claim about an incident pattern that has not recurred in the retained window is a prediction that is
   not being borne out, and is dropped rather than silently trusted.
4. **Fixed rules about what to record can hurt.** The article measured "record only failures" *reducing*
   the benefit of memory by 0.234 for a strong model. `notes.NoteBook.should_revise` currently encodes
   exactly that rule; this module does not fix the policy by fiat, but it does make the *content* correct
   regardless of what triggered the revision.

What this cannot do: judge whether a note is *useful*, or whether a summary is faithful. Both need a model.
It can tell you whether a claim is still supported by the log, and how big the injected layer is.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Hard cap on the injected block. The article's Claude Code example injects at most 200 lines / 25 KB of
#: MEMORY.md as an index; this is the token equivalent for these prompts, chosen small because the block is
#: charged to every single report.
INJECTED_MAX_TOKENS = 400

#: Characters per token, matching `llm.prompts.CHARS_PER_TOKEN` so the two caps are comparable.
CHARS_PER_TOKEN = 3.0


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


@dataclass
class IncidentEntry:
    """One row of the representation: what has happened for a (service, category) pair."""

    service: str
    category: str
    count: int = 0
    last_seen: Optional[str] = None
    actions: List[str] = field(default_factory=list)
    example_cause: str = ""
    #: True once a decision has been counted for this pattern; report-only rows are excluded from
    #: the injected index because they describe a hypothesis, not something that happened.
    seen_decisions: bool = False

    def render(self) -> str:
        return "%s|%s x%d last %s [%s] %s" % (
            self.service, self.category, self.count,
            (self.last_seen or "?")[:16],
            ",".join(self.actions[:3]) or "-",
            self.example_cause[:48],
        )


@dataclass
class IncidentIndex:
    """The representation layer: an aggregated, bounded view of what the transcripts contain."""

    entries: Dict[str, IncidentEntry] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: Iterable[Any]) -> "IncidentIndex":
        """Aggregate decisions and reports into one row per (service, category)."""
        index = cls()
        for record in records:
            value = getattr(record, "value", None) or {}
            topic = getattr(record, "topic", "")
            service = str(value.get("service") or "")
            if not service:
                continue
            if "decisions" in topic:
                category = str(value.get("category") or "unclassified")
                key = "%s|%s" % (service, category)
                entry = index.entries.setdefault(key, IncidentEntry(service, category))
                entry.count += 1
                action = str(value.get("action") or "")
                if action and action not in entry.actions:
                    entry.actions.append(action)
                stamp = (value.get("window_start") or value.get("created_at")
                         or getattr(record, "timestamp", None))
                if stamp is not None and (entry.last_seen is None or str(stamp) > entry.last_seen):
                    entry.last_seen = _render_time(stamp)
                entry.seen_decisions = True
            elif "rca" in topic:
                category = str(value.get("category") or "unclassified")
                key = "%s|%s" % (service, category)
                entry = index.entries.setdefault(key, IncidentEntry(service, category))
                cause = str(value.get("root_cause") or value.get("summary") or "")
                if cause and not entry.example_cause:
                    entry.example_cause = cause
        return index

    def render_injected(self, max_tokens: int = INJECTED_MAX_TOKENS) -> str:
        """The injected block: an index, ordered by recency of activity, cut to fit the cap.

        Ordered by count then last-seen rather than by insertion, so the block stays a *summary of what
        recurs*, not a changelog. When the cap forces a cut, the lines dropped are the least-active ones,
        and the header says how many were dropped so the omission is visible rather than silent.
        """
        # A row with no decisions yet is not part of what has happened; rendering it as `x0` reads as
        # "occurred zero times" rather than "no decisions recorded", which is a different claim.
        ordered = sorted((entry for entry in self.entries.values() if entry.count > 0),
                          key=lambda entry: (entry.count, entry.last_seen or ""), reverse=True)
        header = "# incident index (bounded; %d patterns total)" % len(ordered)
        lines = [header]
        for entry in ordered:
            candidate = lines + [entry.render()]
            if _tokens("\n".join(candidate)) > max_tokens:
                break
            lines.append(entry.render())
        shown = len(lines) - 1
        if shown < len(ordered):
            lines.append("... %d more patterns omitted" % (len(ordered) - shown))
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {"entries": {key: vars(entry) for key, entry in self.entries.items()}}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "IncidentIndex":
        index = cls()
        for key, value in (raw.get("entries") or {}).items():
            index.entries[key] = IncidentEntry(**value)
        return index


@dataclass
class DreamReport:
    """What the pass checked and changed. Returned rather than logged: memory changes must be auditable."""

    claims_checked: int = 0
    claims_dropped: int = 0
    dropped: List[Dict[str, str]] = field(default_factory=list)
    index_entries: int = 0
    injected_tokens: int = 0
    injected: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"claims_checked": self.claims_checked, "claims_dropped": self.claims_dropped,
                "dropped": self.dropped, "index_entries": self.index_entries,
                "injected_tokens": self.injected_tokens}


def _render_time(stamp: Any) -> str:
    """Epoch seconds to ISO, or pass a string through. Keeps the index readable rather than numeric."""
    try:
        if isinstance(stamp, (int, float)):
            return datetime.utcfromtimestamp(float(stamp)).isoformat() + "Z"
    except (OverflowError, OSError, ValueError):
        return str(stamp)
    return str(stamp)


def _words(text: str) -> set:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {word for word in cleaned.split() if len(word) > 2}


def claim_supported(claim: str, index: IncidentIndex) -> Tuple[bool, str]:
    """Is this note line still grounded in the transcripts?

    A claim is supported when at least one of the services or category names it mentions appears in the
    index, or when it is generic guidance with no proper nouns at all (a rule about *how* to reason cannot
    be falsified by the log, so it is kept).

    Deliberately conservative in one direction: the log can only *contradict* a claim about an incident
    that no longer appears. It cannot confirm that a broader rule of thumb is wise — that judgement needs a
    model, and pretending otherwise would be the drift the article warns about, wearing a checkmark.
    """
    if not index.entries:
        return True, "no transcripts to contradict it"
    services = {entry.service.lower() for entry in index.entries.values()}
    categories = {entry.category.lower() for entry in index.entries.values()}
    words = _words(claim)
    if not words:
        return True, "empty claim"
    # Substring, not word-equality. `checkout-api` tokenises to `checkout` + `api`, so an exact-match rule
    # silently deleted a claim the transcripts did support (measured: `checkout-api resource_exhaustion`
    # dropped even though the index held that pattern seven times). A validator that deletes true memory is
    # worse than no validator, because it looks like diligence.
    lowered = claim.lower()
    named = {name for name in (services | categories) if name and name in lowered}
    named |= words & (services | categories)
    if named:
        return True, "mentions %s" % ",".join(sorted(named)[:2])
    # No service or category named: treat a claim with incident-ish nouns as ungrounded, and generic
    # guidance as timeless.
    incident_nouns = {"timeout", "pool", "deploy", "latency", "error", "cpu", "memory", "disk"}
    if words & incident_nouns:
        return False, "references a pattern absent from the transcripts"
    return True, "generic guidance"


def dream(records: Sequence[Any], note_text: str,
          max_tokens: int = INJECTED_MAX_TOKENS) -> Tuple[DreamReport, IncidentIndex, str]:
    """Run the offline pass: rebuild the representation, validate the note, render the injected block.

    Returns (report, index, cleaned_note). The note is *cleaned*, not rewritten: dropping unsupported lines
    is the one correction that can be justified from transcripts alone. Producing better prose needs a model
    and is deliberately out of scope.
    """
    report = DreamReport()
    index = IncidentIndex.from_records(records)
    report.index_entries = len(index.entries)

    kept: List[str] = []
    for line in (note_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        report.claims_checked += 1
        supported, reason = claim_supported(stripped, index)
        if supported:
            kept.append(line)
        else:
            report.claims_dropped += 1
            report.dropped.append({"claim": stripped[:160], "reason": reason})

    report.injected = index.render_injected(max_tokens=max_tokens)
    report.injected_tokens = _tokens(report.injected)
    return report, index, "\n".join(kept)


def persist(directory: Path, report: DreamReport, index: IncidentIndex, note_text: str) -> None:
    """Write the representation and the corrected note next to each other.

    Kept separate from `dream` so the pass can be run without side effects (tests, dry runs) and so the
    files written are explicit rather than a hidden consequence of computing.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "incident_index.json").write_text(
        json.dumps({**index.to_dict(), "generated_at": datetime.utcnow().isoformat() + "Z"}, indent=2),
        encoding="utf-8")
    (directory / "injected.md").write_text(report.injected, encoding="utf-8")
    (directory / "note.md").write_text(note_text, encoding="utf-8")
    (directory / "dream_report.json").write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
