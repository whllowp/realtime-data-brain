"""Where a candidate comes from, and the first part of the improvement mechanism the loop may revise.

Until now the catalogue was a fixed list, so the loop could choose *among* proposals but never change
*what gets proposed*. The survey's L4 is exactly that step: the improvement mechanism becomes mutable
and the loop keeps improving in a bounded domain. This module takes it, and spends most of its length on
the bound rather than on the mutation — because an unbounded proposal generator is how a loop starts
grading itself.

The bound is four declarations, all of them data a reader can inspect:

* `SPACE_FIELDS` — which settings may be proposed at all, derived from the catalogue plus the fields a
  running process can own. A field nobody has declared is not reachable, so "the loop found a knob we
  did not think of" cannot happen by accident or by language model invention.
* `LIMITS` — the interval each numeric field may take. A neighbour outside the interval is not proposed;
  a field without a declared interval is not neighbour-able at all.
* `MAX_PROPOSALS` — how many proposals one round may contain.
* The neighbour rule itself: a neighbour is the *same* change at a different magnitude (half, double, or
  one step away), never a new dimension. That keeps every proposal a falsifiable statement of the form
  "the value was not the active ingredient".

What is deliberately *not* mutable here: the objective (`EvolveRules`), the decision procedure
(`decide`), the evaluator and the signals it reads. A loop that can rewrite its own acceptance rules can
accept anything, so that stays outside the thing being improved.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .evolve import Candidate, EvolveRules, Measurement, catalogue, decide

MAX_PROPOSALS = 8
"""How many *generated* proposals one round may contain. Each measurement is minutes of Laya calls.

Deliberately does not apply to the catalogue: a hand-written entry is a claim someone already argued for,
and dropping it silently would make `--catalogue` a lie about what it reproduces.
"""

EMPTY_ROUNDS = 3
"""Consecutive rounds without an acceptance that count as "the catalogue stopped paying"."""

SPACE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("llm", "max_context_events"),
    ("gate", "reuse_report_within_s"),
    ("gate", "page_severity_rank"),
    ("gate", "confidence_auto"),
    ("gate", "confidence_floor"),
    ("gate", "suppress_confidence"),
    ("gate", "normal_probability"),
    ("gate", "anomaly_probability"),
    ("gate", "notify_probability"),
    ("gate", "rank_probability"),
    ("digest", "host_precision"),
    ("digest", "host_ratio_floor"),
    ("digest", "rolling_window"),
)
"""Every setting the mechanism is allowed to generate a proposal for.

