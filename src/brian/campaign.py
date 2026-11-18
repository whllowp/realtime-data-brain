"""The aggregate view of a campaign: what the rounds actually bought, per domain.

`brian evolve` writes one record per round, which is the right granularity for a decision and the wrong
one for a question like "has anything improved". The survey's own open problem calls this scientific
amnesia: behaviour improves across campaigns while no methodological knowledge accumulates, because
nobody aggregates. This module reads the ledger, the version files and the deployment state and reports
the campaign instead of the round.

It is deliberately read-only: no Laya call, no measurement. Every number it prints was recorded by a
round that measured it, so the report cannot flatter the loop with a fresh measurement it chose itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .evolve import (REFUSAL_EXPIRY_DAYS, EvolveRules, audit, history, rejection_provenance,
                     withheld_due, withheld_evidence_age, withheld_records, withheld_staleness)
from .promote import (CLAIM_EXPIRY_DAYS, UNMEASURABLE, VERIFIED, current_state,
                      load_promotions, shape_note)

__all__ = ["Campaign", "CampaignRow", "campaign", "format_campaign", "span_text"]


@dataclass
class CampaignRow:
    """One domain's share of the campaign."""

    domain: str
    rounds: int = 0
    trials: int = 0
    accepts: Dict[str, int] = field(default_factory=dict)
    rejections: Dict[str, int] = field(default_factory=dict)
    first: str = ""
    last: str = ""
    in_force: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    claim: Dict[str, Any] = field(default_factory=dict)
    verification: Optional[str] = None
    reverted: bool = False
    # How the claim's re-checks ended: the verdict of the last one, and how many came before it.
    verdict: Optional[str] = None
    checks: int = 0
    # A verdict older than the expiry is still the record but no longer the evidence (round 29).
    overdue: bool = False
    # This domain's share of the audit (round 34). The header line is the whole ledger's number; a row
    # that shows rounds and trials but no audit says "this stream is clean" without having looked.
    audit_moved: int = 0
    audit_relabelled: int = 0
    audit_unreconstructable: int = 0
    # Rounds on this stream that a re-measurement superseded (round 34), and the records that replaced
    # them (round 35). Held per row because "2 rounds were retired" is a ledger-wide number on a page
    # where every other number names a stream: the reader deciding about SMD should not wonder whether
    # the retirement concerned SMD.
    retired: int = 0
    successors: List[str] = field(default_factory=list)
    # This domain's share of the refusals the loop still holds, and how many of them nobody re-checked
    # (round 36). The header line is the ledger's number; the row is what a reader of SMD decides with.
    refusals: int = 0
    refusals_retired: int = 0
    # Candidate measurements the mechanism never proposed on this stream because the ledger had already
    # refused them there at this sampling (round 38). A saving is only a saving if it accumulates
    # somewhere; a round's own log line is not that place.
    skipped: int = 0
    # Refusals this stream wrote down as records rather than sentences (round 42), and how many of them no
    # longer hold at today's bar. The second number is why the first is worth keeping: a refusal that only
    # exists inside a round's prose cannot be found again, let alone re-judged.
    withheld: int = 0
    withheld_stale: int = 0
    # Round 44: how many of those refusals have not been measured again within the horizon, and how old the
    # oldest evidence on this stream is. Without both, a reader cannot tell a refusal that was re-checked
    # last week from one that has stood since the day it was written.
    withheld_due: int = 0
    withheld_oldest_days: Optional[float] = None
    # Round 48: which stream files this domain's rounds were measured on, as rounds per file. The row is
    # grouped by dataset because the deployment state is per dataset, but a dataset is not a file: `SMD` is
    # two recordings sampled differently here, so a row that added both up would answer two questions with
    # one total and never say so.
    files: Dict[str, int] = field(default_factory=dict)

    @property
    def accepted(self) -> int:
        return sum(self.accepts.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "rounds": self.rounds,
            "trials": self.trials,
            "accepts": dict(sorted(self.accepts.items())),
            "rejections": dict(sorted(self.rejections.items())),
            "first": self.first,
            "last": self.last,
            "in_force": self.in_force,
            "env": dict(sorted(self.env.items())),
            "claim": dict(self.claim),
            "verification": self.verification,
            "reverted": self.reverted,
            "verdict": self.verdict,
            "checks": self.checks,
            "overdue": self.overdue,
            "audit_moved": self.audit_moved,
            "audit_relabelled": self.audit_relabelled,
            "audit_unreconstructable": self.audit_unreconstructable,
            "rounds_retired": self.retired,
            "re_measured_as": list(self.successors),
            "refusals_carried": self.refusals,
            "refusals_from_retired": self.refusals_retired,
            "measurements_not_repeated": self.skipped,
            "refusals_withheld": self.withheld,
            "withheld_no_longer_holding": self.withheld_stale,
            "withheld_due_for_check": self.withheld_due,
            "withheld_oldest_evidence_days": self.withheld_oldest_days,
            "stream_files": dict(sorted(self.files.items())),
        }


