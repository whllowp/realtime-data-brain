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
from pathlib import Path
from typing import Any, Dict, List, Optional

from .evolve import EvolveRules, audit, history
from .promote import UNMEASURABLE, VERIFIED, current_state, load_promotions, shape_note

__all__ = ["Campaign", "CampaignRow", "campaign", "format_campaign"]


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
            "domains": [row.to_dict() for row in self.rows],
        }


def _source(record: Dict[str, Any]) -> Optional[str]:
    stream = record.get("stream") or {}
    source = stream.get("source")
    return None if source in (None, "") else str(source)


def campaign(root: Path, rules: Optional[EvolveRules] = None) -> Campaign:
    """Summarise every recorded round, grouped by the stream each one measured.

    A round that does not say which stream it measured cannot be attributed to a domain, and is counted
    separately rather than guessed at: the stream field was added in round 3, so early rounds are
    legitimately unknown.
    """
    rounds = history(root)
    result = Campaign(rounds=len(rounds))
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
            if row.verdict == VERIFIED:
                result.holding += 1
            elif row.verdict == UNMEASURABLE:
                result.not_comparable += 1
            elif row.verdict is None:
                result.never_checked += 1

    result.rows = sorted(rows.values(), key=lambda item: (-item.rounds, item.domain))
    result.trials = sum(row.trials for row in result.rows)
    result.accepted = sum(row.accepted for row in result.rows)
    result.rejected = sum(sum(row.rejections.values()) for row in result.rows)
    report = audit(root, rules)
    result.moved = report.moved
    result.relabelled = report.relabelled
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
    if row.checks:
        note = "%s, %d earlier check%s" % (note, row.checks, "" if row.checks == 1 else "s")
    return note


def format_campaign(result: Campaign) -> str:
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("brian evolve - the campaign, not the round")
    lines.append("=" * 96)
    lines.append("rounds            %d  (%d candidate measurements, %d accepted, %d rejected)"
                 % (result.rounds, result.trials, result.accepted, result.rejected))
    lines.append("applied           %d round(s) wrote versions; %d version(s) were reverted"
                 % (result.applied, result.reverted))
    lines.append("audit             %d verdict(s) would be decided differently today, %d relabelled"
                 % (result.moved, result.relabelled))
    lines.append("verification      %d holding, %d not comparable, %d never re-checked"
                 % (result.holding, result.not_comparable, result.never_checked))
    if result.unsourced:
        lines.append("unattributed      %d round(s) predate the stream field" % result.unsourced)
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
        rejects = ", ".join("%s x%d" % (rule, count) for rule, count in sorted(row.rejections.items()))
        if rejects:
            lines.append("  rejected by       %s" % rejects)
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
    return "\n".join(lines)