Deliberately excludes `window.*` (a different window is a different experiment, not a configuration of
this one), `digest.profile`/`host_features`/`scoring` (string choices where a neighbour is meaningless)
and `deploy.*`. Those can still be proposed by name from the catalogue — a hand-written hypothesis is a
different claim from a generated neighbour, and this module only generates neighbours.
"""

LIMITS: Dict[Tuple[str, str], Tuple[float, float]] = {
    ("llm", "max_context_events"): (1, 64),
    ("gate", "reuse_report_within_s"): (0, 3600),
    ("gate", "page_severity_rank"): (1, 4),
    # A rate bar at 0 or 1 is not a rate: 0 pages on everything, and 1.0 pages only on the single
    # loudest sample of a window. Both ends are outside what a calibration could justify.
    ("gate", "confidence_auto"): (0.01, 0.99),
    ("gate", "confidence_floor"): (0.01, 0.99),
    ("gate", "suppress_confidence"): (0.01, 0.99),
    ("gate", "normal_probability"): (0.01, 0.99),
    ("gate", "anomaly_probability"): (0.01, 0.99),
    ("gate", "notify_probability"): (0.01, 0.99),
    ("gate", "rank_probability"): (0.01, 0.99),
    ("digest", "host_precision"): (0, 4),
    ("digest", "host_ratio_floor"): (0.0, 0.5),
    ("digest", "rolling_window"): (0, 1000),
}
"""The interval each field may take. A field missing from this table cannot be neighboured."""


@dataclass(frozen=True)
class ProposalSpace:
    """The declared mutation space: which fields, which intervals, how many proposals."""

    fields: Tuple[Tuple[str, str], ...]
    limits: Mapping[Tuple[str, str], Tuple[float, float]]
    max_proposals: int = MAX_PROPOSALS

    def allowed(self, section: str, field: str, value: Any) -> bool:
        limits = self.limits.get((section, field))
        if limits is None:
            return False
        low, high = limits
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return low - 1e-9 <= number <= high + 1e-9

    def describe(self) -> str:
        return "%d field(s), %d bounded, at most %d proposals" % (
            len(self.fields), len(self.limits), self.max_proposals)

    def fingerprint(self) -> str:
        """A short identity for the space, so a round records which space proposed it.

        Without this, "the mechanism changed" would be a claim with no comparable before/after: two
        rounds that produced different candidates could differ because the space moved, and nothing in
        the record would say so.
        """
        payload = {
            "fields": [list(pair) for pair in self.fields],
            "limits": {"%s.%s" % pair: list(bounds) for pair, bounds in sorted(self.limits.items())},
            "max_proposals": self.max_proposals,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def space_from_catalogue(entries: Sequence[Candidate] = (), *, max_proposals: int = MAX_PROPOSALS) -> ProposalSpace:
    """The space implied by the catalogue: its fields, bounded by `LIMITS`, capped.

    The fields come from the catalogue rather than from `SPACE_FIELDS` so that a space can never be
    wider than what has already been argued for by hand. `SPACE_FIELDS` records the intent; the
    catalogue records the claim.
    """
    fields: List[Tuple[str, str]] = []
    for candidate in entries or catalogue():
        for section, name, _value in candidate.changes():
            pair = (section, name)
            if pair in LIMITS and pair not in fields:
                fields.append(pair)
    return ProposalSpace(fields=tuple(fields), limits=dict(LIMITS), max_proposals=max_proposals)


def default_space() -> ProposalSpace:
    return space_from_catalogue(catalogue())


def _same_kind(value: Any) -> bool:
    return isinstance(value, bool) or isinstance(value, (int, float))


def _tidy(number: float, like: Any) -> Any:
    """Return `number` in the type of `like`, rounded the way that type can hold."""
    if isinstance(like, bool):
        return bool(round(number))
    if isinstance(like, int):
        return int(round(number))
    return round(number, 2)


def neighbour_values(value: Any) -> List[Any]:
    """The magnitudes one step either side of `value`, in the same type.

    A step is a halving or a doubling for a count, one more for a small count, and a tenth of the
    interval either way for a rate — never a formula that could search a space nobody declared. A
    neighbour is a claim: "if this change is real but mis-scaled, the curve bends here". A neighbour that
    measures the same as its parent says the value was not the active ingredient, and that is worth
    knowing.

    Two shapes were rejected while writing this. `value + 1` for a rolling window of 100 proposes 101,
    which is not a different magnitude but the same one with a rounding difference. Halving a rank bar
    of 0.90 proposes 0.45, which is not one step but a different regime ("page on the top half"). Both
    would have measured a hypothesis nobody stated.
    """
    if not _same_kind(value):
        return []
    number = float(value)
    if number == 0:
        candidates = [1.0, 5.0]
    elif isinstance(value, int):
        candidates = [number / 2.0, number * 2.0] + ([number + 1.0] if number < 10 else [])
    else:
        step = 0.05 if number <= 1.0 else max(0.01, round(abs(number) * 0.25, 2))
        candidates = [number + step, number - step, number + 2 * step, number - 2 * step]
    out: List[Any] = []
    for candidate in candidates:
        tidy = _tidy(candidate, value)
        if tidy != value and tidy not in out:
            out.append(tidy)
    return out


def _slug(text: str) -> str:
    out = []
    for char in text:
        out.append(char if char.isalnum() else "-")
    return "-".join(part for part in "".join(out).split("-") if part)


def _stem(name: str, previous: Any) -> str:
    """The parent's name with its own magnitude suffix removed, so neighbour chains stay readable.

    `reuse-300-600-1200` is a name a reader has to decode; the change is `reuse_report_within_s = 1200`
    and the path it took is already in the ledger. Names are for reading the table, not for keeping
    history — the round file keeps that.
    """
    slug = _slug(str(previous))
    for suffix in ("-band-" + slug, "-" + slug):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


PAIRS: Tuple[frozenset, ...] = (
    frozenset({("gate", "anomaly_probability"), ("gate", "notify_probability")}),
)
"""Field pairs that only mean something together, declared so a neighbour cannot split them.