@dataclass
class Campaign:
    """The whole ledger, summarised. Every field was recorded; none was measured here."""

    rows: List[CampaignRow] = field(default_factory=list)
    rounds: int = 0
    trials: int = 0
    accepted: int = 0
    rejected: int = 0
    moved: int = 0
    relabelled: int = 0
    unsourced: int = 0
    applied: int = 0
    reverted: int = 0
    holding: int = 0
    not_comparable: int = 0
    never_checked: int = 0
    # Of the verdicts above, the ones older than the expiry: still recorded, no longer evidence.
    overdue: int = 0
    # Comparisons the audit could not reconstruct: not a moved verdict and not an unchanged one, no
    # answer at all (round 32). Counted separately so a campaign cannot read "nothing moves" about a
    # ledger whose comparison is missing.
    unreconstructable: int = 0
    # Of those, the ones in records that carry the geometry and still lack the reference: a defect in this
    # build rather than a record written before the field existed (round 33).
    inconsistent: int = 0
    # Rounds a re-measurement superseded: still in the ledger, no longer re-decided (round 34).
    retired: int = 0
    # Refusals the loop still holds: the count `previously_rejected` would return, and how many of them
    # come from a retired record — a refusal nobody re-checked (round 35). The second number is printed
    # next to the first because "23 candidates are settled" reads as verified until you ask who settled
    # them.
    refusals: int = 0
    refusals_retired: int = 0
    # Candidate measurements the mechanism never proposed because the ledger had already refused them on
    # that stream at that sampling (round 38). Reported as a count, never as tokens: what it saves is a
    # slot in a capped proposal, not a run.
    skipped: int = 0
    # Refusals written down as `w###` records (round 42), and how many of them the bar has since outgrown.
    # A version page says what shipped; without these two numbers the page says nothing about what was
    # measured and deliberately not shipped.
    withheld: int = 0
    withheld_stale: int = 0
    # Round 44: refusals whose evidence is older than the horizon, and the age of the oldest evidence on the
    # page. A refusal nobody re-measured is a conclusion the loop stopped testing without saying so.
    withheld_due: int = 0
    withheld_oldest_days: Optional[float] = None
    # How many domains add up rounds measured on more than one stream file (round 48): the rows stay per
    # dataset, so this is the page saying which rows are pooling two samplings into one total.
    pooled: int = 0
    # Rounds that do not say which file they measured (round 49). The per-row detail carries the count, but
    # the header is what a reader sees first: "54 rounds" reads as fully attributable until the page says
    # how much of it cannot be placed on a sampling.
    unattributable: int = 0
    # Round 50 splits that count by who is responsible: a record written before the field existed is history
    # nobody can back-fill (an invented measurement), while a record this build wrote and left without a file
    # is a defect here -- `--strict` charges the second kind and only warns about the first (boundary 73).
    unattributable_legacy: int = 0
    unattributable_missing: int = 0

    @property
    def unattributable_share(self) -> float:
        return (float(self.unattributable) / float(self.rounds)) if self.rounds else 0.0

    @property
    def domains(self) -> List[str]:
        return [row.domain for row in self.rows]
    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "trials": self.trials,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "verdicts_moved": self.moved,
            "verdicts_relabelled": self.relabelled,
            "rounds_without_stream": self.unsourced,
            "rounds_applied": self.applied,
            "versions_reverted": self.reverted,
            "versions_holding": self.holding,
            "versions_not_comparable": self.not_comparable,
            "versions_never_checked": self.never_checked,
            "versions_overdue": self.overdue,
            "comparisons_unreconstructable": self.unreconstructable,
            "comparisons_inconsistent": self.inconsistent,
            "rounds_retired": self.retired,
            "refusals_carried": self.refusals,
            "refusals_from_retired": self.refusals_retired,
            "measurements_not_repeated": self.skipped,
            "refusals_withheld": self.withheld,
            "withheld_no_longer_holding": self.withheld_stale,
            "withheld_due_for_check": self.withheld_due,
            "withheld_oldest_evidence_days": self.withheld_oldest_days,
            "domains_pooling_files": self.pooled,
            "rounds_without_recorded_file": self.unattributable,
            "rounds_without_file_legacy": self.unattributable_legacy,
            "rounds_without_file_this_build": self.unattributable_missing,
            "domains": [row.to_dict() for row in self.rows],
        }


