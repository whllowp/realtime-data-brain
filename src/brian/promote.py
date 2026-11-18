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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import ENV_VARS, Settings
from .deploy import overrides_from_state
from .evolve import Candidate, EvolveRules, Measurement

GEOMETRY_TOLERANCE = 0.25
# How long a recorded verdict speaks for the claim. A check is evidence about the day it ran: a `holds`
# from a year ago and an `unmeasurable` from last March are both statements nobody has tested since, and
# `--verify` selected neither of them again. 14 days is deliberately short -- the measurement costs a few
# seconds, and the campaign is supposed to be read as a picture of *now*, not of whenever someone looked.
CLAIM_EXPIRY_DAYS = 14.0

CURRENT = "current.json"
DOMAINS = "domains"
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
    created_at: str = ""
    verification: Optional[Dict[str, Any]] = None
    reverted_at: str = ""
    # Earlier checks, oldest first. A version whose claim was retracted and later re-established keeps
    # both records: the retraction is evidence about how the claim was tested, not a clerical error.
    checks: List[Dict[str, Any]] = field(default_factory=list)

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
            "created_at": self.created_at,
            "verification": self.verification,
            "reverted_at": self.reverted_at,
            "checks": list(self.checks),
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
            created_at=str(payload.get("created_at") or ""),
            verification=payload.get("verification"),
            reverted_at=str(payload.get("reverted_at") or ""),
            checks=list(payload.get("checks") or []),
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
    }


def _write_state(root: Path, state: Mapping[str, Any], domain: Optional[str] = None) -> Path:
    path = state_path(root, domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"env": dict(state.get("env") or {}),
               "sources": dict(state.get("sources") or {}),
               "updated_at": _now()}
    if domain:
        payload["domain"] = domain_slug(domain)
        payload["source"] = domain
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


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
        lines.append("%-8s %-26s %-12s %s%s" % ("v%03d" % promotion.version, promotion.change[:26],
                                                promise, note, legacy_mark))
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