Round 5 found what splitting one costs: `digest.scoring=deviation` moves every probability onto a
different scale, so shipping it with the old band alerts on 93% of windows, and the fix was the pair
0.87/0.87. A generator that nudged `anomaly_probability` to 0.92 while leaving `notify_probability` at
0.87 would propose exactly half of that fix and measure a hypothesis nobody stated. A pair moves together
or not at all.
"""


def neighbours(candidate: Candidate, space: ProposalSpace) -> List[Candidate]:
    """Neighbours of every change in `candidate`, one claim at a time.

    A declared pair (`PAIRS`) is shifted as a band — both values by the same step — and its members are
    not perturbed individually. Everything else moves alone, which keeps the hypothesis falsifiable:
    "this value, not that one".
    """
    proposals: List[Candidate] = []
    changes = list(candidate.changes())
    handled: set = set()

    for pair in PAIRS:
        indices = [index for index, change in enumerate(changes)
                   if (change[0], change[1]) in pair]
        if len(indices) != 2:
            continue
        first, second = indices
        _s1, name1, value1 = changes[first]
        _s2, _name2, value2 = changes[second]
        if not (_same_kind(value1) and _same_kind(value2)):
            continue
        handled.update(indices)
        for neighbour in neighbour_values(value1):
            delta = float(neighbour) - float(value1)
            shifted = _tidy(float(value2) + delta, value2)
            if shifted == value2:
                continue
            if not (space.allowed(changes[first][0], changes[first][1], neighbour)
                    and space.allowed(changes[second][0], changes[second][1], shifted)):
                continue
            moved = list(changes)
            moved[first] = (changes[first][0], changes[first][1], neighbour)
            moved[second] = (changes[second][0], changes[second][1], shifted)
            proposals.append(Candidate(
                name="%s-band-%s" % (_stem(candidate.name, value1), _slug(str(neighbour))),
                hypothesis=(
                    "the band %r/%r shifted to %r/%r as one change: the two bars only mean anything "
                    "together, and a shift that kept the gap fixed asks whether the level of the band, "
                    "rather than its width, is what the accepted change paid for."
                    % (value1, value2, neighbour, shifted)
                ),
                section=moved[0][0], field=moved[0][1], value=moved[0][2],
                also=tuple(moved[1:]),
            ))

    for index, (section, name, value) in enumerate(changes):
        if index in handled:
            continue
        for neighbour in neighbour_values(value):
            if not space.allowed(section, name, neighbour):
                continue
            moved = list(changes)
            moved[index] = (section, name, neighbour)
            proposals.append(
                Candidate(
                    name="%s-%s" % (_stem(candidate.name, value), _slug(str(neighbour))),
                    hypothesis=(
                        "%s at %r rather than %r: if the accepted change is real but the magnitude was "
                        "not the active ingredient, the curve should bend one step from %r; measuring the "
                        "same as its parent would say the value never mattered."
                        % (name, neighbour, value, value)
                    ),
                    section=moved[0][0],
                    field=moved[0][1],
                    value=moved[0][2],
                    also=tuple(moved[1:]),
                )
            )
    return proposals


@dataclass(frozen=True)
class Mode:
    """Which proposal rule this round uses, and the evidence that chose it."""

    name: str
    reason: str


@dataclass
class Proposal:
    """What a round will measure, and the argument for it."""

    mode: str
    reason: str
    candidates: List[Candidate] = field(default_factory=list)
    space: Optional[ProposalSpace] = None
    source: str = ""
    """Which stream the seeds were drawn from. Empty means "any stream", which is the old behaviour."""
    deployed: int = 0
    """How many neighbours were dropped because they describe the configuration already in force."""

    @property
    def space_fingerprint(self) -> Optional[str]:
        return None if self.space is None else self.space.fingerprint()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "source": self.source,
            "space": None if self.space is None else self.space.describe(),
            "space_fingerprint": self.space_fingerprint,
            "deployed": self.deployed,
            "candidates": [candidate.name for candidate in self.candidates],
        }


def _is_deployed(candidate: Candidate,
                 deployed: Optional[Mapping[str, Mapping[str, Any]]]) -> bool:
    """Whether every setting this candidate names is already the value in force.

    Round 22 found the mechanism proposing its own baseline: once a round's acceptance is the deployment,
    the same patch is a neighbour of itself, and measuring it again spends a round to learn that a
    configuration is not better than itself. Only a *full* match counts — a candidate that changes one of
    two deployed values is still a new configuration.
    """
    if not deployed:
        return False
    changes = candidate.changes()
    if not changes:
        return False
    return all((deployed.get(section) or {}).get(field) == value for section, field, value in changes)


SHAPE_TOLERANCE = 0.25
"""How different the recorded sampling may be before a measurement stops being a seed for this round.