def _source(record: Dict[str, Any]) -> Optional[str]:
    stream = record.get("stream") or {}
    source = stream.get("source")
    return None if source in (None, "") else str(source)


def campaign(root: Path, rules: Optional[EvolveRules] = None,
             expiry_days: float = CLAIM_EXPIRY_DAYS, now: Optional[datetime] = None,
             refusal_expiry_days: float = REFUSAL_EXPIRY_DAYS) -> Campaign:
    """Summarise every recorded round, grouped by the stream each one measured.

    A round that does not say which stream it measured cannot be attributed to a domain, and is counted
    separately rather than guessed at: the stream field was added in round 3, so early rounds are
    legitimately unknown.
    """
    rounds = history(root)
    everything = history(root, include_retired=True)
    # Counted from the same file: a retired round is one the campaign still holds but no longer asks the
    # checks to answer for, and hiding it would make "42 rounds" and "40 audited" look like a defect.
    result = Campaign(rounds=len(rounds), retired=max(0, len(everything) - len(rounds)))
    rows: Dict[str, CampaignRow] = {}

    for record in rounds:
        source = _source(record)
        if source is None:
            result.unsourced += 1
            # Rounds written before the stream field existed still carry verdicts, and dropping their
            # numbers silently would make the campaign total disagree with the ledger it claims to
            # summarise. They are grouped under a name no domain can collide with instead.
            source = "(unattributed)"
        row = rows.setdefault(source, CampaignRow(domain=source))
        row.rounds += 1
        # Round 48: which file this round measured, as recorded. A round written before round 47 says
        # nothing, and "(not recorded)" is the honest name for that -- inventing a path from the dataset
        # name is what rounds 46 and 47 each refused to do.
        #
        # Round 54: one file has one name. A record written on Windows says `data\smd\stream-check.json`
        # while a record written by `--stream-file data/smd/stream-check.json` says the same path with
        # forward slashes, and a row that counts them separately reports "3 files here were sampled
        # differently" about two files (the drain produced exactly that: 9 + 6 + 1 rounds over the same
        # two SMD streams). The name on the page is the posix spelling, because that is the spelling a
        # reader can paste into a command.
        path = str(record.get("stream_path") or "").replace("\\", "/")
        row.files[path or "(not recorded)"] = row.files.get(path or "(not recorded)", 0) + 1
        if not path:
            # Counted here rather than derived from the rows later: a round that predates the field is
            # unattributable, and the header says how much of the page that is (round 49).
            result.unattributable += 1
            # Round 50: the key's presence is the seam. `persist` has written `stream_path` since round 47,
            # so a record with the key measured on no file at all (the demo stream), while a record without
            # it was written by a build that could not tell -- history, not a defect in this one.
            if "stream_path" in record:
                result.unattributable_missing += 1
            else:
                result.unattributable_legacy += 1
        finished = record.get("finished_at") or record.get("started_at") or ""
        if not row.first or finished < row.first:
            row.first = finished
        if finished > row.last:
            row.last = finished
        for trial in record.get("trials") or []:
            verdict = trial.get("verdict") or {}
            if not verdict:
                continue
            row.trials += 1
            rule = str(verdict.get("rule") or "?")
            if verdict.get("accepted"):
                row.accepts[rule] = row.accepts.get(rule, 0) + 1
            else:
                row.rejections[rule] = row.rejections.get(rule, 0) + 1
        if record.get("applied"):
            result.applied += 1
        # Round 38: the mechanism's own count of candidates it did not propose. A round is one measurement
        # record; the saving is only visible once the records are added up, which is what this page is for.
        skipped = int((record.get("proposal") or {}).get("refused_skipped") or 0)
        if skipped:
            row.skipped += skipped
            result.skipped += skipped

    # Retired rounds are attributed to their stream too (round 35). They carry no trials in the counts
    # above — the successor measured those questions again — but a domain page that said nothing about
    # them would leave a reader unable to tell "this stream was never re-measured" from "it was, and the
    # old record is still here".
    for record in everything:
        retirement = record.get("retired")
        if not retirement:
            continue
        source = _source(record) or "(unattributed)"
        row = rows.setdefault(source, CampaignRow(domain=source))
        row.retired += 1
        successor = retirement.get("re_measured_as") if isinstance(retirement, dict) else None
        if successor and str(successor) not in row.successors:
            row.successors.append(str(successor))

    active = rules if rules is not None else EvolveRules()
    # Round 42: refusals written down as records. They carry no trials in the counts above — a refusal is
    # not an acceptance — but they are the half of the ledger a version page cannot show, and the stale
    # count is what makes them worth reading again.
    for record in withheld_records(root):
        source = str((record.stream or {}).get("source") or "(unattributed)")
        row = rows.setdefault(source, CampaignRow(domain=source))
        row.withheld += 1
        result.withheld += 1
        if not withheld_staleness(record, active)["still"]:
            row.withheld_stale += 1
            result.withheld_stale += 1
        # Round 44: an objection nobody re-measured is the one kind of refusal a reader cannot price. Both
        # the count and the oldest age are printed, because "1 due" reads as recent until the age is next to
        # it, and a 200-day-old check is a different claim from a 15-day-old one.
        age = withheld_evidence_age(record, now)
        if age is not None:
            oldest = row.withheld_oldest_days
            if oldest is None or age > oldest:
                row.withheld_oldest_days = age
            oldest = result.withheld_oldest_days
            if oldest is None or age > oldest:
                result.withheld_oldest_days = age
        if withheld_due([record], now, refusal_expiry_days):
            row.withheld_due += 1
            result.withheld_due += 1

    for promotion in load_promotions(root):
        if promotion.reverted_at:
            # Counted before the source filter: an early version carries no stream field, and it was still
            # retracted. Counting it after the filter reported the campaign as having retracted nothing.
            result.reverted += 1
        source = (promotion.stream or {}).get("source")
        if source in (None, ""):
            continue
        row = rows.setdefault(str(source), CampaignRow(domain=str(source)))
        state = current_state(root, str(source))
        owned = [name for name, version in (state.get("sources") or {}).items()
                 if version == str(promotion.version)]
        if owned:
            row.in_force = promotion.version
            row.env = {name: (state.get("env") or {})[name] for name in owned}
            row.claim = promotion.claim.to_dict() if promotion.claim else {}
            row.verification = promotion.verification
            row.reverted = bool(promotion.reverted_at)
            row.verdict = str((promotion.verification or {}).get("verdict") or "") or None
            row.checks = len(promotion.checks)
            row.overdue = promotion.overdue(now, expiry_days)
            if row.overdue:
                result.overdue += 1
            if row.verdict == VERIFIED:
                result.holding += 1
            elif row.verdict == UNMEASURABLE:
                result.not_comparable += 1
            elif row.verdict is None:
                result.never_checked += 1

    result.rows = sorted(rows.values(), key=lambda item: (-item.rounds, item.domain))
    # A row that pooled two files answers two questions with one total. Counted here rather than per line so
    # the header can say how many rows a reader has to look at twice (round 48).
    result.pooled = sum(1 for row in result.rows
                        if len({path for path in row.files if path != "(not recorded)"}) > 1)
    result.trials = sum(row.trials for row in result.rows)
    result.accepted = sum(row.accepted for row in result.rows)
    result.rejected = sum(sum(row.rejections.values()) for row in result.rows)
    report = audit(root, rules)
    result.moved = report.moved
    result.relabelled = report.relabelled
    result.unreconstructable = report.cross
    result.inconsistent = report.inconsistent
    # Attribute the audit to the row it belongs to. `audit()` names the stream of each round, and a
    # campaign whose every other number is per domain had one that was not: a reader comparing two
    # streams could see "7 verdicts would change today" and not know which one owes them.
    for entry in report.entries:
        row = rows.get(str(entry.get("stream") or "?"))
        if row is None:
            continue
        row.audit_moved += len(entry.get("changed") or [])
        row.audit_relabelled += len(entry.get("relabelled") or [])
        row.audit_unreconstructable += len(entry.get("cross") or [])
    # What the loop still holds as settled, and how much of that it inherited (round 35). The rejected
    # column above counts verdicts the rounds recorded; this counts the candidates that stay refused
    # after the latest verdict and today's rules, which is the number the *next* round acts on.
    provenance = rejection_provenance(root, rules)
    result.refusals = sum(1 for verdict in provenance.values() if not verdict["accepted"])
    result.refusals_retired = sum(1 for verdict in provenance.values()
                                  if not verdict["accepted"] and verdict["retired"])
    # Attributed to the stream that made the refusal, for the same reason the audit is: the header's
    # number is the ledger's, and a domain page that showed none of it would read as "nothing settled
    # here" (round 36).
    for verdict in provenance.values():
        if verdict["accepted"]:
            continue
        row = rows.get(str(verdict.get("source") or "?"))
        if row is None:
            continue
        row.refusals += 1
        if verdict["retired"]:
            row.refusals_retired += 1
    return result


