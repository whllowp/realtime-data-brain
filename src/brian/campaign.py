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

from .evolve import (
    frequent_retakes, retake_timeline, moved_settlements, reaffirm_plan, reanchor_plan, BACKFILL_KEY, REFUSAL_EXPIRY_DAYS, EvolveRules, audit, history, limit_drift,
                     is_withdrawn, load_settlements, rejection_provenance,
                     restored_settlements,
    unanswered_withdrawals, settlement_summary, withdrawal_plan, withdrawn_settlements, withheld_due,
                     withheld_evidence_age,
                     withheld_records, withheld_staleness)
from .promote import (CLAIM_EXPIRY_DAYS, IN_FORCE_GRACE_DAYS, UNMEASURABLE, VERIFIED,
                      current_state, domain_slug,
                      format_rerun_walks, load_promotions, never_tested_at_origin, queue_runs,
                      repair_history, repair_plan, rerun_walks, shape_note, walks_phrase)

__all__ = ["Campaign", "CampaignRow", "budget_text", "campaign", "format_campaign", "span_text"]


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
    # Round 117: this stream's share of the decisions (the row is what a reader of one stream opens, and a
    # ledger-wide "1 decision has no explanation" does not say whose it is).
    settlement_state: Dict[str, int] = field(default_factory=dict)
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
    # Round 69: how many of this domain's rounds were judged under ceilings that are no longer in force,
    # and how many predate the field that records them. `rules_hash` proves which rules decided a verdict
    # and nothing about the wait it was allowed to spend, so tightening a ceiling rewrites the meaning of
    # every stored verdict without moving a single rule.
    drift: int = 0
    drift_unrecorded: int = 0

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
            "settlements": dict(sorted(self.settlement_state.items())),
            "stream_files": dict(sorted(self.files.items())),
            "ceilings_differ": self.drift,
            "ceilings_unrecorded": self.drift_unrecorded,
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
    # Round 81: replacement links whose two ends disagree (or point at a round the ledger does not hold),
    # and links whose successor end could not have been written because it predates round 78.
    broken_links: int = 0
    link_legacy: int = 0
    # Round 84: ends a repair wrote. Counted apart for the same reason `--audit` counts them apart: an
    # agreement that was copied is not a second witness.
    backfilled: int = 0
    # Round 85: versions in force whose record the books dispute. Round 84 refused to stack these; a deployed
    # version cannot be refused, so it is reported here (and charged when the two books contradict).
    in_force: List[Dict[str, Any]] = field(default_factory=list)
    # Round 86: disputes this run no longer sees, counted so the page can say a debt cleared.
    in_force_cleared: int = 0
    # Round 86: of those in force on a copied end, the ones the gate now charges.
    in_force_stale: int = 0
    #: Round 89: clearings whose check ran and could not test the claim. Recorded, and answering nothing.
    in_force_untested: int = 0
    #: Round 87: the clearings the store remembers, split by what cleared them. An unexplained clearing is a
    #: debt that stopped being visible, not a debt that was paid, so the two are never added together.
    in_force_history: Dict[str, Any] = field(default_factory=dict)
    queue_run: Optional[Dict[str, Any]] = None
    queue_history: Dict[str, Any] = field(default_factory=dict)
    reruns: Dict[str, Any] = field(default_factory=dict)
    #: Round 143: every walk the store holds, in its own order, each carrying the ordinal the store gives it. The page
    #: printed the last walk and the summary counted all of them; nothing said *which* record a reader who opened
    #: `var/evolve/queue_runs.json` was looking at (boundary 342).
    walks: List[Dict[str, Any]] = field(default_factory=list)
    blocked_dossier: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    repair_plan: List[Dict[str, Any]] = field(default_factory=list)
    repair_history: Dict[str, Any] = field(default_factory=dict)
    # Round 112: pairs a person decided, with the reason and the moment. A settlement ends a charge without
    # producing evidence, so it travels with the books a reader checks (boundaries 251/252).
    settlements: List[Dict[str, Any]] = field(default_factory=list)
    #: Round 125: the decisions a person took back. They stop applying, so the contradiction each one settled is
    #: charged again -- kept here so the page can name them and say what the withdrawal was for.
    settlements_withdrawn: List[Dict[str, Any]] = field(default_factory=list)
    # Round 126: decisions a later decision took up again, after the withdrawal. A pair decided twice with a
    # withdrawal in between is a different history from one decided once (boundary 295).
    settlements_restored: List[Dict[str, Any]] = field(default_factory=list)
    settlements_unanswered: List[Dict[str, Any]] = field(default_factory=list)
    # Round 128: the same debt grouped by the stream file it belongs to, because a reader pays in afternoons and
    # eleven withdrawals on eleven files is eleven of them while eleven on one file is one (boundary 279).
    withdrawal_plan: List[Dict[str, Any]] = field(default_factory=list)
    # Round 116: the counts a script needs are computed once, beside the copies the page prints, so "how many
    # decisions still have no explanation" is a number in the JSON rather than a sentence to be parsed.
    settlement_summary: Dict[str, Any] = field(default_factory=dict)
    # Round 118: decisions that rest on books which have moved since they were taken (reported, not charged).
    settlement_moved: List[Dict[str, Any]] = field(default_factory=list)
    reanchorable: List[Dict[str, Any]] = field(default_factory=list)
    # Round 120: the same debt grouped by the file the decision is about, with the command that re-takes each pair.
    reaffirm_plan: List[Dict[str, Any]] = field(default_factory=list)
    # Round 124: each decision's re-takes in order, and the ones that keep needing one. A count says a decision
    # was re-taken; the timeline says what each re-take covered, which is what a reader checks it against.
    retake_timeline: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    frequent_retakes: List[Dict[str, Any]] = field(default_factory=list)
    stale_refusals: List[Dict[str, Any]] = field(default_factory=list)
    # Round 107: per promise, how many of the recorded walks refused it, when the first and the last were, and whether
    # the refusal still stands. `blocked_versions` answers the last question only, so a promise refused twice and then
    # repaired read exactly like one refused once an hour ago.
    refusal_history: Dict[str, Dict[str, Any]] = field(default_factory=dict)
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
    # Round 69: rounds whose recorded wait ceilings no longer match the ones in force, and rounds that
    # predate the field. The audit column recomputes a verdict under today's rules; this one says whether
    # the verdict was also reached under today's ceilings, which `rules_hash` cannot show.
    drift: int = 0
    drift_unrecorded: int = 0
    unattributable_missing: int = 0
    # Round 79: of the rounds the re-run queue paid, which family paid each (round 78 started recording it in
    # the round's `queue` block). Counted here because the record is only half an answer: a reader asking
    # "which book did the queue pay down" should not have to open forty JSON files.
    paid_families: Dict[str, int] = field(default_factory=dict)
    # Round 80: the same count split by the domain the queue paid on. The aggregate answers "was the ceiling
    # family ever paid"; only the split answers "was it paid on this stream", and a drain over two streams
    # hides that distinction inside a total that looks healthy either way.
    paid_by_domain: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # Rounds that say a queue paid them but not which family: written between rounds 58 and 77. Counted
    # separately so their silence cannot read as "the file family" (boundary 162 in its record form).
    paid_unstated: int = 0

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
            "ceilings_differ": self.drift,
            "ceilings_unrecorded": self.drift_unrecorded,
            "queue_paid_families": dict(sorted(self.paid_families.items())),
            "queue_paid_by_domain": {domain: dict(sorted(counts.items()))
                                     for domain, counts in sorted(self.paid_by_domain.items())},
            "queue_paid_unstated": self.paid_unstated,
            "backfilled_links": self.backfilled,
            "in_force_disputed": list(self.in_force),
            "in_force_cleared": self.in_force_cleared,
            "in_force_stale": self.in_force_stale,
            "in_force_cleared_history": dict(self.in_force_history),
            "queue_run": dict(self.queue_run) if isinstance(self.queue_run, Mapping) else None,
            "queue_history": dict(self.queue_history),
            "walks": [dict(item) for item in self.walks],
            "reruns": dict(self.reruns),
            "blocked_dossier": {str(key): dict(value) for key, value in self.blocked_dossier.items()},
            "repair_plan": [dict(item) for item in self.repair_plan],
            "repair_history": {str(key): value for key, value in self.repair_history.items()},
            "settlements": [dict(item) for item in self.settlements],
            "settlement_summary": dict(self.settlement_summary),
            "settlements_moved": [dict(item) for item in self.settlement_moved],
            "reanchorable": [dict(item) for item in self.reanchorable],
            "settlements_withdrawn": [dict(item) for item in self.settlements_withdrawn],
            "settlements_restored": [dict(item) for item in self.settlements_restored],
            "settlements_unanswered": [dict(item) for item in self.settlements_unanswered],
            "withdrawal_plan": [dict(item) for item in self.withdrawal_plan],
            "reaffirm_plan": [dict(item) for item in self.reaffirm_plan],
            "retake_timeline": {key: [dict(entry) for entry in value]
                                for key, value in self.retake_timeline.items()},
            "frequent_retakes": [dict(item) for item in self.frequent_retakes],
            "stale_refusals": [dict(item) for item in self.stale_refusals],
            "refusal_history": {str(key): dict(value) for key, value in self.refusal_history.items()},
            "in_force_untested": self.in_force_untested,
            "domains": [row.to_dict() for row in self.rows],
        }


