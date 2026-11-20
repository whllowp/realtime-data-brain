"""Promotion: what a version file *claims*, and what happens when the claim stops holding.

`src/brian/evolve.py` decides and records; this module is about the two steps after that — putting an
accepted configuration into force, and re-checking the claim on data that did not exist when it was made.

Why this is separate from the loop
----------------------------------
The ladder in arXiv:2607.07663 grades integration separately from selection, and so does this repo: an
accept is a statement about one measurement, a version file is a statement that the change *can* be
described, diffed and reversed, and a promotion is a statement that it is *in force*. Only the third one
can fail later, because only the third one is exposed to the future.

The claim
---------
A version file carries the measurement that justified it. That number is a promise: "at a 10% false
positive budget this configuration finds at least X of the failures, spends at most Y tokens per report,
and alerts on at most Z of windows at its shipped threshold". `verify()` re-measures and asks whether the
promise still pays out. It does not ask whether the change was a good idea — that was decided earlier,
under a different objective, and `brian evolve --audit` is the command that revisits *that* question.

Deployment state
----------------
This repo has no live service handle, so "in force" is expressed as the thing a deployment actually
consumes: a set of environment assignments in `var/evolve/current.json`, with per-variable provenance so a
revert removes exactly what one version added. A process that reads those variables at start-up picks the
change up on its next start; nothing here hot-reloads a running process, and the doc says so rather than
letting "apply" imply more than it does.

The one thing a revert cannot undo
----------------------------------
If two versions override the same variable and the *later* one is reverted, the earlier value is restored
from the recorded provenance. If the earlier version was itself reverted first, there is nothing to
restore and the variable is removed. Neither case invents a value that was never measured.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import ENV_VARS, Settings
from .deploy import overrides_from_state
from .evolve import (EVIDENCE_MISSING, EVIDENCE_PRESENT, EVIDENCE_UNNAMED, LINK_DEFECTS, Candidate,
                     EvolveRules, Measurement, active_settlements, backfilled_links, is_withdrawn,
                     history, load_settlements,
                     replacement_links)

GEOMETRY_TOLERANCE = 0.25

# Round 84: the two shapes of a record that cannot carry a new promise. They are not the same defect: a
# broken link is two books contradicting each other, and no command can settle which one is right; a
# backfilled end is a link this build copied, so the record is usable but its provenance is partly invented.
LINK_BROKEN = "broken link"
LINK_BACKFILLED = "backfilled end"


def provenance_block(root: Path, version: int) -> Optional[Tuple[str, str]]:
    """Why this version's record cannot carry a promise today, or None when it can (round 84).

    Round 81 made the two ends of a replacement comparable and round 83 could write the missing one. Both
    stopped short of the question a promotion asks: *is the record that produced this version still a record
    anybody can believe?* A version is produced by a round (`applied` names it), and that round can be one
    of the participants in a link finding -- the books contradict themselves about which round replaced
    which. Stacking a promise on top of a contradictory record entrenches numbers whose provenance the
    ledger itself disputes, and the next `--audit` cannot settle it either (boundary 170/173).

    A backfilled end is the second, weaker case: the end was copied by round 83's repair, so it agrees by
    construction. It is clearable -- re-running the round writes a successor that names its predecessor
    itself -- which is why it is reported and charged (exit 1) rather than treated as a build defect (2).
    """
    label = "v%03d" % int(version)
    rounds = history(root, include_retired=True)
    produced: List[str] = []
    for record in rounds:
        applied = record.get("applied") or []
        if not isinstance(applied, list):
            # Round 71 met this shape: the ledger stores `applied` as a count while the round file stores
            # names. A count cannot say which version a round produced, so it cannot block anything.
            continue
        if label in {str(item) for item in applied}:
            produced.append(str(record.get("round") or ""))
    if not produced:
        return None
    for item in replacement_links(rounds, active_settlements(load_settlements(root))):
        if item.get("kind") not in LINK_DEFECTS:
            continue
        participants = {str(item.get("round") or "")}
        participants |= {str(name) for name in (item.get("names") or [])}
        for name in produced:
            if name in participants:
                return (LINK_BROKEN, "%s is one of the records in a %s link: %s"
                        % (name, item["kind"], item["detail"]))
    backfilled = set(backfilled_links(rounds))
    for name in produced:
        if name in backfilled:
            return (LINK_BACKFILLED, "%s has no successor-side record of its own: the end was copied from the "
                                     "retirement, not written by the queue" % name)
    return None


def _deployment_states(root: Path) -> List[Tuple[str, Path]]:
    """Every deployment state on disk, as (domain slug, path) (round 85).

    The legacy global `current.json` is the empty slug: round 22 stopped reading it, but a tree that has not
    been migrated still runs from it, and a version in force there is in force somewhere.
    """
    directory = _evolve_dir(root)
    states: List[Tuple[str, Path]] = []
    legacy = directory / CURRENT
    if legacy.is_file():
        states.append(("", legacy))
    for path in sorted((directory / DOMAINS).glob("*/" + CURRENT)):
        states.append((path.parent.name, path))
    return states


def in_force_promotions(root: Path) -> List[Tuple[str, int]]:
    """Which versions the deployment states say are running, as sorted (domain, version) pairs (round 85).

    A state maps environment variable -> owning version in `sources`; that mapping is the only record of what a
    process would take at start-up. A variable owned by two versions is one variable, so the pairs are deduped:
    the question here is "which versions are deployed", not "how many variables each owns".
    """
    pairs: List[Tuple[str, int]] = []
    for slug, path in _deployment_states(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = payload.get("sources") if isinstance(payload, Mapping) else None
        for version in sorted({str(item) for item in (sources or {}).values()}):
            try:
                number = int(version)
            except (TypeError, ValueError):
                # A source that is not a version number is not this function's business: it is reported by
                # `in_force_overrides`, which knows what a refused setting is.
                continue
            pair = (slug, number)
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def in_force_disputes(root: Path) -> List[Dict[str, Any]]:
    """Versions in force whose producing record the books dispute or that a repair copied (round 85).

    Round 84's guard is a refusal at stack time. Once a version is in force the refusal is not available -- it
    is already running -- so the fact moves to the audit and the campaign, where a reader asks what the loop
    claims, and where the same question can be asked again every time the books move.
    """
    rows: List[Dict[str, Any]] = []
    for slug, version in in_force_promotions(root):
        block = provenance_block(root, version)
        if block is None:
            continue
        # `kind` is the label the reader sees; `broken` is the decision, made here because this is the only
        # module that knows both labels. Comparing a label against LINK_DEFECTS is always false.
        rows.append({"domain": slug or "default", "version": version, "broken": block[0] == LINK_BROKEN,
                     "kind": block[0], "detail": block[1]})
    return rows


#: How long a deployed version may sit on a copied record before `--strict` asks for a re-measurement (round 86).
#: The same number as the claim expiry, for the same reason: the check costs a queue run, and nothing else in
#: this loop is trusted to evidence older than two weeks.
IN_FORCE_GRACE_DAYS = 14.0

#: Where the first sighting of each dispute is kept. Outside the ledger on purpose: the ledger records rounds,
#: and this records *when the loop noticed*, which is a fact about this build's runs, not about a measurement.
IN_FORCE_STORE = "in_force.json"


def _in_force_path(root: Path) -> Path:
    return _evolve_dir(root) / IN_FORCE_STORE


def _parse_moment(stamp: Any) -> Optional[datetime]:
    text = str(stamp or "")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp_moment(now: Optional[datetime] = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


#: The store's format. v2 keeps a history of cleared entries beside the live rows; a v1 file (round 86) has no
#: history at all, which reads as "nothing has cleared yet" rather than as a defect.
IN_FORCE_FORMAT = "brian.inforce/2"

#: A cleared entry cites how it stopped being true. A re-measurement is the one action that answers a copied
#: end; anything else is the ledger no longer reproducing the objection, which is an absence of observation
#: rather than a verdict, so the two are counted apart.
CLEARED_BY_MEASUREMENT = "verification"
CLEARED_BY_LEDGER = "unexplained"

#: How many cleared entries to keep. A history that grows without a bound is a state file nobody can read, and
#: the count of what was dropped travels with it so a trimmed history cannot look complete.
IN_FORCE_HISTORY_LIMIT = 200
# Round 93: how many substitutions of one version's named file turn a right choice into a stale record.
STALE_SUBSTITUTION_LIMIT = 2

#: Who wrote the citation on a cleared entry. `measurement` means the check that cleared the objection recorded it
#: at the moment it happened (round 88); `audit` means a later run noticed the row was gone and compared
#: timestamps to guess why. The second depends on the ledger still holding both facts, so a reader is told which
#: one they have.
CLEARED_CITED_BY_MEASUREMENT = "measurement"
CLEARED_CITED_BY_AUDIT = "audit"


def _evidence_state(root: Path, stream_path: str) -> str:
    """Whether the file this citation is about to name can be opened (round 91).

    A clearing is a claim about a measurement, and the measurement is a file. If the file is gone, the claim
    cannot be audited -- and a claim nobody can audit is not how a debt gets paid, it is how one disappears. So a
    missing file makes the entry an attempt rather than an answer: the observation keeps its clock.
    """
    path = str(stream_path or "")
    if not path:
        return EVIDENCE_UNNAMED
    return EVIDENCE_PRESENT if (root / path).is_file() else EVIDENCE_MISSING


def clearing_evidence(entries: Sequence[Mapping[str, Any]], root: Path,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-time check: which named files can no longer be opened today.

    Round 90 made every citation name its file. A file can be deleted afterwards, and nothing writes that down --
    the fact is about the checkout a reader has, not about the moment the clearing happened. So this is computed
    on demand and never stored (boundary 194).
    """
    present = missing = unnamed = 0
    missing_files: List[str] = []
    oldest: Optional[Dict[str, Any]] = None
    oldest_moment: Optional[datetime] = None
    moment = _stamp_moment(now if isinstance(now, datetime) else now)
    for item in entries:
        path = str(item.get("stream_path") or "")
        if not path:
            unnamed += 1
            continue
        if (root / path).is_file():
            present += 1
            continue
        missing += 1
        if path not in missing_files:
            missing_files.append(path)
        # Round 92: which dead link is the oldest is the question a reader actually has. A count says the
        # books are incomplete; the oldest one says where the incompleteness started.
        checked = _parse_moment(item.get("checked_at"))
        if checked is None or (oldest_moment is not None and checked >= oldest_moment):
            continue
        oldest_moment = checked
        oldest = {"checked_at": checked.isoformat(), "version": int(item.get("version") or 0),
                  "stream_path": path,
                  "age_days": round((moment - checked).total_seconds() / 86400.0, 2)}
    return {"present": present, "missing": missing, "unnamed": unnamed,
            "missing_files": sorted(missing_files), "oldest": oldest}




def _read_in_force_store(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    path = _in_force_path(root)
    if not path.is_file():
        return [], [], 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, Mapping):
        return [], [], 0
    observed = [dict(item) for item in payload.get("observed") or [] if isinstance(item, Mapping)]
    cleared = [dict(item) for item in payload.get("cleared") or [] if isinstance(item, Mapping)]
    try:
        trimmed = int(payload.get("trimmed") or 0)
    except (TypeError, ValueError):
        trimmed = 0
    return observed, cleared, trimmed


def _checked_at(promotions: Sequence["Promotion"]) -> Dict[int, Optional[datetime]]:
    """When each deployed version was last measured, so a clearing can say whether a measurement covers it."""
    moments: Dict[int, Optional[datetime]] = {}
    for promotion in promotions:
        moment = _parse_moment((promotion.verification or {}).get("checked_at"))
        if moment is None:
            continue
        previous = moments.get(int(promotion.version))
        if previous is None or moment > previous:
            moments[int(promotion.version)] = moment
    return moments


def _tested_at(promotions: Sequence["Promotion"]) -> Dict[int, Optional[datetime]]:
    """When each deployed version was last measured *in a way that could test the claim* (round 89).

    `unmeasurable` is a statement about this build's ability to compare, not about the deployed version, so
    it must not be able to answer an objection -- nor to displace the record of an earlier failed attempt.
    """
    moments: Dict[int, Optional[datetime]] = {}
    for promotion in promotions:
        verification = promotion.verification or {}
        if str(verification.get("verdict") or "") == UNMEASURABLE:
            continue
        moment = _parse_moment(verification.get("checked_at"))
        if moment is None:
            continue
        previous = moments.get(int(promotion.version))
        if previous is None or moment > previous:
            moments[int(promotion.version)] = moment
    return moments