def _claim_line(claim: Dict[str, Any]) -> str:
    if not claim:
        return "no claim recorded"
    bits = []
    if claim.get("recall_at_budget") is not None:
        bits.append("recall %.3f at the %.0f%% budget" % (claim["recall_at_budget"],
                                                          100 * (claim.get("fpr_budget") or 0.10)))
    if claim.get("tokens_per_report") is not None:
        bits.append("%.0f tokens/report" % claim["tokens_per_report"])
    return ", ".join(bits) or "no claim recorded"


def _verdict_line(row: CampaignRow) -> str:
    """`holds`, `not comparable`, `failed`, or `unverified` — the three-outcome verify, summarised."""
    if row.verdict == VERIFIED:
        note = "holds"
    elif row.verdict == UNMEASURABLE:
        note = "not comparable"
    elif row.verdict:
        note = row.verdict
    else:
        note = "unverified"
    if row.overdue:
        note = "%s (overdue)" % note
    if row.checks:
        note = "%s, %d earlier check%s" % (note, row.checks, "" if row.checks == 1 else "s")
    return note


def span_text(days: float) -> str:
    """How long a trend window was, in a unit a reader can hold.

    Round 56: the gate charged a rise and printed "in the last 0.0 hour(s)" for a window a few seconds long.
    A span rendered as zero reads as no span at all, and a charge about "no time" is not something a reader
    can argue with -- the same reason a verdict that could not be reached must say so (boundary 53).
    """
    seconds = max(0.0, float(days or 0.0)) * 86400.0
    if seconds >= 86400.0:
        return "%.0f day(s)" % (seconds / 86400.0)
    if seconds >= 3600.0:
        return "%.1f hour(s)" % (seconds / 3600.0)
    if seconds >= 60.0:
        return "%.0f minute(s)" % (seconds / 60.0)
    return "%.0f second(s)" % seconds