def _source(record: Dict[str, Any]) -> Optional[str]:
    stream = record.get("stream") or {}
    source = stream.get("source")
    return None if source in (None, "") else str(source)


def _row_limits(limits: Mapping[str, Any], domain: str) -> Dict[str, Any]:
    """The ceilings that apply to one domain, in the shape `evolve.limit_drift` compares.

    A campaign row is one stream, so the comparison it prints must use the ceilings in force for that
    stream: a global ceiling applies everywhere, a per-domain one only to the domain it names. The origin
    is already excluded by `_limit_terms`, so it does not travel here either.
    """
    def scoped(key: str) -> Dict[str, Any]:
        values = limits.get(key) or {}
        for name, value in values.items():
            # The view is keyed by the slug the deployment state uses (`smd`) while a row is named by the
            # dataset the round recorded (`SMD`); comparing the raw strings silently pairs nothing.
            if domain_slug(str(name)) == domain_slug(domain):
                return {str(name): value}
        return {}

    return {
        "max_minutes": limits.get("max_minutes"),
        "max_minutes_by_domain": scoped("max_minutes_by_domain"),
        "max_drain_minutes": limits.get("max_drain_minutes"),
        "max_drain_by_domain": scoped("max_drain_by_domain"),
    }


def campaign(root: Path, rules: Optional[EvolveRules] = None,
             expiry_days: float = CLAIM_EXPIRY_DAYS, now: Optional[datetime] = None,
             refusal_expiry_days: float = REFUSAL_EXPIRY_DAYS,
             limits: Optional[Mapping[str, Any]] = None,
             in_force: Optional[Sequence[Mapping[str, Any]]] = None,
             in_force_cleared: Optional[Sequence[Mapping[str, Any]]] = None,
             in_force_stale: Optional[Sequence[Mapping[str, Any]]] = None,
             in_force_history: Optional[Mapping[str, Any]] = None,
             queue_run: Optional[Mapping[str, Any]] = None,
             queue_history: Optional[Mapping[str, Any]] = None,
             blocked_dossier: Optional[Mapping[str, Mapping[str, Any]]] = None,
             stale_refusals: Optional[Sequence[Mapping[str, Any]]] = None,
             refusal_history: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Campaign:
    """Summarise every recorded round, grouped by the stream each one measured.

    A round that does not say which stream it measured cannot be attributed to a domain, and is counted
    separately rather than guessed at: the stream field was added in round 3, so early rounds are
    legitimately unknown.

    Round 69: when the ceilings in force are passed in, each row also compares the ceilings its own rounds
    recorded against today's. The audit recomputes verdicts; this compares the conditions a verdict was
    reached under, which `rules_hash` cannot see.
    """
    rounds = history(root)
    everything = history(root, include_retired=True)
    # Counted from the same file: a retired round is one the campaign still holds but no longer asks the
    # checks to answer for, and hiding it would make "42 rounds" and "40 audited" look like a defect.
    result = Campaign(rounds=len(rounds), retired=max(0, len(everything) - len(rounds)))
    rows: Dict[str, CampaignRow] = {}
    by_row: Dict[str, List[Mapping[str, Any]]] = {}

    for record in rounds:
        source = _source(record)
        if source is None:
            result.unsourced += 1
            # Rounds written before the stream field existed still carry verdicts, and dropping their
            # numbers silently would make the campaign total disagree with the ledger it claims to
            # summarise. They are grouped under a name no domain can collide with instead.
            source = "(unattributed)"
        by_row.setdefault(source, []).append(record)
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
        # Round 79: which book the re-run queue paid down, accumulated over the rounds it produced. A record
        # written by rounds 58-77 says a queue paid it but not which family, which is a third state and is
        # counted as one -- an older record's silence must not be read as "the file family".
        queue_entry = record.get("queue")
        # Round 84: a repaired end carries a `queue` block so the successor names its predecessor, but its
        # `paid`/`family` are empty on purpose -- copying a claim is not knowing who paid. Counting it as
        # "a queue paid this without naming the family" would make the repair invent a payment.
        repaired = isinstance(queue_entry, Mapping) and isinstance(queue_entry.get(BACKFILL_KEY), Mapping)
        if isinstance(queue_entry, Mapping) and queue_entry and not repaired:
            family = str(queue_entry.get("family") or "")
            if family:
                result.paid_families[family] = result.paid_families.get(family, 0) + 1
                # Round 80: and which domain's book it went into. `paid` is the domain the queue charged,
                # which is not the round's `stream.source` when a drain spans two streams.
                paid_domain = str(queue_entry.get("paid") or "(unstated)")
                counts = result.paid_by_domain.setdefault(paid_domain, {})
                counts[family] = counts.get(family, 0) + 1
            else:
                result.paid_unstated += 1
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
    if limits is not None:
        # Round 69: the same comparison `--audit` runs, scoped to the rows the page prints. A row is one
        # stream, so it is judged against that stream's ceilings; the global total is summed afterwards so
        # the gate and the page cannot disagree about how many rounds moved.
        for row in result.rows:
            scoped = _row_limits(limits, row.domain)
            drift = limit_drift(by_row.get(row.domain, []), scoped)
            row.drift = len(drift.get("differ") or [])
            row.drift_unrecorded = int(drift.get("unrecorded") or 0)
            result.drift += row.drift
            result.drift_unrecorded += row.drift_unrecorded
    # A row that pooled two files answers two questions with one total. Counted here rather than per line so
    # the header can say how many rows a reader has to look at twice (round 48).
    result.pooled = sum(1 for row in result.rows
                        if len({path for path in row.files if path != "(not recorded)"}) > 1)
    result.trials = sum(row.trials for row in result.rows)
    result.accepted = sum(row.accepted for row in result.rows)
    result.rejected = sum(sum(row.rejections.values()) for row in result.rows)
    report = audit(root, rules, in_force=in_force, in_force_cleared=in_force_cleared,
                  in_force_stale=in_force_stale)
    result.moved = report.moved
    result.relabelled = report.relabelled
    result.unreconstructable = report.cross
    result.inconsistent = report.inconsistent
    # Round 81: a broken replacement link is a defect of the books, not of a verdict, so it travels with the
    # campaign's own audit and is charged by the same gate. The campaign is where a reader asks "what does
    # this loop claim", which is exactly the question a link answers wrongly.
    result.broken_links = report.broken_links
    result.link_legacy = report.link_legacy
    result.backfilled = len(report.backfilled)
    result.in_force = list(report.in_force)
    result.in_force_cleared = len(report.in_force_cleared)
    result.in_force_stale = len(report.in_force_stale)
    result.in_force_history = dict(in_force_history or {})
    result.queue_run = dict(queue_run) if isinstance(queue_run, Mapping) else None
    # Round 145: the other queue. Its walks are what a round's `queue.walk` names, and until now no page counted
    # them, so "how much of this campaign was the queue spending money" had no answer.
    try:
        result.reruns = rerun_walks(root)
    except OSError:
        result.reruns = {}
    result.queue_history = dict(queue_history or {})
    # Round 143: the store's own order, numbered, so the page and a payload can both name a walk by position instead
    # of by a timestamp a reader has to match by hand. The ordinal is the store's position, never a counter kept
    # somewhere else -- a second counter would be a second account of how many walks there have been.
    try:
        result.walks = [dict(run, ordinal=index + 1) for index, run in enumerate(queue_runs(root)["runs"])]
    except OSError:
        result.walks = []
    result.stale_refusals = [dict(item) for item in (stale_refusals or [])]
    result.refusal_history = ({str(key): dict(value) for key, value in refusal_history.items()}
                              if isinstance(refusal_history, Mapping) else {})
    result.blocked_dossier = ({str(key): dict(value) for key, value in blocked_dossier.items()}
                           if isinstance(blocked_dossier, Mapping) else {})
    # Round 110: which origin file to open first, computed from the same books the dossier counts.
    result.repair_plan = repair_plan(root)
    result.repair_history = repair_history(root)
    result.settlements = load_settlements(root)
    result.settlements_withdrawn = withdrawn_settlements(result.settlements)
    result.settlements_restored = restored_settlements(result.settlements)
    # Round 139: the decisions that still name a successor their record has left -- the work `--settle-reanchor-file`
    # pays. Computed where the rounds are, so the page prints a plan instead of recomputing one (boundary 264).
    result.reanchorable = reanchor_plan(everything, result.settlements)
    result.settlements_unanswered = unanswered_withdrawals(result.settlements)
    result.withdrawal_plan = withdrawal_plan(everything, result.settlements)
    result.settlement_summary = settlement_summary(result.settlements)
    # Round 118: the same reading the audit page prints, from the same two files.
    result.settlement_moved = moved_settlements(everything, result.settlements)
    result.reaffirm_plan = reaffirm_plan(everything, result.settlements, queue_runs(root)["runs"])
    result.retake_timeline = retake_timeline(result.settlements)
    result.frequent_retakes = frequent_retakes(result.settlements)
    # Round 117: attribute each decision to the stream of the round it settled, so the row a reader opens can
    # say "one decision here has no explanation". `load_settlements` names round files; the source lives on the
    # round record, retired ones included, because a decision can settle a round that has since been superseded.
    _round_source = {str(record.get("round") or ""): _source(record) for record in everything}
    for _decision in result.settlements:
        _owner = _round_source.get(str(_decision.get("record") or ""))
        if _owner is None:
            continue
        _row = rows.get(_owner)
        if _row is None:
            continue
        if any(str(item.get("record") or "") == str(_decision.get("record") or "")
               and str(item.get("successor") or "") == str(_decision.get("successor") or "")
               for item in result.settlement_moved):
            _row.settlement_state["moved"] = _row.settlement_state.get("moved", 0) + 1
        elif not str(_decision.get("why") or "").strip() and not _decision.get("notes"):
            _row.settlement_state["unexplained"] = _row.settlement_state.get("unexplained", 0) + 1
        else:
            _row.settlement_state["explained"] = _row.settlement_state.get("explained", 0) + 1
    # Round 123: the file row splits the stale work in two; the domain row kept only the sum. Both come from
    # `reaffirm_plan`, so a reader comparing the file line and the domain row sees one set of numbers (round 109).
    _payable = {(str(item.get("record") or ""), str(item.get("successor") or ""))
                for item in result.settlement_moved if item.get("clearable")}
    for _plan_row in result.reaffirm_plan:
        for _pair in _plan_row.get("pairs") or []:
            if (str(_pair.get("record") or ""), str(_pair.get("successor") or "")) not in _payable:
                # A retargeted or vanished record cannot be re-affirmed (round 119 refuses it), so it is a
                # decision to take again, not a reading to redo: counted under `moved`, never under a kind.
                continue
            _owner = _round_source.get(str(_pair.get("record") or ""))
            _row = rows.get(_owner) if _owner is not None else None
            if _row is None:
                continue
            _key = "stale_again" if str(_pair.get("kind") or "") == "again" else "stale_never"
            _row.settlement_state[_key] = _row.settlement_state.get(_key, 0) + 1
    result.in_force_untested = int((in_force_history or {}).get("untested") or 0)
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


def budget_text(budget: Mapping[str, Any]) -> str:
    """The one sentence that says what a queue run costs and leaves behind (round 60, shared in round 61).

    The page and the command print the same sentence from the same function, so the recommendation and the
    thing the command then does cannot drift apart — round 60 printed them from two expressions and the page
    argued with itself (it budgeted "all ready" while recommending one round).

    Round 62 prices the budget per domain where the records allow it: a queue that pays two domains whose
    rounds take four and eight minutes is not "about six minutes each", and a reader deciding whether to wait
    is deciding about the sum, not about an average that no single round ever took.
    """
    rounds = int(budget.get("rounds") or 0)
    total = int(budget.get("rounds_total") or 0)
    ceiling_rounds = int(budget.get("ceiling_rounds") or 0)
    skipped = int(budget.get("ceilings_skipped") or 0)
    if not rounds and not ceiling_rounds:
        # Round 76: "nothing to spend" is only true when nothing was left behind either. A reader who passed
        # `--without-ceilings` over a real ceiling family must not be told the queue is empty.
        if skipped:
            return ("nothing to spend on the file family: %d round(s) named their file and never recorded "
                    "their ceilings, and this command leaves them for another run (the share stays %.0f%%)"
                    % (skipped, 100.0 * float(budget.get("share_now") or 0.0)))
        return ("nothing to spend: the queue has no round it can re-run, so the share stays %.0f%%"
                % (100.0 * float(budget.get("share_now") or 0.0)))
    if not rounds:
        # Round 75: the command spends money even when the file family is empty -- the ceiling family is work
        # too. Saying "nothing to spend" above a line that recommends measuring nine rounds is the page
        # arguing with itself (boundary 160: the wait is quoted for the job the command actually does).
        return ("%s measures %d round(s) that named their file but never recorded their ceilings, leaving "
                "the unrecorded share unchanged at %.0f%% of %d round(s)"
                % (budget.get("command"), ceiling_rounds,
                   100.0 * float(budget.get("share_now") or 0.0), total))
    domains = ", ".join(str(domain) for domain in (budget.get("domains") or [])) or "no domain"
    line = ("%s pays %d round(s) (%s); the queue falls %.0f%% -> %.0f%% of %d round(s)"
            % (budget.get("command"), rounds, domains,
               100.0 * float(budget.get("share_now") or 0.0),
               100.0 * float(budget.get("share_after") or 0.0), total))
    # Round 75: a drain pays two families, and this sentence counted one. "N rounds" is the file family; the
    # ceiling family's rounds are measured with the same `run_round`, so leaving them out understates both the
    # work and the wait. The count is stated next to the money, not folded into it: a reader comparing two
    # invocations needs to see that one of them opens two families (item 62).
    if ceiling_rounds:
        line += "; it also re-measures %d round(s) that named their file but never recorded their ceilings" % ceiling_rounds
    else:
        # Round 76: silence here reads as "there is no such debt". A command that deliberately left a family
        # alone must say so, or the reader cannot tell "nothing to do" from "not this time" (boundary 143).
        skipped = int(budget.get("ceilings_skipped") or 0)
        if skipped:
            line += ("; it leaves %d round(s) that named their file and never recorded their ceilings "
                     "for another run" % skipped)
    by_domain = {str(name): float(value) for name, value in (budget.get("minutes_by_domain") or {}).items()}
    minutes = budget.get("estimated_minutes_by_domain")
    if minutes is None:
        minutes = budget.get("estimated_minutes")
    if minutes is not None:
        line += ", about %.0f minute(s)" % float(minutes)
    if len(by_domain) > 1:
        line += " (%s)" % ", ".join("%s %g" % (name, value) for name, value in by_domain.items())
    # Round 63: a step is not the whole job. "about 6 minutes" answers "what does this invocation cost",
    # not "what does finishing the queue cost", and a reader who is deciding whether to start deserves both.
    runs = int(budget.get("runs") or 0)
    drain = budget.get("drain_minutes")
    if runs > 1 and drain is not None and float(drain) > 0.0:
        line += ("; draining it would take %d such run(s) and about %g minute(s)" % (runs, float(drain)))
    return line


def format_campaign(result: Campaign, expiry_days: float = CLAIM_EXPIRY_DAYS,
                    rules: Optional[EvolveRules] = None,
                    refusal_expiry_days: float = REFUSAL_EXPIRY_DAYS,
                    queue: Optional[Mapping[str, Any]] = None,
                    trend: Optional[Mapping[str, Any]] = None,
                    debt: Optional[Mapping[str, Any]] = None,
                    plan: Optional[Sequence[Mapping[str, Any]]] = None,
                    budget: Optional[Mapping[str, Any]] = None,
                    limits: Optional[str] = None,
                    ceiling_note: Optional[str] = None,
                    rerun_offer: Optional[Mapping[str, Any]] = None,
                    claims: Optional[Sequence[Mapping[str, Any]]] = None) -> str:
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
    if result.paid_families or result.paid_unstated:
        # Round 79: the queue's own bookkeeping, accumulated. Without this line the only way to learn which
        # family a drain had been paying was to open the records one by one -- and the number that matters
        # ("was the ceiling family ever paid?") is not the total the queue paid.
        parts = ["%d %s" % (count, family) for family, count in sorted(result.paid_families.items())]
        line = ("queue paid        %d round(s) were produced by the re-run queue%s"
                % (sum(result.paid_families.values()),
                   (": " + ", ".join(parts)) if parts else " but none of them names its family"))
        if result.paid_unstated:
            line += ("; %d more say a queue paid them without naming the family (records from before "
                     "round 78)" % result.paid_unstated)
        lines.append(line)
        if result.paid_by_domain:
            # Round 80: the same count per domain. A drain over two streams can pay the ceiling family on
            # one of them only; the total above reads the same either way, so the split sits next to it.
            split = []
            for domain, counts in sorted(result.paid_by_domain.items()):
                split.append("%s %s" % (domain, ", ".join("%d %s" % (n, f)
                                                          for f, n in sorted(counts.items()))))
            lines.append("paid by domain    %s" % "; ".join(split))
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
    if budget is not None:
        # Round 60: the queue's size, the plan's first step and the projected end were three lines, so "if I
        # run it once, where do I end up?" had to be assembled by the reader. This is that sentence, derived
        # from the same functions the command runs, so the page cannot promise a movement the command will
        # not make. The JSON output carries the same fields, so a script never has to parse this line. Round 61
        # moved the sentence into `budget_text` so the command prints the identical line before it measures.
        lines.append("budget            %s" % budget_text(budget))
    if limits:
        # Round 66: round 65 sent the ceilings to the JSON so a report could be reviewed later, but the page a
        # reader actually reads printed none, so "nothing was capped" and "three things were capped" looked the
        # same. The sentence names only ceilings in force, each with where it came from.
        lines.append("limits            %s" % limits)
    if result.drift:
        # Round 69: the ceilings a verdict was judged under are recorded now, so the page can say how many
        # rounds would be decided under different conditions today. It is not an audit of the verdicts --
        # those are re-decided in the rows below -- it is a count of rounds whose *premises* moved.
        lines.append("ceilings          %d round(s) were judged under ceilings that differ from today's: the "
                     "rules did not change, the limits did" % result.drift)
    if ceiling_note:
        # Round 71: the count above says how many rounds moved; this says how many never recorded a ceiling at
        # all, and whether the re-run queue could change that. A warning nobody can clear and a warning one
        # command could clear look identical on the page otherwise (boundary 151).
        lines.append("ceiling debt      %s" % ceiling_note)
    if claims:
        # Round 153: round 152 recorded which verdict each repair rested on, and printed it once, in the
        # terminal that made it. A page that later counts the round as one the queue paid is describing a row
        # whose file a person chose; saying so is the difference between evidence and bookkeeping (boundary
        # 371).
        lines.append("claims            %d round(s) name a file a person claimed, and the geometry never "
                     "confirmed it: %s"
                     % (len(claims), ", ".join("%s %s (%s)" % (row["round"], row["stream_path"],
                                                               row["geometry"]) for row in claims)))
        lines.append("claims            a re-measurement on such a round is evidence about that file, not about "
                     "the one the round first measured (boundary 371)")
        # Round 155: how old the assertion is, asked once for the page rather than left to a reader who
        # would have to walk `inherited_from` back through the ledger by hand (boundary 375).
        aged = [row for row in claims if int(row.get("hops") or 0)]
        if aged:
            lines.append("claims            %s" % "; ".join(
                "the one on %s was inherited %d time(s) and was first recorded on %s at %s%s"
                % (row["round"], int(row["hops"]), row.get("origin") or "?", row.get("since") or "?",
                   " (that date was completed by a repair, not carried by the record)"
                   if row.get("backfilled") else "")
                for row in aged))
    if result.broken_links:
        # Round 82: round 81 counted these so `--strict` could exit 2, and printed them only on `--audit`.
        # A reader who runs the campaign page and sees a non-zero exit with no line to explain it has to
        # guess which of the many `--strict` charges fired (round 56's complaint, one family on).
        lines.append("links             %d replacement link(s) disagree with their other end: --audit names "
                     "each one, and --strict charges them as a build defect" % result.broken_links)
        if result.link_legacy:
            lines.append("links             %d more have no successor-side record (written before round 78) "
                         "and are reported, not charged" % result.link_legacy)
    elif result.link_legacy:
        # Round 84: "every link that has both ends agrees" is true of a backfilled end too, and means the
        # opposite there -- one end was copied. The clause is dropped whenever any end was repaired.
        lines.append("links             %d replacement link(s) have no successor-side record (written before "
                     "round 78)%s" % (result.link_legacy,
                                      "" if result.backfilled else "; every link that has both ends agrees"))
    if result.backfilled:
        lines.append("links             %d replacement link(s) were backfilled by a repair: their ends agree by "
                     "copying, not as two witnesses" % result.backfilled)
    if result.in_force:
        lines.append("links             %d version(s) in force sit on a record the books dispute: %s"
                     % (len(result.in_force),
                        ", ".join("v%03d%s" % (int(item.get("version") or 0),
                                               "" if item.get("age_days") is None else
                                               " (%.1f day(s))" % float(item.get("age_days") or 0.0))
                                  for item in result.in_force)))
    if result.in_force_stale:
        lines.append("links             %d of them have stood past the %.0f-day grace period with no verdict "
                     "that could test the claim: --strict exits 1 until one is measured again"
                     % (result.in_force_stale, IN_FORCE_GRACE_DAYS))
    if result.in_force_cleared:
        lines.append("links             %d version(s) in force were disputed earlier and no longer are"
                     % result.in_force_cleared)
    if result.in_force_untested:
        # Round 89: a comparison that compared nothing is recorded, and it answers nothing. Printed apart
        # from the measured count so a failed comparison can never look like a paid debt.
        lines.append("links             %d of those clearings rested on a check that could not test the "
                     "claim: recorded, and they answer nothing" % result.in_force_untested)
    moves = (result.in_force_history or {}).get("repaired_paths")
    if isinstance(moves, list) and moves:
        # Round 94: a repaired location is not an original one. Counted apart and named, because a reader who
        # cannot see that the path was rewritten will read the promise as having been made on the file it now
        # names (boundary 203).
        # Round 97: a record that went out and came back is not still "on a repair". Counting it in the
        # sentence that says "the location is a repair, not the original measurement" would contradict the
        # line printed two lines below it, and the page would argue with itself (round 60's rule).
        back_home = [item for item in moves if item.get("returned")]
        elsewhere = [item for item in moves if not item.get("returned")]
        if elsewhere:
            named = ", ".join("v%03d moved %d time(s)" % (int(item.get("version") or 0),
                                                          len(item.get("moves") or []))
                              for item in elsewhere)
            lines.append("links             %d record(s) have a rewritten stream path: %s -- the location is a "
                         "repair, not the original measurement, and `brian evolve --history` prints each move"
                         % (len(elsewhere), named))
        if back_home:
            named = ", ".join("v%03d (round trip of %d move(s))"
                              % (int(item.get("version") or 0), len(item.get("moves") or []))
                              for item in back_home)
            lines.append("links             %d record(s) had their measured file rewritten by hand and are back "
                         "on the file the promise was made on: %s -- the trip is closed, and what has been "
                         "measured there since is stated below" % (len(back_home), named))
        for item in moves:
            version = int(item.get("version") or 0)
            if item.get("returned"):
                # Round 97: the location is restored, the evidence is not. A record pointed back at its original
                # file still holds verdicts taken on the substitute, and saying "back in place" without that
                # would read as "answered again".
                evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
                taken = int(evidence.get("original") or 0)
                if item.get("checked_after_return") and item.get("first_on_original"):
                    # Round 99: the first verdict ever taken where the promise was made is not "another check
                    # since the return" -- before it, nothing had tested the promise at the place it names.
                    lines.append("links             v%03d's record is back on its original file %s, and this was "
                                 "the first verdict ever taken there (%s)"
                                 % (version, str(item.get("first_from") or ""),
                                    str(item.get("checked_after_return") or "")))
                elif item.get("checked_after_return"):
                    lines.append("links             v%03d's record is back on its original file %s, and %d "
                                 "verdict(s) have been taken there since the return (latest %s)"
                                 % (version, str(item.get("first_from") or ""), taken,
                                    str(item.get("checked_after_return") or "")))
                else:
                    lines.append("links             v%03d's record is back on its original file %s, but no check "
                                 "has stood there since the return: the location is restored, the evidence is not "
                                 "-- brian evolve --verify --verify-version %03d --stream-file %s"
                                 % (version, str(item.get("first_from") or ""), version,
                                    str(item.get("first_from") or "")))
            elif item.get("origin_exists"):
                # Round 96: the repair may no longer be necessary. Naming the command matters, because a reader
                # who is told "the original file is back" but not how to use it has been told a suspicion.
                lines.append("links             v%03d's original file %s is back in this checkout: the record can "
                             "be pointed at it again and measured there -- brian evolve --set-stream-path v%03d=%s"
                             % (int(item.get("version") or 0), str(item.get("first_from") or ""),
                                int(item.get("version") or 0), str(item.get("first_from") or "")))
        # Round 98: after a round trip "1 check on the repair" is not the whole story -- the reader needs to
        # know how the verdicts divide between the file the promise was born with and every other file. A path
        # the record cannot place is counted, not guessed (boundary 166).
        for item in moves:
            evidence = item.get("evidence")
            if not isinstance(evidence, Mapping) or not int(evidence.get("total") or 0):
                continue
            elsewhere = [str(name) for name in evidence.get("elsewhere_files") or []]
            detail = (", ".join(elsewhere) if elsewhere else "none named")
            lines.append("links             v%03d holds %d verdict(s) on file: %d on its original file, %d on "
                         "another file (%s), %d it cannot place"
                         % (int(item.get("version") or 0), int(evidence.get("total") or 0),
                            int(evidence.get("original") or 0), int(evidence.get("elsewhere") or 0),
                            detail, int(evidence.get("unplaced") or 0)))
        for item in moves:
            evidence = item.get("evidence")
            if not isinstance(evidence, Mapping):
                continue
            for row in evidence.get("unplaced_rows") or []:
                # Round 99: an unplaceable verdict is named with its time and word, so a reader can go and
                # look it up instead of seeing a bare count they cannot act on (boundary 215).
                lines.append("links             v%03d holds a verdict it cannot place: %s (%s)"
                             % (int(item.get("version") or 0), str(row.get("at") or "?"),
                                str(row.get("verdict") or "?")))
        origin_queue = never_tested_at_origin(moves)
        if origin_queue["records"]:
            # Round 100: the population behind round 99's single-record sentence. A reader who learns that one
            # record was never tested where it was made has to know whether that is one record or the whole
            # book, and which file to open first (boundary 219).
            files = origin_queue["by_file"]
            if len(files) > 1:
                lines.append("links  never tested at their origin: %s"
                             % "; ".join("%s %d record(s)" % (name, count)
                                         for name, count in sorted(files.items())))
            first = origin_queue["versions"][0]
            held = ("%d verdict(s), all elsewhere" % first["verdicts"] if first["verdicts"]
                    else "no verdict at all")
            if first["origin_exists"]:
                next_step = (" -- start with brian evolve --verify --verify-version %03d --stream-file %s"
                             % (first["version"], first["origin"]))
            else:
                next_step = " -- the file it was born with is not in this checkout, so it cannot be measured there"
            lines.append("links             %d record(s) hold no verdict on the file their promise was born "
                         "with: v%03d (%s)%s"
                         % (origin_queue["records"], first["version"], held, next_step))
        after = sum(int(item.get("checks_on_repair") or 0) for item in moves)
        if after:
            # Round 95: the verdicts taken after the move are the part of the evidence a reader would most
            # easily mistake for an original measurement, so their count is printed with the moves.
            lines.append("links             %d check(s) were taken after the repair: their verdict describes the "
                         "repaired location, not the file the promise first named" % after)
    dead = (result.in_force_history or {}).get("dead_record_paths")
    if isinstance(dead, list) and dead:
        # Round 93: what the record itself claims to have been measured on, and this checkout cannot open. It is
        # printed outside the clearing block on purpose: a promise naming a file nobody can open is true whether or
        # not anything was ever cleared, and a gate naming one subject silences the other (round 87, boundary 184).
        # It is fixable, so the line names the command with the file that would take its place.
        for item in dead:
            lines.append("links             v%03d records %s%s, which this checkout cannot open: fix the "
                         "record rather than the check -- brian evolve --set-stream-path v%03d=<a file that "
                         "exists>"
                         % (int(item.get("version") or 0), str(item.get("stream_path") or ""),
                            " (in force)" if item.get("in_force") else "",
                            int(item.get("version") or 0)))

    history = result.queue_history
    if isinstance(history, Mapping) and int(history.get("runs") or 0) > 1:
        # Round 104: one line says the last walk; several walks are a trend, and a trend is what tells a reader
        # whether the queue is being drained or simply walked. Printed only from two runs on: one run is a fact, not
        # a direction, and a "trend" of one would read as a rate.
        line = ("links             %d queue run(s) recorded: measured %d promise(s) in total, %d of those left a "
                "clearing, %d run(s) ended on a build defect (%.1f promise(s) per run on average)"
                % (int(history.get("runs") or 0), int(history.get("measured") or 0),
                   int(history.get("cleared") or 0), int(history.get("stopped") or 0),
                   float(history.get("mean_measured") or 0.0)))
        if int(history.get("trimmed") or 0):
            line += "; %d older run(s) fell out of the store, so this is the recent window" % int(
                history.get("trimmed") or 0)
        lines.append(line)
        origins = history.get("skipped_origins")
        if isinstance(origins, Mapping) and origins:
            repeated = {str(name): int(count) for name, count in origins.items() if int(count or 0) > 1}
            # A file that had to be skipped once may be a checkout that has not fetched it; the same file skipped on
            # every walk is a promise nobody can measure here, and that is worth naming (round 47's rule: an
            # unattributable record is reported, never quietly pooled).
            if repeated:
                lines.append("links             origins skipped on more than one walk: %s"
                             % "; ".join("%s %d" % (name, count) for name, count in sorted(repeated.items())))

    # Round 107: a refusal history, not only the refusals that still stand. One line per promise: how many of the
    # recorded walks refused it, when the first and last were, and whether it still does. The reason is not repeated
    # here -- the dossier prints it once per origin file.
    for key in sorted(result.refusal_history, key=lambda item: int(item) if str(item).isdigit() else 0):
        entry = result.refusal_history.get(key) or {}
        count = int(entry.get("count") or 0)
        if not count:
            continue
        # Round 108: the walk ordinal, so the sentence points at a row a reader can open rather than at a stamp.
        lines.append("links             v%03d was refused by the queue %d time(s) among %d recorded walk(s): "
                     "walk %s to %s, first %s, last %s%s"
                     % (int(entry.get("version") or 0), count, int(entry.get("walks") or 0),
                        str(entry.get("first_walk") or "?"), str(entry.get("last_walk") or "?"),
                        str(entry.get("first_at") or "?"), str(entry.get("last_at") or "?"),
                        ", and the refusal still stands" if entry.get("standing")
                        else ", and it no longer stands"))
    stale = result.stale_refusals
    if isinstance(stale, list) and stale:
        # Round 106: the books moved, so these refusals no longer stand and the queue can try them again without
        # `--retry-blocked`. Printed before the refusals that still hold, because it is the page's good news.
        for item in sorted(stale, key=lambda row: int(row.get("version") or 0)):
            lines.append("links             the refusal of v%03d no longer stands: the ledger has changed since %s"
                         % (int(item.get("version") or 0), str(item.get("at") or "an unrecorded walk")))

    blocked_by_origin = result.blocked_dossier
    if isinstance(blocked_by_origin, Mapping) and blocked_by_origin:
        # Round 105: round 104 made the queue skip a promise the books keep refusing; this line says which file is
        # behind the refusals, because that is the thing a reader has to open to make the queue move again.
        for origin, entry in sorted(blocked_by_origin.items()):
            if not isinstance(entry, Mapping):
                continue
            versions = ", ".join("v%03d" % int(item) for item in entry.get("records") or [])
            records = [int(item) for item in entry.get("records") or []]
            retried = [int(item) for item in entry.get("retried") or []]
            # Round 108: the file's bill -- the refusals of every promise born with it, not only the standing ones.
            # Round 109: the walks themselves, so this line and the per-promise history point at the same rows.
            walked = walks_phrase([int(item) for item in entry.get("refused_walks") or []])
            line = ("links             blocked by the books: %s %d promise(s) (%s), %d refusal(s) across %s, "
                    "last %s (walk %s of %d; its promises were refused in %s, %d time(s))"
                    % (origin, len(records), versions, int(entry.get("attempts") or 0),
                       ("%d retried promise(s)" % len(retried)) if retried else "none retried",
                       str(entry.get("last_at") or "unknown"), str(entry.get("last_walk") or "?"),
                       int(entry.get("walks") or 0), walked or "no recorded walk",
                       int(entry.get("refused") or 0)))
            reasons = [str(item) for item in entry.get("reasons") or [] if str(item)]
            if reasons:
                # Named here rather than on each history row: this is the one place the contradiction is printed
                # once for the file it belongs to (round 105's split between the row and the page).
                line += " -- %s" % reasons[0]
            lines.append(line)
        # Round 110: the reader who has just learned which file is behind the refusals asks which one to open first.
        plan = result.repair_plan
        if isinstance(plan, list) and plan:
            first = plan[0]
            lines.append("links             repair next: %s -- %d walk(s) of refusals, %d promise(s) held behind it "
                         "(%s)" % (str(first.get("origin") or "(unnamed)"), int(first.get("walks_blocked") or 0),
                                   int(first.get("held") or 0), str(first.get("remedy") or "")))
            for row in plan[1:]:
                lines.append("links             then: %s -- %d walk(s), %d promise(s) held"
                             % (str(row.get("origin") or "(unnamed)"), int(row.get("walks_blocked") or 0),
                                int(row.get("held") or 0)))
    # Round 112: a decided pair is not an agreement, and the campaign page said nothing about it at all until the
    # repair line arrived as a side effect. It is printed on its own, before the repair it may have paid for.
    settled_active = [item for item in result.settlements if not is_withdrawn(item)]
    if settled_active:
        lines.append("links             %d pair(s) were settled by a recorded decision, not by agreement: %s"
                     % (len(settled_active),
                        ", ".join("%s -> %s" % (str(item.get("record") or ""), str(item.get("successor") or ""))
                                  for item in settled_active)))
        # Round 113: a decision is reviewed by asking who made it, so the campaign counts them by decider. The
        # empty case is its own bucket, named for what it is rather than dropped (boundary 166).
        by = {}
        for item in settled_active:
            who = str(item.get("by") or "").strip()
            by[who] = by.get(who, 0) + 1
        parts = ["%s %d" % (who if who else "and no decider recorded", count)
                 for who, count in sorted(by.items(), key=lambda pair: (-pair[1], pair[0]))]
        lines.append("links             decided by: %s" % ", ".join(parts))
        # Round 114: an explanation added later is not the explanation the decision came with, so the count is
        # printed on its own line rather than folded into the decider total (boundary 257).
        explained = sum(1 for item in settled_active if item.get("notes"))
        if explained:
            lines.append("links             %d of those decisions were explained after the fact: the record keeps "
                         "its date, and a later note does not make it explained at the time" % explained)
            # Round 115: an explanation has an author, and one person explaining three decisions is a different
            # fact from three people explaining one each. A note written without --settle-by is its own bucket,
            # never folded into the named ones (boundary 166).
            explainers: Dict[str, int] = {}
            for item in settled_active:
                for note in item.get("notes") or []:
                    if not isinstance(note, Mapping):
                        continue
                    who = str(note.get("by") or "").strip()
                    explainers[who] = explainers.get(who, 0) + 1
            if explainers:
                ordered = sorted(explainers.items(), key=lambda pair: (-pair[1], pair[0]))
                lines.append("links             explained later by: %s"
                             % ", ".join("%s %d" % (who if who else "and no explainer named", count)
                                         for who, count in ordered))
        # Round 120: round 118 computed this and printed it only in the JSON. The page is what a reader opens, and
        # "which file do I re-take the decision on" is the question that follows "two decisions moved".
        if result.settlement_moved:
            lines.append("links             %d decision(s) rest on books that have moved since they were taken: %s"
                         % (len(result.settlement_moved),
                            ", ".join("%s -> %s" % (str(item.get("record") or ""),
                                                    str(item.get("successor") or ""))
                                      for item in result.settlement_moved)))
        # Round 124: a re-take says "I read today's books and the decision still stands". Doing that three times
        # says the decision keeps going stale -- evidence about the decision, not about the books (boundary 288).
        # Naming the pairs is the whole point: the next action is a new decision, not a fourth re-take.
        if result.frequent_retakes:
            lines.append("links             %d decision(s) have been re-taken at least 3 time(s), which usually "
                         "means the decision is wrong and not that the books moved: %s"
                         % (len(result.frequent_retakes),
                            ", ".join("%s -> %s (%d time(s), last %s)"
                                      % (str(item.get("record") or ""), str(item.get("successor") or ""),
                                         int(item.get("retakes") or 0), str(item.get("last_at") or ""))
                                      for item in result.frequent_retakes)))
    # Round 125: the other answer to "this decision keeps needing a re-take". Printed after the settled block and
    # before the plan, because it explains why a pair is back among the charges (boundary 292).
    if result.settlements_withdrawn:
        lines.append("links             %d decision(s) were withdrawn by a person, so the contradiction each one "
                     "settled is charged again: %s"
                     % (len(result.settlements_withdrawn),
                        ", ".join("%s -> %s (%s)"
                                  % (str(item.get("record") or ""), str(item.get("successor") or ""),
                                     str((item.get("withdrawn") or [{}])[-1].get("at") or ""))
                                  for item in result.settlements_withdrawn)))
    # Round 126: the answer to a withdrawal. Printed next to it, because the reader who saw the pair come back
    # among the charges needs to see the command that takes it out again -- and needs to know that doing so is a
    # decision, not a repair (boundary 294).
    _reanchorable = result.reanchorable
    if _reanchorable:
        # Round 139: round 138 taught one decision to follow its record; a file that was retargeted owes the same step
        # N times, and a command nobody is told about is a command nobody runs (boundary 261).
        _lead = _reanchorable[0]
        lines.append("links             %d decision(s) still name a successor their record has left: %s -- %s"
                     % (sum(len(row.get("records") or []) for row in _reanchorable),
                        "; ".join("%s %s -> %s" % (str(item.get("record") or ""), str(item.get("from") or "?"),
                                                  str(item.get("to") or "?"))
                                  for item in _lead.get("pairs") or []),
                        str(_lead.get("batch") or "brian evolve --settle-reanchor RECORD")))
    _reanchored = [item for item in result.settlements if item.get("successor_history")]
    if _reanchored:
        # Round 138: a decision moved to the successor its record now names is neither a fresh decision nor the one
        # it was -- its first target is in successor_history, and folding it into "settled" reports a decision taken
        # on evidence it was never taken on (boundary 203, one field over from round 94's repaired path).
        lines.append("links             %d decision(s) were re-anchored to the successor the record now names: %s"
                     % (len(_reanchored),
                        "; ".join("%s %s -> %s" % (str(item.get("record") or ""),
                                                  str((item.get("successor_history") or [{}])[0].get("from") or ""),
                                                  str(item.get("successor") or ""))
                                  for item in _reanchored)))
        lines.append("links             a re-anchored decision was not taken on its new successor: only a check "
                     "measures it there, and `--history` marks the rows")
    if result.settlements_restored:
        lines.append("links             %d decision(s) were taken up again by a new decision: %s"
                     % (len(result.settlements_restored),
                        ", ".join("%s -> %s (withdrawn %s, taken up again %s)"
                                  % (str(item.get("record") or ""), str(item.get("successor") or ""),
                                     str((item.get("withdrawn") or [{}])[-1].get("at") or ""),
                                     str((item.get("redecisions") or [{}])[-1].get("at") or ""))
                                  for item in result.settlements_restored)))
    # Round 127: of the withdrawals, the ones nobody has answered are the debt the gate charges, so the page names
    # the oldest and the command that clears it -- "N are waiting" without the oldest is a number the reader has to
    # go and sort himself (boundary 219's rule, applied to a withdrawal instead of a promise).
    for _row in result.withdrawal_plan:
        # Round 137: `restore_settlement` refuses a withdrawal whose record now names another successor, so part of
        # this queue is work no command in this build can do -- the page must say which half is which instead of
        # offering `--settle-restore-file` for a file it cannot empty (boundary 311, third family).
        if int(_row.get("unpayable") or 0):
            lines.append("links             %d withdrawal(s) on %s cannot be taken up again: the records name other "
                         "successors -- brian evolve --settle-drop-file %s --settle-why TEXT"
                         % (int(_row.get("unpayable") or 0), str(_row.get("origin") or "(unattributed)"),
                            str(_row.get("origin") or "(unattributed)")))
    if len(result.withdrawal_plan) > 1:
        lines.append("links             withdrawn and waiting, by stream file: %s"
                     % "; ".join("%s %d decision(s)" % (str(row.get("origin") or "(unattributed)"),
                                                        int(row.get("waiting") or row.get("decisions") or 0))
                                 for row in result.withdrawal_plan))
    if result.settlements_unanswered:
        oldest = result.settlements_unanswered[0]
        age = oldest.get("age_days")
        # Round 129: when a file holds more than one unanswered withdrawal, the command a reader copies must pay that
        # whole file -- naming the single pair here would undo the grouping one line after making it (boundary 301).
        _lead = result.withdrawal_plan[0] if result.withdrawal_plan else {}
        _command = (str(_lead.get("batch") or "") if int(_lead.get("decisions") or 0) > 1
                    else "brian evolve --settle-restore %s=%s --settle-why TEXT --settle-by NAME"
                         % (str(oldest.get("record") or ""), str(oldest.get("successor") or "")))
        lines.append("links             %d withdrawal(s) have no answer: %s -> %s was taken back on %s (%s day(s) "
                     "ago) and nothing has replaced it -- start with %s"
                     % (len(result.settlements_unanswered), str(oldest.get("record") or ""),
                        str(oldest.get("successor") or ""), str(oldest.get("at") or ""),
                        "?" if age is None else "%.1f" % float(age), _command))
    # Round 133: the pairs no re-take can pay are named with the command that does something about them.
    # Round 132's what-if predicted "paying all of these still leaves two" and the real run agreed by refusing two
    # pairs one at a time: a plan that offers unpairable work hands the reader a command that fails (boundary 311).
    for row in result.reaffirm_plan:
        if not int(row.get("unpayable") or 0):
            continue
        stuck = row.get("unpayable_pairs") or []
        lead = stuck[0] if stuck else {}
        command = (row.get("stuck") or [""])[0]
        lines.append("links             %d stale decision(s) on %s cannot be re-taken at all: %s -> %s (the record "
                     "now names %s) -- %s"
                     % (int(row.get("unpayable") or 0), row.get("origin") or "(unrecorded)",
                        str(lead.get("record") or ""), str(lead.get("successor") or ""),
                        str(lead.get("names") or "(nothing)"), command))
    for item in result.reaffirm_plan[:1]:
        commands = [str(command) for command in item.get("commands") or [] if str(command)]
        walks = item.get("walks_since")
        # Round 121: "stale since when" is the other half of "how many are stale". A file re-taken this morning
        # and walked three times since is a different problem from one nobody has opened in a month (item 107).
        # "never re-taken" is not printed here: the split below already says how many decisions nobody has
        # read the books for, and a fact twice on one line reads as noise (boundary 241).
        when = ("last re-taken %s" % str(item.get("last_retaken") or "")) if item.get("last_retaken") else ""
        since = "unknown" if walks is None else ("%d walk(s) since" % int(walks))
        # Round 122: the debt is two debts. `never` is a promise nobody has read the books for; `again` is one
        # where the books moved after the last reading, which re-stamping would cover while looking like upkeep.
        kinds = "%d never re-taken, %d re-taken before and moved again" % (int(item.get("never") or 0),
                                                                           int(item.get("again") or 0))
        if int(item.get("stale") or 0) > 1 and str(item.get("batch") or ""):
            # One command for the whole file: the reason belongs to the action, and eleven pasted pairs is how
            # one gets skipped (boundaries 277/282).
            command = str(item.get("batch"))
        else:
            command = commands[0] if commands else "brian evolve --settle-reaffirm <record>=<successor>"
        parts = [part for part in (when, since, kinds) if part]
        lines.append("links             re-take next: %s -- %d stale of %d recorded decision(s)%s, %s -- %s"
                     % (str(item.get("origin") or "(unnamed stream)"), int(item.get("stale") or 0),
                        int(item.get("decisions") or 0),
                        " (%d re-taken)" % int(item.get("retaken") or 0) if item.get("retaken") else "",
                        ", ".join(parts), command))
    # Round 111: the ranking said which file to open first; this says whether last time's opening helped.
    last = (result.repair_history or {}).get("last") or {}
    if isinstance(last, Mapping) and last.get("at"):
        before = last.get("before") or {}
        after = last.get("after") or {}
        freed = [str(item) for item in last.get("freed") or [] if str(item)]
        if freed:
            lines.append("links             the last repair (%s, %s) took %d file(s) off the table: %s -- cost "
                         "%d -> %d" % (str(last.get("at") or ""), str(last.get("action") or "a repair"),
                                       len(freed), ", ".join(freed), int(before.get("cost") or 0),
                                       int(after.get("cost") or 0)))
        else:
            lines.append("links             the last repair (%s, %s) changed nothing in the plan: cost %d -> %d, "
                         "%d file(s) still owing" % (str(last.get("at") or ""), str(last.get("action") or "a repair"),
                                                     int(before.get("cost") or 0), int(after.get("cost") or 0),
                                                     int(after.get("files") or 0)))

    run = result.queue_run
    if isinstance(run, Mapping) and run.get("at"):
        # Round 103: the queue can now be walked by a command, and the ledger says nothing about whether anyone ever
        # walked it. "No promise waits in the queue" and "nobody has run the queue" are different states of the same
        # clean page, so the last walk is printed with its own numbers. It is printed outside the clearing block
        # because it is a fact about the queue, not about the disputes the queue happened to settle (round 87).
        line = ("links             the last queue run was %s: measured %d of %d runnable promise(s) on their "
                "origins, left %d out" % (str(run.get("at") or ""), int(run.get("measured") or 0),
                                          int(run.get("runnable") or 0), int(run.get("skipped") or 0)))
        if int(run.get("cleared") or 0):
            line += ", and %d of those measurements left a clearing" % int(run.get("cleared") or 0)
        # Round 143: which walk this was. A payload says the same number, so "the last run" and "walk 3 of 7" name
        # one record instead of two descriptions of one store.
        ordinal = next((int(item.get("ordinal") or 0) for item in result.walks
                        if str(item.get("at") or "") == str(run.get("at") or "")), 0)
        if ordinal:
            line += " (walk %d of %d)" % (ordinal, len(result.walks))
        lines.append(line)
        if int(run.get("stopped_at") or 0):
            lines.append("links             it stopped at v%03d, which exited 2: a build defect is not something "
                         "the next promise in the queue measures away" % int(run.get("stopped_at") or 0))

    rerun_note = format_rerun_walks(result.reruns) if result.reruns else None
    if rerun_note:
        lines.append("re-run walks      %s" % rerun_note)
    if rerun_offer and rerun_offer.get("text"):
        # Round 149: the line above counts how the payments were made; this one says how many the queue could
        # make today and which command makes them (boundary 360).
        lines.append("re-run offer      %s" % rerun_offer["text"])

    if result.in_force_history and int(result.in_force_history.get("total") or 0):
        record = result.in_force_history
        lines.append("links             %d dispute(s) left the in-force list: %d cleared by a re-measurement "
                     "(median %.1f day(s)), %d with no measurement on file"
                     % (int(record.get("total") or 0), int(record.get("measured") or 0),
                        float(record.get("median_age_days") or 0.0), int(record.get("unexplained") or 0)))
        if int(record.get("written") or 0):
            lines.append("links             %d of those clearings were written by the measurement itself; the "
                         "rest were deduced later from the ledger" % int(record.get("written") or 0))
        # Round 102: a clearing earned by a queued run and one earned by a hand-typed command leave the same
        # verdict; what differs is whether anyone retyped an identifier, which is the habit the queue exists to
        # remove (boundary 221). Entries from before this round carry no field, so they are counted apart rather
        # than folded into either side (round 79).
        queued = int(record.get("queued") or 0)
        unstated = int(record.get("unstated_queue") or 0)
        by_hand = int(record.get("total") or 0) - queued - unstated
        if int(record.get("total") or 0):
            line = "links             %d clearing(s) were produced by the queue, %d by a hand-typed command" % (
                queued, by_hand)
            if unstated:
                line += "; %d were written before the store recorded it" % unstated
            lines.append(line)
        files = record.get("by_file")
        if isinstance(files, Mapping) and files:
            last = record.get("last_by_file")
            last = last if isinstance(last, Mapping) else {}
            # Round 91: the count says how often a file answered; the date says whether it is still the evidence
            # a reader would go and open. A file that answered once a year ago and one that answered yesterday
            # read the same in a bare count.
            lines.append("links             clearings by stream file: %s"
                         % "; ".join("%s %d (last %s)" % (name, int(count), str(last.get(name) or "unknown"))
                                     for name, count in sorted(files.items())))
        gone = record.get("evidence_missing")
        if isinstance(gone, Mapping) and int(gone.get("missing") or 0):
            lines.append("links             %d clearing(s) name a file this checkout cannot open: %s"
                         % (int(gone.get("missing") or 0),
                            ", ".join(str(name) for name in gone.get("missing_files") or [])))
            oldest = gone.get("oldest")
            if isinstance(oldest, Mapping) and oldest.get("checked_at"):
                # Round 92: the oldest dead link is the one to open first, so it is named with its own age
                # rather than left to be found by sorting the store by hand.
                age = oldest.get("age_days")
                lines.append("links             the oldest of them was written %s (v%03d, %s)"
                             % (str(oldest.get("checked_at")), int(oldest.get("version") or 0),
                                "an unknown time" if age is None else "%.1f day(s) ago" % float(age)))
        named = record.get("substitutions")
        if isinstance(named, Mapping) and named:
            # Round 93: checks ran on a file the record knew rather than the one they were asked for. That is a
            # caller's habit, so it is reported as a fact and charged by nothing.
            lines.append("links             substitutions: %s"
                         % "; ".join("asked for %s, ran on the recorded file %d time(s)" % (name, int(count))
                                     for name, count in sorted(named.items())))
        domains = record.get("by_domain")
        if isinstance(domains, Mapping) and len(domains) > 1:
            lines.append("links             clearings by domain: %s"
                         % "; ".join("%s %d by measurement, %d without"
                                     % (name, int(values.get("measured") or 0),
                                        int(values.get("unexplained") or 0))
                                     for name, values in sorted(domains.items())))
        if int(record.get("trimmed") or 0):
            lines.append("links             %d older clearing(s) were trimmed from the store, so this count "
                         "is not the whole history" % int(record.get("trimmed") or 0))
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
        if row.drift or row.drift_unrecorded:
            # Per domain, for the same reason the audit column is (round 34): the header's count is the
            # ledger's, and a reader of this row is deciding about this stream.
            bits = []
            if row.drift:
                bits.append("%d of %d round(s) were judged under ceilings that differ from today's"
                            % (row.drift, row.rounds))
            if row.drift_unrecorded:
                bits.append("%d round(s) do not say which ceilings they were judged under"
                            % row.drift_unrecorded)
            lines.append("  ceilings          %s" % "; ".join(bits))
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