def observe_in_force_disputes(root: Path, rows: Sequence[Mapping[str, Any]],
                              now: Optional[datetime] = None,
                              promotions: Sequence["Promotion"] = (),
                              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Give a recomputed fact a history: when each dispute was first seen, and what cleared it (round 87).

    Returns `(aged, cleared)`: the rows enriched with `first_seen`, `last_seen`, `observations` and `age_days`;
    and the entries this run no longer sees, each carrying `cleared_at`, `age_days` and `by` --
    `"verification"` when a measurement of that version is newer than the objection, `"unexplained"` when the
    ledger merely stopped reproducing it. A clean tree writes nothing beyond the history it must keep.
    """
    moment = _stamp_moment(now)
    observed, cleared_history, trimmed = _read_in_force_store(root)
    seen: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for item in observed:
        key = (str(item.get("domain") or "default"), int(item.get("version") or 0),
               str(item.get("kind") or ""))
        seen[key] = item
    checks = _checked_at(promotions)
    tested_at = _tested_at(promotions)
    aged: List[Dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("domain") or "default"), int(row.get("version") or 0), str(row.get("kind") or ""))
        previous = seen.pop(key, None)
        first = str((previous or {}).get("first_seen") or "") or moment.isoformat()
        observations = int((previous or {}).get("observations") or 0) + 1
        # A stored stamp that cannot be read is replaced by "now" rather than treated as old: guessing that a
        # dispute is ancient is how a warning turns into a charge nobody can explain.
        first_moment = _parse_moment(first) or moment
        aged_row = {
            "domain": key[0], "version": key[1], "broken": bool(row.get("broken")),
            "kind": key[2], "detail": row.get("detail"),
            "first_seen": first, "last_seen": moment.isoformat(), "observations": observations,
            "age_days": round((moment - first_moment).total_seconds() / 86400.0, 2),
        }
        # Round 89: the fact that someone tried and could not compare belongs to the observation, not to the
        # run that happened to notice it. It is dropped once a real measurement supersedes it.
        attempt = dict((previous or {}).get("untested") or {})
        if attempt:
            attempt_moment = _parse_moment(attempt.get("at"))
            answered = tested_at.get(key[1])
            if attempt_moment is not None and (answered is None or answered < attempt_moment):
                aged_row["untested"] = attempt
        aged.append(aged_row)
    cleared: List[Dict[str, Any]] = []
    for key, item in seen.items():
        first = str(item.get("first_seen") or "") or moment.isoformat()
        first_moment = _parse_moment(first)
        checked = checks.get(key[1])
        # Round 89: only a verdict that could test the claim pays the debt; a failed comparison, however
        # recent, leaves it exactly where it was.
        answered = tested_at.get(key[1])
        measured = answered is not None and (first_moment is None or answered >= first_moment)
        entry = {
            "domain": key[0], "version": key[1], "kind": key[2],
            "first_seen": first, "last_seen": str(item.get("last_seen") or ""),
            "observations": int(item.get("observations") or 0),
            "cleared_at": moment.isoformat(),
            "age_days": (None if first_moment is None
                         else round((moment - first_moment).total_seconds() / 86400.0, 2)),
            "by": CLEARED_BY_MEASUREMENT if measured else CLEARED_BY_LEDGER,
            "checked_at": None if checked is None else checked.isoformat(),
            "cited_by": CLEARED_CITED_BY_AUDIT,
        }
        cleared.append(entry)
        cleared_history.append(entry)
    if len(cleared_history) > IN_FORCE_HISTORY_LIMIT:
        dropped = len(cleared_history) - IN_FORCE_HISTORY_LIMIT
        cleared_history = cleared_history[dropped:]
        trimmed += dropped
    if aged or cleared or trimmed:
        path = _in_force_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": IN_FORCE_FORMAT, "observed": aged,
                                    "cleared": cleared_history, "trimmed": trimmed},
                                   indent=2) + "\n", encoding="utf-8", newline="\n")
    return aged, cleared


def in_force_history(root: Path) -> Tuple[List[Dict[str, Any]], int]:
    """The cleared entries the store remembers, and how many older ones were trimmed."""
    _, cleared, trimmed = _read_in_force_store(root)
    return cleared, trimmed


def record_clearing(root: Path, version: int, now: Optional[datetime] = None, verdict: str = "",
                    reason: str = "", tested: bool = True,
                    attempt_reason: str = "", stream_path: str = "",
                    substituted_from: str = "", first_original: bool = False,
                    queued: bool = False
                    ) -> List[Dict[str, Any]]:
    """Write the clearing a measurement just earned, instead of waiting for a later audit to notice absence.

    Round 87 deduced every clearing from a comparison of timestamps, which means the fact exists only while the
    ledger still holds both ends of it: rewrite the record, retire the round, or re-run the audit in a checkout
    that never saw the objection, and the clearing is unobservable. A check that answers a copied end knows the
    answer at the moment it runs, so it says so here (round 88).

    Only a copied end is cleared. A contradiction is not answered by measuring the version -- the two books still
    disagree about which end is the evidence -- and it stays observed until someone fixes the records.

    Round 89: a check that *could not test the claim* (`unmeasurable`) is recorded but answers nothing. It is a
    statement about this build's ability to compare, not about the deployed version, so the observation keeps
    its first sighting and keeps running -- otherwise a failed comparison would reset the clock and make an
    unresolved debt look newly arrived (boundary 188). The attempt is stored on the row, so the page can say
    what was tried and why it did not count.

    Round 90: the citation must name its evidence. An entry now carries the stream file the check read
    (`stream_path`) and the ordinal of that attempt in this version's chain. Without the file, "cleared by
    a re-measurement on 2026-11-19" is a claim a reader can neither open nor refute; without the ordinal, a
    chain that was cleared, re-disputed and cleared again reads exactly like one cleared once.

    Round 91: a citation is only as good as the file behind it. If `stream_path` names something this checkout
    cannot open, the entry is recorded as an attempt (`evidence: "missing"`, `tested: false`) and the row keeps
    running -- the check ran, but nobody can audit what it read (boundary 195).

    Round 92: a check can be pointed at a file this checkout does not have while the record names one that is
    here, and running there is the only way the debt ever gets answered. That substitution is part of the fact,
    so the entry names what was asked for (`substituted_from`) beside what was read (`stream_path`): a verdict
    is about a file, and a reader who cannot see the file changed will read one stream's answer as another's
    (boundary 197).

    Round 102: a clearing also says how the check that earned it was started. `--verify --from-queue N` walks the
    queue and measures each record on the file it was born with; a run typed by hand copies a label and a path off a
    page, and that copy is exactly what the queue exists to remove. The verdict is the same either way; what differs
    is whether anyone had to retype an identifier, which is the thing that lands checks on substitutes (boundary 221).
    """
    moment = _stamp_moment(now if isinstance(now, datetime) else _parse_moment(now))
    observed, cleared_history, trimmed = _read_in_force_store(root)
    keep: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []
    for item in observed:
        same = int(item.get("version") or 0) == int(version)
        if not same or item.get("broken"):
            keep.append(item)
            continue
        first = str(item.get("first_seen") or "")
        first_moment = _parse_moment(first)
        evidence = _evidence_state(root, stream_path)
        entry = {
            "domain": str(item.get("domain") or "default"), "version": int(version),
            "kind": str(item.get("kind") or ""), "first_seen": first,
            "last_seen": str(item.get("last_seen") or ""),
            "observations": int(item.get("observations") or 0),
            "cleared_at": moment.isoformat(),
            "age_days": (None if first_moment is None
                         else round((moment - first_moment).total_seconds() / 86400.0, 2)),
            "by": CLEARED_BY_MEASUREMENT,
            "checked_at": moment.isoformat(),
            "cited_by": CLEARED_CITED_BY_MEASUREMENT,
            "verdict": str(verdict or ""),
            "evidence": evidence,
            "tested": bool(tested) and evidence != EVIDENCE_MISSING,
            "stream_path": str(stream_path or ""),
            "substituted_from": str(substituted_from or ""),
            # Round 100: a clearing can be the first verdict ever taken where the promise was made, and the
            # history page has to be able to say so -- the fact otherwise lives only in the version file.
            "first_original": bool(first_original),
            # Round 102: how the check was started. `True` when `--from-queue` picked this record, `False` when a
            # caller typed the version and the path, and absent on entries written before this round.
            "queued": bool(queued),
            # Round 90: the ordinal counts what this version's chain already holds, so an attempt is a
            # position in a history rather than a loose fact.
            "attempt": 1 + sum(1 for prior in cleared_history
                               if int(prior.get("version") or 0) == int(version)),
        }
        if reason:
            entry["reason"] = str(reason)
        entries.append(entry)
        cleared_history.append(entry)
        if not tested or evidence == EVIDENCE_MISSING:
            # Recorded, not answered: the row stays observed with its clock running (rounds 89 and 91).
            attempted = dict(item)
            attempted["untested"] = {"at": moment.isoformat(), "verdict": str(verdict or ""),
                                     "reason": str(attempt_reason or reason or ""),
                                     "stream_path": str(stream_path or ""),
                                     "evidence": evidence}
            keep.append(attempted)
    if not entries:
        return []
    if len(cleared_history) > IN_FORCE_HISTORY_LIMIT:
        dropped = len(cleared_history) - IN_FORCE_HISTORY_LIMIT
        cleared_history = cleared_history[dropped:]
        trimmed += dropped
    path = _in_force_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": IN_FORCE_FORMAT, "observed": keep, "cleared": cleared_history,
                                "trimmed": trimmed}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return entries


def cleared_summary(entries: Sequence[Mapping[str, Any]], trimmed: int = 0) -> Dict[str, Any]:
    """The two counts apart, with the median age, because they answer different questions.

    `by_domain` splits them again by the stream they were charged to: a total over two streams cannot say which
    one stopped being measured (round 80's reason for splitting the paid families).
    """
    measured = [item for item in entries if item.get("by") == CLEARED_BY_MEASUREMENT]
    unexplained = [item for item in entries if item.get("by") != CLEARED_BY_MEASUREMENT]
    ages = sorted(float(item.get("age_days") or 0.0) for item in entries if item.get("age_days") is not None)
    middle = 0.0
    if ages:
        half = len(ages) // 2
        middle = ages[half] if len(ages) % 2 else (ages[half - 1] + ages[half]) / 2.0
    by_domain: Dict[str, Dict[str, int]] = {}
    for item in entries:
        row = by_domain.setdefault(str(item.get("domain") or "default"),
                                   {"measured": 0, "unexplained": 0})
        row["measured" if item.get("by") == CLEARED_BY_MEASUREMENT else "unexplained"] += 1
    written = sum(1 for item in entries if item.get("cited_by") == CLEARED_CITED_BY_MEASUREMENT)
    # Round 89: a clearing whose check could not test the claim is its own count. Folding it into `measured`
    # would let a failed comparison read as an answered objection.
    untested = sum(1 for item in entries if item.get("tested") is False)
    # Round 90: which file did the answering. A count per stream is the same reason round 80 split the
    # paid families: a total over two streams cannot say which one stopped being measured.
    by_file: Dict[str, int] = {}
    last_by_file: Dict[str, str] = {}
    for item in measured:
        key = str(item.get("stream_path") or "(unnamed)")
        by_file[key] = by_file.get(key, 0) + 1
        # Round 91: a count says how often a file answered; the date says whether it is still the evidence
        # anyone uses. Two files with one clearing each read the same unless the age is printed next to them.
        stamp = str(item.get("checked_at") or "")
        if stamp > last_by_file.get(key, ""):
            last_by_file[key] = stamp
    # Round 93: a substitution is a fact about what the checks were asked to read, so it is counted by the path
    # that was named. It is not a defect: the caller names a file, the record knows the file, and the check runs on
    # the one that exists. What *is* a defect -- a record naming a file nobody can open -- is `dead_record_paths`.
    substitutions: Dict[str, int] = {}
    for item in measured:
        named = str(item.get("substituted_from") or "")
        if named:
            substitutions[named] = substitutions.get(named, 0) + 1
    # Round 102: did the queue start this check, or did a hand-copied command? An entry written before this round
    # carries no field at all, and silence is its own state -- folding it into either side lets the default pick the
    # answer (round 79's rule).
    queued = sum(1 for item in entries if item.get("queued") is True)
    unstated = sum(1 for item in entries if "queued" not in item)
    return {"total": len(entries), "measured": len(measured), "unexplained": len(unexplained),
            "queued": queued, "unstated_queue": unstated,
            "median_age_days": round(middle, 2), "trimmed": int(trimmed),
            "written": written, "untested": untested, "by_domain": dict(sorted(by_domain.items())),
            "by_file": dict(sorted(by_file.items())), "last_by_file": dict(sorted(last_by_file.items())),
            "substitutions": dict(sorted(substitutions.items()))}


def stale_in_force_disputes(rows: Sequence[Mapping[str, Any]], promotions: Sequence["Promotion"],
                            grace_days: float = IN_FORCE_GRACE_DAYS) -> List[Dict[str, Any]]:
    """Copied ends that have stood past the grace period with no re-measurement newer than the objection.

    A contradiction is not here: it is charged the moment it exists (round 85), because no measurement settles
    which book is right. A copied end has exactly one remedy -- measure the deployed version again -- so this is
    the list the escalation names, and a version verified after its first sighting is not stale at all: the
    measurement is newer than the objection, and the objection is about the record, not about the number.
    """
    checks = {int(promotion.version): promotion.verification for promotion in promotions}
    stale: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("broken"):
            continue
        if float(row.get("age_days") or 0.0) < float(grace_days):
            continue
        first = _parse_moment(row.get("first_seen"))
        verification = checks.get(int(row.get("version") or 0))
        checked = _parse_moment((verification or {}).get("checked_at"))
        # Round 89: only a verdict that tested the claim answers the objection. An `unmeasurable` check ran and
        # compared nothing, so the debt is exactly as old as it was -- and re-running the same command on the
        # same stream will compare nothing again.
        tested = str((verification or {}).get("verdict") or "") != UNMEASURABLE
        if checked and tested and (first is None or checked >= first):
            continue
        stale.append(dict(row))
    return stale

# How long a recorded verdict speaks for the claim. A check is evidence about the day it ran: a `holds`
# from a year ago and an `unmeasurable` from last March are both statements nobody has tested since, and
# `--verify` selected neither of them again. 14 days is deliberately short -- the measurement costs a few
# seconds, and the campaign is supposed to be read as a picture of *now*, not of whenever someone looked.
CLAIM_EXPIRY_DAYS = 14.0

CURRENT = "current.json"
DOMAINS = "domains"
BUDGET = "budget"
VERSIONS = "versions"
VERIFIED = "holds"
FAILED = "failed"
UNMEASURABLE = "unmeasurable"


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _evolve_dir(root: Path) -> Path:
    return root / "var" / "evolve"


def domain_slug(source: str) -> str:
    """A directory-safe name for a stream source (``RCAEval RE1-OB`` -> ``rcaeval-re1-ob``).

    The deployment state has to be per stream: round 21 adopted the global file on both domains and the
    RCAEval bar (0.87) arrived on SMD, where the engine's answers sit in 0.33 — one domain's calibration
    governing another is not a baseline, it is a bug with provenance.
    """
    text = "".join(character if character.isalnum() else "-" for character in (source or "")).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.lower() or "default"


def state_path(root: Path, domain: Optional[str] = None) -> Path:
    """Where the in-force configuration for `domain` lives (``None`` = the pre-round-21 global file)."""
    if not domain:
        return _evolve_dir(root) / CURRENT
    return _evolve_dir(root) / DOMAINS / domain_slug(domain) / CURRENT


@dataclass
class Claim:
    """The numbers a version promised, and the promise it did not make.

    `fpr_at_budget` and `recall_at_budget` are the pair the loop actually judged; `shipped_fpr` is what a
    deployment running the file's own configuration would alert on without any calibration. They are kept
    apart here for the same reason they are kept apart in `format_round`: the first pair is a ranking
    statement at a chosen cut, the second is what an operator would see.
    """

    recall_at_budget: Optional[float] = None
    fpr_at_budget: Optional[float] = None
    tokens_per_report: Optional[float] = None
    shipped_recall: Optional[float] = None
    shipped_fpr: Optional[float] = None

    @classmethod
    def from_measurement(cls, measurement: Measurement) -> "Claim":
        return cls(
            recall_at_budget=measurement.recall_at_budget,
            fpr_at_budget=measurement.fpr_at_budget,
            tokens_per_report=measurement.tokens_per_report,
            shipped_recall=measurement.recall,
            shipped_fpr=measurement.fpr,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recall_at_budget": self.recall_at_budget,
            "fpr_at_budget": self.fpr_at_budget,
            "tokens_per_report": self.tokens_per_report,
            "shipped_recall": self.shipped_recall,
            "shipped_fpr": self.shipped_fpr,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Claim":
        return cls(
            recall_at_budget=payload.get("recall_at_budget"),
            fpr_at_budget=payload.get("fpr_at_budget"),
            tokens_per_report=payload.get("tokens_per_report"),
            shipped_recall=payload.get("shipped_recall"),
            shipped_fpr=payload.get("shipped_fpr"),
        )


def verification_age_days(verification: Optional[Mapping[str, Any]],
                          now: Optional[datetime] = None) -> Optional[float]:
    """How many days ago a verdict was recorded, or None when it carries no readable stamp.

    Timestamps are written by `_stamp()` as ISO-8601 with an offset, so they are timezone-aware; a record
    without one is treated as UTC rather than as "today", because guessing a date is how a stale check
    starts looking fresh.
    """
    if not verification:
        return None
    stamp = str(verification.get("checked_at") or "")
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - then).total_seconds() / 86400.0


@dataclass
class Promotion:
    """One version file, as the thing in force rather than the record of a measurement."""

    version: int
    path: Path
    candidate: Optional[Candidate]
    change: str
    env: Dict[str, str]
    claim: Claim
    stream: Dict[str, Any] = field(default_factory=dict)
    # Round 47: the summary names the dataset, and `SMD` is two files here sampled differently. A promise
    # that does not say which file it was measured on can only be re-checked by whoever remembers, and a
    # check against the wrong sampling reports `unmeasurable` about the operator's choice, not the claim.
    stream_path: str = ""
    created_at: str = ""
    verification: Optional[Dict[str, Any]] = None
    reverted_at: str = ""
    # Earlier checks, oldest first. A version whose claim was retracted and later re-established keeps
    # both records: the retraction is evidence about how the claim was tested, not a clerical error.
    checks: List[Dict[str, Any]] = field(default_factory=list)
    # Round 95: round 93's `--set-stream-path` can move the file a record was measured on, and a check taken
    # after that move describes the location the repair picked, not the one the promise originally named. The
    # chain is carried here so the version table and the verification can both say so (boundary 203).
    stream_path_history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def path_moves(self) -> int:
        """How many times a hand repair moved the file this promise is measured on."""
        return len(self.stream_path_history or [])

    @property
    def path_origin(self) -> str:
        """The file the record originally named, before any repair moved it (empty when never moved)."""
        return str((self.stream_path_history[0] or {}).get("from") or "") if self.stream_path_history else ""

    @property
    def path_current(self) -> str:
        """The file the record names now (empty when never moved)."""
        return str((self.stream_path_history[-1] or {}).get("to") or "") if self.stream_path_history else ""

    @property
    def pending(self) -> bool:
        return self.verification is None and not self.reverted_at

    @property
    def verdict(self) -> Optional[str]:
        """The last verdict recorded for this version, or None when it was never checked."""
        return str((self.verification or {}).get("verdict") or "") or None

    def overdue(self, now: Optional[datetime] = None,
                days: float = CLAIM_EXPIRY_DAYS) -> bool:
        """True when the last verdict is older than `days`: still the record, no longer the evidence."""
        age = verification_age_days(self.verification, now)
        return age is not None and age > days

    def due(self, now: Optional[datetime] = None, days: float = CLAIM_EXPIRY_DAYS) -> bool:
        """Whether `--verify` should pick this version: never checked, or checked too long ago."""
        return not self.reverted_at and (self.pending or self.overdue(now, days))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "change": self.change,
            "env": dict(self.env),
            "claim": self.claim.to_dict(),
            "stream": dict(self.stream),
            "stream_path": self.stream_path or None,
            "created_at": self.created_at,
            "verification": self.verification,
            "reverted_at": self.reverted_at,
            "checks": list(self.checks),
            "stream_path_history": [dict(item) for item in self.stream_path_history],
        }


def _env_from_payload(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Parse the `"A=1; B=2"` string the version file stores into assignments.

    The file stores the shell line because that is what an operator pastes; this module needs the pairs,
    so the parse lives here rather than in whoever reads the file next.
    """
    env: Dict[str, str] = {}
    for item in str(payload.get("env") or "").split(";"):
        entry = item.strip()
        if not entry or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        env[name.strip()] = value.strip()
    return env


def load_promotions(root: Path) -> List[Promotion]:
    """Every readable version file, oldest first. Unreadable ones are skipped, not guessed at."""
    directory = _evolve_dir(root) / VERSIONS
    promotions: List[Promotion] = []
    for path in sorted(directory.glob("v*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            version = int(payload.get("version") or path.stem.lstrip("v"))
        except (TypeError, ValueError):
            continue
        candidate_payload = payload.get("candidate")
        candidate: Optional[Candidate] = None
        if isinstance(candidate_payload, Mapping) and candidate_payload.get("changes"):
            candidate = Candidate.from_dict(candidate_payload)
        promotions.append(Promotion(
            version=version,
            path=path,
            candidate=candidate,
            change=str(payload.get("change") or ""),
            env=_env_from_payload(payload),
            claim=Claim.from_dict(payload.get("measurement") or {}),
            stream=dict(payload.get("stream") or {}),
            stream_path=str(payload.get("stream_path") or ""),
            created_at=str(payload.get("created_at") or ""),
            verification=payload.get("verification"),
            reverted_at=str(payload.get("reverted_at") or ""),
            checks=list(payload.get("checks") or []),
            stream_path_history=[dict(item) for item in (payload.get("stream_path_history") or [])
                                 if isinstance(item, Mapping)],
        ))
    promotions.sort(key=lambda item: item.version)
    return promotions


def write_verification(root: Path, promotion: Promotion, payload: Mapping[str, Any],
                       reverted_at: str = "") -> None:
    """Patch one version file with its verification result, keeping the rest of it byte-identical.

    Rewriting through this function rather than through `json.dump` of a reconstructed object matters: the
    version file is the rollback record, and a verification must not be able to drop the `rollback` line
    that makes the file useful.
    """
    document = json.loads(promotion.path.read_text(encoding="utf-8"))
    checks = list(document.get("checks") or [])
    previous = document.get("verification") or {}
    if payload.get("shape") is None and previous.get("shape"):
        # Only `--at-hint` takes the second measurement, so a plain re-check would otherwise drop a number
        # an earlier check really measured. Carry it, and say which check measured it (round 29).
        payload = dict(payload)
        payload["shape"] = dict(previous["shape"])
        payload["shape_from"] = previous.get("shape_from") or previous.get("checked_at") or ""
    # An identical re-run adds no information, and a re-check repeated while debugging a refusal would
    # otherwise outnumber the checks that actually moved the verdict (round 28 saw "4 earlier checks" for
    # three copies of one refusal). A run that differs in verdict, reason or recorded shape is kept.
    if previous and (previous.get("verdict") != payload.get("verdict")
                     or previous.get("reason") != payload.get("reason")
                     or previous.get("shape") != payload.get("shape")):
        checks.append(previous)
    if checks:
        document["checks"] = checks
    document["verification"] = dict(payload)
    if reverted_at:
        document["reverted_at"] = reverted_at
    else:
        # A check that did not fail says the configuration may stay in force; leaving an earlier
        # retraction in place would print a version the deployment is running as retracted.
        document.pop("reverted_at", None)
    promotion.path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    promotion.verification = dict(payload)
    promotion.reverted_at = reverted_at or None


def current_state(root: Path, domain: Optional[str] = None) -> Dict[str, Any]:
    """The configuration in force. `domain` selects a stream's own file; `None` reads the legacy global one."""
    path = state_path(root, domain)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"env": {}, "sources": {}, "updated_at": ""}
    if not isinstance(payload, Mapping):
        return {"env": {}, "sources": {}, "updated_at": ""}
    return {
        "env": dict(payload.get("env") or {}),
        "sources": dict(payload.get("sources") or {}),
        "updated_at": str(payload.get("updated_at") or ""),
        "budget": dict(payload.get(BUDGET) or {}),
    }


def _write_state(root: Path, state: Mapping[str, Any], domain: Optional[str] = None) -> Path:
    path = state_path(root, domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"env": dict(state.get("env") or {}),
               "sources": dict(state.get("sources") or {}),
               "updated_at": _now()}
    # Round 64: the domain's wait ceiling lives in the same file as its configuration, and every writer here
    # (apply, revert, and the ceiling itself) goes through this function. A promotion must not delete a policy
    # that happens to share the file, so the block is preserved when the caller did not pass one.
    #
    # Round 66: "did not pass one" is not the same as "passed an empty one". Clearing the last ceiling must
    # remove the block, so the disk copy is only consulted when the caller left the key out entirely -- an
    # explicit `{}` means the policy was withdrawn, and reading the file back would resurrect it.
    budget = dict(state.get("budget") or {})
    if not budget and "budget" not in state:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, Mapping):
            budget = dict(existing.get(BUDGET) or {})
    if budget:
        payload[BUDGET] = budget
    if domain:
        payload["domain"] = domain_slug(domain)
        payload["source"] = domain
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def state_budgets(root: Path) -> Dict[str, float]:
    """The wait ceiling each domain's own state records, keyed by slug (round 64).

    Round 63 let the operator cap a queue run per domain, but only on the command line: the ceiling lived for
    one invocation, so the same reader had to retype it, and the same ceiling could mean two different things
    on two days. A limit belongs to the deployment, not to the shell — it is a statement about how long that
    stream is allowed to keep the loop waiting, and the deployment state is where such statements live.

    Unreadable files and unparseable numbers are skipped rather than raised: a corrupted budget block must not
    make the whole state unreadable, and a ceiling that cannot be read is simply absent (the gate then charges
    nothing for that domain, which is what it did before this existed).
    """
    return _state_budget_field(root, "max_minutes")


def state_drains(root: Path) -> Dict[str, float]:
    """The whole-queue wait ceiling each domain's own state records, keyed by slug (round 65).

    Round 64 recorded "how long one queue run may wait for this domain". That is a different promise from "how
    long emptying its queue may take": a reader who can only cap the first finds the second mid-queue, when the
    money is already spent. Two numbers, one file, one spelling of the domain.
    """
    return _state_budget_field(root, "drain_minutes")


def _state_budget_field(root: Path, field: str) -> Dict[str, float]:
    folder = _evolve_dir(root) / DOMAINS
    values: Dict[str, float] = {}
    if not folder.is_dir():
        return values
    for path in sorted(folder.glob("*/" + CURRENT)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        value = (payload.get(BUDGET) or {}).get(field)
        if value is None:
            continue
        try:
            values[str(path.parent.name)] = float(value)
        except (TypeError, ValueError):
            continue
    return values


def set_state_budget(root: Path, domain: str, minutes: Optional[float] = None, *,
                     drain_minutes: Optional[float] = None, clear_run: bool = False,
                     clear_drain: bool = False) -> Path:
    """Record how long `domain` will let a queue run wait, and how long emptying it may take (rounds 64-66).

    Only the budget block changes: the variables in force and the versions that own them are copied through,
    because writing a policy is not a reason to touch a deployment. The timestamp does move, so a reader
    comparing two states can see that one of them is newer — and the `sources` map still says which version
    owns each variable, which a budget never does.

    Two ceilings, merged rather than replaced: setting the drain ceiling must not silently erase the per-run one
    that a reader recorded yesterday (boundary 139, in a smaller place).

    Round 66: a policy that can only be added is a policy that cannot be withdrawn. `clear_run`/`clear_drain`
    remove their key, and the block disappears from the file when it ends up empty — a `"budget": {}` left behind
    would read as "this domain has a budget" on any page that only checks for the key.
    """
    if not (minutes is not None or drain_minutes is not None or clear_run or clear_drain):
        raise ValueError("set_state_budget wants a run ceiling, a drain ceiling, or something to clear")
    state = current_state(root, domain)
    budget = dict(state.get("budget") or {})
    if clear_run:
        budget.pop("max_minutes", None)
    if clear_drain:
        budget.pop("drain_minutes", None)
    if minutes is not None:
        budget["max_minutes"] = float(minutes)
    if drain_minutes is not None:
        budget["drain_minutes"] = float(drain_minutes)
    _write_state(root, {"env": state["env"], "sources": state["sources"], "budget": budget}, domain)
    return state_path(root, domain)


def promotions_for_domain(promotions: List[Promotion], domain: Optional[str] = None) -> List[Promotion]:
    """The versions recorded on one stream, plus the ones that predate the stream field.

    A promise is a statement about a dataset (boundary 33). The versions written before `stream` was
    recorded cannot be attributed to any dataset, so they are kept in every domain's list and marked in
    the table rather than dropped: dropping them would make a real retraction (v003) disappear from the
    history of the only domain that re-checked it.
    """
    if not domain:
        return list(promotions)
    slug = domain_slug(domain)
    return [promotion for promotion in promotions
            if not (promotion.stream or {}).get("source")
            or domain_slug(str(promotion.stream.get("source"))) == slug]


def legacy_state_note(root: Path, domain: Optional[str]) -> Optional[str]:
    """A sentence when a global state exists but is not adopted for `domain`, else None.

    Silence here would be the round-21 bug with a nicer directory layout: the operator would see an empty
    "in force" line and assume nothing is deployed, while a global file sits one level up claiming to be.
    """
    if not domain:
        return None
    if current_state(root, domain).get("env"):
        return None
    global_state = current_state(root)
    if not global_state.get("env"):
        return None
    known = {version_label(str(promotion.version)): str(promotion.stream.get("source") or "another stream")
             for promotion in load_promotions(root)}
    owners = sorted({known.get(version_label(source), "another stream")
                     for source in global_state.get("sources", {}).values()})
    versions = ", ".join(combined_versions(global_state))
    return ("the global state (%s, measured on %s) is not adopted for %s: a setting measured on one "
            "stream is not a starting point on another" % (versions or "?", ", ".join(owners) or "?",
                                                           domain_slug(domain)))


def in_force_overrides(root: Path, settings: Settings,
                       domain: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str],
                                                            List[str], List[str]]:
    """The overrides a round must adopt so its baseline is the deployment, not the factory default.

    ``load_settings()`` is what the code ships with; ``var/evolve/current.json`` is what the deployment is
    running. Until this function existed every round compared candidates against the former, so an
    "accepted" change could be worse than the configuration already live — round 20 accepted a patch whose
    shipped recall is 0.211 while the in-force v019 gives 0.807, which is a finding about the baseline
    rather than about the patch.

    Only reloadable fields are adopted, because those are the ones a running process could take; anything
    else is reported as refused rather than silently ignored. The second return value maps environment
    variable name to the version that owns it, so the caller can print where each value came from — it is
    filtered to the variables that were actually adopted, because a refused setting is not in the baseline
    and labelling it would describe a configuration that is not running.
    """
    state = current_state(root, domain)
    overrides, refused, unknown = overrides_from_state(state, settings)
    adopted = {ENV_VARS[(section, field)]
               for section, values in overrides.items()
               for field in values
               if (section, field) in ENV_VARS}
    sources = {name: source for name, source in dict(state.get("sources") or {}).items() if name in adopted}
    return overrides, sources, refused, unknown


def stacking_note(root: Path, promotion: Promotion,
                  domain: Optional[str] = None) -> Optional[str]:
    """Why a verified version may not simply be applied next to one already in force (round 44).

    `--verify` walks whatever is due and applies each version it verifies. An unscoped run over the SMD ledger
    therefore verified `v004` while `v020` was in force and merged both into the deployment — the campaign's
    own warning then printed "the variables in force come from 2 versions … their sum is a configuration
    nobody measured". Each version was measured as a patch against the same baseline, so verifying two of
    them is two answers to two questions, not one configuration.

    The note is returned when another version already owns variables in this domain's state, and the caller
    records the check without applying it. Nothing is reverted: the version that was in force stays there, and
    the newly verified one is evidence for a round that proposes the combination deliberately.
    """
    state = current_state(root, domain)
    owners = {str(version) for version in (state.get("sources") or {}).values() if version}
    others = sorted(owners - {str(promotion.version)})
    if not others:
        return None
    return ("%s is already in force for %s, and the sum of two patches was never measured"
            % (", ".join("v%s" % version for version in others), domain_slug(domain)))


def apply_promotion(root: Path, promotion: Promotion, *, respect_newer: bool = True,
                    domain: Optional[str] = None) -> Dict[str, Any]:
    """Merge one version's assignments into the deployment state, recording where each came from.

    A lower version never takes a variable away from a higher one. Verifying an old version is a normal
    thing to do — `--verify` walks whatever is still unverified — and without this rule re-checking v002
    would put its threshold back in force over the newer v011 that replaced it, which is a rollback
    disguised as a verification. The variables it could not take are reported as `superseded` instead of
    being applied quietly.

    `inherited` lists variables already in force that this version does not set, and `interactions` names
    the ones that change what the patch means. Round 19 showed why that matters: a version measured with
    the reader reporting a level was applied on top of an earlier version that kept a rolling window, so
    the same three assignments meant "level" in the measurement and "rank" in the deployment. A stack of
    patches each measured against the baseline is not a configuration anyone measured, and the tool has
    to say so where the operator can see it.
    """
    state = current_state(root, domain)
    env = dict(state["env"])
    sources = dict(state["sources"])
    applied: List[str] = []
    superseded: List[str] = []
    for name, value in promotion.env.items():
        owner = sources.get(name)
        if respect_newer and owner and owner.isdigit() and int(owner) > promotion.version:
            superseded.append(name)
            continue
        env[name] = value
        sources[name] = str(promotion.version)
        applied.append(name)
    _write_state(root, {"env": env, "sources": sources}, domain)
    return {
        "applied": applied,
        "superseded": superseded,
        "inherited": inherited_variables(state, promotion),
        "interactions": interacting_variables(state, promotion),
    }


#: Pairs whose meaning depends on each other, so a patch that sets one while the other is inherited from
#: an earlier version is not the patch that was measured. Declared by hand because the settings have no
#: dependency model: `digest.rolling_window` turns the reader's answer into a quantile, and
#: `gate.rank_probability` is the bar that reads it.
INTERACTIONS: Tuple[Tuple[Tuple[str, str], Tuple[str, str]], ...] = (
    (("digest", "scoring"), ("digest", "rolling_window")),
    (("digest", "rolling_window"), ("gate", "rank_probability")),
    (("gate", "anomaly_probability"), ("gate", "notify_probability")),
)


def inherited_variables(state: Mapping[str, Any], promotion: "Promotion") -> List[Dict[str, Any]]:
    """Variables already in force that this version does not set, with the version that owns them."""
    env = dict((state or {}).get("env") or {})
    sources = dict((state or {}).get("sources") or {})
    mine = set(promotion.env)
    return [{"name": name, "value": env[name], "version": sources.get(name)}
            for name in sorted(env) if name not in mine]


def interacting_variables(state: Mapping[str, Any], promotion: "Promotion") -> List[str]:
    """The inherited variables that change what `promotion` means, as `VAR=value (from vNNN)` lines."""
    from .deploy import FIELD_BY_ENV  # local: keeps the import graph one-way at module load

    mine = {FIELD_BY_ENV[name] for name in promotion.env if name in FIELD_BY_ENV}
    lines: List[str] = []
    for entry in inherited_variables(state, promotion):
        field = FIELD_BY_ENV.get(entry["name"])
        if field is None:
            continue
        for first, second in INTERACTIONS:
            if (field == first and second in mine) or (field == second and first in mine):
                lines.append("%s=%s is inherited from %s and changes what this patch means"
                             % (entry["name"], entry["value"], _version_label(entry["version"])))
    return lines


def _version_label(source: Optional[str]) -> str:
    """``v019`` for a recorded source, ``?`` when the state does not say where a value came from."""
    if source is None or str(source).strip() == "":
        return "?"
    text = str(source).strip()
    if text.lower().startswith("v"):
        return text
    return "v%03d" % int(text) if text.isdigit() else text


def version_label(source: Optional[str]) -> str:
    """Public form of `_version_label`, for callers that print where a value came from."""
    return _version_label(source)


def revert_promotion(root: Path, promotion: Promotion,
                     domain: Optional[str] = None) -> Path:
    """Remove exactly the variables this version put in force, restoring an earlier source if there is one.

    The earlier value comes from the version file that *is* still the source of that variable; when the
    source is this same version there is nothing behind it and the variable is simply removed. Neither
    branch invents a value, which is the only property a rollback has to have.
    """
    state = current_state(root, domain)
    env = dict(state["env"])
    sources = dict(state["sources"])
    others = {item.version: item for item in load_promotions(root) if item.version != promotion.version}
    for name in promotion.env:
        if sources.get(name) != str(promotion.version):
            continue
        previous = None
        for version in sorted(others):
            if name in others[version].env and not others[version].reverted_at:
                previous = (str(version), others[version].env[name])
        if previous is None:
            env.pop(name, None)
            sources.pop(name, None)
        else:
            sources[name], env[name] = previous
    return _write_state(root, {"env": env, "sources": sources}, domain)


GEOMETRY_TOLERANCE = 0.25


def _geometry_mismatch(recorded: Optional[Mapping[str, Any]],
                       checked: Optional[Mapping[str, Any]]) -> Optional[str]:
    """How the re-checked stream differs in shape from the one the claim was measured on.

    Returns `None` when the two are comparable, or a sentence naming the difference. Only the *shape* is
    compared, never the outcome: a stream that is sampled the same way but scores worse is a failed
    promise, while one sampled differently measured a different quantity.
    """
    if not recorded or not checked:
        return None
    recorded_scored = recorded.get("scored")
    checked_scored = checked.get("scored")
    if recorded_scored and checked_scored:
        ratio = float(checked_scored) / float(recorded_scored)
        if abs(ratio - 1.0) > GEOMETRY_TOLERANCE:
            return ("the re-check measured %d scored windows against the %d the claim was made on "
                    "(%.2fx)" % (checked_scored, recorded_scored, ratio))
    recorded_windows = recorded.get("windows")
    checked_windows = checked.get("windows")
    recorded_events = recorded.get("events")
    checked_events = checked.get("events")
    if recorded_windows and checked_windows and recorded_events and checked_events:
        before = float(recorded_events) / float(recorded_windows)
        after = float(checked_events) / float(checked_windows)
        if before and abs(after / before - 1.0) > GEOMETRY_TOLERANCE:
            return ("the re-check saw %.1f events per window against %.1f at the time of the claim"
                    % (after, before))
    # Round 27: a window count is not a duration. `rolling_window = 200` covers 200 windows, and how much
    # traffic that is depends on the cadence — the same 200 meant 50 minutes per window on one SMD stream
    # and 35 on another, which is exactly how a healthy configuration looked like a regression.
    before_cadence = recorded.get("cadence_s")
    after_cadence = checked.get("cadence_s")
    if before_cadence and after_cadence:
        ratio = float(after_cadence) / float(before_cadence)
        if abs(ratio - 1.0) > GEOMETRY_TOLERANCE:
            return ("a window covers %.0f s of traffic here against %.0f s when the claim was made "
                    "(%.2fx cadence)" % (float(after_cadence), float(before_cadence), ratio))
    return None


def _claimed_window(promotion: "Promotion") -> Optional[int]:
    """The window count the version put in force, when it put one there."""
    raw = (promotion.env or {}).get("BRIAN_DIGEST_ROLLING_WINDOW")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def rescale_hint(promotion: "Promotion", recorded: Optional[Mapping[str, Any]],
                 checked: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The window count that would make this stream cover the same wall-clock as the claim.

    Reported rather than applied: rescaling changes the configuration, so the result would be a measurement
    of a different version. The number is here so the next round can propose that version deliberately,
    instead of a person guessing what "200 windows" means on a stream sampled differently.
    """
    if not recorded or not checked:
        return None
    claimed = _claimed_window(promotion)
    before_cadence = recorded.get("cadence_s")
    after_cadence = checked.get("cadence_s")
    if not claimed:
        return None
    # Only when the shape difference is what refused the check: a 2% difference changes the equivalent
    # window count too, and printing a "comparable" number for a comparison that was never refused would
    # imply the deployment should change when nothing is wrong.
    if _geometry_mismatch(recorded, checked) is None:
        return None
    if before_cadence and after_cadence:
        ratio = float(after_cadence) / float(before_cadence)
        basis = "cadence"
    elif recorded.get("windows") and checked.get("windows"):
        # Rounds before `cadence_s` still recorded their window count, and the ratio of window counts is the
        # same quantity the refusal quoted — fewer windows over the same span means less traffic per window.
        ratio = float(recorded["windows"]) / float(checked["windows"])
        basis = "window count"
    else:
        return None
    if not ratio:
        return None
    equivalent = max(1, int(round(claimed / ratio)))
    if equivalent == claimed:
        return None
    if basis == "cadence":
        detail = ("%d windows x %.0f s is %.0f s of traffic, the same span %d windows covered here"
                  % (claimed, float(before_cadence), claimed * float(before_cadence), equivalent))
    else:
        detail = ("%d windows of %d is the same share of the recording as %d of %d"
                  % (claimed, recorded["windows"], equivalent, checked["windows"]))
    return ("a comparable re-check on this stream would use BRIAN_DIGEST_ROLLING_WINDOW=%d (%s)"
            % (equivalent, detail))


def rescaled_candidate(promotion: "Promotion", recorded: Optional[Mapping[str, Any]],
                       checked: Optional[Mapping[str, Any]]) -> Optional[Candidate]:
    """The same version, with its window count changed to cover the same wall-clock on this stream.

    `None` when there is nothing to rescale: no candidate, no window knob, or no comparable shape pair.

    Round 30 turned this from a *measuring instrument* into a proposal: the returned candidate carries the
    recall the version promised (`promise`) and a `provenance` sentence, because `decide()` must judge it
    against the promise rather than against the baseline it was never meant to beat.
    """
    hint = rescale_hint(promotion, recorded, checked)
    if hint is None or promotion.candidate is None:
        return None
    try:
        value = int(hint.split("BRIAN_DIGEST_ROLLING_WINDOW=")[1].split()[0])
    except (IndexError, ValueError):
        return None
    changes = []
    for section, name, current in promotion.candidate.changes():
        if section == "digest" and name == "rolling_window":
            changes.append((section, name, value))
        else:
            changes.append((section, name, current))
    if all(change[2] == current[2] for change, current in zip(changes, promotion.candidate.changes())):
        return None
    first = changes[0]
    claim = promotion.claim
    promise = claim.recall_at_budget if claim is not None else None
    return Candidate(
        name="%s@hint%d" % (promotion.candidate.name, value),
        hypothesis="the version in force was measured where a window covered different traffic; at %d "
                   "windows it covers the span its claim was made over, so the claim can be tested again"
                   % value,
        section=first[0], field=first[1], value=first[2], also=tuple(changes[1:]),
        promise=promise,
        provenance="v%03d's own settings at the window count that covers the same span (%s)"
                   % (promotion.version, hint))


def shape_note(payload: Optional[Mapping[str, Any]]) -> str:
    """One line for the `shape` a verification recorded, or "" when it recorded none."""
    shape = (payload or {}).get("shape")
    if not shape:
        return ""
    recall = shape.get("recall_at_budget")
    claimed = shape.get("claimed_recall")
    if recall is None:
        return ""
    # A carried-over shape names the check that measured it: the number is real, and it is older than the
    # verdict line above it.
    measured = (payload or {}).get("shape_from") or ""
    stamp = (" (measured %s)" % measured[:16].replace("T", " ")) if measured else ""
    if claimed:
        return ("        shape  the same version at rolling_window=%d recalls %.3f on this stream, "
                "%.0f%% of the promised %.3f%s"
                % (shape.get("window") or 0, recall, 100.0 * recall / claimed, claimed, stamp))
    return ("        shape  the same version at rolling_window=%d recalls %.3f on this stream%s"
            % (shape.get("window") or 0, recall, stamp))


def claim_check(claim: Claim, measurement: Measurement, rules: EvolveRules,
                recorded_stream: Optional[Mapping[str, Any]] = None,
                checked_stream: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """Does the re-measured configuration still pay out what the version promised?

    The tolerances are the loop's own (`operational_tolerance` for recall, `cost_tolerance` for the bill)
    rather than new constants, because a promise is judged by the standard of the objective that made it —
    introducing a second standard here would let a version pass a check the loop would have rejected.
    """
    mismatch = _geometry_mismatch(recorded_stream, checked_stream)
    if mismatch is not None:
        return UNMEASURABLE, ("%s, so this is a different measurement rather than a worse one: a "
                              "window means a different amount of traffic when the stream is sampled "
                              "differently" % mismatch)
    if measurement.recall_at_budget is None or claim.recall_at_budget is None:
        return UNMEASURABLE, ("the claim or the re-measurement has no recall at the budget, so the "
                              "promise cannot be checked either way")
    floor = claim.recall_at_budget * (1.0 - rules.operational_tolerance)
    if measurement.recall_at_budget + 1e-9 < floor:
        return FAILED, ("promised recall %.3f at the %.0f%% budget, re-measured %.3f (below %.3f)"
                        % (claim.recall_at_budget, 100 * rules.fpr_budget,
                           measurement.recall_at_budget, floor))
    if measurement.fpr_at_budget is not None and measurement.fpr_at_budget > rules.fpr_budget + 1e-9:
        return FAILED, ("re-measured false-positive rate %.3f exceeds the %.0f%% budget the promise "
                        "was made under" % (measurement.fpr_at_budget, 100 * rules.fpr_budget))
    if (claim.tokens_per_report is not None and measurement.tokens_per_report is not None
            and measurement.tokens_per_report > claim.tokens_per_report * (1.0 + rules.cost_tolerance)):
        return FAILED, ("promised %.0f tokens per report, re-measured %.0f (above %.0f)"
                        % (claim.tokens_per_report, measurement.tokens_per_report,
                           claim.tokens_per_report * (1.0 + rules.cost_tolerance)))
    return VERIFIED, ("recall %.3f at the %.0f%% budget against a promised %.3f, %.0f tokens per report "
                      "against a promised %.0f"
                      % (measurement.recall_at_budget, 100 * rules.fpr_budget, claim.recall_at_budget,
                         measurement.tokens_per_report or 0.0, claim.tokens_per_report or 0.0))


def format_promotions(promotions: List[Promotion], state: Optional[Mapping[str, Any]] = None,
                      domain: Optional[str] = None, expiry_days: float = CLAIM_EXPIRY_DAYS,
                      now: Optional[datetime] = None) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - versions in force and whether their claims still hold")
    lines.append("=" * 96)
    if domain:
        lines.append("domain           %s  (%s)" % (domain_slug(domain), domain))
        # Round 64: a ceiling stored in this file is a policy the operator is trusting, so the page that lists
        # what is in force has to name it -- a limit nobody can see is a limit nobody can review.
        ceiling = (state or {}).get(BUDGET) or {}
        if ceiling.get("max_minutes") is not None:
            lines.append("budget           at most %g minute(s) for one queue run (from the deployment state)"
                         % float(ceiling["max_minutes"]))
        if ceiling.get("drain_minutes") is not None:
            lines.append("drain            at most %g minute(s) to empty the queue (from the deployment state)"
                         % float(ceiling["drain_minutes"]))
        lines.append("")
    if not promotions:
        lines.append("no version files yet: run a round with --apply to record one.")
        return "\n".join(lines)
    if domain:
        # A version measured on one stream is not a promise about another (boundary 48), so a table
        # headed with a domain lists that domain's versions plus the ones that predate the stream
        # field -- and marks the latter, because "recorded before anyone wrote down the dataset" is a
        # different kind of evidence from "measured on this stream".
        shown = promotions_for_domain(promotions, domain)
        others = len(promotions) - len(shown)
        promotions = shown
        if others:
            lines.append("(%d version(s) recorded on other streams are not listed: pass --verify "
                         "with their stream to re-check them)" % others)
            lines.append("")
    lines.append("%-8s %-26s %-12s %s" % ("version", "change", "claim", "state"))
    lines.append("-" * 96)
    for promotion in promotions:
        legacy_mark = "  (no stream recorded)" \
            if (promotion.stream or {}).get("source") in (None, "") else ""
        claim = promotion.claim
        promise = "n/a" if claim.recall_at_budget is None else "recall %.3f" % claim.recall_at_budget
        if promotion.reverted_at:
            note = "reverted %s" % promotion.reverted_at
        elif promotion.verification:
            note = "%s: %s" % (promotion.verification.get("verdict", "?"),
                               _first_line(promotion.verification.get("reason", "")))
            age = verification_age_days(promotion.verification, now)
            if age is not None:
                # The verdict's age belongs next to it: "holds" written a year ago reads as consent.
                note = "%s  (%s%s)" % (note, _age_text(age),
                                       ", overdue" if promotion.overdue(now, expiry_days) else "")
        elif promotion.pending:
            note = "unverified"
        else:
            note = "?"
        earlier = len(getattr(promotion, "checks", []) or [])
        if earlier:
            note = "%s  (%d earlier check%s)" % (note, earlier, "" if earlier == 1 else "s")
        # Round 95: a verdict taken after a hand repair describes the repaired location, not the file the
        # promise was born with. Said next to the verdict, because that is the claim the verdict is about.
        # Round 96: a check taken on the file the promise was born with is not the same evidence as one taken
        # on the substitute, even when the two words ("holds", "failed") are identical (boundary 210) -- and the
        # two marks are exclusive, because one verdict stood on exactly one file.
        if (promotion.verification or {}).get("on_original_path"):
            note = "%s  [measured on the original path]" % note
        elif int((promotion.verification or {}).get("stream_path_moves") or 0):
            note = "%s  [measured on the repaired path]" % note
        moved_mark = ""
        if promotion.path_moves:
            moved_mark = "  (path rewritten %d time(s) by hand)" % promotion.path_moves
        lines.append("%-8s %-26s %-12s %s%s%s" % ("v%03d" % promotion.version, promotion.change[:26],
                                                  promise, note, legacy_mark, moved_mark))
        # A refusal that recorded the comparable shape keeps it here: the number is the point of the
        # refusal, so a table that hides it makes the refusal look like a dead end (round 28).
        shaped = shape_note(promotion.verification)
        if shaped:
            lines.append(shaped)
    if state is not None:
        lines.append("")
        env = dict(state.get("env") or {})
        if env:
            lines.append("in force  %s" % ", ".join("%s=%s" % item for item in sorted(env.items())))
        else:
            lines.append("in force  (nothing: no promotion has been applied)")
        worn = combined_versions(state)
        if len(worn) > 1:
            lines.append("!")
            lines.append("! the variables in force come from %d versions: %s." % (len(worn), ", ".join(worn)))
            lines.append("! Each was measured as a patch against the same baseline, so their sum is a")
            lines.append("! configuration nobody measured. Put one of them in force instead.")
        lines.append("a process reads these at start-up; a pipeline started with BRIAN_DEPLOY_STATE")
        lines.append("follows them between windows, and one started without it does not.")
    return "\n".join(lines)


def combined_versions(state: Optional[Mapping[str, Any]]) -> List[str]:
    """The versions contributing at least one variable to the configuration in force, in order.

    A round can accept several patches, and they are alternatives measured against the same baseline;
    applying all of them merges three measurements into a fourth configuration. Round 18 did exactly
    that (`rolling_window` from one version, the bars from another) and the merged state was a
    configuration no round had measured, so the count is now something the tool says out loud.
    """
    sources = dict((state or {}).get("sources") or {})
    seen = []
    for name in sorted(sources):
        version = "v%03d" % int(sources[name]) if str(sources[name]).isdigit() else str(sources[name])
        if version not in seen:
            seen.append(version)
    return seen


def _age_text(days: float) -> str:
    """`checked 3 days ago` / `checked today`, in the unit a reader can act on."""
    if days < 1.0:
        return "checked today"
    if days < 2.0:
        return "checked 1 day ago"
    return "checked %d days ago" % int(days)


def _first_line(text: str) -> str:
    return str(text).splitlines()[0] if str(text) else ""


def set_stream_path(root: Path, version: int, stream_path: str,
                    now: Optional[datetime] = None, why: str = "") -> Dict[str, Any]:
    """Rewrite the file a version says it was measured on, because the record names one that is not there.

    Round 92 taught `--verify` to stand in with the recorded file when the named one is gone. Doing that twice for
    the same version is not two successes, it is one stale record: every later check pays the substitution, and a
    reader of the record still believes the promise was measured somewhere else. The repair is therefore explicit
    and checked -- the new path must exist (a repair that points at nothing fixes the paperwork and loses the
    evidence, boundary 195), and the old value is kept in `stream_path_history` so the change is visible in the
    record rather than only in a log line (boundary 174's rule, applied to a field instead of a link).
    """
    path = _evolve_dir(root) / VERSIONS / ("v%03d.json" % int(version))
    if not path.is_file():
        raise ValueError("no version record for v%03d: %s" % (int(version), path))
    wanted = str(stream_path or "").strip()
    if not wanted:
        raise ValueError("refusing to record an empty stream path: the record would say less than it does now")
    if not (root / wanted).is_file():
        raise ValueError("the file to record does not exist in this checkout: %s "
                         "(a repair must point at something a reader can open)" % wanted)
    payload = json.loads(path.read_text(encoding="utf-8"))
    before = str(payload.get("stream_path") or "")
    if before == wanted:
        return {"version": int(version), "from": before, "to": wanted, "changed": False, "returned": False}
    payload["stream_path"] = wanted
    history = payload.get("stream_path_history")
    history = list(history) if isinstance(history, list) else []
    origin = str(history[0].get("from") or "") if history else ""
    # Round 97: moving back to the file the promise was born with is not another repair, it is the end of a trip.
    # The command knows it is a return and says so itself, because a caller writing `why` by hand would have to
    # know the record's whole history to phrase it, and a reader cannot tell "moved again" from "moved back".
    returned = bool(origin) and wanted == origin and before != wanted
    note = str(why or ("the original file the record first named is back in this checkout: this move returns the "
                       "promise to it" if returned
                       else "the file the record named does not exist in this checkout"))
    entry = {"from": before, "to": wanted, "at": _stamp_moment(now).isoformat(), "why": note}
    if returned:
        entry["return"] = True
    history.append(entry)
    payload["stream_path_history"] = history
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    return {"version": int(version), "from": before, "to": wanted, "changed": True, "returned": returned}


def stream_path_history(root: Path, version: int) -> List[Dict[str, Any]]:
    """The moves a version's measured file has been through, oldest first (round 93).

    Kept separate from `set_stream_path` because a reader asks this of versions this build never touched: the
    point of recording the change is that it survives in the record, not in the session that made it.
    """
    path = _evolve_dir(root) / VERSIONS / ("v%03d.json" % int(version))
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    history = payload.get("stream_path_history")
    return [dict(item) for item in history if isinstance(item, Mapping)] if isinstance(history, list) else []


def dead_record_paths(root: Path) -> List[Dict[str, Any]]:
    """Versions whose record names a stream file this checkout cannot open (round 93).

    Round 91 taught the pages to mark a *clearing* whose evidence is gone, and round 92 gave the mark an age. Neither
    can say what to do about it: the promise itself still names a file nobody can open, so every later check either
    substitutes or refuses, and the record never learns. This is the missing list -- computed against the checkout
    rather than stored (boundary 194), because it is a fact about the reader's tree.

    `in_force` is carried so the page can say which of them is also deployed: a dead path on a retired promise is
    housekeeping, on a live one it is a check that cannot be repeated.
    """
    deployed = {int(version) for _slug, version in in_force_promotions(root)}
    rows: List[Dict[str, Any]] = []
    directory = _evolve_dir(root) / VERSIONS
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("v*.json")):
        match = re.fullmatch(r"v(\d+)\.json", path.name)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recorded = str(payload.get("stream_path") or "")
        if not recorded or (root / recorded).is_file():
            continue
        rows.append({"version": int(match.group(1)), "stream_path": recorded,
                     "in_force": int(match.group(1)) in deployed})
    return rows


def same_stream_path(left: str, right: str) -> bool:
    """Whether two recorded paths name the same file, ignoring separators and a leading `./` (round 96).

    The same stream is spelled with a backslash by one command and a slash by another (round 54), so a byte
    comparison would report a check on the original file as a check on the substitute.
    """
    def normal(value):
        text = str(value or "").replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        return text.rstrip("/")

    return bool(normal(left)) and normal(left) == normal(right)


def verification_evidence(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every verdict a record holds, with the file each one was taken on (round 98).

    Round 96 separated "measured on the repair" from "measured on the original"; what stayed folded together is
    *how many* verdicts came from each. Two sources of truth exist and they are not equal: a check written from
    round 98 on records `checked_path` itself (a written citation), while older checks only recorded how many
    moves the record had at the time, from which the file can be deduced exactly -- the k-th move's target
    (boundary 187: a citation written at the moment it happened must say so, because one deduced later from
    timestamps can vanish with the ledger). Anything else is returned with an empty path and counted as
    unplaceable rather than guessed.
    """
    moves = [dict(item) for item in (payload.get("stream_path_history") or []) if isinstance(item, Mapping)]
    rows: List[Dict[str, Any]] = []
    for item in [payload.get("verification")] + list(payload.get("checks") or []):
        if not isinstance(item, Mapping) or not item.get("verdict"):
            continue
        written = str(item.get("checked_path") or "")
        source = "written" if written else ""
        if not written:
            index = int(item.get("stream_path_moves") or 0)
            if 0 < index <= len(moves):
                written = str(moves[index - 1].get("to") or "")
                source = "deduced" if written else ""
        rows.append({"at": str(item.get("checked_at") or ""),
                     "verdict": str(item.get("verdict") or ""),
                     "path": written,
                     "source": source})
    return rows


def evidence_split(evidence: Sequence[Mapping[str, Any]], origin: str) -> Dict[str, Any]:
    """How a record's verdicts divide between the file it was born with and everything else (round 98).

    "Elsewhere" rather than "the substitute", because a record may have been moved through several files and the
    only claim the numbers can support is "not the original". A path the record cannot place is counted apart
    instead of being folded into either side (boundary 166: silence must not pick the answer).
    """
    original = 0
    elsewhere = 0
    unplaced = 0
    files: List[str] = []
    for row in evidence:
        path = str(row.get("path") or "")
        if not path:
            unplaced += 1
        elif same_stream_path(path, origin):
            original += 1
        else:
            elsewhere += 1
            if path not in files:
                files.append(path)
    unplaced_rows = [{"at": str(row.get("at") or ""), "verdict": str(row.get("verdict") or "")}
                     for row in evidence if not str(row.get("path") or "")]
    return {"original": original, "elsewhere": elsewhere, "unplaced": unplaced,
            "total": len(evidence), "elsewhere_files": files, "unplaced_rows": unplaced_rows}


def prior_evidence(promotion: "Promotion") -> List[Dict[str, Any]]:
    """What the record held *before* this check, read from the version file itself (round 98)."""
    try:
        payload = json.loads(promotion.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return verification_evidence(payload)


def never_tested_at_origin(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The records that hold no verdict on the file their promise was born with (round 100).

    Round 99 taught one page to say "this is the first verdict taken on the file the promise was born with". That
    is a statement about one record at one moment. A reader deciding what to measure next needs the population: how
    many promises have never been tested where they were made, and which file each of them was born with -- a
    record's split without the queue behind it answers "is this one rare?" with silence.

    Records with no verdict at all are listed too, and counted: "never tested" includes "never checked", and a
    page that silently omitted them would report the debt as smaller than it is (round 79's rule about a field a
    reader cannot see being counted as its own state).
    """
    by_file: Dict[str, int] = {}
    versions: List[Dict[str, Any]] = []
    for row in rows:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
        if int(evidence.get("original") or 0):
            continue
        origin = str(row.get("first_from") or "")
        key = origin or "(unrecorded)"
        by_file[key] = by_file.get(key, 0) + 1
        versions.append({"version": int(row.get("version") or 0), "origin": origin,
                         "verdicts": int(evidence.get("total") or 0),
                         "origin_exists": bool(row.get("origin_exists"))})
    versions.sort(key=lambda item: item["version"])
    return {"records": len(versions), "by_file": by_file, "versions": versions}


QUEUE_RUNS = "queue_runs.json"
QUEUE_RUN_FORMAT = "brian.queue/1"
QUEUE_RUN_LIMIT = 20


def _queue_runs_path(root: Path) -> Path:
    return _evolve_dir(root) / QUEUE_RUNS


def queue_runs(root: Path, limit: int = QUEUE_RUN_LIMIT) -> Dict[str, Any]:
    """The recorded queue runs, oldest first, plus how many older ones were dropped.

    Round 103: `--from-queue` measured promises on their origins and then forgot that it had run. A campaign page
    could say which promises still hold no verdict where they were made and never when anyone last walked the
    queue -- so a ledger that had never been queued and one queued an hour ago read the same.
    """
    path = _queue_runs_path(root)
    empty = {"format": QUEUE_RUN_FORMAT, "runs": [], "trimmed": 0}
    if not path.is_file():
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, Mapping):
        return empty
    runs = [dict(item) for item in payload.get("runs") or [] if isinstance(item, Mapping)]
    return {"format": str(payload.get("format") or QUEUE_RUN_FORMAT), "runs": runs[-limit:],
            "trimmed": int(payload.get("trimmed") or 0)}


def last_queue_run(root: Path) -> Optional[Dict[str, Any]]:
    runs = queue_runs(root)["runs"]
    return runs[-1] if runs else None


def record_queue_run(root: Path, run: Mapping[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Write down one walk of the origin queue.

    The fields are what a reader cannot reconstruct later: when it ran, how much of the queue it covered, how many
    measurements it actually took, which origins it had to leave out, the worst exit code it produced, and the
    version it stopped at when a build defect ended the walk. `cleared` counts only the clearings this run wrote
    itself, so "the queue measured three promises" and "the queue answered three disputes" stay two facts.
    """
    moment = _stamp_moment(now if isinstance(now, datetime) else _parse_moment(now))
    entry = {
        "at": moment.isoformat(),
        "limit": int(run.get("limit") or 0),
        "runnable": int(run.get("runnable") or 0),
        "measured": int(run.get("measured") or 0),
        "skipped": int(run.get("skipped") or 0),
        "skipped_origins": [str(item) for item in run.get("skipped_origins") or []],
        "cleared": int(run.get("cleared") or 0),
        "worst": int(run.get("worst") or 0),
        "stopped_at": int(run.get("stopped_at") or 0),
        # Round 104: which versions this walk actually attempted, and what the inner check returned for each. Without
        # it the store knows how many were measured but not which ones -- so the next walk cannot tell "nobody has
        # tried this" from "this was tried and the books refused it twice".
        # Round 106: the reason travels with the attempt, so a later walk can tell a refusal that still stands from
        # one the ledger has already answered. A store that keeps only the code can never notice a repaired book.
        "attempted": [{"version": int(item.get("version") or 0), "code": int(item.get("code") or 0),
                       "why": str(item.get("why") or "")}
                      for item in run.get("attempted") or [] if isinstance(item, Mapping)],
    }
    held = queue_runs(root)
    runs = held["runs"] + [entry]
    trimmed = int(held["trimmed"] or 0)
    if len(runs) > QUEUE_RUN_LIMIT:
        trimmed += len(runs) - QUEUE_RUN_LIMIT
        runs = runs[-QUEUE_RUN_LIMIT:]
    path = _queue_runs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": QUEUE_RUN_FORMAT, "runs": runs, "trimmed": trimmed}
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return entry

# Round 144: the second queue. `--from-queue` keeps `queue_runs.json` and pays no rounds; the re-run queue
# (`--rerun-unrecorded`, `--with-ceilings`) pays rounds and until now kept no record of itself at all -- so a
# round's `queue` block said who paid it and not which invocation, and "walk 3" had nothing to point at.
RERUN_RUNS = "rerun_runs.json"
RERUN_RUN_FORMAT = "brian.rerun/1"
RERUN_RUN_LIMIT = 20


def _rerun_runs_path(root: Path) -> Path:
    return _evolve_dir(root) / RERUN_RUNS


def rerun_runs(root: Path, limit: int = RERUN_RUN_LIMIT) -> Dict[str, Any]:
    """The recorded re-run queue invocations, oldest first, plus how many older ones were dropped (round 144).

    One entry per invocation, whether it paid one round or twenty: "the command ran" and "the command paid" are
    two facts, and a drain that paid nothing (every round refused) still happened.
    """
    path = _rerun_runs_path(root)
    empty = {"format": RERUN_RUN_FORMAT, "runs": [], "trimmed": 0}
    if not path.is_file():
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, Mapping):
        return empty
    runs = [dict(item) for item in payload.get("runs") or [] if isinstance(item, Mapping)]
    return {"format": str(payload.get("format") or RERUN_RUN_FORMAT), "runs": runs[-limit:],
            "trimmed": int(payload.get("trimmed") or 0)}


def _write_rerun_runs(root: Path, runs: List[Dict[str, Any]], trimmed: int) -> None:
    path = _rerun_runs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": RERUN_RUN_FORMAT, "runs": runs, "trimmed": trimmed}
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def begin_rerun_run(root: Path, run: Mapping[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Reserve this invocation's place in the store **before** it measures anything (round 144).

    The ordinal has to exist while the rounds are being written, because each round records the walk that paid
    it, and a walk recorded only at the end would leave every round it paid naming a number nobody could look
    up. Writing the entry first also settles the unattended case: a command that dies mid-walk leaves an entry
    with `finished: ""`, which says "a walk started and never ended" instead of nothing at all.
    """
    moment = _stamp_moment(now if isinstance(now, datetime) else _parse_moment(now))
    held = rerun_runs(root)
    runs = held["runs"] + [{
        "at": moment.isoformat(),
        "started": moment.isoformat(),
        "finished": "",
        "limit": int(run.get("limit") or 0),
        "ceiling_limit": run.get("ceiling_limit"),
        "families": [str(item) for item in run.get("families") or []],
        "with_ceilings": bool(run.get("with_ceilings")),
        "paid": 0,
        "paid_ceilings": 0,
        "failures": 0,
        "stopped": False,
        "rounds": [],
    }]
    trimmed = int(held["trimmed"] or 0)
    if len(runs) > RERUN_RUN_LIMIT:
        trimmed += len(runs) - RERUN_RUN_LIMIT
        runs = runs[-RERUN_RUN_LIMIT:]
    _write_rerun_runs(root, runs, trimmed)
    return {"walk": len(runs), "of": len(runs) + trimmed}


def finish_rerun_run(root: Path, walk: int, run: Mapping[str, Any],
                     now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Write what the reserved entry accomplished, without moving it (round 144).

    Updating the entry rather than appending a second one is the point: the ordinal is the identity, so the walk
    a round names must stay the same record after the walk finishes. A store trimmed between the two calls
    returns None instead of guessing -- the rounds still name an ordinal that the store no longer holds, and
    that is the truth about them.
    """
    moment = _stamp_moment(now if isinstance(now, datetime) else _parse_moment(now))
    held = rerun_runs(root)
    runs = held["runs"]
    index = int(walk or 0) - 1
    if index < 0 or index >= len(runs):
        return None
    entry = dict(runs[index])
    entry["finished"] = moment.isoformat()
    entry["paid"] = int(run.get("paid") or 0)
    entry["paid_ceilings"] = int(run.get("paid_ceilings") or 0)
    entry["failures"] = int(run.get("failures") or 0)
    entry["stopped"] = bool(run.get("stopped"))
    entry["rounds"] = [str(item) for item in run.get("rounds") or []]
    runs = list(runs)
    runs[index] = entry
    _write_rerun_runs(root, runs, int(held["trimmed"] or 0))
    return entry



def blocked_versions(root: Path) -> Dict[int, Dict[str, Any]]:
    """Versions whose most recent walk ended on a build defect, and when that walk was.

    Round 104: a record blocked by a contradictory ledger fails the same way on every walk, because the walk reads
    the ledger and the ledger has not moved. Paying for it again is not diligence, it is a second bill for the same
    refusal -- so the queue can name it and skip it, and `--retry-blocked` exists for the caller who has just fixed
    the books. Newest attempt wins: a later walk that measured the record cleanly takes it off this list.
    """
    blocked: Dict[int, Dict[str, Any]] = {}
    for run in reversed(queue_runs(root)["runs"]):
        for item in run.get("attempted") or []:
            if not isinstance(item, Mapping):
                continue
            version = int(item.get("version") or 0)
            if not version or version in blocked:
                continue
            code = int(item.get("code") or 0)
            if code == 2:
                # Round 105: a refusal is only useful if it says what refused it. `provenance_block` knows which
                # record's book contradicts this promise; without it the page says "the books disagree" and leaves
                # the reader with no record to open.
                # Round 106: the reason is recorded *with the refusal*, and today's answer is compared against it.
                # A refusal that no longer stands does not have to be re-bought with `--retry-blocked`: the caller
                # repaired the books, and the ledger can see it. A refusal whose wording changed but whose fault
                # did not (a different record named) is still a refusal, so the comparison is on the whole sentence.
                stored = str(item.get("why") or "")
                current = _blocked_reason(root, version)
                # Round 106: a refusal that has gone away is at least as changed as one whose sentence moved.
                # Requiring both sentences meant a fully repaired record stayed refused forever -- exactly the case
                # this round exists for, since `provenance_block` returning None is the strongest answer there is.
                blocked[version] = {"version": version, "at": str(run.get("at") or ""), "code": code,
                                    "why": current or stored, "stored_why": stored,
                                    "changed": bool(stored) and stored != current,
                                    # Round 112: the pairs a person could decide, so the plan can name the command.
                                    "pairs": blocked_pairs(root, version)}
            else:
                # A later attempt that did not hit the wall settles the question: the record is not blocked.
                blocked[version] = {"version": version, "at": "", "code": code}
    return {version: item for version, item in blocked.items()
            if item["code"] == 2 and not item.get("changed")}


def stale_refusals(root: Path) -> List[Dict[str, Any]]:
    """Refusals whose reason is no longer the reason (round 106).

    A walk records the sentence `provenance_block` gave it. If the ledger has moved since -- repaired, retired,
    re-nominated -- today's sentence differs, and the promise is no longer refused by the same book. Reporting it
    apart is what keeps `--retry-blocked` from being the only way out: the caller who repaired the books gets the
    record back in the queue, and the caller who did not is told the refusal still stands, with its reason.
    """
    stale: List[Dict[str, Any]] = []
    # Newest first, one entry per promise: a refusal recorded by three walks is one refusal that ended, not three.
    seen: set = set()
    for run in reversed(queue_runs(root)["runs"]):
        for item in run.get("attempted") or []:
            if not isinstance(item, Mapping) or int(item.get("code") or 0) != 2:
                continue
            version = int(item.get("version") or 0)
            if not version:
                continue
            stored = str(item.get("why") or "")
            current = _blocked_reason(root, version)
            if version in seen:
                continue
            if stored and stored != current:
                seen.add(version)
                stale.append({"version": version, "at": str(run.get("at") or ""), "was": stored, "now": current})
    return stale


def refusal_history(root: Path) -> Dict[int, Dict[str, Any]]:
    """Every refusal the recorded walks left behind, per promise (round 107).

    `blocked_versions` answers "is this promise refused *now*", which is all a queue needs and not enough for a
    reader: a promise refused twice and then repaired and one refused once an hour ago print the same row. The runs
    the store still holds have each attempt with its stamp, so the history is a scan -- first refusal, last refusal,
    how many walks refused it, and whether the refusal still stands. It is a *windowed* history: `walks` says how
    many runs are in the store, because a count over a trimmed store is not a count over all time (boundary 239).
    """
    held = queue_runs(root)
    runs = [run for run in held["runs"] if isinstance(run, Mapping)]
    standing = blocked_versions(root)
    history: Dict[int, Dict[str, Any]] = {}
    # Oldest first, so each attempt can name the walk it happened in: "refused in walk 2 of 4" is a sentence a reader
    # can find in the store, while "refused at 00:37:28.706167" only matches a stamp (boundary 242).
    for ordinal, run in enumerate(runs, start=1):
        moment = str(run.get("at") or "")
        for item in run.get("attempted") or []:
            if not isinstance(item, Mapping) or int(item.get("code") or 0) != 2:
                continue
            version = int(item.get("version") or 0)
            if not version:
                continue
            row = history.setdefault(version, {"version": version, "count": 0, "first_at": "", "last_at": "",
                                              "reasons": [], "walks": len(runs), "_moments": [],
                                              "_ordinals": []})
            row["count"] += 1
            row["_moments"].append(moment)
            row["_ordinals"].append(ordinal)
            why = str(item.get("why") or "")
            if why and why not in row["reasons"]:
                row["reasons"].append(why)
    for version, row in history.items():
        row["standing"] = version in standing
        row["walks"] = len(runs)
        # The scan runs newest-first, so the stamps must be ordered after the fact: "first" is the oldest refusal,
        # and printing the newest one under that word makes a promise look twice as recently refused as it was.
        moments = sorted(str(item or "") for item in row.pop("_moments", []))
        if moments:
            row["first_at"] = next((item for item in moments if item), "")
            row["last_at"] = next((item for item in reversed(moments) if item), "")
        ordinals = sorted(int(item or 0) for item in row.pop("_ordinals", []))
        if ordinals:
            row["first_walk"] = ordinals[0]
            row["last_walk"] = ordinals[-1]
            # Round 109: the ordinals themselves, not only the outermost pair. "Refused in walks 1 and 3" is what
            # lets the file's bill and the plan cite the same walks the history names (boundary 244).
            row["refused_walks"] = sorted(set(ordinals))
    return history


def walks_phrase(ordinals: Sequence[int]) -> str:
    """A list of walk ordinals written the way a sentence needs it (round 109).

    `[1, 2]` is not a phrase a reader can drop into a sentence, and `walk 1, 2, 3` reads as one walk with a comma in
    its number. Three shapes cover every case and the plural/singular split is the only decision.
    """
    numbers = [int(item) for item in ordinals if int(item or 0) > 0]
    if not numbers:
        return ""
    if len(numbers) == 1:
        return "walk %d" % numbers[0]
    if len(numbers) == 2:
        return "walks %d and %d" % (numbers[0], numbers[1])
    return "walks %s and %d" % (", ".join(str(item) for item in numbers[:-1]), numbers[-1])


def latest_refusals(root: Path) -> Dict[str, Any]:
    """Which promises the most recent walk refused, if any (round 108).

    The history answers "how often was this promise refused"; a reader also wants "what did the last walk hit", because
    that is the state of the queue as somebody left it. Kept apart from `refusal_history` so neither has to guess the
    other's shape.
    """
    held = queue_runs(root)
    runs = [run for run in held["runs"] if isinstance(run, Mapping)]
    if not runs:
        return {"walk": 0, "walks": 0, "at": "", "versions": []}
    run = runs[-1]
    versions = sorted({int(item.get("version") or 0) for item in run.get("attempted") or []
                       if isinstance(item, Mapping) and int(item.get("code") or 0) == 2})
    return {"walk": len(runs), "walks": len(runs), "at": str(run.get("at") or ""),
            "versions": [version for version in versions if version]}


def _blocked_reason(root: Path, version: int) -> str:
    """Why the ledger refuses this version, in words a reader can act on (round 105).

    `code == 2` says a build defect stopped the walk; it does not say which record's book is the contradictory one.
    That sentence already exists in `provenance_block`, and a page printing only the code sends the reader looking
    for a fault that names no file.
    """
    try:
        block = provenance_block(root, version)
    except (OSError, ValueError):
        return ""
    return str(block[1]) if block else ""



def blocked_pairs(root: Path, version: int) -> List[Dict[str, str]]:
    """The link pairs that refuse this version, as the arguments of a command (round 112).

    Round 111's record said every available repair changed nothing. The reason was that the refusing book is a
    contradiction, and the only repair commands edit a legacy end or a record's path. This returns the pairs an
    operator would have to decide, so `repair_plan` can name `--settle-link` instead of a diagnostic.
    """
    rounds = history(root, include_retired=True)
    decided = active_settlements(load_settlements(root))
    produced = set()
    for record in rounds:
        applied = record.get("applied")
        if not isinstance(applied, list):
            continue
        for label in applied:
            digits = "".join(character for character in str(label) if character.isdigit())
            if digits and int(digits) == int(version):
                produced.add(str(record.get("round") or ""))
    pairs: List[Dict[str, str]] = []
    for item in replacement_links(rounds, decided):
        if item.get("kind") not in ("mismatch", "dangling"):
            continue
        claimant = str(item.get("claimant") or "")
        claimed = str(item.get("claimed") or "")
        if claimant and claimed and claimant in produced:
            pair = {"record": claimant, "successor": claimed}
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def queue_history(root: Path) -> Dict[str, Any]:
    """What the recorded walks add up to, for a page that should show a trend rather than a last line (round 104).

    The store keeps a window, so the totals are the totals of that window and `trimmed` says how much of the older
    history the window no longer represents. A trend printed without that number would be a claim about all of time.
    """
    held = queue_runs(root)
    runs = held["runs"]
    measured = sum(int(run.get("measured") or 0) for run in runs)
    cleared = sum(int(run.get("cleared") or 0) for run in runs)
    skipped: Dict[str, int] = {}
    for run in runs:
        for path in run.get("skipped_origins") or []:
            skipped[str(path)] = skipped.get(str(path), 0) + 1
    stopped = sum(1 for run in runs if int(run.get("worst") or 0) == 2 or int(run.get("stopped_at") or 0))
    return {
        "runs": len(runs),
        "measured": measured,
        "cleared": cleared,
        "stopped": stopped,
        "skipped": sum(int(run.get("skipped") or 0) for run in runs),
        "last_at": str(runs[-1].get("at") or "") if runs else "",
        "trimmed": int(held.get("trimmed") or 0),
        "mean_measured": (measured / len(runs)) if runs else 0.0,
        "skipped_origins": dict(sorted(skipped.items())),
    }


def origin_queue(root: Path) -> List[Dict[str, Any]]:
    """The promises never measured where they were made, in the order a reader should act on them (round 101).

    Round 100 could count this queue and name the first command; the run itself was still a copy-and-paste of a
    version label and a file path, and a hand-copied path is how a check ends up measuring somewhere the promise
    was never made. One function stands behind the page, the JSON payload and `--verify --from-queue`, so the
    command a reader is given and the command a script runs are the same list.

    Order is by version, which is the order the records were written: an older promise has stood longer with its
    evidence somewhere else, and the queue is a backlog, not a ranking.
    """
    return list(never_tested_at_origin(repaired_paths(root))["versions"])


REPAIRS = "repairs.json"
REPAIR_FORMAT = "brian.repairs/1"
REPAIR_LIMIT = 20


def _plan_totals(plan: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The two numbers a repair is judged by: what the refusals cost, and how many files owed it."""
    rows = [row for row in plan if isinstance(row, Mapping)]
    return {"files": len(rows),
            "cost": sum(int(row.get("cost") or 0) for row in rows),
            "held": sum(int(row.get("held") or 0) for row in rows),
            "walks_blocked": sum(int(row.get("walks_blocked") or 0) for row in rows)}


def record_repair(root: Path, action: str, before: Sequence[Mapping[str, Any]],
                  after: Sequence[Mapping[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Write down what one repair changed about the repair plan (round 111).

    Round 110 ranked the books to repair; nothing said whether running the command helped. The plan is recomputed
    from the ledger every time, so it cannot answer that on its own -- the comparison has to be captured at the
    moment the repair happens, which is the only time both readings exist (boundary 248).
    """
    moment = now if isinstance(now, datetime) else _stamp_moment(now)
    rows_before = _plan_totals(before)
    rows_after = _plan_totals(after)
    freed = sorted({str(row.get("origin") or "") for row in before if isinstance(row, Mapping)}
                   - {str(row.get("origin") or "") for row in after if isinstance(row, Mapping)})
    entry = {"at": moment.isoformat(), "action": str(action),
             "before": rows_before, "after": rows_after, "freed": freed,
             "cost_fell": rows_before["cost"] - rows_after["cost"]}
    path = _evolve_dir(root) / REPAIRS
    held = {"format": REPAIR_FORMAT, "runs": [], "trimmed": 0}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    if isinstance(stored, Mapping):
        held["runs"] = [dict(item) for item in stored.get("runs") or [] if isinstance(item, Mapping)]
        held["trimmed"] = int(stored.get("trimmed") or 0)
    held["runs"].append(entry)
    if len(held["runs"]) > REPAIR_LIMIT:
        held["trimmed"] += len(held["runs"]) - REPAIR_LIMIT
        held["runs"] = held["runs"][-REPAIR_LIMIT:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(held, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return entry


def repair_history(root: Path) -> Dict[str, Any]:
    """The repairs the store still remembers, newest last (round 111). Windowed, like every other history here."""
    path = _evolve_dir(root) / REPAIRS
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"format": REPAIR_FORMAT, "runs": [], "trimmed": 0, "last": {}}
    runs = [dict(item) for item in (stored.get("runs") or []) if isinstance(item, Mapping)] \
        if isinstance(stored, Mapping) else []
    return {"format": REPAIR_FORMAT, "runs": runs, "trimmed": int(stored.get("trimmed") or 0) if
            isinstance(stored, Mapping) else 0, "last": runs[-1] if runs else {}}


def blocked_dossier(root: Path) -> Dict[str, Dict[str, Any]]:
    """Which origin file stands behind the promises the ledger keeps refusing, and how often (round 105).

    Round 104 made a blocked promise cheap to skip; the reader's next question is where the fault lives. Blocked
    versions group by the file the promise was born with, not by its current path: the refusal is about the record,
    and the origin is what a reader has to open to repair it.
    """
    blocked = blocked_versions(root)
    if not blocked:
        return {}
    origins = {int(row.get("version") or 0): str(row.get("first_from") or "")
               for row in repaired_paths(root)}
    attempts: Dict[int, int] = {}
    for run in queue_runs(root)["runs"]:
        for item in run.get("attempted") or []:
            if isinstance(item, Mapping) and int(item.get("code") or 0) == 2:
                version = int(item.get("version") or 0)
                attempts[version] = attempts.get(version, 0) + 1
    history = refusal_history(root)
    dossier: Dict[str, Dict[str, Any]] = {}
    for version, row in blocked.items():
        origin = origins.get(version, "") or "(unnamed)"
        entry = dossier.setdefault(origin, {"origin": origin, "records": [], "attempts": 0, "last_at": "",
                                            "reasons": [], "pairs": []})
        entry["records"].append(version)
        # A refusal recorded before round 104 has no attempt list, but it was refused at least once.
        tries = attempts.get(version, 1)
        entry["attempts"] += tries
        if tries > 1:
            # Round 106: "this file was blocked three times" and "three promises here were each blocked once" are
            # different faults on the same file, and one total reads them the same.
            entry.setdefault("retried", []).append(version)
        entry["last_at"] = max(str(entry.get("last_at") or ""), str(row.get("at") or ""))
        reason = str(row.get("why") or "")
        if reason and reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        for pair in row.get("pairs") or []:
            if isinstance(pair, Mapping) and pair not in entry["pairs"]:
                entry["pairs"].append(dict(pair))
    for entry in dossier.values():
        entry["records"] = sorted(entry["records"])
        entry["reasons"] = sorted(entry["reasons"])
        # Round 108: how many of the recorded walks refused any promise born with this file, all its promises
        # together. The per-promise count lives in `refusal_history`; this is the file's bill.
        entry["refused"] = sum(int(history.get(version, {}).get("count") or 0) for version in entry["records"])
        entry["last_walk"] = max((int(history.get(version, {}).get("last_walk") or 0)
                                  for version in entry["records"]), default=0)
        entry["first_walk"] = min((int(history.get(version, {}).get("first_walk") or 0)
                                   for version in entry["records"]), default=0)
        # The window the ordinals are counted in: "walk 3" means nothing without "of 4".
        entry["walks"] = max((int(history.get(version, {}).get("walks") or 0)
                              for version in entry["records"]), default=0)
        # Round 109: which walks refused any promise born with this file, so the bill and the per-promise history
        # cite the same walks instead of two counts that cannot be joined (boundary 244).
        entry["refused_walks"] = sorted({int(ordinal)
                                         for version in entry["records"]
                                         for ordinal in history.get(version, {}).get("refused_walks") or []})
    return dict(sorted(dossier.items()))



def _settle_command(pairs: Sequence[Mapping[str, Any]]) -> str:
    """The command that would clear one refusal, or the diagnostic when nobody knows which pair to decide.

    Round 125: a pair whose decision was withdrawn cannot be settled again -- `--settle-link` refuses it, because
    recording it a second time is a new decision. Naming that refused command would hand the reader an action that
    cannot run (boundary 261), so round 125's remedy said the command for a new decision did not exist.

    Round 126 built it (`--settle-restore`), so the remedy names it: the same rule that stopped the plan from
    printing a command which refuses now stops it from printing "there is no command" about one that exists. A
    withdrawn pair still names the restore command even if another pair on the same file could be settled, because
    the restore is the only route for that pair and the settle commond would leave it behind.
    """
    withdrawn = [pair for pair in pairs if isinstance(pair, Mapping) and pair.get("withdrawn")]
    for pair in withdrawn:
        record = str(pair.get("record") or "")
        successor = str(pair.get("successor") or "")
        if record and successor:
            return "brian evolve --settle-restore %s=%s --settle-why TEXT --settle-by NAME" % (record, successor)
    for pair in pairs:
        if not isinstance(pair, Mapping):
            continue
        record = str(pair.get("record") or "")
        successor = str(pair.get("successor") or "")
        if record and successor:
            return "brian evolve --settle-link %s=%s" % (record, successor)
    return "brian evolve --audit"


def repair_plan(root: Path) -> List[Dict[str, Any]]:
    """Which origin file to repair first, and what its refusal has cost so far (round 110).

    `blocked_dossier` says which file stands behind the refusals; it does not say which one to open first. Two numbers
    decide that, and both are facts the store already holds: how many recorded walks the queue spent being refused
    (`refused_walks`), and how many promises are still waiting behind the file (records that never got a verdict where
    they were born). Their product is the cost -- a file that blocked one promise for one walk is cheap, a file that
    blocked three promises across five walks is not, and a single "2 refusals" reads them the same.

    Ranking is by cost, then by walks blocked, then by name, so the order is deterministic: a page that reshuffles
    between two runs teaches the reader not to trust it.
    """
    dossier = blocked_dossier(root)
    if not dossier:
        return []
    withdrawn_pairs = {(str(item.get("record") or ""), str(item.get("successor") or ""))
                       for item in load_settlements(root) if is_withdrawn(item)}
    waiting = {int(row.get("version") or 0) for row in origin_queue(root)}
    plan: List[Dict[str, Any]] = []
    for origin, entry in dossier.items():
        versions = sorted(int(item) for item in entry.get("records") or [])
        walks_blocked = len(entry.get("refused_walks") or [])
        held = len([version for version in versions if version in waiting])
        plan.append({
            "origin": origin,
            "records": versions,
            "walks_blocked": walks_blocked,
            "walks": int(entry.get("walks") or 0),
            "last_walk": int(entry.get("last_walk") or 0),
            "held": held,
            # Named, not implied: the walks the queue already spent, times the promises still queued behind the file.
            "cost": walks_blocked * max(1, held),
            # Round 112: the refusing book has a command now. `--audit` only diagnoses, and a plan whose remedy
            # cannot clear anything is a plan that ranks work nobody can do (boundary 250).
            "pairs": [dict(pair, withdrawn=(str(pair.get("record") or ""),
                                            str(pair.get("successor") or "")) in withdrawn_pairs)
                      for pair in entry.get("pairs") or [] if isinstance(pair, Mapping)],
            "remedy": _settle_command([dict(pair, withdrawn=(str(pair.get("record") or ""),
                                                             str(pair.get("successor") or "")) in withdrawn_pairs)
                                       for pair in entry.get("pairs") or [] if isinstance(pair, Mapping)]),
        })
    plan.sort(key=lambda row: (-int(row["cost"]), -int(row["walks_blocked"]), str(row["origin"])))
    return plan


def repaired_paths(root: Path) -> List[Dict[str, Any]]:
    """Versions whose measured file has been rewritten by hand, with every move (round 94).

    Round 93's `--set-stream-path` repairs a record whose file is gone. The repair leaves the promise pointing at a
    file it was not born with, and that is a fact a reader has to see: a record that says "measured on X" after a
    hand edit means "measured on something, then told to say X". The same reasoning as round 83's `backfilled` end
    -- an edited record must name itself as edited, or a repair reads as original evidence (boundary 174, applied to
    a field). Every move is returned rather than the first and last, because "moved once" and "moved three times,
    ending here" read the same from a single pair (boundary 204).
    """
    rows: List[Dict[str, Any]] = []
    deployed = {int(version) for _slug, version in in_force_promotions(root)}
    directory = _evolve_dir(root) / VERSIONS
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("v*.json")):
        match = re.fullmatch(r"v(\d+)\.json", path.name)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        history = payload.get("stream_path_history")
        if not isinstance(history, list) or not history:
            continue
        moves = [dict(item) for item in history if isinstance(item, Mapping)]
        if not moves:
            continue
        version = int(match.group(1))
        # Round 95: a verdict is about a location. Count the checks this record holds that were taken after a
        # repair moved it, so the campaign can say how much of the evidence describes a moved target.
        checks = [payload.get("verification")] + list(payload.get("checks") or [])
        after = sum(1 for item in checks
                    if isinstance(item, Mapping) and int(item.get("stream_path_moves") or 0) > 0
                    and not item.get("on_original_path"))
        origin = str(moves[0].get("from") or "")
        # Round 97: a move back to the original file ends the trip. The record is then in place again, while the
        # evidence it holds may still describe the substitute -- two facts that must not be folded into one.
        returned = len(moves) > 1 and str(moves[-1].get("to") or "") == origin
        returned_at = str(moves[-1].get("at") or "") if returned else ""
        on_original = [str(item.get("checked_at") or "") for item in checks
                       if isinstance(item, Mapping) and item.get("on_original_path")]
        newest = max(on_original) if on_original else ""
        checked_after_return = newest if returned and newest and newest >= returned_at else ""
        rows.append({"version": version,
                     "moves": moves,
                     "first_from": origin,
                     "to": str(payload.get("stream_path") or moves[-1].get("to") or ""),
                     "at": str(moves[-1].get("at") or ""),
                     # Round 96: a repair can stop being necessary. When the file the promise was originally
                     # measured on is back in this checkout, the record can be pointed at it again and measured
                     # there -- a fact an operator can act on, so the pages say it (boundary 209).
                     "origin_exists": bool(origin) and (root / origin).is_file(),
                     "returned": returned,
                     "returned_at": returned_at,
                     "checked_after_return": checked_after_return,
                     "checks_on_repair": after,
                     # Round 98: the verdicts this record holds, split by where they were taken. The line says
                     # "N verdict(s) on file" and not "N checks", because the two differ once a re-check was
                     # deduplicated (round 28).
                     "evidence": evidence_split(verification_evidence(payload), origin),
                     # Round 99: "measured after the return" and "measured there for the first time" are two
                     # facts. A record whose verdicts were all taken elsewhere has never had its promise tested
                     # where it was made, so the page must not read one against the others as if it had.
                     "first_on_original": (returned and len(on_original) == 1),
                     "in_force": version in deployed})
    return rows