def _trend_line(trend: Mapping[str, Any]) -> str:
    """One line saying whether the unrecorded share is falling, and how fast when it can be known.

    Round 55: the campaign printed how much of the page cannot be attributed and how much of it a queue
    could repay, but never whether the number had moved. Movement is recomputed from the ledger's arrival
    and retirement events, so it cannot flatter the loop with a separate progress file.

    A rate needs at least a day of history: a drain that finished inside a minute says what moved, not how
    fast, and printing points-per-day from it would turn one afternoon into a promise (round 40's lesson
    about a tolerance being an assumption, applied to time).
    """
    points = float(trend.get("points") or 0.0)
    share = 100.0 * float(trend.get("share") or 0.0)
    before = 100.0 * float(trend.get("before_share") or 0.0)
    days = float(trend.get("days") or 0.0)
    span = span_text(days)
    window = ("since the first recorded round %s ago" % span if trend.get("truncated")
              else "in the last %s" % span)
    if points > 0.005:
        line = ("trend             the unrecorded share fell %.0f point(s) %s (%.0f%% -> %.0f%%)"
                % (points, window, before, share))
    elif points < -0.005:
        line = ("trend             the unrecorded share rose %.0f point(s) %s (%.0f%% -> %.0f%%)"
                % (-points, window, before, share))
    else:
        line = ("trend             the unrecorded share has not moved %s (%.0f%%)" % (window, share))
    if trend.get("paid") or trend.get("arrived"):
        line += (": %d round(s) were retired after a named re-measurement and %d record(s) arrived in that "
                 "window" % (int(trend.get("paid") or 0), int(trend.get("arrived") or 0)))
    if trend.get("reaches_zero"):
        line += ", so at this rate it reaches 0%% on %s" % trend.get("reaches_zero")
    elif points > 0.005 and trend.get("per_day") is None:
        line += "; the history is shorter than a day, so no rate is claimed"
    return line