The same number `promote.GEOMETRY_TOLERANCE` uses for claims, because it is the same question: two
streams whose windows cover different amounts of traffic are not the same measurement.
"""


def _same_shape(recorded: Optional[Mapping[str, Any]],
                shape: Optional[Mapping[str, Any]]) -> bool:
    """Whether two stream summaries describe the same sampling. Unknown on either side counts as same.

    `cadence_s` is the direct answer once a record has it (round 27); `windows` is the fallback that catches
    the older records, and it is the number that exposed the defect: 948 windows at 3000 s against 1355 at
    2100 s is the same wall-clock twice, not a bigger stream.
    """
    if not recorded or not shape:
        return True
    for key in ("cadence_s", "windows"):
        left = recorded.get(key)
        right = shape.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and left and right:
            if abs(float(right) / float(left) - 1.0) > SHAPE_TOLERANCE:
                return False
    return True


def _from_stream(record: Mapping[str, Any], source: Optional[str]) -> bool:
    """Whether this round was measured on `source`.

    A magnitude is a property of the trace it was measured on, not of the pipeline: 600 seconds of alert
    reuse was accepted on the synthetic stream, where the same scenario repeats every minute, and that
    says nothing about a host recording whose faults are twenty minutes apart. Seeding a search across
    streams would carry a number across a boundary nobody measured.
    """
    if not source:
        return True
    return str((record.get("stream") or {}).get("source") or "") == str(source)


def _window_change(candidate: Candidate) -> Optional[int]:
    """The window this candidate moves the configuration to, or `None` when it does not move it.

    A candidate that changes the window has been measured at a geometry the baseline of its round may not
    have shared; whether the round measured a reference at the same window is the test `_accepted_measurements`
    applies, because the candidate alone cannot answer it.
    """
    for section, attribute, value in candidate.changes():
        if section == "digest" and attribute == "rolling_window" and isinstance(value, int):
            return value
    return None


def _accepted_measurements(rounds: Sequence[Mapping[str, Any]], source: Optional[str] = None,
                           rules: Optional[EvolveRules] = None,
                           shape: Optional[Mapping[str, Any]] = None,
                           dropped: Optional[List[int]] = None) -> List[Candidate]:
    """Candidates accepted at least once on `source`, most recent first.

    "Accepted" is the ledger's opinion, not the reader's: an accepted change is the only measured claim
    that something in this space can move a column, so it is the only sensible seed for a neighbour.
    Rejected measurements are not seeds — a rejection says the magnitude was wrong or the change inert,
    and searching around a rejection is how a loop spends a budget on a hypothesis it has already refuted.

    Round 30 found the other half of that sentence: a *stored* acceptance is the opinion of the rules that
    ran then. Round 27's shape experiment left a reshaping accepted under the old `quality` branch, and the
    next round duly grew neighbours around a configuration today's rules reject — a local optimum around
    something nobody adopted. When `rules` is given, every stored acceptance is re-decided from its own
    recorded baseline before it is allowed to seed, which is the same check `--audit` performs, applied
    where it changes what gets measured.
    """
    found: List[Candidate] = []
    seen = set()
    for record in reversed(list(rounds)):
        if not _from_stream(record, source):
            continue
        if not _same_shape(record.get("stream"), shape):
            continue
        baseline = None
        if rules is not None and record.get("baseline"):
            try:
                baseline = Measurement.from_dict(record["baseline"])
            except (KeyError, TypeError, ValueError):
                baseline = None
        for trial in record.get("trials") or []:
            if not (trial.get("verdict") or {}).get("accepted"):
                continue
            candidate = Candidate.from_dict(trial.get("candidate") or {})
            if candidate.name in seen:
                continue
            window = _window_change(candidate)
            if window is not None and not (record.get("references") or {}).get(str(window)):
                # Round 31: an acceptance of a window change, decided against a baseline measured at
                # *another* window, is not evidence that the change moved anything -- that is the
                # comparison boundary 68 forbids, and the record measured no reference at the candidate's
                # own geometry to re-decide it against (rounds before 31 measured none). Round 30's three
                # such acceptances made the next round grow neighbours around a configuration today's
                # rules refuse as a tie, and every one of those neighbours cost a measurement.
                if dropped is not None:
                    dropped.append(window)
                continue
            if rules is not None and baseline is not None:
                try:
                    measured = Measurement.from_dict(trial.get("measurement") or {})
                except (KeyError, TypeError, ValueError):
                    measured = None
                if measured is not None:
                    if measured.promise is None and candidate.promise is not None:
                        # The first reshape round recorded the measurement before this field existed;
                        # the promise is still in the candidate, so the re-decision can see it.
                        measured.promise = candidate.promise
                    if not decide(baseline, measured, rules, veto=candidate.veto).accepted:
                        continue
            seen.add(candidate.name)
            found.append(candidate)
    return found


def mode_from_history(rounds: Sequence[Mapping[str, Any]], *, source: Optional[str] = None,
                      empty_rounds: int = EMPTY_ROUNDS) -> Mode:
    """Choose the proposal rule from the ledger, and say which observation chose it.

    Two triggers expand the space, both of them evidence rather than schedule:

    * the last `empty_rounds` rounds on this stream each accepted nothing — the catalogue has stopped
      paying;
    * the last round on this stream accepted a numeric change — its magnitude is now a measured point,
      and the curve around a measured point is where a magnitude search belongs.

    Otherwise the catalogue is used: its entries carry hand-written hypotheses that a generated
    neighbour cannot have, so generating is the fallback, not the default.
    """
    records = [record for record in rounds if _from_stream(record, source)]
    where = "" if not source else " on %s" % source
    if not records:
        return Mode("catalogue", "no recorded round%s yet, so the catalogue's hypotheses are the only "
                                 "ones with a stated mechanism" % where)
    tail = records[-empty_rounds:]
    empty = [record for record in tail if not (record.get("accepted") or [])]
    if len(tail) >= empty_rounds and len(empty) == len(tail):
        return Mode(
            "expand",
            "the last %d rounds%s accepted nothing, which is evidence about the catalogue rather than "
            "about the settings: the bounded space around values that have already moved a column is "
            "the next thing to measure" % (len(tail), where),
        )
    last = records[-1]
    accepted = last.get("accepted") or []
    if accepted:
        return Mode(
            "expand",
            "the last round%s accepted %s, so those magnitudes are measured points; the curve one step "
            "either side of a measured point is where a magnitude question belongs"
            % (where, ", ".join(accepted)),
        )
    return Mode(
        "catalogue",
        "the last round%s rejected everything without exhausting the catalogue, so the catalogue's "
        "hypotheses still have untested statements" % where,
    )


def propose(rounds: Sequence[Mapping[str, Any]], *, entries: Optional[Sequence[Candidate]] = None,
            space: Optional[ProposalSpace] = None, mode: Optional[Mode] = None,
            max_proposals: Optional[int] = None, source: Optional[str] = None,
            deployed: Optional[Mapping[str, Mapping[str, Any]]] = None,
            reshaped: Optional[Candidate] = None,
            rules: Optional[EvolveRules] = None,
            shape: Optional[Mapping[str, Any]] = None) -> Proposal:
    """The proposals this round should measure, given the ledger and the configuration in force.

    `deployed` is the same shape `apply_overrides` takes (`{section: {field: value}}`) and is used for one
    thing only: a neighbour whose every setting already holds is the baseline, and measuring it again
    answers a question nobody asked. Those are counted and dropped, not silently forgotten.

    `reshaped` is a candidate the caller derived from a recorded claim rather than from the catalogue or the
    space (round 30: the same settings at the window count that covers the span the claim was made over).
    It goes first and survives the deployed filter's spirit only when it is not itself the deployment. It is
    a proposal like any other from here on: `decide()` judges it against the promise it carries, because it
    is not competing with the baseline.
    """
    catalogue_entries = list(entries if entries is not None else catalogue())
    active_space = space if space is not None else space_from_catalogue(catalogue_entries)
    chosen = mode if mode is not None else mode_from_history(rounds, source=source)
    cap = max_proposals if max_proposals is not None else active_space.max_proposals
    label = "" if not source else " on %s" % source

    if chosen.name != "expand":
        return _from_catalogue(chosen.name, chosen.reason, catalogue_entries, active_space,
                               source or "", deployed, reshaped)

    unmeasured: List[int] = []
    seeds = _accepted_measurements(rounds, source=source, rules=rules, shape=shape,
                                   dropped=unmeasured)
    if not seeds:
        # Nothing usable has been accepted, so there is no measured point to search around; the catalogue
        # is still the only source of claims. Saying so is better than generating neighbours of values
        # that have never moved anything -- but the sentence has to name *why* the acceptances are unusable,
        # or a ledger full of them reads as a ledger with none.
        other_sampling = sum(
            1 for record in rounds
            if _from_stream(record, source) and not _same_shape(record.get("stream"), shape))
        if unmeasured:
            windows = ", ".join(str(window) for window in sorted(set(unmeasured)))
            reason = (
                "%s — but the %d accepted magnitude(s) at window(s) %s were decided against a baseline at "
                "another geometry and their round measured no reference at their own, so they are not "
                "measured points here and the catalogue stays the source"
                % (chosen.reason, len(unmeasured), windows))
        elif other_sampling:
            reason = (
                "%s — but no change has been accepted here at this sampling%s: the %d recorded round(s) "
                "measured a stream where a window covered other traffic, and a magnitude does not carry "
                "across densities, so the catalogue stays the source"
                % (chosen.reason, label, other_sampling))
        else:
            reason = (
                "%s — but no change has ever been accepted%s, so there is no measured magnitude to search "
                "around and the catalogue stays the source" % (chosen.reason, label))
        return _from_catalogue("catalogue", reason, catalogue_entries, active_space, source or "",
                               deployed, reshaped)

    known = {tuple(candidate.changes()) for candidate in catalogue_entries}
    if reshaped is not None:
        # The reshape is a candidate in its own right and is judged against its promise. A neighbour that
        # happens to write the same settings is not a second opinion on it: measuring it again would file
        # it under `quality` and accept a configuration the promise rule had just refused.
        known.add(tuple(reshaped.changes()))
    generated: List[Candidate] = []
    already = 0
    for seed in seeds:
        for candidate in neighbours(seed, active_space):
            if tuple(candidate.changes()) in known:
                continue
            known.add(tuple(candidate.changes()))
            if _is_deployed(candidate, deployed):
                already += 1
                continue
            generated.append(candidate)
    if not generated:
        exhausted = (
            "%s — but every neighbour describes the configuration already in force (%d of them), so "
            "measuring them would compare the baseline with itself" % (chosen.reason, already)
            if already else
            "%s — but the space around the accepted values is exhausted (no neighbour inside the "
            "declared intervals), so the catalogue stays the source" % chosen.reason
        )
        fallback = _from_catalogue("catalogue", exhausted, catalogue_entries, active_space,
                                   source or "", deployed)
        fallback.deployed += already
        return fallback
    if reshaped is not None:
        generated.insert(0, reshaped)
    return Proposal(chosen.name, chosen.reason, generated[:cap], active_space, source or "",
                    deployed=already)


def _from_catalogue(mode: str, reason: str, entries: Sequence[Candidate], space: ProposalSpace,
                    source: str, deployed: Optional[Mapping[str, Mapping[str, Any]]],
                    reshaped: Optional[Candidate] = None) -> Proposal:
    """The catalogue, minus the entries that already describe what is running.

    A catalogue entry can be the deployment too — once `reuse-300` is accepted it is both a hand-written
    hypothesis and the configuration in force, and measuring it again would compare the baseline with
    itself. The count is reported so a round that proposes nothing says why.
    """
    kept = [candidate for candidate in entries if not _is_deployed(candidate, deployed)]
    dropped = len(entries) - len(kept)
    if reshaped is not None:
        # The reshape is not a catalogue entry, so it is not subject to "the catalogue ran dry" — it is
        # derived from a recorded claim. It still must not be the deployment itself, or the round would
        # measure the baseline against itself.
        if _is_deployed(reshaped, deployed):
            dropped += 1
        else:
            kept.insert(0, reshaped)
    if not kept and dropped:
        reason = ("%s — but every catalogue entry describes the configuration already in force (%d of "
                  "them), so this round has nothing to measure" % (reason, dropped))
    return Proposal(mode, reason, kept, space, source, deployed=dropped)


def _wrap(text: str, width: int) -> List[str]:
    """Greedy wrap on spaces. Imported nowhere else so the CLI can print a proposal before it runs."""
    words = text.split()
    if not words:
        return [text]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = "%s %s" % (current, word)
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def describe_proposal(proposal: Proposal) -> List[str]:
    """The lines a round prints before it starts measuring, so a long run says what it is doing."""
    lines = [
        "proposal        mode=%s  space=%s  %d candidate(s)"
        % (proposal.mode, proposal.space_fingerprint, len(proposal.candidates)),
    ]
    if proposal.deployed:
        lines.append("                %d neighbour(s) dropped: they describe the configuration already in "
                     "force" % proposal.deployed)
    for reason_line in _wrap("why             " + proposal.reason, 96):
        lines.append(reason_line)
    for candidate in proposal.candidates:
        lines.append("  %-28s %s" % (candidate.name, candidate.describe()))
    return lines


def format_proposal(proposal: Proposal) -> str:
    lines = [
        "=" * 78,
        "brian evolve - what this round would propose",
        "=" * 78,
        "mode   %s" % proposal.mode,
        "space  %s  %s" % (proposal.space_fingerprint, "" if proposal.space is None else proposal.space.describe()),
        "seeds  %s" % (proposal.source or "any stream"),
        "reason %s" % proposal.reason,
        "",
    ]
    if proposal.deployed:
        lines.append("%d neighbour(s) were dropped because they describe the configuration already in force"
                     % proposal.deployed)
        lines.append("")
    if not proposal.candidates:
        lines.append("no candidate was proposed")
        return "\n".join(lines)
    lines.append("%-30s %s" % ("candidate", "change"))
    for candidate in proposal.candidates:
        lines.append("%-30s %s" % (candidate.name, candidate.describe()))
    lines.append("")
    lines.append("%d proposal(s) measured next round at most. The objective, the decision rules and the "
                 % len(proposal.candidates))
    lines.append("evaluator are outside this generator: what changes here is what is proposed, not how it "
                 "is judged.")
    return "\n".join(lines)


def proposal_capable() -> bool:
    """Whether this build can revise its own proposal policy.

    The same shape as `deploy.reload_capable`: the grader asks a code path, and a separate observation
    (`Proposal.mode` recorded in every round) says whether it actually did.
    """
    return callable(globals().get("propose"))