def format_campaign(result: Campaign, expiry_days: float = CLAIM_EXPIRY_DAYS,
                    rules: Optional[EvolveRules] = None,
                    refusal_expiry_days: float = REFUSAL_EXPIRY_DAYS,
                    queue: Optional[Mapping[str, Any]] = None,
                    trend: Optional[Mapping[str, Any]] = None,
                    debt: Optional[Mapping[str, Any]] = None,
                    plan: Optional[Sequence[Mapping[str, Any]]] = None) -> str:
    # The rules are needed even though nothing is re-decided here: the withheld line has to name the bar it
    # is comparing the recorded loss against, and a bar read from a default nobody passed would be the
    # page lying about which assumption it used (round 42).
    active = rules if rules is not None else EvolveRules()
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - the campaign, not the round")
    lines.append("=" * 96)
    lines.append("rounds            %d  (%d candidate measurements, %d accepted, %d rejected)"
                 % (result.rounds, result.trials, result.accepted, result.rejected))
    lines.append("applied           %d round(s) wrote versions; %d version(s) were reverted"
                 % (result.applied, result.reverted))
    audit_line = ("audit             %d verdict(s) would be decided differently today, %d relabelled"
                  % (result.moved, result.relabelled))
    if result.unreconstructable:
        # Separate from both columns: "would move" and "did not move" are answers, and this is the absence
        # of one. Printing only the first two would let a ledger with missing comparisons read as settled.
        audit_line += ", %d not reconstructable" % result.unreconstructable
        if result.inconsistent:
            audit_line += " (%d of them in records that should carry the reference)" % result.inconsistent
        else:
            audit_line += " (all from records written before the geometry was recorded)"
    lines.append(audit_line)
    lines.append("verification      %d holding, %d not comparable, %d never re-checked"
                 % (result.holding, result.not_comparable, result.never_checked))
    if result.overdue:
        # A verdict from before the horizon is a record of what was true then. Saying the number out loud
        # is the difference between "we checked" and "we checked once".
        lines.append("expired           %d of them are older than %.0f day(s): the record stands, the "
                     "evidence does not" % (result.overdue, expiry_days))
    if result.retired:
        lines.append("retired           %d round(s) were re-measured; their records stay, the checks skip them"
                     % result.retired)
    if result.refusals:
        # A refusal is what the next round acts on, so it belongs on this page next to the accept counts.
        # The second number says how much of it nobody re-checked (round 35).
        line = "refusals          %d candidate(s) are still held as rejected" % result.refusals
        if result.refusals_retired:
            line += ", %d of them on retired record(s)" % result.refusals_retired
        lines.append(line)
    if result.skipped:
        # What the refusals bought, accumulated (round 38). Counted in measurements, not tokens: a dropped
        # candidate frees a slot in a capped proposal, and the slot is refilled — so this number says how
        # many questions were never asked twice, not how much compute the loop saved.
        lines.append("saving            %d candidate measurement(s) the mechanism never proposed: a stream "
                     "had already refused them" % result.skipped)
    if result.withheld:
        # Round 42: the half of the ledger a version page cannot show. Printed above the rows because the
        # second number is a question every domain page owes an answer to: which refusals no longer hold.
        # Round 44 adds the third: how many of them nobody has measured again inside the horizon.
        line = "withheld          %d candidate(s) were measured and refused for replication" % result.withheld
        if result.withheld_stale:
            line += ", %d no longer hold at today's bar (%.3f)" % (result.withheld_stale, active.replication_bar)
        if result.withheld_due:
            line += ", %d due for a re-check at the %.0f-day horizon" % (result.withheld_due,
                                                                        refusal_expiry_days)
        if result.withheld_oldest_days is not None:
            line += " (oldest evidence %.0f day(s) old)" % result.withheld_oldest_days
        lines.append(line)
    if result.unsourced:
        lines.append("unattributed      %d round(s) predate the stream field" % result.unsourced)
    if result.pooled:
        # Round 48: the rows stay per dataset, so the page has to say when a row adds up two samplings. A
        # reader comparing domains otherwise cannot tell that SMD's total answers two questions.
        lines.append("stream files      %d domain(s) add up rounds measured on more than one file: their totals "
                     "answer more than one question" % result.pooled)
    if result.unattributable:
        # Round 49: the per-row detail has said this since round 48, but the header is what a reader reads
        # first, and "54 rounds" reads as fully attributable until the page states otherwise.
        share = 100.0 * result.unattributable / result.rounds if result.rounds else 0.0
        line = ("file unknown      %d of %d round(s) do not say which file they measured, so %.0f%% of "
                "the totals above cannot be placed on a sampling"
                % (result.unattributable, result.rounds, share))
        if result.unattributable_missing:
            # Round 50: not all of it is history. This build writes the field, so a round without one is a
            # record it cannot explain -- the reader deserves to know which debt is still open.
            line += ", %d of them written by this build" % result.unattributable_missing
        lines.append(line)
    if queue is not None and (queue.get("ready") or queue.get("paid")):
        # Round 54: the share above is a debt, and a debt is only worth printing next to its repayment. The
        # numbers come from the re-run queue so the page and the command cannot disagree about the balance.
        lines.append("queue             %d round(s) can be re-run from the record; %d already re-measured; "
                     "all of them would take the share %.0f%% -> %.0f%%"
                     % (queue.get("ready") or 0, queue.get("paid") or 0,
                        100.0 * float(queue.get("share_now") or 0.0),
                        100.0 * float(queue.get("share_after") or 0.0)))
    if trend is not None:
        # Round 55: "how much is left" and "is it falling" are different questions, and the second one is
        # answered from the ledger's own events rather than from a second bookkeeping file.
        line = _trend_line(trend)
        if line:
            lines.append(line)
    if debt is not None and debt.get("ready"):
        # Round 57: the page's `file unknown` line is ledger-wide, so it cannot answer "which stream should
        # the next round measure?"; this line names the domain a re-run could actually pay down (a round
        # whose file cannot be chosen is a debt nobody can pay by re-running, so it is not counted here).
        lines.append("debt              the largest payable debt is on %s: %d of %d round(s) cannot be placed "
                     "on a sampling (%.0f%%), %d of them can be re-run from the record"
                     % (debt.get("domain") or "?", int(debt.get("without") or 0),
                        int(debt.get("rounds") or 0), 100.0 * float(debt.get("share") or 0.0),
                        int(debt.get("ready") or 0)))
    if plan is not None:
        # Round 59: "where is the debt" and "what does paying it do" are different questions, and the second
        # one is what an operator needs after `--rerun-limit 1`. The plan is the queue's own order, so the
        # second entry is what the next invocation pays -- naming it turns two runs into a visible two-step
        # instead of a table the reader has to re-sort by eye.
        rest = [row for row in plan if row.get("ready")]
        if rest:
            lines.append("measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays %s by one "
                         "round; the command prints the balance at the end"
                         % (rest[0].get("domain") or "?"))
            if len(rest) > 1:
                following = rest[1]
                lines.append("then              %s: %d of %d round(s) unrecorded (%.0f%%), %d re-runnable"
                             % (following.get("domain") or "?", int(following.get("without") or 0),
                                int(following.get("rounds") or 0),
                                100.0 * float(following.get("share") or 0.0),
                                int(following.get("ready") or 0)))
        else:
            # Nothing payable is a result, not a blank: the reader who sees no `debt` line must not conclude
            # that the page failed to compute one (boundary 100, in the other direction).
            lines.append("measure next      nothing is payable by re-running: every unrecorded round keeps no "
                         "candidate definitions or no unique file")
    lines.append("")
    if not result.rows:
        lines.append("no round recorded a stream, so there is no domain to report.")
        return "\n".join(lines)

    lines.append("%-18s %6s %6s %6s %-9s %s"
                 % ("domain", "rounds", "trials", "accept", "in force", "shipped claim"))
    lines.append("-" * 96)
    for row in result.rows:
        accepts = ", ".join("%s x%d" % (rule, count) for rule, count in sorted(row.accepts.items()))
        mark = "  (no stream recorded)" if row.domain == "(unattributed)" else ""
        lines.append("%-18s %6d %6d %6d %-9s %s%s"
                     % (row.domain, row.rounds, row.trials, row.accepted,
                        row.in_force or "-", _claim_line(row.claim), mark))
        if accepts:
            lines.append("  accepted by       %s" % accepts)
        # Round 48: the file each round measured, as recorded. Printed always -- a row that names one file is
        # also saying the other file's rounds are not in its totals.
        known = {path: count for path, count in row.files.items() if path != "(not recorded)"}
        unknown = int(row.files.get("(not recorded)") or 0)
        if known:
            named = ", ".join("%s (%d round(s))" % (path, count)
                              for path, count in sorted(known.items(), key=lambda item: (-item[1], item[0])))
            if unknown:
                named += ", %d round(s) without a recorded file" % unknown
            lines.append("  measured on       %s" % named)
            if len(known) > 1:
                # The totals above add these files up, so the page says so instead of leaving the reader to
                # discover it in `--history` (round 48).
                lines.append("  ! %d files here were sampled differently: this row's totals add up more than "
                             "one question" % len(known))
        elif unknown:
            lines.append("  measured on       %d round(s), none of which say which file they measured"
                         % unknown)
        rejects = ", ".join("%s x%d" % (rule, count) for rule, count in sorted(row.rejections.items()))
        if rejects:
            # "As recorded" is not decoration (round 36): this column counts the verdicts the rounds wrote,
            # while the `refusals` line below counts what today's rules still reject. Re-deciding moves
            # candidates between the two, and a page that printed them as the same number would let a
            # reader believe the loop still holds refusals it has since reversed.
            lines.append("  rejected by       %s (as recorded)" % rejects)
        if row.audit_moved or row.audit_relabelled or row.audit_unreconstructable:
            bits = ["%d moved" % row.audit_moved, "%d relabelled" % row.audit_relabelled]
            if row.audit_unreconstructable:
                bits.append("%d not reconstructable" % row.audit_unreconstructable)
            lines.append("  audit             %s" % ", ".join(bits))
        if row.retired:
            # Per domain, because the header's count is the ledger's: an SMD reader should not have to
            # guess whether the retired rounds concerned SMD (round 35).
            note = "  retired           %d round(s)" % row.retired
            if row.successors:
                note += ": re-measured as %s" % ", ".join(row.successors)
            lines.append(note)
        if row.refusals:
            bits = "%d held" % row.refusals
            if row.refusals_retired:
                bits += ", %d inherited from a retired record" % row.refusals_retired
            lines.append("  refusals          %s" % bits)
        if row.skipped:
            # Per domain, because the header's number is the ledger's and a reader of this row wants to
            # know what this stream's own refusals have been buying (round 38).
            lines.append("  saving            %d candidate measurement(s) not repeated"
                         % row.skipped)
        if row.withheld:
            note = "  withheld          %d candidate(s) refused for replication" % row.withheld
            if row.withheld_stale:
                note += ", %d no longer hold at today's bar" % row.withheld_stale
            if row.withheld_due:
                note += ", %d due for a re-check" % row.withheld_due
            if row.withheld_oldest_days is not None:
                note += " (oldest evidence %.0f day(s) old)" % row.withheld_oldest_days
            lines.append(note)
        if row.in_force:
            lines.append("  in force          %s  (%s)"
                         % (" ".join("%s=%s" % item for item in sorted(row.env.items()))
                            or "no variables", _verdict_line(row)))
            if row.reverted:
                lines.append("  ! this version was reverted after its claim failed")
            # Round 28: a refusal that recorded the comparable shape carries its number here too. The
            # campaign is the page a reader consults to decide what is in force, so "not comparable" alone
            # would leave them without the measurement that says how far off it is.
            shaped = shape_note(row.verification)
            if shaped:
                lines.append("  " + shaped.strip())
    lines.append("")
    lines.append("What this is: one row per stream the loop has measured, the rules that accepted or")
    lines.append("refused each candidate, and the configuration a deployment on that stream would now")
    lines.append("be running. What it is not: a claim that the campaign improved anything — every number")
    lines.append("here was measured by the round that wrote it, and the audit column is the only part")
    lines.append("that was recomputed today.")
    if result.unreconstructable:
        lines.append("Where the audit says \"not reconstructable\", it did not recompute anything: the")
        lines.append("record no longer says which reference the round compared against, so the absence")
        lines.append("of a verdict there is not the absence of a problem.")
    return "\n".join(lines)
