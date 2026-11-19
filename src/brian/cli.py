"""Command line interface: `python -m brian.cli <command>` (or `brian` after install).

Commands are grouped by what they answer:

    RUN IT      demo      the full chain over synthetic telemetry, printing the reasoning
                eval      score System 1 against labelled windows, and say whether it may act
    INSPECT IT  config    effective settings, the question catalog, and the gate policy
                topics    the topic plan (names, keys, purpose)
                lineage   walk the trail backwards from any record id — the audit query
                tail      dump records from a topic
    REBUILD IT  replay    re-derive digests and decisions from the stored events
                calibrate fit confidence temperatures on labelled data
                export    emit fine-tuning data for the Laya checkpoint
                reset     delete the local journal

Every command works with `--offline`-capable defaults: no Kafka, no LLM key, and (for `eval`)
optionally no Laya service, so the plumbing is testable anywhere. The one thing that is never
faked is the Laya call in `demo`/`eval`: a benchmark against a stub measures the stub.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .bus import JournalBus, MemoryBus, open_bus
from .config import ENV_VARS, Settings, load_settings
from .contracts import Event, Lineage, dumps, loads
from .demo import DEFAULT_SEQUENCE, EXPECTED, LabeledWindow, flatten, generate
from .cost import format_ledger, ledger_from_traces
from .eval import Evaluator, format_report, gate_recommendation
from .evolve import (
    EVOLVE_DIR,
    EvolveRules,
    REFUSAL_EXPIRY_DAYS,
    apply_overrides,
    audit,
    build_pool,
    Candidate,
    catalogue,
    ceiling_shortfall,
    debt_history,
    format_audit,
    format_ceiling_shortfall,
    format_limit_drift,
    format_rejudge,
    format_round,
    format_sensitivity,
    format_withheld,
    grade,
    history,
    label_free_cut,
    limit_drift,
    measure,
    persist,
    refusal_plan,
    refusal_sensitivity,
    rejection_provenance,
    reader_scores,
    record_withheld_check,
    rejudge,
    replacement_links,
    retire,
    round_limits_text,
    run_round,
    Withheld,
    withheld_check,
    withheld_due,
    withheld_evidence_age,
    withheld_records,
    withheld_streams,
    BACKFILL_KEY,
    backfilled_links,
    format_repairs,
    repair_legacy,)
from .laya.client import LayaClient, LayaError, LayaUnavailable
from .campaign import budget_text, campaign as campaign_result, format_campaign, span_text
from .laya.gate import explain_thresholds
from .laya.qcatalog import CATALOG_VERSION, catalog_hash, describe as describe_catalog, validate_questions
from .llm.rca import RcaEngine
from .deploy import reload_capable
from .propose import Mode, Proposal, describe_proposal, format_proposal, propose, proposal_capable
from .promote import (
    FAILED,
    LINK_BROKEN,
    UNMEASURABLE,
    VERIFIED,
    apply_promotion,
    claim_check,
    current_state,
    domain_slug,
    format_promotions,
    IN_FORCE_GRACE_DAYS,
    cleared_summary,
    in_force_disputes,
    in_force_history,
    in_force_overrides,
    legacy_state_note,
    load_promotions,
    observe_in_force_disputes,
    stale_in_force_disputes,
    promotions_for_domain,
    provenance_block,
    record_clearing,
    revert_promotion,
    set_state_budget,
    state_budgets,
    state_drains,
    version_label,
    CLAIM_EXPIRY_DAYS,
    rescale_hint,
    rescaled_candidate,
    shape_note,
    stacking_note,
    verification_age_days,
    write_verification,
)
from .pipeline import Pipeline
from .state import BaselineTracker, Windower, build_digest
from .streams import Stream, load_stream_file, summarise
from .topics import Topic, describe as describe_topics, key_field


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _print(payload: Any = "") -> None:
    print(payload, flush=True)


def _stamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _load_events(path: str) -> List[Event]:
    """Read events from a JSONL file of raw events, for `replay`/`decide`."""
    events: List[Event] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(loads(line, Event))
    return events


def _service_report(settings: Settings, laya: LayaClient) -> Dict[str, Any]:
    """Probe Laya and report what the pipeline will actually talk to."""
    try:
        health = laya.health()
    except LayaError as exc:
        return {"reachable": False, "error": str(exc), "url": settings.laya.url}
    report: Dict[str, Any] = {
        "reachable": True,
        "url": settings.laya.url,
        "ready": bool(health.get("ready")),
        "device": health.get("device"),
        "loaded": health.get("loaded"),
        "warmed": health.get("warmed"),
        "boot_s": health.get("boot_s"),
        "warm_s": health.get("warm_s"),
        "configured_model": settings.laya.model,
    }
    try:
        checkpoint = laya.verify_model()
        report["checkpoint"] = checkpoint
        report["model_available"] = True
    except LayaError as exc:
        report["model_available"] = False
        report["model_error"] = str(exc)
    return report


def _tag(verdict: Optional[bool]) -> str:
    if verdict is None:
        return "  ?  "
    return "ANOMALY" if verdict else " normal"


def _action_style(action: Optional[str]) -> str:
    return {
        "page_human": "PAGE",
        "auto_triage": "TRIAGE",
        "deep_analysis": "ANALYSE",
        "observe": "observe",
        "suppress": "suppress",
    }.get(str(action), str(action))


# --------------------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------------------


def cmd_cost(args: argparse.Namespace, settings: Settings) -> int:
    """Run the stream end to end and print what each half cost."""
    sequence = (tuple(s.strip() for s in args.sequence.split(",") if s.strip())
                if args.sequence else DEFAULT_SEQUENCE)
    windows = generate(sequence=sequence, service=args.service, warmup_minutes=args.warmup,
                       seed=args.seed)
    events = flatten(windows)
    gate = type(settings.gate)(**{
        **vars(settings.gate),
        "always_analyze_alerts": args.always_analyze,
        "analyze_actions": args.analyze_actions,
    })
    settings = settings.with_overrides(gate=gate)
    pipeline = Pipeline(settings, bus=MemoryBus(), rca=RcaEngine(settings.llm))
    traces = []
    for event in events:
        pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)

    traces.extend(pipeline.drain_ready(final=True))
    ledger = ledger_from_traces(traces)
    _print("=" * 78)
    _print("brian cost - execution vs analysis")
    _print("=" * 78)
    _print("reasoner          %s" % pipeline.rca.mode)
    _print("always_analyze    %s" % settings.gate.always_analyze_alerts)
    _print("analyze_actions   %s" % settings.gate.analyze_actions)
    _print("")
    _print(format_ledger(ledger))
    share = ledger.share_below_page()
    if share is not None and ledger.total_tokens:
        _print("")
        _print("Interpretation: %.1f%% of the analysis budget went to reports on alerts that did not"
               % (100 * share))
        _print("Each of those reports is still its action's deliverable. The number says what a")
        _print("cheaper-narrative policy could target, not that the work was wasted.")
    if ledger.total_tokens == 0:
        _print("")
        _print("No tokens were spent: the reasoner is in deterministic mode, so there was no analysis")
        _print("bill to measure.")
    return 0


def _evolve_streams(args: argparse.Namespace, settings: Settings
                    ) -> Tuple[Settings, List[Stream], str, Dict[str, Any]]:
    """The window stream a round (or a verification) measures on: a file, or the synthetic demo.

    Extracted because `--verify` must measure on the same footing as the round that made the promise:
    two commands that resolve their stream in two places drift, and a verification that quietly ran on a
    different stream from the promotion would be a transfer test reported as a repetition.
    """
    if args.stream_file:
        stream_set = load_stream_file(Path(args.stream_file))
        settings = apply_overrides(settings, stream_set.overrides)
        streams = stream_set.streams
        source = stream_set.dataset or Path(args.stream_file).name
        return settings, streams, source, dict(stream_set.overrides)
    sequence = (tuple(part.strip() for part in args.sequence.split(",") if part.strip())
                if args.sequence else DEFAULT_SEQUENCE)
    windows = generate(sequence=sequence, service=args.service, warmup_minutes=args.warmup,
                       seed=args.seed)
    streams = [Stream(name=args.service, windows=list(windows),
                      meta={"service": args.service, "seed": args.seed})]
    return settings, streams, "demo", {}


def _strict_exit(strict: bool, moved: int, unreconstructable: int, inconsistent: int = 0,
                 due: int = 0, oldest_days: Optional[float] = None,
                 horizon: float = REFUSAL_EXPIRY_DAYS, unrecorded: int = 0,
                 legacy_unrecorded: int = 0, unrecorded_share: float = 0.0,
                 max_unrecorded: Optional[float] = None, trend_rise: float = 0.0,
                 trend_days: Optional[float] = None, trend_paid: int = 0, trend_arrived: int = 0,
                 trend_tolerance: float = 0.5, max_rise: Optional[float] = None,
                 worst: Optional[Mapping[str, Any]] = None,
                 payable: Optional[Mapping[str, Any]] = None,
                 max_debt: Optional[float] = None,
                 budget_minutes: Optional[float] = None,
                 max_minutes: Optional[float] = None,
                 minutes_by_domain: Optional[Mapping[str, Any]] = None,
                 max_minutes_by_domain: Optional[Mapping[str, Any]] = None,
                 drain_minutes: Optional[float] = None,
                 max_drain: Optional[float] = None,
                 drain_by_domain: Optional[Mapping[str, Any]] = None,
                 drain_ceilings: Optional[Mapping[str, Any]] = None,
                 limit_drift: int = 0, max_limit_drift: Optional[float] = None,
                 ceiling_missing: int = 0, ceiling_rounds: int = 0, ceiling_payable: int = 0,
                 max_ceiling_debt: Optional[float] = None,
                 broken_links: int = 0, in_force: int = 0,
                 in_force_stale: int = 0, in_force_untested: int = 0) -> int:
    """The exit code is what makes an audit a check rather than a report (round 15).

    Four outcomes, three codes: 0 the record still says what today's rules say; 1 a recorded verdict
    would be decided differently now; 2 part of the ledger cannot be replayed at all, because the
    record does not say which reference the round compared against (round 32).

    2 outranks 1 on purpose. A moved verdict is an answer — a rule change rewrote history — while an
    unreconstructable comparison has no answer, and a check that only reports the part it could finish
    is the failure mode this contains: "no verdict moved" printed above a list of trials nobody looked
    at.

    Only `inconsistent` counts against the gate (round 33). A comparison that cannot be replayed because
    the record was written before the geometry was recorded is history: nothing about it is wrong, and
    re-running that round writes a record that can be. Failing the build on it forever would leave the
    check permanently red, which is how a check stops being run.

    `due` is the other kind of debt (round 45): a refusal older than the horizon that nobody measured
    again. That is not history — one command retires it — so it exits 1 like a moved verdict, but it
    prints its own line, because "a rule would decide differently" and "a check has not been run" are
    different statements and a reader must be able to tell them apart. It exits 1 even when no verdict
    moved: exit 0 may only mean the check ran on every record and passed (boundary 74).
    """
    if not strict:
        return 0
    if broken_links:
        # Round 81: the two ends of a replacement relation are written by two different commands, and
        # neither checks the other. A ledger whose books contradict each other cannot be repaired by
        # re-deciding under today's rules, so this is a build defect (exit 2), like `inconsistent`.
        _print("")
        _print("%d replacement link(s) disagree: the retired record and the successor name different "
               "rounds," % broken_links)
        _print("or one end points at a round the ledger does not hold. The two ends cannot both be true, and")
        _print("no re-deciding settles it, so --strict exits 2. `--audit` names each one.")
    if inconsistent:
        _print("")
        _print("%d comparison(s) cannot be reconstructed in records that carry the geometry and still lack "
               "the reference:" % inconsistent)
        _print("that is a defect in this build, so --strict exits 2.")
        return 2
    if in_force:
        # Round 85: the same contradiction, one step further along -- a version running on top of it. The exit
        # code is the one `broken_links` already returns; a second `return 2` for the same finding would not
        # change the answer, it would only look like an independent check.
        _print("")
        _print("%d version(s) in force sit on a replacement link whose two ends contradict each other, so a"
               % in_force)
        _print("deployed configuration inherits the contradiction. `--audit` names the version and the link.")
    if in_force_stale:
        # Round 86: a copied end is a debt with exactly one remedy -- measuring the deployed version again -- so
        # an aged one is charged, and the charge names that command. Before the grace period it stays a report:
        # the note is new, and a check that fails on the first sighting teaches nothing about what changed.
        _print("")
        _print("%d version(s) in force have sat on a copied record for longer than the %.0f-day grace period"
               % (in_force_stale, IN_FORCE_GRACE_DAYS))
        _print("with no re-measurement that could test the claim, so --strict exits 1:")
        _print("`brian evolve --verify --verify-version <n> --stream-file <the file that version measured> --accept-copied`")
        _print("clears it, because the copy is about the record, not about the number.")
        if in_force_untested:
            # Round 89: the command above was already run on at least one of these, and it measured nothing
            # comparable. Naming it again without saying so is the charge that can never be cleared.
            _print("%d of them were measured and the check could not test the claim: measuring again on the"
                   % in_force_untested)
            _print("same stream will compare nothing again. Use the file that version recorded, or retire the")
            _print("version whose claim cannot be checked (boundary 188).")
    if broken_links:
        return 2
    if unreconstructable:
        _print("")
        _print("%d comparison(s) predate the geometry field and cannot be replayed; re-running those rounds "
               "writes records that can be." % unreconstructable)
        _print("They do not fail the check (their rules still move the exit code), but --strict has not "
               "verified them.")
    if due:
        _print("")
        _print("%d refusal(s) are older than the %.0f-day horizon and were not measured again%s:"
               % (due, horizon,
                  "" if oldest_days is None else " (oldest evidence %.0f day(s) old)" % oldest_days))
        _print("a refusal nobody re-tests is an assertion the loop cannot lose, so --strict exits 1.")
        _print("pass --verify-withheld to re-measure them.")
    if legacy_unrecorded:
        # Round 50, same seam as round 33: a round written before the field existed is history nobody can
        # back-fill (writing a path in would invent a measurement), so it is a warning even when a cap is
        # set -- otherwise the gate is red until 40 rounds have been re-run, which is how a gate stops being
        # read. One line, because this debt only falls as new rounds are written.
        _print("")
        _print("%d round(s) predate the stream-file field, so which sampling their totals belong to is not "
               "recorded." % legacy_unrecorded)
        _print("Re-running a round writes a record that says; --unrecorded lists the ones that still decide "
               "something.")
    if unrecorded:
        # Round 50 learned this from a failing test: a round this build writes without a file is not
        # automatically a defect. `brian evolve` with no --stream-file measures the generated demo stream,
        # which has no file to name -- so charging this would leave the demo domain permanently red, the
        # failure round 33 fixed. Named as a warning; the share cap is what a deployment can ask for.
        _print("")
        _print("%d round(s) were written by this build and say no stream file: a round measured on the "
               "generated stream has none to name." % unrecorded)
        _print("Pass --stream-file to make a round's totals attributable to a sampling.")
    if max_unrecorded is not None and unrecorded_share > max_unrecorded:
        _print("")
        _print("%.0f%% of the recorded rounds cannot be placed on a sampling, above the %.0f%% asked for:"
               % (100.0 * unrecorded_share, 100.0 * max_unrecorded))
        _print("re-run the rounds whose totals still decide something, on the file each one measured.")
    if trend_rise > trend_tolerance:
        # Round 56: a debt that grows while the loop is not paying it is the failure the whole queue exists
        # for, and until now the page only printed it. It is a warning rather than a failure by default
        # because this build writes nameless rounds on purpose when it measures the generated demo stream
        # (round 50), so a blanket "a rise fails" would keep the demo domain red forever -- the exit code a
        # check can never clear is the check nobody runs (round 33). The opt-in cap makes it chargeable.
        window = span_text(trend_days)
        rose = max_rise is not None and trend_rise > max_rise
        _print("")
        _print("%.1f point(s) of the unrecorded share came back in the last %s: %d round(s) were re-measured "
               "and %d record(s) arrived." % (trend_rise, window, trend_paid, trend_arrived))
        if rose:
            _print("the share rose by more than the %.1f point(s) asked for, so --strict exits 1."
                   % max_rise)
            _print("re-run the queue, or write the rounds with --stream-file so they name a sampling.")
        elif max_rise is not None:
            _print("the share rose by less than the %.1f point(s) asked for, so --strict does not charge it."
                   % max_rise)
        else:
            _print("a share that grows while nothing is paid is a debt nobody is working on; pass "
                   "--max-share-rise to make it fail the check.")
    # Round 58: the per-domain share is the number a reader of one row actually faces, and until now only the
    # ledger-wide share could be required. The paragraph prints the worst domain either way; the exit code
    # only moves when that domain's rounds can be re-run, so the gate cannot be red for a debt no command pays.
    debt_capped = False
    if max_debt is not None and worst is not None and float(worst.get("share") or 0.0) > max_debt:
        debt_capped = (payable is not None
                       and float(payable.get("share") or 0.0) > max_debt
                       and str(payable.get("domain") or "") == str(worst.get("domain") or ""))
        _print("")
        _print("%s has %.0f%% of its rounds unrecorded (%d of %d), above the %.0f%% asked for:"
               % (worst.get("domain"), 100.0 * float(worst.get("share") or 0.0),
                  int(worst.get("without") or 0), int(worst.get("rounds") or 0), 100.0 * max_debt))
        if debt_capped:
            _print("a row whose rounds are mostly unattributable cannot be read as a measurement, so "
                   "--strict exits 1.")
            _print("%d of those round(s) can be paid by re-running them on the file they measured."
                   % int(payable.get("ready") or 0))
        else:
            _print("no recorded round on it can be re-run (its candidates or its file are not in the record), "
                   "so --strict does not charge it: a gate that can never be cleared is a gate nobody runs.")
    # Round 62: a queue command the reader is told to run is a wait, and a wait can be too long. This charge is
    # narrower than the others in one way and wider in another: it fails only when the *recommended* budget
    # already exceeds the limit (so fewer rounds clear it, which is a command the reader has), and it fails on
    # a budget derived from measurements their own records took, not on an assumption about how long a round is.
    #
    # Round 63: the limit may be per domain, because a queue paying SMD and RCAEval does not wait the same way
    # in both — four and a half minutes against six is not "ten and a half is fine here and not there". A
    # per-domain ceiling overrides the global one for its own domain and is silent about the rest.
    ceilings = {domain_slug(str(name)): float(value)
                for name, value in (max_minutes_by_domain or {}).items()}
    over: List[Tuple[str, float, float]] = []
    for domain, minutes in sorted((minutes_by_domain or {}).items()):
        ceiling = ceilings.get(domain_slug(str(domain)), max_minutes)
        if ceiling is not None and float(minutes) > float(ceiling):
            over.append((str(domain), float(minutes), float(ceiling)))
    minutes_capped = bool(over)
    if over:
        _print("")
        for domain, minutes, ceiling in over:
            _print("the next queue run would spend about %.1f minute(s) on %s, above the %.1f asked for:"
                   % (minutes, domain, ceiling))
        _print("lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.")
    elif (max_minutes is not None and budget_minutes is not None and budget_minutes > max_minutes):
        minutes_capped = True
        _print("")
        _print("the next queue run would spend about %.1f minute(s), above the %.1f asked for:"
               % (budget_minutes, max_minutes))
        _print("lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.")
    # Round 65: the whole queue has a price of its own. "Four minutes a run" and "twenty minutes to empty it"
    # are different promises, and a reader who can only cap the first will discover the second mid-queue.
    drain_ceilings = {domain_slug(str(name)): float(value)
                      for name, value in (drain_ceilings or {}).items()}
    drain_over: List[Tuple[str, float, float]] = []
    for domain, minutes in sorted((drain_by_domain or {}).items()):
        ceiling = drain_ceilings.get(domain_slug(str(domain)), max_drain)
        if ceiling is not None and float(minutes) > float(ceiling):
            drain_over.append((str(domain), float(minutes), float(ceiling)))
    if drain_over:
        minutes_capped = True
        _print("")
        for domain, minutes, ceiling in drain_over:
            _print("emptying the queue would spend about %.1f minute(s) on %s, above the %.1f asked for:"
                   % (minutes, domain, ceiling))
        _print("raise the ceiling, or drain the queue over several runs with --rerun-limit.")
    elif max_drain is not None and drain_minutes is not None and drain_minutes > max_drain:
        minutes_capped = True
        _print("")
        _print("emptying the queue would spend about %.1f minute(s), above the %.1f asked for:"
               % (drain_minutes, max_drain))
        _print("raise the ceiling, or drain the queue over several runs with --rerun-limit.")
    # A check must not fail for something it does not print: the rise is only chargeable when it is large
    # enough to be printed at all, or the reader would get exit 1 with no paragraph explaining it.
    rose = max_rise is not None and trend_rise > max_rise and trend_rise > trend_tolerance
    capped = max_unrecorded is not None and unrecorded_share > max_unrecorded
    # Round 68: a recorded round judged under other ceilings is not a wrong record -- the limits really were
    # those -- so this is chargeable only when asked for, the same way a debt that history created is
    # (boundary 123). The flag names a *count*, because the reader is deciding whether to re-decide rounds.
    drift_capped = (max_limit_drift is not None and limit_drift > float(max_limit_drift))
    if drift_capped:
        _print("")
        _print("%d recorded round(s) were judged under ceilings that differ from today's, above the %d "
               "asked for:" % (limit_drift, int(max_limit_drift)))
        _print("their verdicts were reached under other limits, so --strict exits 1 until they are re-decided")
        _print("with the ceilings in force now, or the cap is raised.")
    # Round 72: the ceiling debt may be capped, but only when the queue can pay it. A share that no command can
    # lower is the always-red gate (boundary 126), so the cap is asked for explicitly and the exit code moves
    # only when `ceiling_payable` covers every missing round -- the same shape as the per-domain debt and the
    # minutes cap. When it cannot be paid the paragraph still prints: silence would read as "nothing owed".
    ceiling_share = (float(ceiling_missing) / float(ceiling_rounds)) if ceiling_rounds else 0.0
    if max_ceiling_debt is not None and ceiling_missing and ceiling_share > float(max_ceiling_debt):
        _print("")
        _print("%d of %d round(s) do not record the ceilings they were judged under (%.0f%%), above the %.0f%% "
               "asked for:" % (ceiling_missing, ceiling_rounds, 100.0 * ceiling_share,
                               100.0 * float(max_ceiling_debt)))
        if ceiling_payable >= ceiling_missing:
            _print("re-running the queue would leave none, so --strict exits 1 until it is paid.")
            _print("run `brian evolve --rerun-unrecorded` to measure them again with their ceilings.")
        else:
            _print("only %d of them can be re-run, so --strict does not charge it: a gate that can never be "
                   "cleared is a gate nobody runs." % max(0, int(ceiling_payable)))
            _print("nothing was charged; the debt is on the page above.")
    ceiling_capped = (max_ceiling_debt is not None and ceiling_missing
                      and ceiling_share > float(max_ceiling_debt) and ceiling_payable >= ceiling_missing)
    return 1 if (moved or due or capped or rose or debt_capped or minutes_capped or drift_capped
                 or ceiling_capped or in_force_stale) else 0

def _limits_view(run_all: Optional[float], run_by_domain: Mapping[str, float],
                 run_from_flag: Mapping[str, float], drain_all: Optional[float],
                 drain_by_domain: Mapping[str, float],
                 drain_from_flag: Mapping[str, float]) -> Dict[str, Any]:
    """The wait ceilings this page was judged against, each with the place it came from (round 65).

    A reader comparing two reports needs to know whether a ceiling was recorded in a deployment state or typed
    on the command line: the first is a policy that will still be there tomorrow, the second was today's
    decision. Printing only the numbers makes the two indistinguishable, which is how a report becomes
    unreviewable without anyone noticing.

    The origin is read from the flags that were given, not from "does the state also have a number": a flag that
    overrides a recorded policy is today's decision even when both carry the same value, and attributing it to
    the state would tell a later reader that nobody overrode anything.
    """
    def origin(name: str, from_flag: Mapping[str, float]) -> str:
        return "flag" if domain_slug(str(name)) in from_flag else "state"

    return {
        "max_minutes": run_all,
        "max_minutes_by_domain": {str(name): float(value) for name, value in run_by_domain.items()},
        "max_minutes_origin": {str(name): origin(name, run_from_flag) for name in run_by_domain},
        "max_drain_minutes": drain_all,
        "max_drain_by_domain": {str(name): float(value) for name, value in drain_by_domain.items()},
        "max_drain_origin": {str(name): origin(name, drain_from_flag) for name in drain_by_domain},
    }


def _ceiling_entry(entry: Any, flag: str) -> Tuple[str, Optional[float], bool]:
    """Parse one `domain=minutes` ceiling entry, including the word that withdraws it (round 66).

    A policy that can only be added is a policy that cannot be withdrawn: before this, removing a ceiling meant
    editing `current.json` by hand and hoping the next writer preserved it. `none` (also `-` and `off`) clears
    the value, and everything else must parse as a non-negative number -- a typo that silently cleared a limit
    would be worse than one that stops the command.
    """
    name, _, number = str(entry).partition("=")
    text = number.strip().lower()
    if not name.strip() or not text:
        raise ValueError("%s wants domain=minutes (or domain=none), got %r" % (flag, entry))
    if text in ("none", "-", "off"):
        return name.strip(), None, True
    try:
        value = float(text)
    except ValueError:
        raise ValueError("%s wants domain=minutes (or domain=none), got %r" % (flag, entry))
    if value < 0.0:
        raise ValueError("%s wants a non-negative number of minutes, got %r" % (flag, entry))
    return name.strip(), value, False


def _drift_cap(args: Any, drift: Mapping[str, Any]) -> Optional[float]:
    """The ceiling-drift cap this check should use, arming itself only when the ledger can satisfy it.

    Round 69: asking for a number is explicit and always honoured. The *default* only switches on once every
    live round records the ceilings it was judged under -- until then a default of zero would fail on
    history nobody can back-fill, and a gate that is red for a reason no command can clear is the gate
    nobody runs (round 33). So the default is 0 exactly when `unrecorded` is 0.
    """
    asked = getattr(args, "max_limit_drift", None)
    if asked is not None:
        return float(asked)
    if int(drift.get("rounds") or 0) and not int(drift.get("unrecorded") or 0):
        return 0.0
    return None


def _limits_text(limits: Optional[Mapping[str, Any]]) -> Optional[str]:
    """One sentence naming the ceilings a page was judged against, or None when there are none (round 66).

    Round 65 put this in the JSON so a report could be reviewed later; the page that a reader actually reads
    still printed no ceiling at all, so "nothing was capped" and "several things were capped" looked the same.
    Only ceilings in force are named, each with its origin, because a number whose origin is missing cannot be
    argued with.
    """
    if not limits:
        return None
    pieces: List[str] = []
    run_all = limits.get("max_minutes")
    if run_all is not None:
        pieces.append("one run %.1f minute(s) for every domain (command line)" % float(run_all))
    for name, value in sorted((limits.get("max_minutes_by_domain") or {}).items()):
        origin = (limits.get("max_minutes_origin") or {}).get(name, "state")
        pieces.append("one run %.1f minute(s) on %s (%s)" % (float(value), name,
                                                            "command line" if origin == "flag" else "state"))
    drain_all = limits.get("max_drain_minutes")
    if drain_all is not None:
        pieces.append("a drain of %.1f minute(s) for every domain (command line)" % float(drain_all))
    for name, value in sorted((limits.get("max_drain_by_domain") or {}).items()):
        origin = (limits.get("max_drain_origin") or {}).get(name, "state")
        pieces.append("a drain of %.1f minute(s) on %s (%s)" % (float(value), name,
                                                                "command line" if origin == "flag" else "state"))
    if not pieces:
        return None
    return "; ".join(pieces)


def _domain_name(root: Path, typed: str) -> str:
    """The dataset name a slug refers to, so the state records the name and not the spelling (round 64).

    `--set-domain-limit rcaeval-re1-ob=4` writes into that domain's state, and the file's `source` field is
    what `promotions_for_domain` and the campaign page compare against. Writing the slug there would make the
    domain look like a stream nobody measured, so the typed spelling is resolved against the names the ledger
    actually used; when nothing matches, the typed name is recorded as given rather than silently renamed.
    """
    slug = domain_slug(typed)
    for row in _debt_by_domain(root):
        if domain_slug(str(row[0])) == slug:
            return str(row[0])
    for record in history(root):
        source = str((record.get("stream") or {}).get("source") or "")
        if source and domain_slug(source) == slug:
            return source
    return typed.strip()


def _minute_limits(values: Optional[Sequence[str]]) -> Tuple[Optional[float], Dict[str, float]]:
    """Read `--max-minutes` as a global ceiling and/or one ceiling per domain (round 63).

    The flag used to take one number, so a reader who could wait ten minutes for a whole queue but only
    four for the long SMD recording had to pick a number that was wrong about one of them. A bare number
    stays the global ceiling; `DOMAIN=MINUTES` (or `default=MINUTES`) overrides it for that domain only,
    and the domain is compared as a slug so `rcaeval-re1-ob=4` and `RCAEval RE1-OB` are the same domain.

    An entry that is neither is an error, not a silently ignored flag: a ceiling that does not apply is
    worse than no ceiling, because the page keeps claiming a limit nobody is checking.
    """
    global_ceiling: Optional[float] = None
    by_domain: Dict[str, float] = {}
    for entry in values or []:
        text = str(entry).strip()
        if not text:
            continue
        if "=" not in text:
            try:
                global_ceiling = float(text)
            except ValueError:
                raise ValueError("--max-minutes wants a number or domain=minutes, got %r" % entry)
            continue
        name, _, number = text.partition("=")
        name = name.strip()
        try:
            limit = float(number.strip())
        except ValueError:
            raise ValueError("--max-minutes wants a number or domain=minutes, got %r" % entry)
        if not name:
            raise ValueError("--max-minutes wants a domain before the '=' (use domain=minutes), got %r" % entry)
        if name.lower() == "default":
            global_ceiling = limit
        else:
            by_domain[domain_slug(name)] = limit
    return global_ceiling, by_domain


def _withheld_debt(root: Path, horizon: float) -> Tuple[int, Optional[float]]:
    """How many refusals are due for a re-check, and how old the oldest evidence is (round 45).

    The campaign page counts this per domain; `--strict` reads the same numbers, so a ledger whose
    oldest objection has never been re-tested cannot exit 0 in either mode — and both modes use
    `--refusal-expiry-days`, because two commands disagreeing about the horizon would let a reader pass
    a check by picking the mode that measures less.
    """
    records = withheld_records(root)
    due = withheld_due(records, expiry_days=horizon)
    ages = [withheld_evidence_age(record) for record in records]
    known = [age for age in ages if age is not None]
    return len(due), (max(known) if known else None)


def _unrecorded_debt(root: Path) -> Tuple[int, int, int, float]:
    """Rounds that cannot be placed on a sampling, split by who can answer for it (round 50).

    `persist` has written `stream_path` since round 47, so the key's presence is the seam: a record with the
    key measured on no file at all (the demo stream) and belongs to this build, while a record without it was
    written by a build that could not tell. The first is a debt one command pays; the second is history, and
    charging it to the gate would keep the gate red for as long as the ledger is older than the field — the
    failure round 33 already fixed once (boundary 73).
    """
    rounds = history(root)
    missing = 0
    legacy = 0
    for record in rounds:
        if str(record.get("stream_path") or ""):
            continue
        if "stream_path" in record:
            missing += 1
        else:
            legacy += 1
    share = (float(missing + legacy) / float(len(rounds))) if rounds else 0.0
    return missing, legacy, len(rounds), share


def _stream_library(root: Path) -> List[Tuple[str, Dict[str, Any]]]:
    """Every stream file under `data/`, summarised, so a round can be matched to a file by geometry.

    Round 51: `--unrecorded` used to say "pass --stream-file", which asks the operator to remember the one
    fact the record lost. Geometry answers it instead: the round recorded how many windows it saw and how
    many were scored, and a file whose summary says the same is a candidate. It is a suggestion — nothing is
    written back, and a file that does not match is never proposed (boundary 102).
    """
    library: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted((root / "data").rglob("stream*.json")):
        try:
            stream_set = load_stream_file(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        library.append((str(path.relative_to(root)).replace("\\", "/"), summarise(stream_set.streams)))
    return library


def _matching_streams(record: Mapping[str, Any],
                      library: Sequence[Tuple[str, Dict[str, Any]]]) -> List[str]:
    """Files whose summary matches the geometry this round recorded (round 51).

    Counts are compared exactly; the cadence, the event count and the recording names only when both sides
    recorded them, because rounds written before round 27 have no cadence and the earliest ones no names. The
    names are what separates two files with identical geometry — the two RCAEval files hold the same number of
    windows of the same length and differ only in which cases they contain, so a matcher that stopped at the
    counts had to list both. An empty list means "no file here has that geometry", which is a fact worth
    printing: it says the round measured something this checkout cannot name at all.
    """
    summary = record.get("stream") or {}
    windows = summary.get("windows")
    scored = summary.get("scored")
    if windows is None or not library:
        return []
    out: List[str] = []
    for name, shape in library:
        if shape.get("windows") != windows:
            continue
        if scored is not None and shape.get("scored") != scored:
            continue
        cadence = summary.get("cadence_s")
        theirs = shape.get("cadence_s")
        if cadence is not None and theirs is not None and abs(float(cadence) - float(theirs)) > 1e-6:
            continue
        events = summary.get("events")
        if events is not None and shape.get("events") is not None and shape["events"] != events:
            continue
        names = summary.get("names")
        if names and shape.get("names") and list(shape["names"]) != list(names):
            continue
        out.append(name)
    return out


def _candidate_label(files: Sequence[str]) -> str:
    """How a geometry match reads in the table (round 51).

    Zero is printed as `unknown` rather than left blank: "no file in this checkout has that geometry" is a
    fact about the round, and a blank cell reads as "nobody looked".
    """
    if not files:
        return "unknown"
    if len(files) == 1:
        return files[0]
    return "%d candidates: %s" % (len(files), ", ".join(files))


def _recorded_candidates(record: Mapping[str, Any]) -> List[Candidate]:
    """The candidate definitions a round measured, read back from its record (round 52).

    A re-run has to measure the question the record asked, not the one today's proposer would ask: the first
    version of `--rerun-unrecorded` re-ran the proposal, and its retirement had to say "none of its trials
    appear in the successor — retired as a question, not answered". Reading the candidates out of the record is
    what makes the overlap meaningful, so the round is only skipped when the record kept none.
    """
    out: List[Candidate] = []
    for trial in record.get("trials") or []:
        if not isinstance(trial, Mapping):
            continue
        payload = trial.get("candidate")
        if not isinstance(payload, Mapping):
            continue
        out.append(Candidate.from_dict(payload))
    return out


def _rerun_limit(args: Any, ready: int) -> int:
    """How many of the ready rounds this invocation re-runs (round 54).

    `0` means the whole queue. It is not the default because a round is minutes of measurement (boundary 114);
    it is allowed because the alternative — an operator typing the command thirteen times — is how a debt stops
    being paid. The queue stops at the first failure so a broken round cannot spend the rest (boundary 117).
    """
    value = int(getattr(args, "rerun_limit", 1) or 0)
    if value <= 0:
        return ready
    return min(value, ready)


def _rerun_progress(root: Path) -> Dict[str, Any]:
    """How far the queue of nameable rounds has been paid down (round 53).

    Round 52 turned "which file did this round measure?" into a re-run, one round per invocation. A one-round
    command that never says how many are left cannot answer the only question a reader has about it — "will the
    98 % ever come down?" — so the count is derived from the ledger rather than kept in a second state file: a
    round is paid off when it is retired **into a successor that names a file**. A retirement without a named
    successor (or into a record that still cannot say which file it measured) crossed the question out without
    answering it, which is exactly the defect round 52's overlap check reports.
    """
    matters = _unrecorded_matters(root)
    ready = [item for item in matters if len(item[3]) == 1]
    blocked = [item for item in matters if len(item[3]) != 1]
    rounds = history(root, include_retired=True)
    by_name = {str(row.get("round")): row for row in rounds}
    paid: List[str] = []
    for row in rounds:
        retired = row.get("retired")
        if not isinstance(retired, Mapping):
            continue
        successor = by_name.get(str(retired.get("re_measured_as") or ""))
        if successor is None:
            continue
        if str(successor.get("stream_path") or ""):
            paid.append(str(row.get("round")))
    live = [row for row in rounds if not row.get("retired")]
    without_file = [row for row in live if not str(row.get("stream_path") or "")]
    total = len(live)
    share_now = (len(without_file) / total) if total else 0.0
    # Re-running the rest retires one nameless round and writes one that names its file, so the denominator
    # stays and the numerator falls by one per round. Projecting the end of the queue is the cheapest way to
    # say whether continuing is worth the measurement minutes.
    left = min(len(ready), max(0, len(without_file)))
    share_after = ((len(without_file) - left) / total) if total else 0.0
    return {
        "ready": len(ready),
        "blocked": len(blocked),
        "paid": len(paid),
        "remaining": len(ready),
        "rounds": total,
        "without_file": len(without_file),
        "share_now": share_now,
        "share_after": share_after,
    }


def _unrecorded_matters(root: Path,
                        library: Optional[Sequence[Tuple[str, Dict[str, Any]]]] = None
                        ) -> List[Tuple[str, str, str, List[str]]]:
    """(domain, round, why, candidate files) for rounds nobody can attribute but whose numbers still decide.

    Round 50's second half: the share can only fall by re-running rounds, and re-running all 53 is not worth
    it. A round matters when it wrote a version that is still on the page, or when a candidate it refused is
    still being held. Round 51 adds the candidate files, matched by geometry, so the re-run is a choice rather
    than a recollection.
    """
    provenance = rejection_provenance(root)
    carried: Dict[str, List[str]] = {}
    for name, verdict in provenance.items():
        if verdict.get("accepted"):
            continue
        carried.setdefault(str(verdict.get("round") or ""), []).append(str(name))
    known = list(library) if library is not None else _stream_library(root)
    out: List[Tuple[str, str, str, List[str]]] = []
    for record in history(root):
        if str(record.get("stream_path") or ""):
            continue
        name = str(record.get("round") or "")
        why: List[str] = []
        applied = record.get("applied")
        if applied:
            # The ledger writes the versions that were applied as a list; older lines wrote a count. Count the
            # list where there is one, because the number of versions is what a reader can still act on.
            written = len(applied) if isinstance(applied, (list, tuple)) else int(applied or 0)
            why.append("wrote %d version(s) still on the page" % written)
        held = carried.get(name) or []
        if held:
            why.append("%d refusal(s) still carried" % len(held))
        if not why:
            continue
        domain = str((record.get("stream") or {}).get("source") or "(unattributed)")
        out.append((domain, name, "; ".join(why), _matching_streams(record, known)))
    return out


def _ceiling_matters(root: Path) -> List[Tuple[str, str, str, str]]:
    """(domain, round, why, stream path) for rounds that named their file and still recorded no ceilings.

    Round 73. Round 72's charge could only be aimed at rounds whose stream file was unknown, because those are
    the rounds `--rerun-unrecorded` pays — but a round that recorded its `stream_path` and no ceilings is a debt
    the queue never touches: the file is known and the ceiling is not, and nothing re-measured it. This is the
    list that lets the queue reach those rounds too, so the payable count in the gate is the count of rounds a
    command can actually lower.

    The "still decides something" rule is `_unrecorded_matters`': a round matters when it wrote a version that is
    still on the page, or when a candidate it refused is still held. Re-measuring a round that decided nothing
    would spend minutes to answer a question nobody asks again.
    """
    provenance = rejection_provenance(root)
    carried: Dict[str, List[str]] = {}
    for candidate, verdict in provenance.items():
        if verdict.get("accepted"):
            continue
        carried.setdefault(str(verdict.get("round") or ""), []).append(str(candidate))
    out: List[Tuple[str, str, str, str]] = []
    for record in history(root):
        path = str(record.get("stream_path") or "")
        if not path or record.get("limits"):
            continue
        name = str(record.get("round") or "")
        why: List[str] = []
        applied = record.get("applied")
        if applied:
            written = len(applied) if isinstance(applied, (list, tuple)) else int(applied or 0)
            why.append("wrote %d version(s) still on the page" % written)
        held = carried.get(name) or []
        if held:
            why.append("%d refusal(s) still carried" % len(held))
        if not why:
            continue
        domain = str((record.get("stream") or {}).get("source") or "(unattributed)")
        out.append((domain, name, "; ".join(why), path))
    return out


def _debt_by_domain(root: Path) -> List[Tuple[str, int, int, float, int]]:
    """(domain, rounds, without a file, share, re-runnable) per stream (round 57).

    The campaign page counts the unrecorded rounds of the whole ledger and the rows name their files, but
    nothing said **which domain owes what**, so "where should the next round measure?" was a question the
    operator answered by reading a table by eye. The debt is computed from the live records (a retired round
    has been paid), and "re-runnable" reuses the geometry match: a round whose file cannot be chosen is a
    debt nobody can pay by re-running, so it is counted separately rather than added to the payable total.
    """
    counts: Dict[str, List[int]] = {}
    for record in history(root):
        domain = str((record.get("stream") or {}).get("source") or "(unattributed)")
        entry = counts.setdefault(domain, [0, 0])
        entry[0] += 1
        if not str(record.get("stream_path") or ""):
            entry[1] += 1
    ready: Dict[str, int] = {}
    for domain, _name, _why, files in _unrecorded_matters(root):
        if len(files) == 1:
            ready[domain] = ready.get(domain, 0) + 1
    rows: List[Tuple[str, int, int, float, int]] = []
    for domain, (rounds, without) in counts.items():
        share = (float(without) / float(rounds)) if rounds else 0.0
        rows.append((domain, rounds, without, share, ready.get(domain, 0)))
    rows.sort(key=lambda row: (-row[3], -row[4], row[0]))
    return rows


def _row_dict(row: Optional[Tuple[str, int, int, float, int]]) -> Optional[Dict[str, Any]]:
    """A debt row as a mapping, for the callers that pass it around (round 58)."""
    if row is None:
        return None
    return {"domain": row[0], "rounds": row[1], "without": row[2], "share": row[3], "ready": row[4]}


def _debt_view(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(worst share, largest payable debt) — two different domains and two different questions (round 58).

    `--max-debt-share` asks "is any domain's table mostly unreadable?" — that is the worst share, whether or
    not a re-run can lower it. The charge is only raised when the same domain is also payable, because a
    gate that fails on a debt no command can pay is red forever (round 33's rule, applied to a new number).
    """
    rows = _debt_by_domain(root)
    return _row_dict(rows[0] if rows else None), _largest_debt(root)


def _debt_plan(root: Path) -> List[Dict[str, Any]]:
    """Every payable debt in the order the queue pays them, biggest share first (round 59).

    Round 57 made the page name the largest debtor, which answers "where is the debt?" but not "what happens
    if I pay it?" — the operator runs `--rerun-limit 1`, reads `balance`, and has to work out the next step
    from a table sorted by a different column. The plan is the queue's own order, so the second entry is what
    the next invocation pays; an empty plan is a fact worth printing too.
    """
    return [_row_dict(row) for row in _debt_by_domain(root) if row[4]]


def _round_minutes(root: Path) -> Optional[float]:
    """The median wall-clock minutes a recorded round took, or None when no round timed itself (round 61).

    A budget in rounds is a budget in minutes only for someone who already knows how long a round takes, and
    that is exactly the number the records hold and the page never showed. Records that predate the timestamps
    contribute nothing rather than a guess; a median is used because one slow round should not price the queue.
    """
    lengths: List[float] = []
    for values in _timed_lengths(root).values():
        lengths.extend(values)
    if not lengths:
        return None
    return _median(lengths)


def _timed_lengths(root: Path) -> Dict[str, List[float]]:
    """Wall-clock minutes of every recorded round that timed itself, grouped by domain (round 62).

    One parse for both rates: the queue-wide median and the per-domain medians must be built from the same
    records, or a page could price one domain with a round another rate was derived from.
    """
    lengths: Dict[str, List[float]] = {}
    for record in history(root):
        started, finished = record.get("started_at"), record.get("finished_at")
        if not started or not finished:
            continue
        try:
            begin = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
        except ValueError:
            continue
        seconds = (end - begin).total_seconds()
        if seconds <= 0:
            continue
        domain = str((record.get("stream") or {}).get("source") or "(unattributed)")
        lengths.setdefault(domain, []).append(seconds / 60.0)
    return lengths


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _round_minutes_by_domain(root: Path) -> Dict[str, float]:
    """The median wall-clock minutes each domain's recorded rounds took (round 62).

    One queue can span two domains whose rounds cost different amounts of time — SMD's rounds measure two
    hosts over a long recording, an RCAEval round measures fifteen cases — so a single queue-wide rate prices
    one of them wrongly. A domain with no timed round is simply absent, and `_queue_budget` falls back to the
    queue-wide median for it rather than inventing a rate.
    """
    return {domain: _median(values) for domain, values in _timed_lengths(root).items()}


def _wants_ceilings(args) -> bool:
    """Whether this queue run re-measures the named ceiling debt (round 74).

    A drain (`--rerun-limit 0`) pays everything payable, so it pays this family too; a single step measures one
    round of one family, because a step that silently doubled its own cost would break the promise boundary 114
    makes about the default. `--with-ceilings` overrides the step rule, `--without-ceilings` the drain rule.
    """
    if getattr(args, "without_ceilings", False):
        return False
    if getattr(args, "with_ceilings", False):
        return True
    return int(getattr(args, "rerun_limit", 1) or 0) <= 0


def _ceiling_command(payable: int, named: int) -> Optional[str]:
    """The command that lowers the ceiling debt, or None when no command can (round 74).

    Two families, two commands: a round whose *file* is unknown is paid by the plain queue, a round that named
    its file needs `--with-ceilings`. The sentence on the page names one command, so it names the one that pays
    the family which actually owes something -- and names nothing when both counts are zero, because a command
    that would measure nothing is not a next step (round 59's lesson about true next steps).
    """
    if int(named) > 0:
        return "brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0"
    if int(payable) > 0:
        return "brian evolve --rerun-unrecorded --rerun-limit 0"
    return None


def _queue_entry(kind: str, domain: str, name: str, largest_start: Optional[Mapping[str, Any]],
                 progress: Mapping[str, Any], shape: Mapping[str, Any]) -> Dict[str, Any]:
    """What one invocation of the re-run queue records in the round it paid (round 58, extended in round 78).

    The record lives in the round because the terminal is gone by the time a reader asks why the ledger moved.
    Round 78 adds two facts a reader cannot recover otherwise: which family this entry paid (`file` or
    `ceiling`), and the shape of the invocation that paid it -- number of families and rounds per family, which
    is not the same as the number of rounds (boundary 163: "two families of one round" and "one family of two
    rounds" push different books).
    """
    entry: Dict[str, Any] = {"paid": domain, "paid_round": name, "family": kind, "shape": dict(shape)}
    if kind == "file":
        # Only the file family moves the unrecorded share, so only it has a before/after share and an aim.
        entry["largest_at_start"] = dict(largest_start or {})
        entry["share_after"] = float(progress.get("share_after") or 0.0)
    return entry


def _queue_budget(root: Path, limit: Optional[int] = None,
                  ceiling_limit: Optional[int] = None) -> Dict[str, Any]:
    """What one invocation of the queue would spend and what it would leave behind (round 60, cost in round 61).

    The page printed the queue's size, the plan's first step, and the projected end of the queue as three
    separate lines, so a reader who wanted "if I run it once, where do I end up?" had to combine them. This is
    that combination: the command the reader would type, the rounds it would pay, the domains those rounds
    belong to, and the share before and after — all derived from the same functions the command uses, so the
    page cannot promise a movement the command does not make.

    `limit` is what the reader would type, and its default is the command's own default (one round); `0` means
    "drain the queue", exactly as `_rerun_limit` reads it. Passing the same value to both is the point: a page
    whose budget line describes a different command than the one it recommends is a page arguing with itself.

    Round 61 added `estimated_minutes`, because "three rounds" is not yet an answer to "how long will I be
    waiting" for a reader who does not already know what a round costs.
    """
    plan = _debt_plan(root)
    progress = _rerun_progress(root)
    ready = int(progress.get("ready") or 0)
    requested = int(limit or 0)
    rounds = ready if requested <= 0 else max(0, min(requested, ready))
    without = int(progress.get("without_file") or 0)
    total = int(progress.get("rounds") or 0)
    paid: List[str] = []
    paid_by_domain: Dict[str, int] = {}
    left = rounds
    for row in plan:
        take = min(left, int(row.get("ready") or 0))
        domain = str(row.get("domain"))
        paid.extend([domain] * take)
        paid_by_domain[domain] = paid_by_domain.get(domain, 0) + take
        left -= take
        if left <= 0:
            break
    share_after = ((without - min(rounds, without)) / total) if total else 0.0
    each = _round_minutes(root)
    rates = _round_minutes_by_domain(root)
    # Round 75: a drain pays two families, and the budget priced one. The ceiling family's rounds cost the same
    # minutes as the file family's (both run `run_round`), so a sentence that counts only the file rounds
    # understates the wait by exactly the work it is about to do. `ceiling_limit` is how many ceiling rounds this
    # invocation would pay; `families` is the count the sentence states, because "two families" is a different
    # fact from "N rounds" (item 62: money and dimensions are separate answers).
    # Round 76: `pending` is a fact about the ledger, not about this invocation. Counting it only when the
    # command opened the family made "no debt" and "not this time" print the same thing -- and a reader who
    # passed `--without-ceilings` deserves to see what they asked to leave undone.
    pending = 0
    try:
        pending = len(_ceiling_matters(root))
    except OSError:
        pending = 0
    ceiling_rounds = 0
    if ceiling_limit is not None:
        ceiling_rounds = pending if int(ceiling_limit or 0) <= 0 else max(0, min(int(ceiling_limit), pending))
    ceilings_skipped = max(0, pending - ceiling_rounds)
    families = (1 if rounds else 0) + (1 if ceiling_rounds else 0)
    counted_rounds = rounds + ceiling_rounds
    estimated_total = (each * counted_rounds) if (each is not None and counted_rounds) else None
    minutes_by_domain: Dict[str, float] = {}
    for domain, count in paid_by_domain.items():
        rate = rates.get(domain, each)
        if rate is not None:
            minutes_by_domain[domain] = rate * count
    estimated = sum(minutes_by_domain.values()) if minutes_by_domain else None
    if ceiling_rounds and each is not None:
        # The ceiling family's rounds take the same kind of time; a wait that leaves them out is a wait for a
        # smaller command than the one the reader was told to type.
        estimated = (estimated or 0.0) + each * ceiling_rounds

    # Round 63: price the queue being drained as well as the step, so "pay it all now" and "pay it in steps"
    # are two numbers a reader can compare instead of two words they have to trust.
    drain_by_domain: Dict[str, float] = {}
    for row in plan:
        domain = str(row.get("domain"))
        rate = rates.get(domain, each)
        count = int(row.get("ready") or 0)
        if rate is not None and count:
            drain_by_domain[domain] = rate * count
    drain_minutes = sum(drain_by_domain.values()) if drain_by_domain else None
    runs = -(-ready // rounds) if (ready and rounds) else 0
    # Round 75: the sentence can now be about a command that opens the ceiling family, so the command it names
    # must say so. A drain opens it by default, but "by default" is a fact about this build, not something a
    # reader can see in the string they copy -- and the string is what they will run.
    command = "brian evolve --rerun-unrecorded --rerun-limit %d" % requested
    if ceiling_rounds:
        command += " --with-ceilings"
    return {
        "command": command,
        "rounds": rounds,
        "ceiling_rounds": ceiling_rounds,
        "ceiling_pending": pending,
        "ceilings_skipped": ceilings_skipped,
        "families": families,
        "domains": paid,
        "ready": ready,
        "share_now": float(progress.get("share_now") or 0.0),
        "share_after": share_after,
        "rounds_total": total,
        "minutes_each": each,
        "estimated_minutes": estimated_total,
        "minutes_by_domain": minutes_by_domain,
        "estimated_minutes_by_domain": estimated,
        "drain_by_domain": drain_by_domain,
        "drain_minutes": drain_minutes,
        "runs": runs,
    }


def _largest_debt(root: Path) -> Optional[Dict[str, Any]]:
    """The domain whose share a round could still pay down, or None when nothing is payable (round 57).

    Only a debt with a nameable file is payable by the commands this build has, so a domain that is 100 %
    unrecorded but has no re-runnable round is not the largest *payable* debt even when its share is bigger.
    """
    for domain, rounds, without, share, ready in _debt_by_domain(root):
        if ready:
            return {"domain": domain, "rounds": rounds, "without": without, "share": share,
                    "ready": ready}
    return None


def _order_by_debt(root: Path,
                   ready: Sequence[Tuple[str, str, str, List[str]]]
                   ) -> List[Tuple[str, str, str, List[str]]]:
    """The re-run queue, biggest debtor first (round 57).

    An unordered queue pays whichever stream happened to be measured first, so the loudest domain gets fixed
    and the largest debt does not. The order comes from `_debt_by_domain`, and a domain it does not know
    (a round whose stream was never recorded) keeps its relative place at the end rather than being dropped.
    """
    order = {row[0]: index for index, row in enumerate(_debt_by_domain(root))}
    return sorted(ready, key=lambda item: order.get(item[0], len(order)))


def _print_debt(root: Path, source: str, *, named_file: bool) -> None:
    """Say where the largest payable debt is before the round decides what to measure (round 57).

    A round run without `--stream-file` measures the generated stream, which cannot name a file: it adds one
    unrecorded round and pays none of the debt. That was true since round 50 and printed nowhere, so the loop
    could grow the debt it then had to pay. The line is printed before the proposal, because it is an input
    to the decision and not a postscript to it.
    """
    largest = _largest_debt(root)
    if largest is not None:
        _print("debt              the largest payable debt is on %s: %d of %d round(s) cannot be placed on a "
               "sampling (%.0f%%), %d of them can be re-run from the record"
               % (largest["domain"], largest["without"], largest["rounds"],
                  100.0 * largest["share"], largest["ready"]))
    if named_file:
        return
    mine = next((row for row in _debt_by_domain(root) if row[0] == source), None)
    if mine is None:
        # A domain with no recorded round yet cannot be described by a share: "0 of 0" reads as a measurement
        # of nothing, which is the defect round 27 fixed for an empty stream summary.
        here = "it is the first round on %s" % source
    else:
        here = "%s already has %d of %d unrecorded" % (source, mine[2], mine[1])
    if largest is not None:
        _print("!                 this round measures the generated stream, which has no file to name: it adds "
               "one unrecorded round, %s, and pays none of %s's debt (boundary 124)"
               % (here, largest["domain"]))
    else:
        # Nothing is payable by a re-run, but the round still grows the unrecorded side: saying "no debt" would
        # hide the only fact that matters here, which is that this round's own record will be unattributable.
        _print("!                 no payable debt remains, and this round measures the generated stream, which "
               "has no file to name: its record will be unattributable too (%s)" % here)


def _withheld_pairs(values: Sequence[str]) -> List[Tuple[str, Path]]:
    """`--withheld-stream domain=stream.json`, repeatable, into (domain, path) pairs (round 45).

    One stream re-checks one domain's objections, but the ledger holds objections on several streams and
    the command that retires them should be one command: an operator who has to remember which refusal
    lives where is an operator who re-tests the loudest stream and not the oldest objection.
    """
    pairs: List[Tuple[str, Path]] = []
    for value in values:
        text = str(value or "").strip()
        if "=" not in text:
            raise ValueError("--withheld-stream wants domain=stream.json, got %r" % (value,))
        domain, _, path = text.partition("=")
        domain = domain.strip()
        path = path.strip()
        if not domain or not path:
            raise ValueError("--withheld-stream wants domain=stream.json, got %r" % (value,))
        pairs.append((domain, Path(path)))
    return pairs


def _refusals_on(root: Path, domain: str) -> List[Withheld]:
    """The `w###` records a domain owns, compared by slug (round 45).

    The refusal stores the dataset it was raised on ("SMD", "RCAEval RE1-OB"), while the deployment state and
    every page use the slug. Comparing raw strings meant `--withheld-stream smd=...` reported "nothing due"
    for a record that was right there -- a filter that fails silently, which is the worst kind: the command
    looks like it checked and found nothing.
    """
    slug = domain_slug(domain)
    return [record for record in withheld_records(root)
            if domain_slug(str((record.stream or {}).get("source") or "")) == slug]


def _self_described_pairs(root: Path) -> Tuple[List[Tuple[str, Path]], List[Withheld]]:
    """The (domain, stream file) pairs the refusals name for themselves, and the records that cannot (round 46).

    `--verify-withheld` used to require the operator to remember which file each domain was measured on,
    which makes an objection's re-check depend on a memory rather than on the ledger. Now the round writes
    the path and this reads it. What it does **not** do is guess: `"SMD"` is two files in this repo sampled
    differently (828 scored windows vs 1235), so a path derived from the dataset name would re-check the
    objection on whatever sampling happened to be at that path and report the difference as a result —
    exactly the defect rounds 26 and 44 each had in a different place.
    """
    paths, missing = withheld_streams(root)
    pairs = sorted(((domain, path) for domain, path in paths.items()), key=lambda item: item[0])
    return pairs, missing


def _promotion_stream(promotions: Sequence[Any]) -> Tuple[Optional[str], List[Any], Dict[str, List[Any]]]:
    """The stream file the versions being checked name for themselves (round 47).

    A promise is a statement about one recording, and `SMD` is two files here sampled differently (828 scored
    windows at 3000 s against 1235 at 2100 s), so the file cannot be derived from the dataset name. It is read
    from the record instead: every version a check would look at has to agree on one file, and a version whose
    record predates the field has to be named by hand.

    Returns the agreed path (or None), the versions that name no file, and the versions grouped by the file
    they name -- so the caller can refuse with the list rather than pick one.
    """
    by_path: Dict[str, List[Any]] = {}
    missing: List[Any] = []
    for promotion in promotions:
        path = str(getattr(promotion, "stream_path", "") or "")
        if path:
            by_path.setdefault(path, []).append(promotion)
        else:
            missing.append(promotion)
    if missing or len(by_path) != 1:
        return None, missing, by_path
    return next(iter(by_path)), missing, by_path


def _recheck_withheld(root: Path, domain: str, settings: Any, streams: Any, rules: EvolveRules,
                      horizon: float, *, from_default: bool) -> int:
    """Re-measure this domain's overdue refusals and return how many stopped holding (round 44/45).

    The objection was raised by a round, and a round measures on top of the deployment in force —
    `scoring=auto`, the window count, the bar. Re-measuring on the stream's own base settings answers a
    different question: the first live run of this block "cleared" `host-features-3` at recall 0.146
    because the reader was not configured the way the round had it. A re-check must stand where the
    objection stood. Nothing is promoted here: an objection that stopped holding returns the candidate to
    the rounds (boundary 95).
    """
    records = _refusals_on(root, domain)
    due = withheld_due(records, expiry_days=horizon)
    if not due:
        _print("no refusal recorded on %s is older than the %.0f-day horizon: nothing to re-measure."
               % (domain_slug(domain), horizon))
        return 0
    recheck = settings
    deployment: Dict[str, Dict[str, Any]] = {}
    if not from_default:
        deployment, _sources, _refused_names, _unknown_names = in_force_overrides(
            root, settings, domain=domain)
        if deployment:
            recheck = apply_overrides(settings, deployment)
    # The coverage denominator must be the configuration the objection was measured in, for the same
    # reason the measurement is: `build_pool` fixes the event cap, not the rest of the settings.
    pool = build_pool(recheck, streams)
    checked_stream = summarise(streams)
    _print("re-measuring %d refusal(s) recorded on %s%s"
           % (len(due), domain_slug(domain),
              " against the deployment in force" if deployment else " on the stream's own settings"))
    baseline = measure(recheck, None, streams, pool, budget=rules.fpr_budget,
                       alert_budget=rules.alert_budget)
    cleared = 0
    for record in due:
        candidate = Candidate.from_dict(record.candidate)
        measurement = measure(recheck, candidate, streams, pool,
                              budget=rules.fpr_budget, alert_budget=rules.alert_budget)
        check = withheld_check(baseline, measurement, rules,
                               checked_at=_stamp(), stream=checked_stream,
                               round_name="verify")
        if record_withheld_check(root, record.name, check) is None:
            _print("  %-5s %-22s %s  (an identical check is already recorded, so this one is "
                   "not appended)" % (record.label, record.candidate.get("name", "?"),
                                      check["verdict"]))
            continue
        if check["verdict"] == "no longer":
            cleared += 1
        _print("  %-5s %-22s %-9s recall %.3f at the %.0f%% budget  (bar %.3f)"
               % (record.label, record.candidate.get("name", "?"), check["verdict"],
                  measurement.recall_at_budget, 100 * rules.fpr_budget, rules.replication_bar))
    _print("")
    if cleared:
        _print("%d objection(s) no longer hold at the numbers measured here. A cleared refusal "
               "goes back to the rounds: nothing is promoted by this command." % cleared)
    else:
        _print("every re-measured objection still holds: the numbers that produced it are still "
               "the numbers today.")
    return cleared


def _rules_from_args(args: Any) -> EvolveRules:
    """The objective this run decides under, from the flags that name it.

    One construction point instead of one per branch: `--rejudge`, `--audit`, `--campaign`, `--propose` and
    the round all have to read the same rules, or a flag that moves a verdict in one mode silently does
    nothing in another (round 40 added `--replication-tolerance` and found six copies of the constructor).
    """
    tolerance = getattr(args, "replication_tolerance", None)
    return EvolveRules(min_gain=args.min_gain, replication_tolerance=tolerance)


def _retire_names(values: List[str]) -> List[str]:
    """Flatten `--retire` into one name per round, in the order the operator wrote them.

    The flag repeats and also accepts comma-separated names, because retiring the two records a single
    re-measurement superseded was two commands with the same `--re-measured-as`, and an operator
    transcribing a successor twice is an operator who will eventually mistype one. Duplicates collapse
    to the first mention: retiring the same round twice is idempotent in `retire()`, but printing it
    twice would read as two records.
    """
    names: List[str] = []
    for value in values:
        for piece in str(value).replace(",", " ").split():
            if piece and piece not in names:
                names.append(piece)
    return names


def _skip_refused(selected: Sequence[Any], refused: Sequence[str],
                  *, named: bool) -> Tuple[List[Any], List[Any]]:
    """Split a round's candidates into the ones to measure and the ones a refusal already covers.

    `named` says the caller wrote `--candidates`: an explicit list is an instruction to measure those
    candidates, and quietly dropping one would make the flag silent about the only thing it was asked to
    do. The saving applies to what the *mechanism* proposed (round 36).
    """
    refused_set = set(refused)
    skipped = [candidate for candidate in selected if candidate.name in refused_set]
    if named or not skipped:
        return list(selected), skipped
    return [candidate for candidate in selected if candidate.name not in refused_set], skipped


def _print_refusal_plan(plan: Mapping[str, List[str]]) -> None:
    """Print the standing refusals as the four different answers they are (round 36).

    Round 35 counted them (`35 held, 14 inherited`) and that number was the defect in miniature: three of
    the four reasons to hold a refusal are reasons to measure the candidate *again*, and one is a reason to
    save the measurement. A single count let a reader take the four for the same thing.
    """
    if not any(plan.values()):
        return
    _print("Previously rejected (re-judged under today's rules, so a retired rule cannot poison this):")
    for key, label in (("skip", "refused on this stream at this sampling: not measured again"),
                       ("elsewhere", "refused on another stream only: measured here for the first time"),
                       ("inherited", "refused by a retired record nobody re-checked: re-opened"),
                       ("reopened", "refused at a different sampling: re-measured on these windows"),
                       ("expired", "refused here, but longer ago than the expiry: the evidence has aged, "
                                   "so it is measured again")):
        if not plan.get(key):
            continue
        _print("  %-11s %2d candidate(s)  %s" % (key, len(plan[key]), ", ".join(plan[key])))
        _print("              %s" % label)
    _print("")


def cmd_evolve(args: argparse.Namespace, settings: Settings) -> int:
    """Run the improvement mechanism: propose, measure, select, and (only with --apply) integrate."""
    root = Path(".")
    directory = Path(EVOLVE_DIR)
    # Round 63: the wait ceiling is read once, before any branch, so every mode charges the same limits and a
    # badly formed flag fails loudly here instead of being ignored by the mode the reader happened to pick.
    try:
        minute_ceiling, minute_ceilings = _minute_limits(getattr(args, "max_minutes", None))
    except ValueError as error:
        _print(str(error))
        return 2
    # Round 64: a ceiling recorded in a domain's own state applies without being retyped; an explicit flag for
    # the same domain wins, because today's operator is allowed to disagree with yesterday's policy.
    state_run_ceilings: Dict[str, float] = {}
    state_drain_ceilings: Dict[str, float] = {}
    flag_run_ceilings: Dict[str, float] = dict(minute_ceilings)
    if not getattr(args, "ignore_state_limits", False):
        state_run_ceilings = state_budgets(root)
        # Round 65: the drain ceiling is a second promise in the same file, so it is merged the same way and by
        # the same rule -- the flag for a domain answers today's question, the state answers the standing one.
        state_drain_ceilings = state_drains(root)
        minute_ceilings = {**state_run_ceilings, **minute_ceilings}
    drain_ceiling, drain_ceilings = _minute_limits(getattr(args, "max_drain_minutes", None))
    flag_drain_ceilings: Dict[str, float] = dict(drain_ceilings)
    drain_ceilings = {**state_drain_ceilings, **drain_ceilings}

    # Round 85: round 84 refused to stack a promise on a disputed record, but a version can be applied before
    # its record's link breaks (a retirement or a repair recorded afterwards). The rows are computed once,
    # before any branch, so the campaign page, the audit page and the gate cannot disagree about them.
    # Round 87: the clearings are cited, so the store has to see the verdicts -- a "cleared" row that cites a
    # measurement is a resolution, and one that cites nothing is an absence of observation.
    try:
        deployed = load_promotions(root)
    except OSError:
        deployed = []
    try:
        in_force_rows, in_force_cleared = observe_in_force_disputes(root, in_force_disputes(root),
                                                                   promotions=deployed)
    except OSError:
        in_force_rows, in_force_cleared = [], []
    in_force_broken = sum(1 for item in in_force_rows if item.get("broken"))
    try:
        in_force_stale_rows = stale_in_force_disputes(in_force_rows, deployed)
    except OSError:
        in_force_stale_rows = []
    try:
        in_force_history_rows, in_force_trimmed = in_force_history(root)
    except OSError:
        in_force_history_rows, in_force_trimmed = [], 0
    in_force_record = cleared_summary(in_force_history_rows, trimmed=in_force_trimmed)

    if getattr(args, "max_limit_drift", None) is not None and not (args.audit or args.campaign):
        # Round 68: a flag that only one mode reads must say so, otherwise it is inert in every other mode and
        # the reader believes a check ran (round 40's lesson, in a new place). Round 69 taught the campaign
        # the same comparison, so both modes may carry it.
        _print("--max-limit-drift reads the ceilings recorded in each round, which --audit and --campaign "
               "compare; pass one of them, or drop the flag")
        return 2

    if getattr(args, "max_ceiling_debt", None) is not None and not getattr(args, "strict", False):
        # Round 72: `--max-ceiling-debt` is read by the gate, not by the page, so without --strict it would be
        # inert and the reader would believe a check ran (round 40's lesson, in a third place).
        _print("--max-ceiling-debt caps a --strict charge; without --strict nothing reads it")
        return 2

    # Round 72: the ceiling debt is computed once, before any branch, because the campaign page, the audit gate
    # and the re-run queue must agree about how many rounds lack ceilings and how many of those the queue can
    # pay. `payable` counts a missing round only when exactly one candidate file is known and the round is still
    # live: a re-run writes a new record that carries its ceilings and retires the old one.
    try:
        live_records = history(root, include_retired=False)
    except OSError:
        live_records = []
    ceiling_rows = ceiling_shortfall(live_records)
    ceiling_missing = sum(int(row["missing"]) for row in ceiling_rows)
    ceiling_rounds = sum(int(row["rounds"]) for row in ceiling_rows)
    lacking_limits = {str(record.get("round") or ""): not record.get("limits")
                      for record in live_records}
    ceiling_payable = 0
    try:
        ceiling_payable += sum(1 for _domain, name, _why, files in _unrecorded_matters(root)
                               if len(files) == 1 and lacking_limits.get(str(name), False))
    except OSError:
        pass
    # Round 73: a round that recorded its file and no ceilings was outside the queue's reach, so it was counted
    # as unparyable and the gate could never be aimed at it. `--with-ceilings` gives the queue that path, so
    # those rounds now count as payable -- the charge may only exist where a command can lower it (boundary 154).
    ceiling_named = 0
    try:
        ceiling_named = len(_ceiling_matters(root))
    except OSError:
        ceiling_named = 0
    # Round 74: two numbers that mean different things must not be handed to the same parameter. `ceiling_payable`
    # is the gate's count (both families, because both can now be paid), while the page's sentence distinguishes
    # "the queue reaches it as it stands" from "it needs --with-ceilings"; adding them first made the second
    # clause vanish (named was clamped to missing - payable == 0).
    ceiling_file_payable = ceiling_payable
    ceiling_payable += ceiling_named

    if getattr(args, "set_domain_limit", None) or getattr(args, "set_domain_drain", None):
        written: List[Tuple[str, Optional[float], Optional[float], bool, bool]] = []
        for entry, flag in [(item, "--set-domain-limit") for item in
                            getattr(args, "set_domain_limit", None) or []] + \
                           [(item, "--set-domain-drain") for item in
                            getattr(args, "set_domain_drain", None) or []]:
            try:
                name, value, clear = _ceiling_entry(entry, flag)
            except ValueError as error:
                _print(str(error))
                return 2
            if flag == "--set-domain-limit":
                written.append((name, value, None, clear, False))
            else:
                written.append((name, None, value, False, clear))
        recorded_entries: List[Dict[str, Any]] = []
        cleared_entries: List[Dict[str, Any]] = []
        state_files: List[str] = []
        for name, run_limit, drain_limit, clear_run, clear_drain in written:
            domain = _domain_name(root, name)
            path = set_state_budget(root, domain, run_limit, drain_minutes=drain_limit,
                                    clear_run=clear_run, clear_drain=clear_drain)
            state_files.append(str(path))
            if run_limit is not None:
                _print("recorded          %s: at most %g minute(s) for one queue run" % (domain_slug(domain),
                                                                                        run_limit))
                recorded_entries.append({"domain": domain_slug(domain), "kind": "run",
                                         "minutes": float(run_limit)})
            if clear_run:
                _print("cleared           %s: no queue-run ceiling" % domain_slug(domain))
                cleared_entries.append({"domain": domain_slug(domain), "kind": "run"})
            if drain_limit is not None:
                _print("recorded          %s: at most %g minute(s) to empty the queue" % (domain_slug(domain),
                                                                                         drain_limit))
                recorded_entries.append({"domain": domain_slug(domain), "kind": "drain",
                                         "minutes": float(drain_limit)})
            if clear_drain:
                _print("cleared           %s: no drain ceiling" % domain_slug(domain))
                cleared_entries.append({"domain": domain_slug(domain), "kind": "drain"})
            _print("                  %s" % path)
        # Round 67: a withdrawal used to exist only in the terminal that typed it. The state file cannot carry
        # the fact (an absent key says "never set" as loudly as "withdrawn"), so the command that withdraws
        # says so in its own machine-readable output -- the report of a decision must travel with the decision.
        if args.json_out:
            payload = {"recorded": recorded_entries, "cleared": cleared_entries,
                       "in_force": {"run": state_budgets(root), "drain": state_drains(root)},
                       "files": list(dict.fromkeys(state_files))}
            Path(args.json_out).write_text(_json(payload), encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return 0

    if args.history:
        rounds = history(root, include_retired=True)
        if not rounds:
            _print("no rounds recorded yet in %s" % directory)
            return 0
        _print("=" * 78)
        _print("brian evolve - recorded rounds (oldest first)")
        _print("=" * 78)
        # Round 70: the ceilings each round was judged under live in the record, but nothing printed them, so a
        # reader deciding whether two rows are comparable had to open the JSON. The count of rounds that do not
        # say is printed too: a column that is usually absent should announce that it is usually absent, or its
        # silence reads as "nothing was capped" (boundary 142) and the list looks more uniform than it is.
        today_limits = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                    drain_ceiling, drain_ceilings, flag_drain_ceilings)
        recorded_limits = [record for record in rounds if record.get("limits")]
        # Round 81: the replacement links, checked once for the whole page. Both ends of a relation are
        # printed here (the retired row says what replaced it, the successor says what it replaced), so this
        # is the one page where a disagreement is visible to a reader who does not already suspect one.
        link_rows = replacement_links(rounds)
        # Round 83: an end written by a repair agrees with the other side by construction, so it is counted
        # apart from the links the queue wrote. "Every link agrees" is true of both, and means different
        # things; a page that prints only the first sentence lets a copied statement read as a witness.
        repaired_links = backfilled_links(rounds)
        link_legacy = [item for item in link_rows if item.get("kind") == "legacy"]
        link_defects = [item for item in link_rows if item.get("kind") in ("mismatch", "unretired", "dangling")]
        if link_defects:
            _print("links             %d replacement link(s) disagree with their other end: %s"
                   % (len(link_defects), ", ".join(sorted({str(item["round"]) for item in link_defects}))))
        else:
            # Round 83: "every link agrees" is false when one end was never written, and it says nothing about
            # how an agreement came about. Both are printed on their own line rather than folded into the
            # sentence, because a reader who sees only "agrees" will trust a link that has one end (boundary
            # 174's converse: silence about the missing end reads as completeness).
            if link_legacy:
                _print("links             %d replacement link(s) have no successor-side record (written before "
                       "round 78) and are reported, not charged: %s"
                       % (len(link_legacy), ", ".join(sorted({str(item["round"]) for item in link_legacy}))))
            if repaired_links:
                _print("links             %d replacement link(s) were backfilled by a repair: the agreement is "
                       "copied, not a second opinion" % len(repaired_links))
            if not link_legacy and not repaired_links:
                _print("links             every replacement link agrees with the other end it names")
        _print("ceilings          %d of %d round(s) record the wait ceilings they were judged under; %d do not"
               % (len(recorded_limits), len(rounds), len(rounds) - len(recorded_limits)))
        history_rows: List[Dict[str, Any]] = []
        # Round 71: the count above is the ledger's. A reader asking "where does that debt sit" needs it per
        # stream, and a reader deciding whether to run the queue needs to know which of those rounds it could
        # pay. Both come from the same rows the campaign page prints, so the two pages cannot disagree.
        for row in ceiling_shortfall(rounds):
            if not row["missing"]:
                continue
            _print("  ceiling debt    %-16s %d of %d round(s) do not record them"
                   % (row["domain"], row["missing"], row["rounds"]))
        # Round 75: the campaign page already ends its debt sentence with the command that lowers it; this page
        # named the debt and stopped. A reader told where the debt sits and not what to type has to go looking
        # for the command in a different command's output (boundary 158, applied to the page a reader opens when
        # something is wrong).
        history_command = _ceiling_command(ceiling_file_payable, ceiling_named) if ceiling_rows else None
        if history_command and any(int(row["missing"]) for row in ceiling_rows):
            _print("  measure         %s" % history_command)
        for index, record in enumerate(rounds, start=1):
            baseline_row = record.get("baseline") or {}
            measured = baseline_row.get("tokens_per_report")
            baseline = "n/a" if measured is None else "%.0f" % measured
            before_recall = baseline_row.get("recall_at_budget")
            baseline_recall = "n/a" if before_recall is None else "%.3f" % before_recall
            trials = record.get("trials") or []
            accepted = record.get("accepted") or []
            # Round 71: a hand-written or older record may carry the ledger's *count* where the round file
            # carries the list of names, and merging them left an int here, which crashed the page on
            # `",".join`. A history page is where a reader goes when something is wrong, so it prints what it
            # can and never dies on the shape of a record (it said "accepted=-" in the round-70 demo).
            if not isinstance(accepted, list):
                accepted = []
            if not isinstance(trials, list):
                trials = []
            kept = [t for t in trials if (t.get("verdict") or {}).get("accepted")]
            # Both columns are printed because a round can be accepted on either one: a quality change
            # leaves tokens/report untouched, so a history that shows only the bill reads "nothing
            # happened" about a round that tripled recall at the budget.
            costs = [value for value in ((t.get("measurement") or {}).get("tokens_per_report")
                                         for t in kept) if value is not None]
            after = "%.0f" % min(costs) if costs else "(none accepted)"
            recalls = [value for value in ((t.get("measurement") or {}).get("recall_at_budget")
                                           for t in kept) if value is not None]
            recall_after = "%.3f" % max(recalls) if recalls else "-"
            applied = record.get("applied")
            mark = "" if applied is None or applied else "  [recorded, not applied]"
            retired = record.get("retired")
            if retired:
                # The row stays in the list because a reader asking "what has this loop measured" is
                # asking a different question from "what does it still owe an answer for".
                successor = retired.get("re_measured_as") if isinstance(retired, dict) else None
                mark += "  [retired%s]" % ("" if not successor else ", re-measured as %s" % successor)
            # Round 49: which file this round measured, on its own line. The campaign's header gives the
            # proportion that cannot be attributed; this is the other half of that question, answered one
            # round at a time, and it is where a reader looks after asking "which file was that?".
            stream_file = str(record.get("stream_path") or "")
            mark += "  [%s]" % (stream_file or "no file recorded")
            # Round 79: which book the queue paid when it produced this round. The record has said so since
            # round 78; without this the fact was only readable by opening the JSON, and a page listing
            # finished rounds is exactly where a reader asks "what paid for this one".
            queue_entry = record.get("queue")
            if isinstance(queue_entry, Mapping) and queue_entry.get("family"):
                mark += "  [paid by the %s queue]" % queue_entry.get("family")
                # Round 80: and which record it replaced. The retired round already says what took its
                # place; this is the same sentence from the other side, so a reader never has to match two
                # stamps by eye to answer "which measurement is behind this row".
                replaced = str(queue_entry.get("paid_round") or "")
                if replaced:
                    mark += "  [replacing %s]" % replaced
            # Round 81: a link that disagrees with its other end is marked on both of the rows it names, so a
            # reader who starts at either row sees it without consulting the header.
            own_name = str(record.get("round") or record.get("finished_at") or "")
            for item in link_defects:
                participants = [str(name) for name in (item.get("names") or [])]
                if own_name not in participants and own_name != str(item.get("round")):
                    continue
                # Round 82: `names` is every record the finding involves, so a reader landing on either the
                # retired row, the successor or a second round that nominated the same successor all see it.
                others = sorted({str(item.get("round"))} | set(participants))
                others = [name for name in others if name and name != own_name]
                if others:
                    mark += "  [broken link: %s with %s]" % (item.get("kind"), ", ".join(others))
            _print("%2d  %s  accepted=%-24s recall@b %s -> %s  tokens/report %5s -> %s%s"
                   % (index, record.get("finished_at") or record.get("started_at") or "?",
                      ",".join(accepted) or "-", baseline_recall, recall_after, baseline, after, mark))
            # Round 70: only the rounds that recorded ceilings get a line, and each says whether today's
            # ceilings are still the ones it was judged under. A retired round is compared as well: its record
            # stands as written, and a reader who sees "differs from today's" on a retired row learns that the
            # round was superseded, not that the ledger is inconsistent.
            recorded = record.get("limits")
            if recorded:
                drift = limit_drift([record], today_limits)
                note = "  (differs from today's)" if drift.get("differ") else "  (as in force now)"
                _print("      ceilings  %s%s" % (round_limits_text(recorded), note))
            # Round 72: the same facts the lines above print, in a form a script can read without parsing the
            # text -- including the ceilings payload itself, which the page renders but never emits verbatim.
            history_rows.append({
                "round": str(record.get("round") or record.get("finished_at") or ""),
                "retired": bool(record.get("retired")),
                "ceilings": record.get("limits") or None,
                "ceilings_text": round_limits_text(recorded),
                "ceilings_match_today": (not drift.get("differ")) if recorded else None,
                # Round 79: the queue's verdict on this round, verbatim -- which family paid for it and the
                # shape of the invocation that did (round 78's record). A script asking "was the ceiling
                # family ever paid" should not have to parse this page's marks.
                "queue": record.get("queue") or None,
            })
        if getattr(args, "json_out", None):
            Path(args.json_out).write_text(_json({
                "rounds": history_rows,
                "links": link_rows,
                "ceilings": {"missing": ceiling_missing, "rounds": ceiling_rounds,
                             "payable": ceiling_payable, "by_domain": ceiling_rows},
            }), encoding="utf-8")
        return 0

    if args.rerun_unrecorded:
        # Round 52: round 51 turned "which file did this round measure?" into a printed answer; this turns the
        # answer into a run. A round with exactly one candidate file is re-measured on that file, so its totals
        # become placeable, and the old record retires naming the new one — the debt falls by measurement, not
        # by printing. Two candidate files is not a tie this command breaks: it skips the round and says which
        # files it could not choose between (boundary 104).
        matters = _unrecorded_matters(root)
        ready = [item for item in matters if len(item[3]) == 1]
        blocked = [item for item in matters if len(item[3]) != 1]
        # Round 57: the queue is paid in order of what each domain owes, not in ledger order. A reader who
        # starts at the top of an unordered list pays whichever stream happened to be measured first, which
        # is how the loudest domain gets fixed and the largest debt does not.
        ready = _order_by_debt(root, ready)
        _print("=" * 96)
        _print("brian evolve - re-run the rounds whose stream file can be named")
        _print("=" * 96)
        _print("candidates        %d of %d round(s) have exactly one file; %d cannot be placed"
               % (len(ready), len(matters), len(blocked)))
        for domain, rounds, without, share, re_runnable in _debt_by_domain(root):
            if re_runnable:
                _print("debt              %-16s %d of %d round(s) unrecorded (%.0f%%), %d re-runnable"
                       % (domain, without, rounds, 100.0 * share, re_runnable))
        progress = _rerun_progress(root)
        _print("progress          %d round(s) already re-measured into a named successor; %d still to do"
               % (progress["paid"], progress["remaining"]))
        # Round 61: the command prints the *same* sentence the page recommends, from `budget_text`, instead of
        # its own `projection`/`limit` pair. Round 60 made the page and the command share the arithmetic; this
        # makes them share the words, so the reader recognises the command they were told to run.
        limit = _rerun_limit(args, len(ready))
        # Round 73: the second family. A round that recorded its file and no ceilings cannot be paid by the
        # unrecorded queue (its file is known, so `_unrecorded_matters` never lists it), and until now nothing
        # re-measured it -- the gate's payable count said so correctly and the debt was unpayable. The file in
        # the record is all the queue needs.
        named_pending = _ceiling_matters(root)
        drain = int(getattr(args, "rerun_limit", 1) or 0) <= 0
        ceiling_ready = named_pending if _wants_ceilings(args) else []
        ceiling_limit = _rerun_limit(args, len(ceiling_ready))
        # Round 75: the budget sentence must describe the command that is about to run, and this one may open two
        # families. Passing the ceiling count is what keeps "about N minutes" from pricing a smaller command.
        # `None` means this command does not touch the family at all; `0` means "drain all of it", so an
        # unwanted family must be `None` -- `0` would price work the command was told not to do.
        budget = _queue_budget(root, limit=limit,
                               ceiling_limit=ceiling_limit if ceiling_ready else None)
        _print("budget            %s" % budget_text(budget))
        # Round 76: the price names the families it opens, but not how many rounds each holds. A reader who is
        # waiting on this command wants to see the dimensions next to the money -- "one family of one round" and
        # "two families of one round each" are different jobs at the same count (item 62's second half).
        _print("families          %d: %d round(s) from the file queue%s"
               % (int(budget.get("families") or 0), int(budget.get("rounds") or 0),
                  (", %d from the ceiling queue" % int(budget.get("ceiling_rounds") or 0))
                  if int(budget.get("ceiling_rounds") or 0) else ""))
        # Round 78: the header said what it pays and left the rest unsaid. A reader who passed
        # `--without-ceilings` was told the dimensions of the file family only, and "not this time" again read
        # as "nothing there" (boundary 162). The line below also names the command that would pay it, so the
        # refusal comes with its remedy and not with a second round of guesswork.
        skipped_family = int(budget.get("ceilings_skipped") or 0)
        if skipped_family:
            _print("leaves            %d round(s) that named their file and never recorded their ceilings; "
                   "measure them with %s"
                   % (skipped_family, _ceiling_command(0, skipped_family)))
        queue_shape = {"families": int(budget.get("families") or 0),
                       "file_rounds": int(budget.get("rounds") or 0),
                       "ceiling_rounds": int(budget.get("ceiling_rounds") or 0),
                       "ceilings_skipped": skipped_family}
        # Round 68: the queue judges every round it pays, so it names the ceilings it judges by -- the same
        # sentence the page and `--audit` print, from the same function, so the three cannot drift apart.
        queue_limits_text = _limits_text(_limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                                      drain_ceiling, drain_ceilings, flag_drain_ceilings))
        if queue_limits_text:
            _print("limits            %s" % queue_limits_text)
        if limit == len(ready) and len(ready) > 1:
            # A drain still has to say *before the first measurement* that one broken round stops the rest: a
            # reader who asked for ten rounds should learn that at the top, not from a stack trace halfway down.
            _print("stops             at the first round that cannot be re-run; the rest keep their place")
        if ceiling_ready:
            _print("ceilings          %d round(s) name their file and never recorded their ceilings; "
                   "re-measuring them here%s" % (len(ceiling_ready),
                                                 " (a drain pays this family too)" if drain else ""))
        elif named_pending:
            _print("ceilings          %d round(s) name their file and never recorded their ceilings; "
                   "add --with-ceilings to re-measure them" % len(named_pending))
        for domain, name, _why, files in blocked:
            _print("  skipped         %-32s %s" % (name, _candidate_label(files)))
        failures = 0
        stopped = False
        paid_now = 0
        share_before = float(progress["share_now"])
        # Round 58: which domain owed the most when this command started. Written into each round it pays, so
        # the record says what the payment was aimed at rather than leaving it to the reader's arithmetic.
        largest_start = _largest_debt(root)
        paid_domains: Dict[str, int] = {}
        rules = _rules_from_args(args)
        # Round 73: the per-round work is one function because a second family of rounds needs it -- the rounds
        # that named their file and never recorded a ceiling. The queue reaches them through `--with-ceilings`,
        # which is the only reason the gate may charge that debt (boundary 154).
        def run_one(entry: Tuple[str, str, str, List[str], str]) -> Tuple[bool, bool]:
            kind, domain, name, _why, files = entry
            target_file = files[0]
            record = next((row for row in history(root) if str(row.get("round")) == name), None) or {}
            recorded = _recorded_candidates(record)
            _print("")
            _print("---- re-running %s on %s" % (name, target_file))
            if not recorded:
                # A record with no candidate definitions cannot be measured again: the numbers in it were
                # compared to something nobody wrote down. Retirement would return nothing.
                _print("  this record keeps no candidate definitions, so there is nothing to measure again; "
                       "its totals stay where they are")
                return False, False
            clone = argparse.Namespace(**vars(args))
            clone.stream_file = target_file
            # A re-run is a measurement, not a deployment: it writes a round that names its file, and nothing
            # becomes live because of it (boundary 95, the same rule `--verify` follows).
            clone.apply = False
            run_settings, streams, source, _overrides = _evolve_streams(clone, settings)
            deployment, _sources, _refused, _unknown = in_force_overrides(root, run_settings, domain=domain)
            run_settings = apply_overrides(run_settings, deployment)
            result = run_round(run_settings, streams, selected=recorded, rules=rules, source=source,
                               claim=record.get("claim") or {})
            result.stream_path = target_file
            # Round 68: a re-run is judged under the same ceilings as any other round, so it records them.
            result.limits = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                         drain_ceiling, drain_ceilings, flag_drain_ceilings)
            # Round 58: the record of what this entry of the queue paid belongs in the round it paid, not in
            # the terminal that ran it. `largest_at_start` is the domain a reader would have expected this
            # command to aim at, so a later reader can tell an aimed payment from an incidental one.
            # Round 78: the record also says which family this entry paid and what the invocation opened --
            # a round paid by the ceiling queue and one paid by the file queue are different work, and the
            # record is the only place that survives the terminal (boundary 163).
            result.queue = _queue_entry(kind, domain, name, largest_start, progress, queue_shape)
            _print("")
            _print(format_round(result))
            written = persist(root, result, write_versions=False)
            fresh = [item for item in history(root) if str(item.get("round")) == str(Path(
                written.get("round") or "").name)]
            if not fresh:
                _print("")
                _print("the re-run recorded nothing, so %s stays as it is" % name)
                return False, True
            successor = str(fresh[0].get("round"))
            try:
                mark = retire(root, name, re_measured_as=successor)
            except (OSError, ValueError) as error:
                _print("")
                _print("cannot retire %s: %s" % (name, error))
                return False, True
            _print("")
            _print("recorded round %s" % written.get("round"))
            _print("retired %s (re-measured as %s)" % (name, successor))
            overlap = (mark or {}).get("overlap") or {}
            matches = overlap.get("matches") or []
            rest = overlap.get("not_repeated") or []
            if not overlap.get("successor_readable"):
                _print("  the successor record is not readable, so how much of this round it repeats is "
                       "unknown")
            elif matches:
                _print("  %d of its %d trial(s) appear in the successor: %s"
                       % (len(matches), len(matches) + len(rest), ", ".join(matches)))
                if rest:
                    _print("  the other %d are not repeated there, so this retirement does not claim they "
                           "were answered" % len(rest))
            else:
                _print("  none of its %d trial(s) appear in the successor: this round is retired as a "
                       "question, not answered" % len(rest))
            if kind == "file":
                step = _rerun_progress(root)
                _print("  progress         %d of %d re-measured; %d left, share %.0f%% -> %.0f%% if the rest run"
                       % (step["paid"], step["paid"] + step["remaining"], step["remaining"],
                          100.0 * step["share_now"], 100.0 * step["share_after"]))
            else:
                _print("  ceilings         the successor records the ceilings it was judged under")
            return True, False

        for domain, name, _why, files in ready[:limit]:
            paid, failure = run_one(("file", domain, name, _why, files))
            if failure:
                failures += 1
                stopped = True
                break
            if paid:
                paid_now += 1
                paid_domains[domain] = paid_domains.get(domain, 0) + 1
        paid_ceilings = 0
        for domain, name, why, path in ceiling_ready[:ceiling_limit]:
            if stopped:
                break
            paid, failure = run_one(("ceiling", domain, name, why, [path]))
            if failure:
                failures += 1
                stopped = True
                break
            if paid:
                paid_ceilings += 1
                # Round 74: the drain summary below counts what this command spent. A family it pays silently
                # would make "1 round(s) retired" describe a two-round drain -- the same defect boundary 98
                # names, one line lower.
                paid_now += 1
                paid_domains[domain] = paid_domains.get(domain, 0) + 1
        if ceiling_ready:
            still = len(_ceiling_matters(root))
            _print("")
            _print("ceilings          %d of %d re-measured with their ceilings; %d still without"
                   % (paid_ceilings, len(ceiling_ready), still))
        if ready:
            if stopped:
                # A batch that keeps spending after a broken step is not a batch, it is a way to burn the
                # budget proving the same failure thirteen times (boundary 117).
                _print("")
                _print("queue             stopped after a round that could not be re-run: the remaining "
                       "round(s) keep their place and the command can be run again")
            remaining = sum(1 for record in history(root) if not str(record.get("stream_path") or ""))
            total = len(history(root))
            _print("")
            _print("file unknown      %d of %d round(s) still cannot be placed%s"
                   % (remaining, total,
                      " — %d left by the limit above" % max(0, len(ready) - limit) if len(ready) > limit
                      else ""))
            if paid_now:
                # Round 55: the drain records itself in the ledger it changed (each retirement carries its
                # own moment), so this line is derived, not written a second time. A command that pays a
                # debt should end by saying what it paid -- the reader who stopped watching at round four
                # has no other place to learn it.
                after = _rerun_progress(root)
                _print("drained           %d round(s) retired in this invocation; %d still in the queue; the "
                       "unrecorded share %.0f%% -> %.0f%%"
                       % (paid_now, after["remaining"], 100.0 * share_before,
                          100.0 * float(after["share_now"])))
                # Round 58: which domains this command actually paid, and which one is still the largest
                # payable debt afterwards -- the two facts a reader needs to decide whether to run it again.
                _print("paid              %s"
                       % ", ".join("%s x%d" % (name, count)
                                   for name, count in sorted(paid_domains.items())))
                left = _largest_debt(root)
                if left is None:
                    _print("balance           no payable debt remains: every remaining unrecorded round keeps "
                           "no candidate definitions or no unique file")
                else:
                    _print("balance           the largest payable debt is now %s: %d of %d round(s) unrecorded "
                           "(%.0f%%), %d re-runnable"
                           % (left["domain"], left["without"], left["rounds"],
                              100.0 * float(left["share"]), left["ready"]))
            _print("The re-run measures the candidates the record names, on the file it names: the totals move")
            _print("from one sampling to a named one without changing the question. Nothing is deployed by it.")
        return 2 if failures else 0

    if args.retire:
        names = _retire_names(args.retire)
        failed: List[str] = []
        for name in names:
            try:
                mark = retire(root, name, re_measured_as=args.re_measured_as or None)
            except (OSError, ValueError) as error:
                _print("cannot retire %s: %s" % (name, error))
                failed.append(name)
                continue
            successor = mark.get("re_measured_as") if isinstance(mark, dict) else None
            _print("retired           %s  (%s)"
                   % (name, "re-measured as %s" % successor if successor
                      else "no successor named: the record stays, the checks skip it"))
            overlap = (mark or {}).get("overlap") or {}
            if successor:
                matches = overlap.get("matches") or []
                rest = overlap.get("not_repeated") or []
                if not overlap.get("successor_readable"):
                    _print("                  the successor record is not readable, so how much of this "
                           "round it repeats is unknown")
                elif matches:
                    _print("                  %d of its %d trial(s) appear in the successor: %s"
                           % (len(matches), len(matches) + len(rest), ", ".join(matches)))
                    if rest:
                        # A single shared candidate must not launder the rest of the record: those
                        # comparisons were not re-measured, they are questions nobody is asking again.
                        _print("                  the other %d are not repeated there, so this retirement "
                               "does not claim they were answered" % len(rest))
                else:
                    _print("                  none of its %d trial(s) appear in the successor: this round is "
                           "retired as a question, not answered" % len(rest))
        if len(names) > 1:
            # One command, but the overlap above is per round: a shared successor cannot stand in for the
            # trials it never repeated, so the batch reports each record's own share (boundary 77).
            _print("retired           %d of %d round(s)%s"
                   % (len(names) - len(failed), len(names),
                      "" if not args.re_measured_as else ", each naming %s" % args.re_measured_as))
        _print("The record(s) are unchanged; --audit no longer re-decides them, and --history still lists them.")
        return 2 if failed else 0

    if args.rejudge:
        path = Path(args.rejudge)
        if not path.exists():
            _print("no such round file: %s" % path)
            return 2
        rules = _rules_from_args(args)
        try:
            result = rejudge(path, rules)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            _print("cannot rejudge %s: %s" % (path, error))
            return 2
        _print(format_rejudge(result))
        return 0

    if args.unrecorded:
        # Round 50: the campaign's `file unknown` line says how much of the page cannot be attributed; this
        # says which of it is still deciding something, because re-running 53 rounds to move a percentage is
        # not work anyone does. An in-force version or a carried refusal is what makes a total load-bearing.
        try:
            rounds = history(root)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        matters = _unrecorded_matters(root)
        unknown = sum(1 for record in rounds if not str(record.get("stream_path") or ""))
        _print("=" * 96)
        _print("brian evolve - rounds whose totals cannot be placed on a sampling")
        _print("=" * 96)
        _print("rounds            %d of %d cannot be attributed; %d of them still decide something"
               % (unknown, len(rounds), len(matters)))
        queue = _rerun_progress(root)
        if queue["ready"] or queue["paid"]:
            # Round 53: the same queue the re-run consumes, on the page that motivates it — a reader who
            # asks "how much of this can actually be paid off" gets the count and the end state, not a
            # principle.
            _print("queue             %d round(s) can be re-run here; %d already re-measured; the share "
                   "would fall %.0f%% -> %.0f%%"
                   % (queue["ready"], queue["paid"], 100.0 * queue["share_now"], 100.0 * queue["share_after"]))
        if not matters:
            _print("")
            _print("Every round that still decides something says which file it measured.")
            return 0
        _print("")
        _print("%-16s %-32s %-34s %s" % ("domain", "round", "likely file", "why it still matters"))
        _print("-" * 130)
        # Round 71: a re-run writes a record that carries its own ceilings, so this list is also the list of
        # rounds that would let the default --max-limit-drift cap arm. Marked per row rather than summarised,
        # because the reader is deciding which rows to run, not how many.
        lacking = {str(record.get("round") or ""): not record.get("limits")
                   for record in history(root, include_retired=False)}
        for domain, name, why, files in matters:
            note = "  [+ceilings]" if lacking.get(str(name)) else ""
            _print("%-16s %-32s %-34s %s%s" % (domain, name, _candidate_label(files), why, note))
        _print("")
        _print("Re-run one with `brian evolve --stream-file <file>`: the new record names the file, and this")
        _print("list shrinks. The likely-file column is matched by geometry, not written anywhere: a file whose")
        _print("summary disagrees with the round's is never proposed, and a round that matches none is shown")
        _print("as such rather than given the nearest name (boundary 102).")
        if args.json_out:
            Path(args.json_out).write_text(
                _json({"rounds": len(rounds), "unattributed": unknown,
                       "matters": [{"domain": d, "round": n, "why": w, "files": f}
                                   for d, n, w, f in matters],
                       # Round 72: which of these rows would also record their ceilings, and what the debt
                       # would look like afterwards -- the two numbers a reader deciding whether to run the
                       # queue has to add up by hand otherwise.
                       "ceilings": {"missing": ceiling_missing, "rounds": ceiling_rounds,
                                    "payable": ceiling_payable,
                                    "after": max(0, ceiling_missing - ceiling_payable),
                                    "by_domain": ceiling_rows}}),
                encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return 0

    if args.campaign:
        rules = _rules_from_args(args)
        horizon = float(getattr(args, "refusal_expiry_days", REFUSAL_EXPIRY_DAYS))
        trend = debt_history(root, days=float(getattr(args, "trend_days", 7.0) or 7.0))
        # Round 69: the ceilings are built before the report, because the report now compares each row's
        # recorded ceilings against them. One view feeds the page, the JSON and the gate.
        limits_view = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                   drain_ceiling, drain_ceilings, flag_drain_ceilings)
        try:
            result = campaign_result(root, rules, expiry_days=args.expiry_days,
                                     refusal_expiry_days=horizon, limits=limits_view,
                                     in_force=in_force_rows, in_force_cleared=in_force_cleared,
                                     in_force_stale=in_force_stale_rows, in_force_history=in_force_record)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        try:
            drift = limit_drift(live_records, limits_view)
        except OSError:
            drift = {"rounds": 0, "differ": [], "unrecorded": 0}
        budget_limit = _rerun_limit(args, int(_rerun_progress(root).get("ready") or 0))
        # Round 62: one budget, computed once. The page, the JSON payload and the minutes cap must describe the
        # same invocation; computing `_queue_budget` three times would be three chances for the price to differ
        # from the sentence that quotes it. Round 75 adds the family the recommended command would open: a drain
        # pays the named ceiling debt too, and a budget that priced only one family would be quoting a different
        # command than the one on the line above it.
        # `None` (not `0`) when the recommended command would not open the family: inside `_queue_budget`, 0
        # means "drain the whole family", so passing 0 for "leave it alone" prices work nobody asked for.
        ceiling_would = (_rerun_limit(args, ceiling_named)
                         if (_wants_ceilings(args) and ceiling_named) else None)
        budget = _queue_budget(root, limit=budget_limit, ceiling_limit=ceiling_would)
        # Round 71/72: the shortfall and the queue's reach were computed once before the branches, so the page
        # and the gate cannot disagree about how many rounds lack ceilings or how many of them a re-run pays.
        # Round 74: the sentence names the command that lowers the debt, and it keeps the two families apart.
        ceiling_note = format_ceiling_shortfall(ceiling_rows, payable=ceiling_file_payable,
                                                named=ceiling_named,
                                                command=_ceiling_command(ceiling_file_payable, ceiling_named))
        _print(format_campaign(result, expiry_days=args.expiry_days, rules=rules,
                               refusal_expiry_days=horizon, queue=_rerun_progress(root),
                               trend=trend, debt=_largest_debt(root), plan=_debt_plan(root),
                               budget=budget, limits=_limits_text(limits_view),
                               ceiling_note=ceiling_note))
        worst, payable = _debt_view(root)
        if args.json_out:
            # Round 60: the page's next-step lines are derived facts, so the machine-readable form carries the
            # same ones rather than making every caller parse the text (or worse, recompute them differently).
            payload = result.to_dict()
            payload["debt_plan"] = _debt_plan(root)
            payload["budget"] = budget
            # Round 65: a report that can be judged must say what it was judged against. Every ceiling that was
            # in force while this page was printed appears here with where it came from, because a number whose
            # origin is unknown cannot be reviewed -- and the state and the command line can disagree.
            payload["limits"] = limits_view
            # Round 69: the drift the gate charged is a derived fact, so it travels with the page.
            payload["ceiling_drift"] = drift
            Path(args.json_out).write_text(_json(payload), encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return _strict_exit(args.strict, result.moved, result.unreconstructable,
                            result.inconsistent, due=result.withheld_due,
                            oldest_days=result.withheld_oldest_days, horizon=horizon,
                            unrecorded=result.unattributable_missing,
                            legacy_unrecorded=result.unattributable_legacy,
                            unrecorded_share=result.unattributable_share,
                            max_unrecorded=args.max_unrecorded,
                            trend_rise=max(0.0, -float(trend["points"])),
                            trend_days=float(trend["days"]),
                            trend_paid=int(trend["paid"]), trend_arrived=int(trend["arrived"]),
                            max_rise=getattr(args, "max_share_rise", None),
                            worst=worst, payable=payable,
                            max_debt=getattr(args, "max_debt_share", None),
                            budget_minutes=(None if budget.get("estimated_minutes_by_domain") is None
                                            else float(budget["estimated_minutes_by_domain"])),
                            minutes_by_domain=budget.get("minutes_by_domain") or {},
                            max_minutes=minute_ceiling,
                            max_minutes_by_domain=minute_ceilings,
                            drain_minutes=(None if budget.get("drain_minutes") is None
                                           else float(budget["drain_minutes"])),
                            drain_by_domain=budget.get("drain_by_domain") or {},
                            max_drain=drain_ceiling,
                            drain_ceilings=drain_ceilings,
                            limit_drift=len(drift.get("differ") or []),
                            max_limit_drift=_drift_cap(args, drift),
                            ceiling_missing=ceiling_missing, ceiling_rounds=ceiling_rounds,
                            ceiling_payable=ceiling_payable,
                            max_ceiling_debt=getattr(args, "max_ceiling_debt", None),
                            broken_links=result.broken_links, in_force=in_force_broken,
                            in_force_stale=len(in_force_stale_rows),
                            in_force_untested=sum(1 for row in in_force_stale_rows if row.get("untested")))

    if args.sensitivity:
        rules = _rules_from_args(args)
        bars = [value for value in (args.sensitivity_bar or [])] or None
        try:
            result = refusal_sensitivity(root, rules, bars=bars)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_sensitivity(result))
        if args.json_out:
            Path(args.json_out).write_text(_json(result.to_dict()), encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return 0

    if args.withheld:
        rules = _rules_from_args(args)
        try:
            records = withheld_records(root)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_withheld(records, rules))
        if args.json_out:
            payload = {"bar": rules.replication_bar,
                       "withheld": [record.to_dict() for record in records]}
            Path(args.json_out).write_text(_json(payload), encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return 0

    if args.audit:
        rules = _rules_from_args(args)
        horizon = float(getattr(args, "refusal_expiry_days", REFUSAL_EXPIRY_DAYS))
        try:
            result = audit(root, rules, in_force=in_force_rows,
                           in_force_cleared=in_force_cleared, in_force_stale=in_force_stale_rows)
        except OSError as error:
            _print("cannot audit %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_audit(result))
        # Round 67: the gate charges ceilings, so it must print the ones it read (item 54). A reader who runs
        # `--audit --strict` and gets exit 1 needs to see whether the number came from a policy recorded in a
        # domain's state or from today's command line, exactly as the campaign page does; two modes reading one
        # ledger must not describe the judgement differently.
        audit_limits = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                    drain_ceiling, drain_ceilings, flag_drain_ceilings)
        audit_text = _limits_text(audit_limits)
        if audit_text:
            _print("limits            %s" % audit_text)
        # Round 68: a ceiling is not in `EvolveRules`, so a re-decision can move without the rules moving.
        # The gate says how many recorded rounds were judged under different limits, and never guesses a
        # round that predates the field (boundary 147).
        try:
            drift = limit_drift(history(root, include_retired=False), audit_limits)
        except OSError:
            drift = {"rounds": 0, "differ": [], "unrecorded": 0}
        _print(format_limit_drift(drift))
        # Round 45: the refusals are records too (boundary 91), so the ledger-wide gate counts a refusal
        # nobody re-tested as debt. Computed from the same horizon `--campaign` uses, or the two modes
        # would disagree about the same ledger and a reader could pass the check by picking one.
        try:
            due, oldest = _withheld_debt(root, horizon)
        except OSError:
            due, oldest = 0, None
        # Round 50: the same seam the campaign page prints, read here so the gate can charge it. `--audit`
        # re-decides records and never looks at the campaign page, but a reader who runs the check deserves
        # the same debt list in either mode.
        try:
            missing, legacy, _rounds, share = _unrecorded_debt(root)
        except OSError:
            missing, legacy, share = 0, 0, 0.0
        # The exit code is the part that makes this a check rather than a report: a rule change that
        # rewrites history should fail the build, or it will be noticed months later by whoever reads the
        # table last.
        try:
            trend = debt_history(root, days=float(getattr(args, "trend_days", 7.0) or 7.0))
        except OSError:
            trend = {"points": 0.0, "days": 0.0, "paid": 0, "arrived": 0}
        worst, payable = _debt_view(root)
        # Round 62: the check reads the same budget the page would print, computed by the same function, or a
        # reader could pass `--max-minutes` in one mode and fail it in the other without changing anything.
        try:
            # Round 75: the audit prices the same command the page recommends, which may open the ceiling
            # family too -- otherwise passing `--max-minutes` fails in one mode and passes in the other.
            audit_ceiling = (_rerun_limit(args, ceiling_named)
                             if (_wants_ceilings(args) and ceiling_named) else None)
            audit_budget = _queue_budget(
                root, limit=_rerun_limit(args, int(_rerun_progress(root).get("ready") or 0)),
                ceiling_limit=audit_ceiling)
        except OSError:
            audit_budget = {}
        # Round 67: the machine-readable side of the same judgement. A check whose stdout says "judged under
        # a 10-minute ceiling" while its JSON says nothing leaves the reader unable to review it later, and
        # boundary 131 already makes that a rule for the campaign page.
        if args.json_out:
            payload = {"limits": audit_limits, "limits_text": audit_text,
                       "ceiling_drift": drift,
                       "rules_hash": result.fingerprint, "rounds": result.rounds,
                       "trials": result.trials, "retired": result.retired,
                       "moved": result.moved, "relabelled": result.relabelled,
                       "unreconstructable": result.cross, "inconsistent": result.inconsistent,
                       "entries": result.entries}
            Path(args.json_out).write_text(_json(payload), encoding="utf-8")
            _print("")
            _print("wrote %s" % args.json_out)
        return _strict_exit(args.strict, result.moved, result.cross, result.inconsistent,
                            due=due, oldest_days=oldest, horizon=horizon,
                            unrecorded=missing, legacy_unrecorded=legacy, unrecorded_share=share,
                            max_unrecorded=args.max_unrecorded,
                            trend_rise=max(0.0, -float(trend["points"])),
                            trend_days=float(trend["days"]),
                            trend_paid=int(trend["paid"]), trend_arrived=int(trend["arrived"]),
                            max_rise=getattr(args, "max_share_rise", None),
                            worst=worst, payable=payable,
                            max_debt=getattr(args, "max_debt_share", None),
                            budget_minutes=(None if audit_budget.get("estimated_minutes_by_domain") is None
                                            else float(audit_budget["estimated_minutes_by_domain"])),
                            minutes_by_domain=audit_budget.get("minutes_by_domain") or {},
                            max_minutes=minute_ceiling,
                            max_minutes_by_domain=minute_ceilings,
                            drain_minutes=(None if audit_budget.get("drain_minutes") is None
                                           else float(audit_budget["drain_minutes"])),
                            drain_by_domain=audit_budget.get("drain_by_domain") or {},
                            max_drain=drain_ceiling,
                            drain_ceilings=drain_ceilings,
                            limit_drift=len(drift.get("differ") or []),
                            max_limit_drift=_drift_cap(args, drift),
                            ceiling_missing=ceiling_missing, ceiling_rounds=ceiling_rounds,
                            ceiling_payable=ceiling_payable,
                            max_ceiling_debt=getattr(args, "max_ceiling_debt", None),
                            broken_links=result.broken_links, in_force=in_force_broken,
                            in_force_stale=len(in_force_stale_rows),
                            in_force_untested=sum(1 for row in in_force_stale_rows
                                                  if row.get("untested")))

    if args.grade:
        try:
            rounds = history(root)
        except OSError:
            rounds = []
        result = grade(has_catalogue=bool(catalogue()), has_history=bool(rounds),
                       mutates_mechanism=proposal_capable(), applies_changes=reload_capable(),
                       promotes=bool(load_promotions(root)))
        _print("=" * 78)
        _print("brian evolve - how much of the loop is autonomous")
        _print("=" * 78)
        _print("level            %s%s" % (result["level"],
                                           "" if not result.get("scope") else "  (%s)" % result["scope"]))
        _print("")
        _print("capabilities")
        for line in result["capabilities"]:
            _print("  + %s" % line)
        _print("")
        _print("missing for the next level")
        for line in result["missing_for_next"]:
            _print("  - %s" % line)
        _print("")
        _print("Note: the pipeline reloads the deployment state between windows, so an accepted")
        _print("change and an automatic revert both reach a running process; a process started")
        _print("without BRIAN_DEPLOY_STATE keeps its start-up configuration, and grading that")
        _print("case L3 is the self-flattery the survey warns about.")
        _print("")
        _print("L4 comes from the proposal rule being selected from the ledger (bounded by a declared")
        _print("space); the objective, decide() and the evaluator stay outside. Each round records")
        _print("which rule proposed it, so 'the mechanism changed' is a fact with a fingerprint.")
        return 0

    if getattr(args, "repair_links", False):
        # Round 83: a `legacy` end is repaired by copying the retirement's claim into the successor record.
        # Without `--apply` this only prints what it would write: a command that edits recorded history has
        # to be able to show the edit first (boundary 175).
        rounds = history(root, include_retired=True)
        try:
            rows = repair_legacy(root, dry_run=not args.apply)
        except (OSError, ValueError) as error:
            _print("cannot repair the replacement links: %s" % error)
            return 2
        _print(format_repairs(rows, dry_run=not args.apply))
        # `would_write` counts the links a plan could repair; `written` counts the ones an apply did. Asking a
        # plan for `written` prints nothing and leaves the reader with a plan they cannot act on (round 83).
        repairable = sum(1 for row in rows if row.get("would_write"))
        written = sum(1 for row in rows if row.get("written"))
        if not args.apply and repairable:
            _print("")
            _print("nothing was written; add --apply to write these %d end(s)." % repairable)
        # A repaired end is not evidence: say so on the same page, so the next `--audit` line is not read as
        # "the ledger corroborates itself". This is the whole point of the mark (boundary 174).
        if written:
            _print("")
            _print("a repaired end records `%s`, so the two ends agree by copying; --audit counts these"
                   % BACKFILL_KEY)
            _print("separately and does not treat them as independent confirmation.")
        json_out = getattr(args, "json_out", None)
        if json_out:
            target = Path(json_out)
            target.write_text(__import__("json").dumps({"repairs": rows}, indent=2) + "\n",
                              encoding="utf-8")
        return 0

    if args.propose:
        # No Laya: this is a statement about the ledger, and asking the engine to answer it would only
        # make it slower and less checkable.
        try:
            rounds = history(root)
        except OSError:
            rounds = []
        mode = None
        if args.expand:
            mode = Mode("expand", "forced by --expand, so the bounded space is listed even though the "
                                  "ledger did not ask for it")
        # The seeds must come from the stream this round will be measured on, so the dry run has to name
        # the same source the real round would: a magnitude accepted on one trace is not a prior for
        # another one.
        if args.stream_file:
            try:
                source = load_stream_file(Path(args.stream_file)).dataset or Path(args.stream_file).stem
            except (OSError, ValueError) as error:
                _print("cannot read %s: %s" % (args.stream_file, error))
                return 2
        else:
            source = "demo"
        # The dry run reports what the real round would measure, so it needs the same deployment baseline:
        # a neighbour that describes the configuration in force is a round spent comparing the baseline
        # with itself, and the dry run is where that is cheapest to notice.
        try:
            deployed, _sources, _refused, _unknown = in_force_overrides(root, settings, domain=source)
        except (OSError, ValueError):
            deployed = {}
        # Round 36: the dry run reports what the real round would *not* measure, because a proposal the
        # round will skip is not part of what it costs or answers. The shape comes from the stream file
        # when there is one, so a refusal made at another sampling is re-opened here exactly as it would
        # be in the round.
        shape = None
        if args.stream_file:
            try:
                shape = summarise(load_stream_file(Path(args.stream_file)).streams)
            except (OSError, ValueError):
                shape = None
        plan = refusal_plan(rejection_provenance(root, _rules_from_args(args)),
                            source=source, shape=shape,
                            expiry_days=args.refusal_expiry_days)
        # Round 37: the plan is consulted *before* proposing, so the mechanism does not spend a slot on a
        # candidate the ledger already answered — and the dry run shows the proposal the round would really
        # make rather than one it would then filter.
        # Round 57: the dry run also says where the largest payable debt is, because it is the place an
        # operator decides whether this round is worth its minutes at all.
        _print_debt(root, source, named_file=bool(args.stream_file))
        proposal = propose(rounds, mode=mode, source=source, deployed=deployed,
                           rules=_rules_from_args(args), refused=plan["skip"])
        if args.catalogue:
            proposal = Proposal(
                "catalogue",
                "forced by --catalogue: the fixed list, so the rule selection was not consulted",
                list(catalogue()), proposal.space, proposal.source)
        _print(format_proposal(proposal))
        _print_refusal_plan(plan)
        # Printed even when the answer is zero: a saving that never fires on the current ledger is a guard,
        # and a reader who is not told that cannot tell this round from one that saved nothing because the
        # plan was never consulted (round 36). `--catalogue` does not go through `propose`, so its drop is
        # counted against the plan here instead of inside `Proposal`.
        skipped = set(plan["skip"])
        dropped = proposal.refused if not args.catalogue else sum(
            1 for candidate in proposal.candidates if candidate.name in skipped)
        _print("would drop        %d candidate(s) this stream already refused at this sampling"
               % dropped)
        _print("")
        return 0

    if args.calibrate_bar:
        # No Laya either: the reader is deterministic, so a bar read off its own scores needs no model.
        settings, streams, source, base_overrides = _evolve_streams(args, settings)
        if not streams:
            _print("no streams to calibrate on")
            return 2
        reader_settings = replace(settings.digest, scoring="deviation")
        bars_settings = settings.with_overrides(digest=reader_settings)
        try:
            scored = reader_scores(bars_settings, streams)
        except (OSError, ValueError) as error:
            _print("cannot calibrate: %s" % error)
            return 2
        budgets = (0.02, 0.05, 0.10)
        pooled = [score for rows in scored.values() for score, _label in rows]
        _print("=" * 78)
        _print("brian evolve - bars a deployment can compute without labels")
        _print("=" * 78)
        _print("stream           %s  (%d windows)" % (source, len(pooled)))
        _print("")
        _print("%-24s %8s %10s %8s" % ("recording", "budget", "bar", "windows"))
        for name in list(scored) + ["pooled"]:
            rows = pooled if name == "pooled" else [score for score, _label in scored[name]]
            for budget in budgets:
                _print("%-24s %7.0f%% %10.4f %8d"
                       % (name, budget * 100.0, label_free_cut(rows, budget) or 0.0, len(rows)))
        _print("")
        _print("Each bar is the (1 - budget) quantile of these scores: the value a deployment would")
        _print("need in order to alert on that share of windows without ever looking at a label.")
        _print("A fitted cut needs the labels; this one needs a day of traffic. Put one of these")
        _print("numbers in a candidate to measure what it costs to deploy without the answer.")
        return 0

    laya = LayaClient(settings.laya)
    service = _service_report(settings, laya)
    if not service.get("reachable") or not service.get("model_available"):
        _print("cannot evolve: %s" % (service.get("error") or service.get("model_error")))
        _print("Start Laya with:  D:\\project\\laya\\service\\start.cmd")
        return 3

    if args.verify:
        wanted = int(getattr(args, "verify_version", 0) or 0)
        horizon = float(getattr(args, "refusal_expiry_days", REFUSAL_EXPIRY_DAYS))
        # Round 45: several domains in one command. `--withheld-stream domain=stream.json` names the stream
        # each refusal was raised on, so retiring objections does not depend on which stream happens to be
        # in `--stream-file` — and a promise check is left out of this mode on purpose: one command should
        # answer one kind of question, and mixing them is how round 44 verified a promise while merging two
        # patches into a deployment nobody measured.
        pairs: List[Tuple[str, Path]] = []
        try:
            pairs = _withheld_pairs(getattr(args, "withheld_stream", []) or [])
        except ValueError as error:
            _print("cannot re-measure refusals: %s" % error)
            return 2
        if args.verify_withheld and not pairs:
            # Round 46: the refusals name the file they were measured on, so this mode needs no arguments.
            pairs, missing = _self_described_pairs(root)
            if missing:
                _print("%d refusal(s) do not say which stream file they were measured on: %s"
                       % (len(missing), ", ".join("%s (%s)" % (record.label, record.domain)
                                                  for record in missing)))
                _print("pass --withheld-stream for %s; a source name does not name a file, and guessing one "
                       "would re-check the objection on whatever sampling happens to be at that path."
                       % ", ".join(sorted({"%s=<stream.json>" % domain_slug(record.domain)
                                           for record in missing})))
                return 2
            if not pairs:
                _print("no refusal has been recorded yet, so there is nothing to re-measure.")
                return 0
        if pairs:
            if wanted:
                _print("--withheld-stream re-measures refusals; --verify-version re-checks a promise. "
                       "Pass one, not both: they answer different questions about different records.")
                return 2
            rules = _rules_from_args(args)
            cleared_total = 0
            for pair_domain, pair_path in pairs:
                clone = argparse.Namespace(**vars(args))
                clone.stream_file = str(pair_path)
                pair_settings, pair_streams, _pair_source, _pair_overrides = _evolve_streams(clone, settings)
                _print("")
                _print("---- %s  <-  %s" % (domain_slug(pair_domain), pair_path))
                cleared_total += _recheck_withheld(root, pair_domain, pair_settings, pair_streams, rules,
                                                   horizon, from_default=args.from_default)
            _print("")
            _print("%d domain(s) re-checked, %d objection(s) cleared. A cleared refusal is measured by the "
                   "next round that proposes the candidate; this command promotes nothing."
                   % (len(pairs), cleared_total))
            return 0
        if not args.stream_file:
            # Round 47: the promise records the file it was measured on, so a check should stand on it without
            # the operator remembering, and must not fall back to the demo stream -- grading every version
            # against a recording none of them was measured on is the kind of answer that looks like one.
            known = [item for item in load_promotions(root)
                     if item.version == wanted or (not wanted and item.due(days=args.expiry_days))]
            if known:
                resolved, missing, by_path = _promotion_stream(known)
                if missing:
                    _print("%d version(s) do not say which stream file they were measured on: %s"
                           % (len(missing), ", ".join("v%03d" % item.version for item in missing)))
                    _print("pass --stream-file; a dataset name does not name a file, and guessing one would "
                           "grade the promise on whatever sampling happens to be at that path.")
                    return 2
                if len(by_path) > 1:
                    _print("%d version(s) to check name %d different stream files: %s"
                           % (len(known), len(by_path),
                              ", ".join("%s (%s)" % (path, ", ".join("v%03d" % item.version
                                                                     for item in group))
                                        for path, group in sorted(by_path.items()))))
                    _print("pass --stream-file for one of them, or --verify-version: picking one silently "
                           "decides which promise gets graded.")
                    return 2
                if resolved:
                    _print("stream file    %s  (named by %s)"
                           % (resolved, ", ".join("v%03d" % item.version for item in by_path[resolved])))
                    args.stream_file = resolved
        settings, streams, source, _overrides = _evolve_streams(args, settings)
        domain = source
        # A promise is a statement about one dataset (boundary 33), so a stream only re-checks the
        # versions recorded on it: re-measuring another domain's patch here would grade a claim against
        # data it never described.
        # Round 29: `pending` used to mean "never checked", so a version whose check came back
        # `unmeasurable` -- or whose verdict is older than the horizon -- was never selected again. A
        # verdict is evidence about the day it ran, so an expired one is due again.
        promotions = [item for item in promotions_for_domain(load_promotions(root), domain)
                      if item.version == wanted or (not wanted and item.due(days=args.expiry_days))]
        # Round 44: the same question for the refusals. A `w###` record is only re-measured when a round
        # happens to propose the candidate again, and round 43 made the plan stop proposing settled ones --
        # so an objection can outlive every check that could have retired it. `--verify-withheld` asks it
        # directly, on this stream, with the deployment in force as the baseline.
        horizon = float(getattr(args, "refusal_expiry_days", REFUSAL_EXPIRY_DAYS))
        # Round 46: the filter is `_refusals_on`, which compares slugs. The raw-string version here was the
        # same silent miss round 45 fixed in `--withheld-stream`: a record stores "SMD", the stream says
        # "SMD" today, but the moment either side is slugged the hint below stops firing and a due objection
        # is never mentioned again.
        due = withheld_due(_refusals_on(root, domain), expiry_days=horizon)
        if not promotions and not (args.verify_withheld and due):
            _print("no unverified promotions for %s: every version recorded on it has been re-checked."
                   % domain_slug(domain))
            if due:
                _print("%d refusal(s) recorded on %s have not been measured again in %.0f day(s): "
                       "pass --verify-withheld to re-measure them." % (len(due), domain_slug(domain), horizon))
            return 0
        rules = _rules_from_args(args)
        pool = build_pool(settings, streams)
        checked_stream = summarise(streams)
        if promotions:
            _print("verifying %d promotion(s) from %s on %s"
                   % (len(promotions), domain_slug(domain), source))
        # Round 84: a version is only as trustworthy as the round that produced it. That round's record
        # names the version in `applied`, and rounds 81-83 made it possible to say whether the record's
        # replacement link is contradicted or was copied.
        # Round 88: the two kinds are not the same refusal. A contradiction cannot be settled by measuring --
        # both books still disagree about which end is the evidence -- so it stays unchecked. A copied end is an
        # absence of provenance, and re-measuring the deployed version is exactly what round 86's stale charge
        # asks for: refusing it left that charge unclearable by the command that named it. So the copy can be
        # measured with `--accept-copied`, recorded, and never applied.
        blocked: List[Tuple[str, str]] = []
        copied_ends: List[Tuple[str, str]] = []
        copied_measured: List[Tuple[str, str]] = []
        # Round 88: the versions this run actually measures, with the moment of the check. A version that was
        # skipped (a contradicted record, an expired definition) measured nothing and clears nothing.
        measured_versions: List[Tuple[int, str, str, str]] = []
        for promotion in promotions:
            block = provenance_block(root, promotion.version)
            if block is not None and block[0] == LINK_BROKEN:
                blocked.append(block)
                _print("  v%03d  %s  %s: %s"
                       % (promotion.version, promotion.change, block[0], block[1]))
                _print("        not checked: a promise stacked on this record would entrench numbers the "
                       "ledger itself disputes")
                continue
            copied = block is not None
            if copied:
                copied_ends.append(block)
                if not args.accept_copied:
                    _print("  v%03d  %s  %s: %s"
                           % (promotion.version, promotion.change, block[0], block[1]))
                    _print("        not checked: a promise stacked here would inherit an end a repair copied; "
                           "--accept-copied measures it without applying")
                    continue
                copied_measured.append(block)
                _print("  v%03d  %s  %s: %s"
                       % (promotion.version, promotion.change, block[0], block[1]))
                _print("        measured, not applied: the record's end was copied, so the deployment is left "
                       "alone")
            if not promotion.pending:
                age = verification_age_days(promotion.verification)
                _print("  v%03d  re-checking: its last check is %s old (expiry %.0f day(s))"
                       % (promotion.version,
                          "less than a day" if (age or 0) < 1.0 else "%.0f day(s)" % age,
                          args.expiry_days))
            if promotion.candidate is None:
                write_verification(root, promotion, {
                    "checked_at": _stamp(), "stream": source, "verdict": UNMEASURABLE,
                    "reason": "the version file does not name the change, so it cannot be re-measured",
                })
                _print("  v%03d  %s  unmeasurable" % (promotion.version, promotion.change))
                continue
            measurement = measure(settings, promotion.candidate, streams, pool,
                                  budget=rules.fpr_budget)
            verdict, reason = claim_check(promotion.claim, measurement, rules,
                                          recorded_stream=promotion.stream,
                                          checked_stream=checked_stream)
            payload = {"checked_at": _stamp(), "stream": source, "verdict": verdict,
                       "reason": reason, "measurement": measurement.to_dict()}
            if copied:
                payload["copied_end"] = {"kind": block[0], "detail": block[1]}
            if verdict == FAILED:
                # The promise stopped paying out, so the configuration stops being the deployed one.
                # The file is kept, because a retracted claim is part of the record, not a hole in it.
                # Retracting is not stacking, so a copied end does not block it.
                revert_promotion(root, promotion, domain=domain)
                write_verification(root, promotion, payload, reverted_at=_stamp())
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason))
                _print("  v%03d  %s  FAILED and reverted: %s" % (promotion.version,
                                                                 promotion.change, reason))
            elif verdict == VERIFIED:
                write_verification(root, promotion, payload)
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason))
                if copied:
                    _print("  v%03d  %s  %s (not applied: the record's link was repaired from one end, and a "
                           "promise stacked on it would inherit that copy)"
                           % (promotion.version, promotion.change, verdict))
                    continue
                # Round 44: applying every version a check verifies stacked them. The check is recorded
                # either way; only a domain with no other version in force takes the change.
                stacked = stacking_note(root, promotion, domain)
                if stacked:
                    _print("  v%03d  %s  %s (verified, not applied: %s)"
                           % (promotion.version, promotion.change, verdict, stacked))
                else:
                    apply_promotion(root, promotion, domain=domain)
                    _print("  v%03d  %s  %s" % (promotion.version, promotion.change, verdict))
            else:
                # Not comparable: this stream is sampled differently from the one the claim was made
                # on, so the check neither confirms nor retracts. The deployment state is left alone —
                # applying on an unknown would be as wrong as reverting on one.
                shape = rescaled_candidate(promotion, promotion.stream, checked_stream) \
                    if getattr(args, "at_hint", False) else None
                if shape is not None:
                    # The same version at a comparable horizon. Recorded beside the refusal, never applied:
                    # a different window count is a different version, so it cannot inherit this claim.
                    measured = measure(settings, shape, streams, pool, budget=rules.fpr_budget)
                    payload["shape"] = {
                        "window": next((value for section, field, value in shape.changes()
                                        if section == "digest" and field == "rolling_window"), 0),
                        "recall_at_budget": measured.recall_at_budget,
                        "fpr_at_budget": measured.fpr_at_budget,
                        "reports": measured.reports,
                        "claimed_recall": promotion.claim.recall_at_budget,
                        "candidate": shape.describe(),
                    }
                write_verification(root, promotion, payload)
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason))
                _print("  v%03d  %s  unmeasurable: %s" % (promotion.version, promotion.change, reason))
                hint = rescale_hint(promotion, promotion.stream, checked_stream)
                if hint:
                    _print("        hint  %s" % hint)
                note = shape_note(payload)
                if note:
                    _print(note)
        cleared_now: List[Dict[str, Any]] = []
        for version, checked_at, verdict, why in measured_versions:
            cleared_now.extend(record_clearing(root, version, now=checked_at, verdict=verdict,
                                               reason="measured again by --verify",
                                               tested=verdict != UNMEASURABLE,
                                               attempt_reason=why))
        answered = [entry for entry in cleared_now if entry.get("tested") is not False]
        attempted = [entry for entry in cleared_now if entry.get("tested") is False]
        if answered:
            _print("")
            _print("cleared         %d in-force observation(s) were answered by this check:" % len(answered))
            for entry in answered:
                age = entry.get("age_days")
                _print("  %-16s v%03d  %-13s stood %s"
                       % (entry["domain"], int(entry["version"]), entry["kind"],
                          "an unknown time" if age is None else "%.1f day(s)" % float(age)))
        if attempted:
            _print("")
            _print("not answered  %d in-force observation(s) were measured, but the check could not test"
                   " the claim:" % len(attempted))
            for entry in attempted:
                _print("  %-16s v%03d  %-13s %s"
                       % (entry["domain"], int(entry["version"]), entry["kind"],
                          str(entry.get("reason") or "no reason recorded")))
            _print("The row keeps its first sighting: a comparison that compared nothing is not an answer,")
            _print("and it must not reset the clock it was supposed to close (boundary 188).")
            _print("A measurement answers the objection, not the copy: the record is unchanged, so the next")
            _print("audit observes it again with today's date, and this verdict is what keeps it out of the")
            _print("stale charge (boundary 185).")
        _print("")
        _print(format_promotions(load_promotions(root), current_state(root, domain), domain=domain,
                                 expiry_days=args.expiry_days))
        broken = sum(1 for kind, _detail in blocked if kind == LINK_BROKEN)
        if blocked or copied_ends:
            _print("")
            if blocked:
                _print("%d version(s) were left unchecked: their producing record carries a replacement link "
                       "the ledger contradicts." % len(blocked))
                _print("A contradiction is a build defect: no re-measurement settles which of the two books "
                       "is right, so `--verify` exits 2. `--audit` names the link.")
                _print("No verification file was written for these versions, so they stay due on every run.")
            if copied_ends:
                _print("%d version(s) sit on an end a repair copied, and `--verify` exits 1 while one is in "
                       "force." % len(copied_ends))
                _print("A copied end is a debt, not a defect: re-running the round writes a successor that "
                       "names its predecessor itself.")
                withheld = len(copied_ends) - len(copied_measured)
                if withheld:
                    _print("%d of them were not checked; `--accept-copied` measures them without applying, "
                           "which is what the stale charge asks for." % withheld)
        if broken:
            return 2
        if copied_ends:
            return 1
        return 0

    settings, streams, source, base_overrides = _evolve_streams(args, settings)

    # The baseline is the deployment, not the factory default. `load_settings()` is what the code ships
    # with; `current.json` is what is running. Round 20 accepted a patch whose shipped form was worse than
    # the configuration already in force, which is a statement about the baseline, not about the patch.
    domain = source
    deployed: Dict[str, Dict[str, Any]] = {}
    if not args.from_default:
        in_force, sources, refused, unknown = in_force_overrides(root, settings, domain=domain)
        deployed = in_force
        if in_force:
            settings = apply_overrides(settings, in_force)
            names = sorted(ENV_VARS.get((section, key), "%s.%s" % (section, key))
                           for section, values in in_force.items() for key in values)
            _print("in force        %s  (%d variable(s) from the deployment state for %s)"
                   % (", ".join(sorted({version_label(sources.get(name)) for name in names})),
                      len(names), domain_slug(domain)))
            for name in names:
                pair = next((pair for pair, env in ENV_VARS.items() if env == name), None)
                value = in_force[pair[0]][pair[1]] if pair else "?"
                _print("  %s=%r  from %s" % (name, value, version_label(sources.get(name))))
            for section, values in sorted(in_force.items()):
                for key, value in sorted(values.items()):
                    base = (base_overrides.get(section) or {}).get(key)
                    if base is not None and base != value:
                        _print("  ! the deployment replaces this domain's own base setting: %s %r -> %r"
                               % ("%s.%s" % (section, key), base, value))
            if refused or unknown:
                _print("  ! not adopted: refused %s, unknown %s" % (refused or "[]", unknown or "[]"))
            _print("")
        elif args.stream_file:
            _print("in force        (nothing) the deployment state for %s sets no reloadable setting"
                   % domain_slug(domain))
            note = legacy_state_note(root, domain)
            if note:
                _print("  ! %s" % note)
            _print("")

    rules = _rules_from_args(args)

    # Round 30: when the deployment's own claim was made on a stream whose windows cover a different
    # amount of traffic, the round proposes the same settings at the window count that restores the span.
    # The candidate carries the promise it is trying to re-establish, so `decide()` judges it against that
    # promise instead of against a baseline it was never meant to beat.
    stream_shape = summarise(streams)
    reshaped = None
    owner = None
    if not args.from_default and deployed:
        versions = {str(version) for version in sources.values() if version}
        owners = [item for item in promotions_for_domain(load_promotions(root), domain)
                  if str(item.version) in versions and item.candidate is not None]
        if owners:
            owner = owners[0]
            reshaped = rescaled_candidate(owner, owner.stream, stream_shape)
            if reshaped is not None:
                _print("reshape         v%03d at window %d: the span its claim was made over"
                       % (owner.version, reshaped.value if reshaped.field == "rolling_window"
                          else next((value for section, field, value in reshaped.changes()
                                     if field == "rolling_window"), 0)))
                _print("  %s" % reshaped.provenance)
                _print("")

    # Round 57: where the largest payable debt is belongs before the proposal, not in a report afterwards —
    # a round aimed at the wrong stream grows the debt it will later have to pay.
    _print_debt(root, source, named_file=bool(args.stream_file))
    known = {candidate.name: candidate for candidate in catalogue()}
    # Round 37: the mechanism must not spend its proposal budget on questions this stream already answered,
    # so the refusal plan is computed before `propose` rather than after it. The plan is also what the round
    # prints below, and the proposal carries the count so the round record says how many it saved.
    plan = refusal_plan(rejection_provenance(root, rules), source=source, shape=stream_shape,
                        expiry_days=args.refusal_expiry_days)
    proposal_record: dict = {}
    if args.candidates:
        names = [name.strip() for name in args.candidates.split(",") if name.strip()]
        unknown = [name for name in names if name not in known]
        if unknown:
            _print("unknown candidates: %s" % ", ".join(unknown))
            _print("available: %s" % ", ".join(sorted(known)))
            return 2
        selected = [known[name] for name in names]
        proposal_record = {"mode": "named", "reason": "the caller named these candidates, so the "
                            "proposal rule was not consulted", "candidates": names}
    else:
        # The mechanism chooses what to measure, from what the ledger already says. This is the part of
        # the loop that is now mutable: `propose` reads the recorded rounds and decides between the
        # catalogue's hand-written hypotheses and a bounded search around magnitudes that have moved a
        # column. `--catalogue` keeps the fixed list for a reproduction.
        mode = None
        if args.expand:
            mode = Mode("expand", "forced by --expand, so the bounded space is measured even though the "
                                  "ledger did not ask for it")
        proposal = (propose(history(root), mode=mode, source=source, deployed=deployed,
                            reshaped=reshaped, rules=rules, shape=stream_shape,
                            refused=plan["skip"])
                    if not args.catalogue else None)
        if proposal is None:
            selected = list(known.values())
            proposal_record = {"mode": "catalogue", "reason": "forced by --catalogue",
                               "candidates": list(known)}
        else:
            selected = list(proposal.candidates)
            proposal_record = proposal.to_dict()
            for line in describe_proposal(proposal):
                _print(line)
            _print("")

    # Round 36: a refusal is a statement about a stream and a sampling, so the two questions "may this
    # round skip a candidate" and "is this a new measurement" have different answers. The plan splits the
    # refusals that still stand into the ones this round may skip and the three kinds it must spend a
    # measurement on again; printing a single count let a reader take all four for the same thing.
    # Round 37: the plan is computed before `propose` (above), so the mode=expand path never even generates
    # a neighbour the ledger already answered.
    _print_refusal_plan(plan)
    kept, not_measured = _skip_refused(selected, plan["skip"], named=bool(args.candidates))
    if args.candidates and not_measured:
        _print("named anyway      %d candidate(s) were refused on this stream and are measured because the "
               "caller named them: %s" % (len(not_measured),
                                           ", ".join(candidate.name for candidate in not_measured)))
        _print("")
    elif not_measured:
        _print("not measured      %d candidate(s) the proposal includes, already refused on this stream: %s"
               % (len(not_measured), ", ".join(candidate.name for candidate in not_measured)))
        _print("")
    selected = kept
    if not selected:
        _print("nothing to measure: every candidate this round would measure has already been refused on "
               "this stream at this sampling. Name them with --candidates to measure them anyway.")
        return 0
    # Round 37: the count of measurements this round did not spend travels with the proposal record, so a
    # reader of the round file can tell a mechanism that was spared work from one that was simply idle.
    proposal_record["refused_skipped"] = int(proposal_record.get("refused_skipped", 0)) + len(not_measured)

    totals = summarise(streams)
    for section, values in base_overrides.items():
        _print("base override   %s: %s" % (section, ", ".join(
            "%s=%r" % (key, value) for key, value in sorted(values.items()))))
    _print("evolving over %d recording(s), %d windows (%d scored, %d anomalous, %d events)"
           % (totals["streams"], totals["windows"], totals["scored"], totals["anomalous"],
              totals["events"]))
    _print("measuring baseline…")
    # Round 31: the round states what the configuration in force promised and whether its own stream can
    # test that promise. A round whose baseline is not comparable is a measurement of this sampling, not
    # evidence about the claim, and the difference decides how a reader may use the table below.
    claim_record: Dict[str, Any] = {}
    result = run_round(settings, streams, selected=selected, rules=rules, source=source,
                       proposal=proposal_record, claim=claim_record)
    # Round 46: the round says which file it measured, so a refusal written from it can be re-checked
    # without an operator remembering the path. Two files can carry the same dataset sampled differently,
    # and a re-check on the other one measures a different quantity (rounds 26 and 44).
    result.stream_path = str(args.stream_file or "")
    # Round 68: the round records the ceilings it was judged under. A ceiling is not in `EvolveRules`, so it
    # can move without moving the rules hash -- the same rulebook would then re-decide the same measurements
    # and reach another answer, and nothing in the record would explain why.
    result.limits = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                 drain_ceiling, drain_ceilings, flag_drain_ceilings)
    if owner is not None:
        verdict, reason = claim_check(owner.claim, result.baseline, rules,
                                      recorded_stream=owner.stream, checked_stream=stream_shape)
        age = verification_age_days(owner.verification)
        result.claim = {
            "version": owner.version,
            "promise": owner.claim.recall_at_budget,
            "measured": result.baseline.recall_at_budget,
            "verdict": verdict,
            "reason": reason,
            "age_days": age,
            "due": owner.due(days=args.expiry_days),
        }
    _print("")
    _print(format_round(result))

    written = persist(root, result, write_versions=args.apply)
    # Round 43: a candidate that was refused before and measured again writes the new number back into the
    # record that refused it. `persist` does it (so a scripted round documents itself too); this only prints
    # what happened, because a write-back nobody is told about is the defect the round is about.
    for check in written.get("checks") or []:
        _print("  re-check  %s  %s: %s" % (check["name"], check["label"], check["verdict"]))
    _print("")
    _print("recorded round %s" % written.get("round"))
    for version_path in written.get("versions") or []:
        _print("  version %s" % version_path)
        try:
            payload = json.loads(Path(version_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _print("    export %s" % payload.get("env"))
        _print("    rollback: %s" % payload.get("rollback"))
    if written.get("versions"):
        # A version file the deployment state does not mention is a claim nobody made live; put the
        # accepted configuration into force here, so `--verify` has something to re-check later.
        fresh = [promotion for promotion in load_promotions(root)
                 if str(promotion.path) in (written.get("versions") or [])]
        if len(fresh) == 1:
            applied = apply_promotion(root, fresh[0], domain=domain)
            _print("in force (%s): %s" % (domain_slug(domain),
                                        ", ".join("%s=%s" % item for item in
                                                  sorted(current_state(root, domain)["env"].items()))))
            for line in applied.get("interactions") or []:
                _print("  ! %s" % line)
                _print("    that setting was measured as part of the earlier version, not this one: the")
                _print("    configuration in force is not the one this round measured.")
        elif fresh:
            # They were measured against the same baseline, so they are alternatives. Applying all of
            # them merges several measurements into a configuration no round has looked at.
            _print("in force: (nothing changed)")
            _print("%d patches were accepted and they are alternatives measured against the same" % len(fresh))
            _print("baseline, so merging them would produce a configuration nobody measured. Put one of")
            _print("them in force with:  brian evolve --verify --verify-version <n>")
    if result.accepted and not args.apply:
        _print("Dry run: the round is recorded so the next run will not re-propose it, but no version")
        _print("file was written. Re-run with --apply to record the environment assignment too.")

    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        _print("")
        _print("round written to %s" % target)
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    """Run the whole chain over synthetic telemetry and print the reasoning."""
    if args.sequence:
        sequence = tuple(s.strip() for s in args.sequence.split(",") if s.strip())
    else:
        sequence = DEFAULT_SEQUENCE
    if args.only:
        sequence = (args.only,)

    windows = generate(
        sequence=sequence,
        service=args.service,
        warmup_minutes=args.warmup,
        seed=args.seed,
    )
    events = flatten(windows)

    bus = MemoryBus() if args.bus == "memory" else open_bus(settings)
    laya = LayaClient(settings.laya)
    rca = RcaEngine(settings.llm)

    _print("=" * 78)
    _print("brian — Flink → Laya → LLM real-time triage")
    _print("=" * 78)
    _print("memory backend   %s" % bus.backend)
    _print("laya             %s (model=%s)" % (settings.laya.url, settings.laya.model))
    _print("reasoner         %s (%s)" % (rca.mode, settings.llm.model if rca.mode == "llm" else "deterministic rules"))
    _print("window           %ss tumble, lateness %ss, min %d events"
           % (settings.window.size_s, settings.window.slide_s, settings.window.min_events))
    _print("catalog          %s  %s" % (CATALOG_VERSION, catalog_hash()))
    fit = settings.load_calibration()
    if fit is not None:
        _print("calibration      platt slope=%.3f intercept=%+.3f (T=%.2f) on %d windows; AUC %.3f, ECE %s -> %s"
               % (fit.slope, fit.intercept, fit.temperature, fit.n, fit.auc or 0.0,
                  fit.ece_before, fit.ece_after))
    else:
        _print("calibration      none — raw model probabilities are used as-is")
        _print("                 (run `brian calibrate` once you have labelled windows)")
    problems = validate_questions()
    if problems:
        _print("catalog problems %s" % problems)
    _print("")

    service = _service_report(settings, laya)
    if not service.get("reachable"):
        _print("Laya is NOT reachable at %s: %s" % (settings.laya.url, service.get("error")))
        _print("Start it with:  D:\\project\\laya\\service\\start.cmd")
        _print("Nothing is faked here: without the decision engine there is no System 1 judgement.")
        return 3
    _print("laya health      ready=%s device=%s checkpoint=%s"
           % (service.get("ready"), service.get("device"),
              (service.get("checkpoint") or {}).get("name", settings.laya.model)))
    if not service.get("model_available"):
        _print("laya model       NOT AVAILABLE: %s" % service.get("model_error"))
        return 3
    if not service.get("ready"):
        _print("laya is still warming up; the first calls will pay the load.")
    _print("")

    pipeline = Pipeline(settings, bus, laya=laya, rca=rca, keep_events=40)

    label_by_window: Dict[str, str] = {}
    for window in windows:
        label_by_window[window.window_start.replace(second=0, microsecond=0).isoformat()] = window.scenario
    expected_by_key = {
        (w.service, int(w.window_start.timestamp())): w for w in windows
    }

    judged = 0
    alert_lines: List[str] = []
    reports: List[Any] = []

    for event in events:
        pipeline.ingest(event, publish=True)
        for trace in pipeline.drain_ready():
            key = (trace.digest.service, int(trace.digest.window_start.timestamp()))
            label = expected_by_key.get(key)
            scenario = label.scenario if label else "?"
            if label is not None and not label.scored:
                continue
            decision = trace.decision
            if decision is None:
                _print("%s %-20s  skipped (fewer than %d events in the window)"
                       % (trace.digest.window_start.strftime("%H:%M"), scenario,
                          settings.window.min_events))
                continue
            judged += 1
            expected = EXPECTED.get(scenario, {})
            mark = ""
            if label is not None:
                mark = "ok" if decision.is_anomaly == bool(expected.get("is_anomaly")) else "MISS"
            _print("%s %-20s %-7s p=%-6s conf=%-6s %-20s %-9s %-8s %5.0fms %s" % (
                trace.digest.window_start.strftime("%H:%M"),
                scenario,
                _tag(decision.is_anomaly),
                "n/a" if decision.anomaly_probability is None else "%.3f" % decision.anomaly_probability,
                "%.3f" % decision.confidence,
                (decision.category.value if decision.category else "-"),
                (decision.severity.value if decision.severity else "-"),
                _action_style(decision.action.value),
                (trace.decision.latency_ms or 0.0),
                mark,
            ))
            raw = (trace.gate.signals if trace.gate else {}).get("raw_anomaly_probability")
            if raw is not None and decision.anomaly_probability is not None and abs(raw - decision.anomaly_probability) > 1e-6:
                record = (trace.gate.signals.get("calibration") or {})
                _print("        raw P(anomaly)=%.3f -> calibrated %.3f (slope=%.2f intercept=%+.2f)"
                       % (raw, decision.anomaly_probability, record.get("slope") or 1.0,
                          record.get("intercept") or 0.0))
            _print("        %s" % decision.gate_reason)
            if trace.alert is not None:
                alert_lines.append(
                    "[%s] %s → %s" % (trace.alert.severity.value.upper(), trace.alert.title,
                                      decision.action.value))
                _print("        ALERT  %s" % trace.alert.title)
            if trace.report is not None:
                reports.append(trace.report)
                _print("        RCA    %s" % trace.report.summary)
                _print("               cause: %s" % trace.report.root_cause[:150])
                if trace.report.hypotheses:
                    top = trace.report.hypotheses[0]
                    _print("               hypothesis 1 (p=%.2f): %s" % (
                        top.get("confidence", 0), str(top.get("cause"))[:130]))
                if len(trace.report.hypotheses) > 1:
                    second = trace.report.hypotheses[1]
                    _print("               hypothesis 2 (p=%.2f): %s" % (
                        second.get("confidence", 0), str(second.get("cause"))[:130]))
                for action in trace.report.actions[:3]:
                    _print("               action [risk=%s autonomous=%s] %s" % (
                        action.get("risk"), action.get("autonomous"), str(action.get("step"))[:120]))
                if trace.report.degraded:
                    _print("               NOTE: deterministic fallback report (no LLM) — actions require a human")
            _print("")

    summary = pipeline.summary()
    _print("=" * 78)
    _print("Summary")
    _print("=" * 78)
    _print(_json(summary["counters"]))
    _print("")
    _print("alerts raised: %d" % len(alert_lines))
    for line in alert_lines:
        _print("  " + line)
    _print("")
    _print("memory (what is stored for the audit trail):")
    _print(_json(summary["bus"].get("topics", {})))

    if args.json_out:
        Path(args.json_out).write_text(
            _json({"summary": summary, "windows_judged": judged,
                   "rca_report_ids": [r.rca_id for r in reports]}),
            encoding="utf-8",
        )
        _print("")
        _print("wrote %s" % args.json_out)
    return 0


# --------------------------------------------------------------------------------------
# eval
# --------------------------------------------------------------------------------------


def cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    sequence = tuple(s.strip() for s in args.sequence.split(",") if s.strip()) if args.sequence else None
    if args.only:
        sequence = (args.only,)
    base = list(sequence) if sequence else list(DEFAULT_SEQUENCE)
    windows = _repeat_windows(base, args.service, args.warmup, args.repeat, args.seed)

    laya = LayaClient(settings.laya)
    service = _service_report(settings, laya)
    if not service.get("reachable") or not service.get("model_available"):
        _print("cannot evaluate: %s" % (service.get("error") or service.get("model_error")))
        return 3

    _print("evaluating %d labelled windows against %s (model=%s)…"
           % (len(windows), settings.laya.url, settings.laya.model))

    def progress(score: Any) -> None:
        if not args.quiet:
            sys.stderr.write(
                "\r  %s %-20s expected=%-5s got=%-5s p=%s   "
                % (
                    score.window_start.strftime("%H:%M"),
                    score.scenario,
                    score.expected_anomaly,
                    score.predicted_anomaly,
                    "n/a" if score.predicted_probability is None else "%.3f" % score.predicted_probability,
                )
            )
            sys.stderr.flush()

    evaluator = Evaluator(settings, laya=laya)
    report = evaluator.run(windows, warmup=args.warmup, on_window=progress)
    if not args.quiet:
        sys.stderr.write("\r" + " " * 100 + "\r")

    _print(format_report(report, verbose=not args.brief))
    recommendation = gate_recommendation(report)
    _print("Automation readiness: %s" % recommendation["verdict"].upper())
    for check, detail in recommendation["checks"].items():
        _print("  %-22s %s  (%s)" % (check, detail["value"], "pass" if detail["pass"] else "FAIL"))
    for reason in recommendation["reasons"]:
        _print("  ! %s" % reason)
    _print("  %s" % recommendation["guidance"])

    if args.json_out:
        payload = report.to_dict()
        payload["recommendation"] = recommendation
        Path(args.json_out).write_text(_json(payload), encoding="utf-8")
        _print("")
        _print("wrote %s" % args.json_out)
    return 0


# --------------------------------------------------------------------------------------
# calibrate
# --------------------------------------------------------------------------------------


def _repeat_windows(sequence: List[str], service: str, warmup: int, repeat: int,
                    seed: int) -> List[LabeledWindow]:
    """Build `repeat` passes over a scenario sequence, on a non-overlapping timeline.

    The offset is not cosmetic. Windows are keyed by `(service, window_start)`, so two passes
    sharing a timeline produce *colliding* windows and silently collapse into one — a bug that
    presented exactly as this harness reporting 21 scored windows while claiming to have generated
    126. Each pass therefore starts after every window of the previous one has closed.
    """
    windows: List[LabeledWindow] = []
    cursor = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    for index in range(max(1, repeat)):
        created = generate(
            sequence=sequence,
            service=service,
            start=cursor,
            warmup_minutes=warmup if index == 0 else 0,
            seed=seed + index,
        )
        windows.extend(created)
        # One minute per window, plus a gap so the next pass cannot share a window start.
        cursor = cursor + timedelta(minutes=len(created) + 1)
    return windows


def cmd_calibrate(args: argparse.Namespace, settings: Settings) -> int:
    """Fit a confidence temperature on labelled windows and save it.

    Runs the evaluator with calibration *disabled* so the collected probabilities are the raw
    model output, fits T by minimising log loss, and writes the fit next to the models. The
    fitted value is then picked up automatically by `demo`, `eval` and the Flink job.
    """
    from .calibration import collect_from_scores, fit_calibration, format_fit
    from .config import CalibrationSettings

    if args.from_outcomes:
        # Fit on real operator labels instead of the synthetic suite. This is the preferred source:
        # the scenario suite is a bootstrap, and its base rate is an artefact of how many windows each
        # scenario happens to contribute.
        from .outcomes import LabelSource, format_summary as format_outcome_summary

        bus = open_bus(settings, verbose=False)
        source = LabelSource(settings).load(bus)
        _print(format_outcome_summary(source.summary()))
        _print("")
        try:
            fit = source.fit()
        except ValueError as exc:
            _print("cannot fit from outcomes: %s" % exc)
            return 2
        _print(format_fit(fit, alert_rate=args.alert_rate))
        labels = source.usable_for_calibration
        probabilities = [label.raw_probability for label in labels if label.raw_probability is not None]
        y = [label.is_anomaly for label in labels]
        operating = _operating_point(probabilities, y, args.max_fpr)
        if operating:
            fit.operating = operating
            _print("")
            _print("Operating point stored with the fit (bounded at fpr <= %.2f):" % args.max_fpr)
            _print("  raw threshold %.4f -> calibrated %.4f" % (
                operating["threshold"], fit.apply(operating["threshold"]) or 0.0))
            _print("  recall=%s  fpr=%s  precision=%s  alerts/1000 windows=%s"
                  % (operating["recall"], operating["fpr"], operating["precision"],
                     operating["alerts_per_1000_windows"]))
        return _finish_calibration(args, settings, fit, probabilities, y, "outcomes")

    sequence = tuple(s.strip() for s in args.sequence.split(",") if s.strip()) if args.sequence else None
    if args.only:
        sequence = (args.only,)
    base = list(sequence) if sequence else list(DEFAULT_SEQUENCE)
    windows = _repeat_windows(base, args.service, args.warmup, args.repeat, args.seed)

    laya = LayaClient(settings.laya)
    service = _service_report(settings, laya)
    if not service.get("reachable") or not service.get("model_available"):
        _print("cannot calibrate: %s" % (service.get("error") or service.get("model_error")))
        return 3

    _print("collecting raw probabilities over %d labelled windows…" % len(windows))
    # Calibration off: measuring the checkpoint, not the correction.
    raw_settings = settings.with_overrides(
        calibration=CalibrationSettings(path=settings.calibration.path, enabled=False)
    )
    evaluator = Evaluator(raw_settings, laya=laya, calibrator=None, apply_calibration_from_settings=False)

    def progress(score: Any) -> None:
        if not args.quiet:
            sys.stderr.write(
                "\r  %s %-20s expected=%-5s p=%s      "
                % (score.window_start.strftime("%H:%M"), score.scenario, score.expected_anomaly,
                   "n/a" if score.predicted_probability is None else "%.4f" % score.predicted_probability)
            )
            sys.stderr.flush()

    report = evaluator.run(windows, warmup=args.warmup, on_window=progress)
    if not args.quiet:
        sys.stderr.write("\r" + " " * 100 + "\r")

    probabilities, labels = collect_from_scores(report.scores)
    try:
        fit = fit_calibration(probabilities, labels,
                              source="brian calibrate (%d windows)" % len(windows))
    except ValueError as exc:
        _print("cannot fit a calibration: %s" % exc)
        _print("")
        _print("What to do instead: run more scenarios (--repeat), or accept the raw probabilities")
        _print("and keep the gate escalating to System 2 rather than acting on System 1 alone.")
        return 2

    _print(format_fit(fit, alert_rate=args.alert_rate))

    # Record the operating point this fit was evaluated at, chosen by bounding noise rather than by
    # chasing recall. Alert fatigue is what actually kills a real-time detector: a page nobody reads
    # catches nothing, however good its recall looks on paper.
    operating = _operating_point(probabilities, labels, args.max_fpr)
    if operating:
        fit.operating = operating
        _print("")
        _print("Operating point stored with the fit (bounded at fpr <= %.2f):" % args.max_fpr)
        _print("  raw threshold %.4f -> calibrated %.4f" % (
            operating["threshold"], fit.apply(operating["threshold"]) or 0.0))
        _print("  recall=%s  fpr=%s  precision=%s  alerts/1000 windows=%s"
              % (operating["recall"], operating["fpr"], operating["precision"],
                 operating["alerts_per_1000_windows"]))
        _print("  %s" % operating["note"])
    _print("")
    _print("Before the fit, at the current gate thresholds:")
    _print("  recall=%s  false_positive_rate=%s" % (
        _fmt_rate(report.recall), _fmt_rate(report.false_positive_rate)))
    calibrated_report = _replay_fit(report, fit, settings)
    return _finish_calibration(args, settings, fit, probabilities, labels, "windows",
                               calibrated_report=calibrated_report)


def _finish_calibration(args: argparse.Namespace, settings: Settings, fit: Any,
                        probabilities: Sequence[float], labels: Sequence[bool], source: str,
                        calibrated_report: Any = None) -> int:
    """Shared tail of `calibrate`: report the effect, guard, and write the fit.

    Shared between the synthetic-suite and outcome-based paths so the *guard* cannot diverge between
    them — a refusal to write a fit for an indiscriminate model is a safety property, and having two
    copies of it is how one copy ends up missing.
    """
    from .calibration import apply_calibration

    if calibrated_report is not None:
        _print("After applying the fit to the same stored probabilities (threshold p>=%.2f):"
               % settings.gate.anomaly_probability)
        _print("  recall=%s  false_positive_rate=%s  category accuracy=%s" % (
            _fmt_rate(calibrated_report.recall), _fmt_rate(calibrated_report.false_positive_rate),
            _fmt_rate(calibrated_report.category_accuracy)))
        recall_after = calibrated_report.recall
    else:
        # No stored report: recompute the confusion directly from the labelled probabilities.
        threshold = settings.gate.anomaly_probability
        tp = fp = tn = fn = 0
        for probability, truth in zip(probabilities, labels):
            predicted = (apply_calibration(probability, fit) or 0.0) >= threshold
            if truth and predicted:
                tp += 1
            elif truth and not predicted:
                fn += 1
            elif not truth and predicted:
                fp += 1
            else:
                tn += 1
        positives, negatives = tp + fn, fp + tn
        recall_after = tp / positives if positives else None
        _print("After applying the fit to the same stored probabilities (threshold p>=%.2f):"
               % threshold)
        _print("  recall=%s  false_positive_rate=%s  (tp=%d fp=%d tn=%d fn=%d)" % (
            _fmt_rate(recall_after), _fmt_rate(fp / negatives if negatives else None), tp, fp, tn, fn))

    if recall_after == 0 and fit.discriminates:
        # AUC is fine but nothing crosses the threshold: the boundary is in the wrong place, and the
        # fix is a policy decision about the escalation rate, not a different transform.
        point = fit.operating_point(args.alert_rate)
        _print("")
        _print("The model separates the classes (AUC %.3f) but nothing crosses P>=%.2f, so the "
               "threshold is misplaced rather than the model being blind." % (
                   fit.auc or 0.0, settings.gate.anomaly_probability))
        if "threshold" in point:
            _print("A threshold of %.4f escalates ~%.1f%% of windows; set it with:"
                   % (point["threshold"], args.alert_rate * 100))
            _print("  BRIAN_GATE_NOTIFY_P=%.4f BRIAN_GATE_ANOMALY_P=%.4f" % (
                point["threshold"], point["threshold"]))
        _print("Choose that rate deliberately: it is the false-page versus missed-incident trade, "
               "and it belongs in a config file rather than in a default.")

    if args.dry_run:
        _print("")
        _print("--dry-run: nothing written")
        return 0

    if not fit.discriminates and not args.force:
        # Writing this file would be the single most damaging thing this command could do: it would
        # turn a checkpoint that cannot separate the classes into one that reports a *better* ECE
        # (by predicting ~0.5 for everything) while being unable to raise a single alert. Refuse,
        # and remove any previously written fit so the pipeline does not keep using it.
        stale = settings.calibration_file()
        if stale.is_file():
            stale.unlink()
            _print("")
            _print("removed the previous fit at %s (it was masking this problem)" % stale)
        _print("")
        _print("REFUSING to write a calibration: AUC is %s, so the checkpoint does not separate "
               "anomalous from healthy windows." % fit.auc)
        _print("")
        _print("Why a recalibration cannot help: it is a monotone transform of the probability, so it")
        _print("cannot create separation that is not there. A flatter fit makes ECE look better by")
        _print("pushing every window toward the base rate, while recall stays at zero. That is a")
        _print("worse outcome than an honest 0.45 for every window, because it looks fixed.")
        _print("")
        _print("What actually fixes it, in order:")
        _print("  1. Build a labelled set from this domain (the `outcomes` topic is where they come from:")
        _print("     `brian outcome <alert_id> --result resolved|false_alarm` records one).")
        _print("  2. Fine-tune the checkpoint on it — see docs/FINETUNE.zh.md. Upstream measured the base")
        _print("     checkpoints at 0.362/0.342 on typed-decisions against a 0.318 random baseline:")
        _print("     the capability comes from fine-tuning, not from the base model.")
        _print("  3. Re-run calibrate on the fine-tuned checkpoint, then re-run eval.")
        _print("")
        _print("Until then keep the gate as shipped (confidence_floor=0.45 escalates to System 2)")
        _print("rather than acting on System 1 alone. Use --force to write the fit anyway.")
        return 2

    path = fit.save(str(settings.calibration_file()))
    _print("")
    _print("wrote %s (fitted from %s)" % (path, source))
    if not fit.discriminates:
        _print("WARNING: written with --force despite AUC=%s. Automation must not gate on this." % fit.auc)
    else:
        _print("It is applied automatically from now on. Disable with BRIAN_CALIBRATION_ENABLED=0.")
    return 0


def _replay_fit(report: Any, fit: Any, settings: Settings) -> Any:
    """Re-apply the fitted calibration to already-collected raw probabilities.

    No new Laya calls: the answers are in `report.scores` already, and re-asking would burn GPU
    time to measure the same numbers. This is only possible because the raw probability is kept
    alongside the calibrated one — which is the reason it is kept.
    """
    from .calibration import apply_calibration
    from .eval import EvalReport, WindowScore

    replay = EvalReport(model=report.model, catalog_hash=report.catalog_hash)
    for score in report.scores:
        clone = WindowScore(
            scenario=score.scenario,
            service=score.service,
            window_start=score.window_start,
            expected_anomaly=score.expected_anomaly,
            expected_category=score.expected_category,
            expected_severity=score.expected_severity,
            raw_probability=score.raw_probability,
            calibrated=True,
            action=score.action,
            confidence=score.confidence,
            latency_ms=score.latency_ms,
        )
        probability = apply_calibration(score.raw_probability, fit)
        clone.predicted_probability = probability
        clone.predicted_anomaly = (
            None if probability is None else probability >= settings.gate.anomaly_probability
        )
        clone.predicted_category = score.predicted_category if clone.predicted_anomaly else None
        clone.predicted_severity = score.predicted_severity if clone.predicted_anomaly else None
        replay.scores.append(clone)
    replay.elapsed_s = report.elapsed_s
    replay.warmup_windows = report.warmup_windows
    return replay


def _fmt_rate(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.3f" % value


def _operating_point(probabilities: Sequence[float], labels: Sequence[bool],
                     max_fpr: float) -> Optional[Dict[str, Any]]:
    """The highest-recall raw threshold whose false-positive rate stays within `max_fpr`.

    Deliberately bounds the false-positive rate rather than the recall. Both are one-line changes,
    but only one of them describes a system somebody will keep running: an escalation stream that
    is wrong most of the time gets muted, and a muted detector has a recall of zero in practice no
    matter what the offline table says.
    """
    positives = sum(1 for y in labels if y)
    if not positives:
        return None
    negatives = len(labels) - positives
    best: Optional[Dict[str, Any]] = None
    for candidate in sorted(set(probabilities), reverse=True):
        tp = sum(1 for p, y in zip(probabilities, labels) if y and p >= candidate)
        fp = sum(1 for p, y in zip(probabilities, labels) if not y and p >= candidate)
        if not tp:
            continue
        fpr = fp / negatives if negatives else 0.0
        if fpr > max_fpr:
            continue
        recall = tp / positives
        if best is None or recall > best["recall"]:
            best = {
                "threshold": round(candidate, 6),
                "recall": round(recall, 4),
                "fpr": round(fpr, 4),
                "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
                "tp": tp, "fp": fp, "fn": positives - tp,
                "alerts_per_1000_windows": round((tp + fp) / len(labels) * 1000, 1),
                "max_fpr": max_fpr,
                "note": (
                    "chosen for the highest recall at fpr <= %.2f on %d labelled windows; "
                    "this is a false-page versus missed-incident policy choice, not a calibrated "
                    "probability" % (max_fpr, len(labels))
                ),
            }
    return best


# --------------------------------------------------------------------------------------
# config / topics
# --------------------------------------------------------------------------------------


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    payload = {
        "settings": settings.masked(),
        "bus": open_bus(settings, verbose=False).describe() if args.probe else {"backend": settings.kafka.backend},
        "catalog": {k: v for k, v in describe_catalog().items() if k != "questions"},
        "gate_policy": explain_thresholds(settings.gate),
        "topics": describe_topics(settings.kafka.prefix),
    }
    if args.laya:
        payload["laya"] = _service_report(settings, LayaClient(settings.laya))
    _print(_json(payload))
    problems = validate_questions()
    if problems:
        _print("")
        _print("CATALOG PROBLEMS:")
        for problem in problems:
            _print("  - %s" % problem)
        return 1
    return 0


def cmd_topics(args: argparse.Namespace, settings: Settings) -> int:
    from .bus import ensure_topics

    plan = describe_topics(settings.kafka.prefix)
    bus = open_bus(settings, verbose=False)
    created = ensure_topics(bus, settings, partitions=args.partitions)
    _print("backend: %s" % bus.backend)
    _print("")
    _print("%-26s %-12s %s" % ("topic", "key", "purpose"))
    _print("-" * 110)
    for entry in plan:
        const = entry["topic"].split(".", 1)[1] if "." in entry["topic"] else entry["topic"]
        _print("%-26s %-12s %s" % (entry["topic"], key_field(const), entry["purpose"]))
    _print("")
    _print("%d topics ensured: %s" % (len(created), ", ".join(created) if created else "(already present)"))
    return 0


# --------------------------------------------------------------------------------------
# lineage / tail
# --------------------------------------------------------------------------------------


def cmd_lineage(args: argparse.Namespace, settings: Settings) -> int:
    """Walk the trail backwards from any record id — the audit query.

    By default raw events are summarised as a count rather than listed: a 60-second window carries
    ~75 of them, and printing each one buries the records a reader is actually looking for (the
    digest, the decision, the report) under evidence that is already summarised in the digest. Pass
    `--events` to see them.
    """
    bus = open_bus(settings, verbose=False)
    pipeline = Pipeline(settings, bus, laya=LayaClient(settings.laya), rca=RcaEngine(settings.llm))
    chain = pipeline.lineage_of(args.record_id)
    if not chain:
        _print("no record with id %r in the memory (checked every topic)" % args.record_id)
        return 2

    events = [entry for entry in chain if entry["topic"] == Topic.EVENTS]
    others = [entry for entry in chain if entry["topic"] != Topic.EVENTS]
    _print("lineage for %s — %d records, oldest first:" % (args.record_id, len(chain)))
    _print("")
    if events and not args.events:
        spans = [r["record"].get("event_time") for r in events if r["record"].get("event_time")]
        window = (" from %s to %s" % (min(spans), max(spans))) if spans else ""
        _print("  [events] %d raw events%s" % (len(events), window))
        _print("      (already summarised by the digest below; pass --events to list them)")
        _print("")
    for entry in events if args.events else others:
        record = entry["record"]
        _print("  [%s] %s" % (entry["topic"], entry["id"]))
        _print("      %s" % _summarise_record(entry["topic"], record))
    if not args.events and others:
        _print("")
        _print("This is the evidential basis for the action above: the window's digest is what the")
        _print("classifier read, the decision records what it concluded and which thresholds fired,")
        _print("and the report is System 2's explanation. `brian lineage <id> --events` adds the raw")
        _print("telemetry behind the digest.")
    return 0


def _summarise_record(topic: str, record: Dict[str, Any]) -> str:
    if topic == Topic.EVENTS:
        return "%s %s %s" % (record.get("event_time"), record.get("service"), record.get("kind"))
    if topic == Topic.FEATURES:
        return "%s window=%s events=%s" % (
            record.get("service"), record.get("window_start"), record.get("event_count"))
    if topic == Topic.DECISIONS:
        return "verdict=%s action=%s p=%s category=%s severity=%s confidence=%s" % (
            record.get("verdict"), record.get("action"), record.get("anomaly_probability"),
            record.get("category"), record.get("severity"), record.get("confidence"))
    if topic == Topic.ALERTS:
        return "%s [%s] %s" % (record.get("severity"), record.get("action"), record.get("title"))
    if topic == Topic.RCA:
        return "cause=%s (%s hypotheses, degraded=%s)" % (
            str(record.get("root_cause"))[:110], len(record.get("hypotheses") or []), record.get("degraded"))
    if topic == Topic.OUTCOMES:
        return "outcome=%s" % json.dumps(record)[:140]
    return json.dumps(record)[:140]


def cmd_tail(args: argparse.Namespace, settings: Settings) -> int:
    bus = open_bus(settings, verbose=False)
    topic = settings.topic(args.topic)
    records = bus.replay(topic, start_offset=args.offset, limit=args.limit)
    _print("%d records from %s (offset %d)" % (len(records), topic, args.offset))
    for record in records:
        if args.raw:
            _print(dumps(record.value))
        else:
            _print("  %s %s" % (record.record_id, _summarise_record(args.topic, record.value)))
    return 0


# --------------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------------


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    """Re-derive digests and decisions from stored events — the audit path.

    This does not read the `features` or `decisions` topics; it rebuilds from `events` so the
    result is independent of whatever the live pipeline wrote. If the rebuild disagrees with the
    stored decision, either the renderer/catalog changed (which the versions will show) or
    something is wrong — and either way, that is exactly what an audit is for.
    """
    bus = open_bus(settings, verbose=False)
    if args.events:
        events = _load_events(args.events)
        _print("replaying %d events from %s" % (len(events), args.events))
    else:
        records = bus.replay(settings.topic(Topic.EVENTS), limit=args.limit)
        events = []
        for record in records:
            try:
                events.append(loads(record.value, Event))
            except Exception as exc:
                _print("skipping unreadable event %s: %s" % (record.record_id, exc))
        _print("replaying %d events from %s" % (len(events), settings.topic(Topic.EVENTS)))
    if not events:
        _print("nothing to replay (is the journal empty? try `demo` first)")
        return 2
    events.sort(key=lambda e: e.event_time)

    baselines = BaselineTracker(
        window=settings.window.baseline_windows,
        long_window=settings.window.baseline_windows_long,
    )
    windower = Windower(
        size_s=settings.window.size_s,
        slide_s=settings.window.slide_s,
        allowed_lateness_s=settings.window.allowed_lateness_s,
        min_events=settings.window.min_events,
    )
    for event in events:
        windower.add(event)
    for aggregator in windower.flush():
        digest = build_digest(aggregator, baselines)
        line = "%s %-16s events=%-4d %s" % (
            digest.window_start.strftime("%H:%M"), digest.service, digest.event_count,
            digest.interpretation.splitlines()[1] if len(digest.interpretation.splitlines()) > 1 else "")
        _print(line)
        if args.decide:
            laya = LayaClient(settings.laya)
            try:
                result = laya.decide(digest)
                answers = result.answers
                _print("        verdict p(anomaly)=%s category=%s severity=%s (%.0f ms)" % (
                    answers.get("is_anomaly", {}).get("noul"),
                    (answers.get("category") or {}).get("choice"),
                    (answers.get("severity") or {}).get("score"),
                    result.latency_ms,
                ))
            except LayaError as exc:
                _print("        Laya unavailable: %s" % exc)
        baselines.observe(digest.service, digest.metrics)
    return 0


def cmd_reset(args: argparse.Namespace, settings: Settings) -> int:
    journal = JournalBus(settings.kafka.journal_dir)
    if not args.yes:
        _print("this deletes every file in %s" % journal.dir)
        for path in sorted(journal.dir.glob("*.jsonl")):
            _print("  %s" % path.name)
        _print("re-run with --yes to confirm")
        return 1
    journal.reset(args.topic)
    _print("reset %s" % ("topic " + args.topic if args.topic else "journal"))
    return 0


# --------------------------------------------------------------------------------------
# export-training
# --------------------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    """Export fine-tuning data in the shape the Laya trainer reads.

    The measurement chain needs this to close: the evaluation shows the base checkpoint has real but
    unusable discrimination on this domain, and only fine-tuning fixes that — so the pipeline must be
    able to produce labelled windows itself. Two label sources are supported: the synthetic scenario
    suite (a bootstrap, used here) and the `outcomes` topic (real operator decisions, the good source).
    """
    from .export import export_examples, format_summary, summarise, write_jsonl

    sequence = tuple(s.strip() for s in args.sequence.split(",") if s.strip()) if args.sequence else None
    if args.only:
        sequence = (args.only,)
    base = list(sequence) if sequence else list(DEFAULT_SEQUENCE)
    windows = _repeat_windows(base, args.service, args.warmup, args.repeat, args.seed)
    calibrator = settings.load_calibration()
    if calibrator is None and not args.raw_scale:
        _print("note: no calibration file found, so the anomaly targets are written on the model's raw")
        _print("      scale. Run `brian calibrate` first if you intend to deploy with a fitted")
        _print("      threshold, so the targets match the scale the gate actually uses.")
        _print("")

    examples = export_examples(
        windows, calibrator=calibrator, min_label_fraction=args.min_label_fraction,
        service=args.service,
    )
    if not examples:
        _print("no examples: the scenario suite produced no scored windows")
        return 2

    summary = summarise(examples)
    _print(format_summary(summary))
    trainable = summary["trainable_answers_total"]
    total = summary["answers_total"]
    if total and trainable / total < 0.6:
        _print("")
        _print("WARNING: only %d of %d answers are trainable. The masked-out questions have no ground"
               % (trainable, total))
        _print("truth in the scenario suite. Label them from real outcomes (the `outcomes` topic) or a")
        _print("teacher model before training on them — see docs/FINETUNE.zh.md.")

    if args.dry_run:
        _print("")
        _print("--dry-run: nothing written")
        return 0
    path = write_jsonl(examples, args.out)
    _print("")
    _print("wrote %s (%d examples)" % (path, len(examples)))
    if args.summary_out:
        Path(args.summary_out).write_text(_json(summary), encoding="utf-8")
        _print("wrote %s" % args.summary_out)
    return 0


# --------------------------------------------------------------------------------------
# outcome
# --------------------------------------------------------------------------------------


def cmd_outcome(args: argparse.Namespace, settings: Settings) -> int:
    """Record what happened. This is the label that makes the loop closed rather than open.

    Everything else the system writes is its own opinion. This is the one record carrying a human
    judgement, and it is the label source for calibration, evaluation and training.
    """
    from .contracts import Outcome, OutcomeResult

    bus = open_bus(settings, verbose=False)

    if args.list:
        from .outcomes import LabelSource, format_summary

        source = LabelSource(settings).load(bus)
        _print(format_summary(source.summary()))
        return 0

    if not args.alert_id or not args.result:
        _print("need --alert-id and --result (or --list to see what has been recorded)")
        _print("results: %s" % ", ".join(member.value for member in OutcomeResult))
        return 2

    try:
        result = OutcomeResult.coerce(args.result)
    except Exception:
        _print("unknown --result %r; choose one of: %s"
               % (args.result, ", ".join(member.value for member in OutcomeResult)))
        return 2

    # Find the alert and its decision so the outcome can reference the judgement it judges. Refusing
    # to record an outcome with no matching decision is deliberate: an unjoinaable label would look
    # like data and be silently unusable, which is worse than an error message now.
    alert_record = None
    for record in bus.replay(settings.topic(Topic.ALERTS)):
        if (record.value or {}).get("alert_id") == args.alert_id:
            alert_record = record.value
            break
    if alert_record is None:
        _print("no alert with id %r in %s" % (args.alert_id, settings.topic(Topic.ALERTS)))
        _print("use `brian tail alerts --limit 50` to list recent ones")
        return 2

    decision_id = alert_record.get("decision_id")
    decision_present = any(
        (record.value or {}).get("decision_id") == decision_id
        for record in bus.replay(settings.topic(Topic.DECISIONS))
    )
    if not decision_present:
        _print("alert %s references decision %s, which is not in the memory" % (args.alert_id, decision_id))
        return 2

    report_present = None
    if args.hypothesis is not None:
        for record in bus.replay(settings.topic(Topic.RCA)):
            if (record.value or {}).get("alert_id") == args.alert_id:
                report_present = record.value
                break
        if report_present is None:
            _print("no RCA report for alert %s, so --hypothesis cannot be confirmed" % args.alert_id)
            return 2

    outcome = Outcome(
        alert_id=args.alert_id,
        decision_id=decision_id,
        service=alert_record.get("service") or "unknown",
        result=result,
        summary=args.summary or "",
        action_taken=args.action,
        operator=args.operator,
        confirmed_hypothesis_index=args.hypothesis,
        duration_s=args.duration_s,
        notes=list(args.note or []),
        lineage=Lineage(
            upstream_ids=[args.alert_id, decision_id],
            producer="operator",
        ),
    )
    bus.publish(outcome, settings.topic(Topic.OUTCOMES), key=outcome.alert_id, settings=settings)
    _print("recorded outcome %s for alert %s (result=%s, anomaly_label=%s)"
           % (outcome.outcome_id, outcome.alert_id, outcome.result.value, outcome.anomaly_label))
    if outcome.anomaly_label is None:
        _print("note: %r implies no anomaly label, so it will not be used for calibration or scoring."
               % outcome.result.value)
    _print("")
    _print("It is now label material. Next:")
    _print("  python -m brian outcome --list                 # production recall / false-alarm rate")
    _print("  python -m brian calibrate --from-outcomes       # refit on real labels once you have 20+")
    return 0


# --------------------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brian",
        description="Flink → Laya → LLM real-time anomaly triage, with Kafka as the memory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cost = sub.add_parser("cost", help="run the stream and report execution and analysis cost")
    cost.add_argument("--sequence", default="", help="comma-separated scenarios")
    cost.add_argument("--service", default="checkout-api")
    cost.add_argument("--warmup", type=int, default=20)
    cost.add_argument("--seed", type=int, default=7)
    cost.add_argument("--always-analyze", action=argparse.BooleanOptionalAction, default=True,
                      help="analyse every alerting action (default); --no-always-analyze routes by action")
    cost.add_argument("--only-actions", dest="analyze_actions", default="auto_triage,deep_analysis,page_human",
                      help="comma list of actions allowed to spend tokens when --no-always-analyze is used")
    cost.set_defaults(func=cmd_cost)

    evolve = sub.add_parser(
        "evolve",
        help="run the improvement mechanism: propose, measure, select, persist",
    )
    evolve.add_argument("--sequence", default="", help="comma-separated scenarios")
    evolve.add_argument("--stream-file", default="",
                        help="measure a labelled stream file instead of the demo suite "
                             "(see scripts/service_data/rcaeval_stream.py)")
    evolve.add_argument("--service", default="checkout-api")
    evolve.add_argument("--warmup", type=int, default=20)
    evolve.add_argument("--seed", type=int, default=7)
    evolve.add_argument("--candidates", default="",
                        help="comma list of catalogue names (default: whatever the proposal rule chooses)")
    evolve.add_argument("--propose", action="store_true",
                        help="print what the proposal rule would measure and why, then exit (no Laya)")
    evolve.add_argument("--calibrate-bar", action="store_true",
                        help="print the reader's label-free bars for this stream at 2/5/10%% "
                             "alert rates, then exit (no Laya)")
    evolve.add_argument("--catalogue", action="store_true",
                        help="force the fixed catalogue instead of letting the ledger choose the rule")
    evolve.add_argument("--expand", action="store_true",
                        help="force a bounded search around magnitudes that have already moved a column")
    evolve.add_argument("--min-gain", type=float, default=0.05,
                        help="fraction of tokens/report a change must save to be accepted")
    evolve.add_argument("--replication-tolerance", type=float, default=None,
                        help="how far one recording's at-budget recall may fall while the pooled number "
                             "rises before a quality gain stops counting as replicated (default: the "
                             "quality bar); stored with the refusal so --rejudge can disagree with it")
    evolve.add_argument("--withheld", action="store_true",
                        help="list the candidates the loop measured and refused for replication, and say "
                             "which refusals no longer hold at today's bar (no Laya)")
    evolve.add_argument("--refusal-expiry-days", type=float, default=REFUSAL_EXPIRY_DAYS,
                        metavar="DAYS",
                        help="how long a refusal speaks for itself before the round measures the candidate "
                             "again (default: %(default).0f); --expiry-days is the same question for a "
                             "version's promise")
    evolve.add_argument("--sensitivity", action="store_true",
                        help="for each stream, how many `replicate` refusals would become acceptances at a "
                             "looser replication bar (re-decides stored measurements, no re-run), then exit")
    evolve.add_argument("--sensitivity-bar", type=float, default=[], action="append", metavar="BAR",
                        help="with --sensitivity: a bar to test (repeat the flag); default: the bar in "
                             "force, 2x and 4x it")
    evolve.add_argument("--unrecorded", action="store_true",
                        help="list the rounds that cannot be placed on a sampling but whose numbers still "
                             "decide something, per stream, and exit (no Laya)")
    evolve.add_argument("--max-unrecorded", type=float, default=None, metavar="SHARE",
                        help="with --strict: fail when more than this share of the recorded rounds cannot "
                             "be placed on a sampling (0.25 means 25%%); a round written before the field "
                             "existed is still only a warning")
    evolve.add_argument("--trend-days", type=float, default=7.0, metavar="DAYS",
                        help="how far back the campaign and the audit recompute the unrecorded share; a "
                             "window older than the first record takes that record as its baseline")
    evolve.add_argument("--max-share-rise", type=float, default=None, metavar="POINTS",
                        help="with --strict: fail when the unrecorded share rose by more than this many "
                             "percentage points over the trend window (a rise is a warning otherwise: a "
                             "round measured on the generated stream names no file by design)")
    evolve.add_argument("--max-debt-share", type=float, default=None, metavar="SHARE",
                        help="with --strict: fail when the worst domain's unrecorded share is above this "
                             "fraction (0.5 = half its rounds cannot be placed), provided a re-run can lower "
                             "it; a share no command can pay is printed but never charged")
    evolve.add_argument("--max-limit-drift", type=float, default=None, metavar="ROUNDS",
                        help="with --audit --strict: fail when more than this many recorded rounds were "
                             "judged under wait ceilings that differ from today's (a ceiling is not in the "
                             "rulebook, so a rules audit cannot otherwise see it move)")
    evolve.add_argument("--max-ceiling-debt", type=float, default=None, metavar="SHARE",
                        help="with --strict: fail when more than this fraction of the live rounds do not "
                             "record the ceilings they were judged under -- charged only when the re-run queue "
                             "can measure every one of them again, because a debt no command can pay is a "
                             "warning, not a gate")
    evolve.add_argument("--max-minutes", action="append", default=None, metavar="MINUTES|DOMAIN=MINUTES",
                        help="with --strict: fail when the next queue run would spend more than this many "
                             "minutes (estimated per domain from the median duration of the recorded rounds, "
                             "so it is a wait the ledger can prove, not a guess). Repeat as domain=minutes to "
                             "give one domain its own ceiling; a bare number is the global one")
    evolve.add_argument("--set-domain-limit", action="append", default=None, metavar="DOMAIN=MINUTES",
                        help="record in that domain's own deployment state how long one queue run may wait for "
                             "it; later runs charge it without the flag, and --ignore-state-limits opts out")
    evolve.add_argument("--set-domain-drain", action="append", default=None, metavar="DOMAIN=MINUTES",
                        help="record in that domain's own deployment state how long emptying its queue may "
                             "take; a different promise from one run, and recorded beside it")
    evolve.add_argument("--max-drain-minutes", action="append", default=None, metavar="MINUTES",
                        help="with --strict: fail when emptying the queue would spend more than this many "
                             "minutes; takes a number, domain=minutes, or default=minutes, like --max-minutes")
    evolve.add_argument("--ignore-state-limits", action="store_true",
                        help="with --strict: do not read the per-domain wait ceilings recorded in the deployment "
                             "state, only the ones on this command line")
    evolve.add_argument("--rerun-unrecorded", action="store_true",
                        help="re-run the rounds whose stream file can be named by geometry, then retire them "
                             "naming the new record; skips a round whose file cannot be chosen")
    evolve.add_argument("--rerun-limit", type=int, default=1, metavar="N",
                        help="with --rerun-unrecorded: how many rounds to re-run in this command (default 1, "
                             "because a round is minutes of measurement and the rest keep their place)")
    evolve.add_argument("--with-ceilings", action="store_true",
                        help="with --rerun-unrecorded: also re-measure the rounds that named their stream file "
                             "but never recorded the wait ceilings they were judged under, so the ceiling debt "
                             "those rounds represent can actually be paid "
                             "(default: on for a drain, off for a single step)")
    evolve.add_argument("--without-ceilings", action="store_true",
                        help="with --rerun-unrecorded: do not re-measure the named ceiling debt even when "
                             "draining, so a drain pays only the rounds whose stream file is unknown")
    evolve.add_argument("--apply", action="store_true",
                        help="also write a version file per accepted change "
                             "(the round itself is always recorded)")
    evolve.add_argument("--from-default", action="store_true",
                        help="measure against the factory defaults instead of the settings in force "
                             "(the deployment state is the baseline by default)")
    evolve.add_argument("--grade", action="store_true",
                        help="print the L1-L5 grading and exit")
    evolve.add_argument("--repair-links", action="store_true",
                        help="write the missing end of a legacy replacement link; plan only without --apply")
    evolve.add_argument("--history", action="store_true",
                        help="print every round recorded so far and exit")
    evolve.add_argument("--rejudge", default="",
                        help="re-decide a recorded round file under today's rules and exit")
    evolve.add_argument("--audit", action="store_true",
                        help="re-decide every recorded round under today's rules and exit")
    evolve.add_argument("--retire", default=[], action="append", metavar="ROUND",
                        help="mark a recorded round superseded by a re-measurement and exit; its record "
                             "stays as written, the checks stop asking the mechanism to answer for it. "
                             "Repeat the flag or separate the names with commas to retire several")
    evolve.add_argument("--re-measured-as", default="",
                        help="with --retire: the round that replaced it, so the retirement names a successor")
    evolve.add_argument("--campaign", action="store_true",
                        help="summarise every recorded round per stream, then exit (no Laya)")
    evolve.add_argument("--expiry-days", type=float, default=CLAIM_EXPIRY_DAYS,
                        help="how long a recorded verdict speaks for its claim (days); overdue versions "
                             "are re-checked by --verify and marked in --campaign")
    evolve.add_argument("--at-hint", action="store_true",
                        help="with --verify: also measure the version at the window count that covers "
                             "the same span, and record it as another shape of the same promise")
    evolve.add_argument("--verify", action="store_true",
                        help="re-measure every unverified promotion and revert the ones whose claim "
                             "no longer holds, then exit")
    evolve.add_argument("--verify-version", type=int, default=0,
                        help="with --verify: only that version number (default: everything unverified)")
    evolve.add_argument("--accept-copied", action="store_true",
                        help="with --verify: measure a version whose producing record's link was repaired from "
                             "one end; the verdict is recorded and the deployment is left alone")
    evolve.add_argument("--verify-withheld", action="store_true",
                        help="with --verify: also re-measure this stream's refusals whose evidence is older "
                             "than the refusal horizon, and write the result into their record")
    evolve.add_argument("--withheld-stream", action="append", default=[], metavar="DOMAIN=STREAM",
                        help="re-measure every due refusal of each domain in one command; repeatable. "
                             "The stream is the one the refusal was raised on, so this mode checks no "
                             "promotion (round 45)")
    evolve.add_argument("--strict", action="store_true",
                        help="with --audit or --campaign: exit 1 when a recorded verdict would be decided "
                             "differently today, 2 when part of the ledger cannot be replayed at all")
    evolve.add_argument("--json-out", help="write the round to this file as well")
    evolve.set_defaults(func=cmd_evolve)

    demo = sub.add_parser("demo", help="run the full chain over synthetic telemetry")
    demo.add_argument("--sequence", help="comma-separated scenario names (default: the built-in sequence)")
    demo.add_argument("--only", help="run a single scenario repeatedly")
    demo.add_argument("--service", default="checkout-api")
    demo.add_argument("--warmup", type=int, default=6, help="healthy windows used to build the baseline first")
    demo.add_argument("--seed", type=int, default=20260114)
    demo.add_argument("--bus", choices=["memory", "configured"], default="configured")
    demo.add_argument("--json-out", help="write the run summary to this file")
    demo.set_defaults(func=cmd_demo)

    evaluate = sub.add_parser("eval", help="score System 1 against labelled windows")
    evaluate.add_argument("--only", help="evaluate a single scenario")
    evaluate.add_argument("--sequence", help="comma-separated scenarios")
    evaluate.add_argument("--repeat", type=int, default=1, help="repeat the sequence N times for a larger sample")
    evaluate.add_argument("--service", default="checkout-api")
    evaluate.add_argument("--warmup", type=int, default=6)
    evaluate.add_argument("--seed", type=int, default=20260114)
    evaluate.add_argument("--brief", action="store_true", help="omit the per-miss digest dump")
    evaluate.add_argument("--quiet", action="store_true", help="no progress line")
    evaluate.add_argument("--json-out")
    evaluate.set_defaults(func=cmd_eval)

    config = sub.add_parser("config", help="effective settings, catalog and gate policy")
    config.add_argument("--laya", action="store_true", help="also probe the Laya service")
    config.add_argument("--probe", action="store_true", help="also probe the memory backend")
    config.set_defaults(func=cmd_config)

    calibrate = sub.add_parser(
        "calibrate",
        help="fit a confidence calibration (Platt slope+intercept) so the gate's thresholds mean "
             "something",
        description=(
            "Fit a Platt correction (p' = sigmoid(slope * logit(p) + intercept)) by maximum "
            "likelihood on labelled windows, and store it beside the model. The gate applies it "
            "BEFORE any threshold, so the thresholds are stated on the calibrated scale.\n\n"
            "Reports AUC alongside ECE deliberately: AUC is invariant under any monotone "
            "recalibration, so it is the evidence for whether there is any signal to recalibrate. "
            "The command refuses to write a fit when there is not."
        ),
    )
    calibrate.add_argument("--only", help="use a single scenario")
    calibrate.add_argument("--sequence", help="comma-separated scenarios")
    calibrate.add_argument("--repeat", type=int, default=4, help="repeat the sequence to enlarge the sample")
    calibrate.add_argument("--service", default="checkout-api")
    calibrate.add_argument("--warmup", type=int, default=6)
    calibrate.add_argument("--seed", type=int, default=20260114)
    calibrate.add_argument("--quiet", action="store_true")
    calibrate.add_argument("--dry-run", action="store_true", help="fit and report without writing the file")
    calibrate.add_argument("--alert-rate", type=float, default=0.01,
                           help="fraction of windows that may escalate, used to place the threshold")
    calibrate.add_argument("--max-fpr", type=float, default=0.10,
                           help="false-positive rate budget; bounds the stored operating point")
    calibrate.add_argument("--from-outcomes", action="store_true",
                           help="fit on real operator outcomes instead of the synthetic suite (preferred)")
    calibrate.add_argument("--force", action="store_true",
                           help="write the fit even when the model shows no discrimination")
    calibrate.set_defaults(func=cmd_calibrate)

    topics = sub.add_parser("topics", help="show the topic plan and create the topics")
    topics.add_argument("--partitions", type=int, default=3)
    topics.set_defaults(func=cmd_topics)

    lineage = sub.add_parser("lineage", help="walk the trail backwards from a record id")
    lineage.add_argument("record_id", help="e.g. dec_ab12cd34ef56, alrt_..., rca_...")
    lineage.add_argument("--events", action="store_true",
                         help="also list the raw telemetry events (75 per window: verbose)")
    lineage.set_defaults(func=cmd_lineage)

    tail = sub.add_parser("tail", help="print records from a topic")
    tail.add_argument("topic", choices=[Topic.EVENTS, Topic.FEATURES, Topic.DECISIONS, Topic.ALERTS,
                                        Topic.RCA, Topic.OUTCOMES, Topic.DLQ])
    tail.add_argument("--offset", type=int, default=0)
    tail.add_argument("--limit", type=int, default=50)
    tail.add_argument("--raw", action="store_true", help="print the full JSON of each record")
    tail.set_defaults(func=cmd_tail)

    replay = sub.add_parser("replay", help="re-derive digests (and optionally decisions) from stored events")
    replay.add_argument("--events", help="a JSONL file of events instead of the journal")
    replay.add_argument("--limit", type=int, default=100000)
    replay.add_argument("--decide", action="store_true", help="also re-ask Laya for each window")
    replay.set_defaults(func=cmd_replay)

    export = sub.add_parser(
        "export-training",
        help="export fine-tuning data (digest state + typed answers + label provenance)",
    )
    export.add_argument("--out", default="models/training/anomaly_windows.jsonl")
    export.add_argument("--summary-out", help="also write the export summary as JSON")
    export.add_argument("--only", help="use a single scenario")
    export.add_argument("--sequence", help="comma-separated scenarios")
    export.add_argument("--repeat", type=int, default=6)
    export.add_argument("--service", default="checkout-api")
    export.add_argument("--warmup", type=int, default=6)
    export.add_argument("--seed", type=int, default=20260114)
    export.add_argument("--min-label-fraction", type=float, default=0.0,
                        help="drop windows with fewer than this fraction of trainable answers")
    export.add_argument("--raw-scale", action="store_true",
                        help="write anomaly targets on the model's raw scale even if a fit exists")
    export.add_argument("--dry-run", action="store_true")
    export.set_defaults(func=cmd_export)

    outcome = sub.add_parser(
        "outcome",
        help="record what actually happened (the label that closes the learning loop)",
    )
    outcome.add_argument("alert_id", nargs="?", help="the alert this outcome closes")
    outcome.add_argument("--result", help="resolved | unresolved | false_alarm | different_remedy | unknown")
    outcome.add_argument("--summary", help="one line on what was actually wrong")
    outcome.add_argument("--action", help="what was actually done")
    outcome.add_argument("--operator", help="who recorded it")
    outcome.add_argument("--hypothesis", type=int,
                         help="which RCA hypothesis turned out to be right (0 = the first)")
    outcome.add_argument("--duration-s", type=float, help="how long the incident lasted")
    outcome.add_argument("--note", action="append", help="free-form note; repeatable")
    outcome.add_argument("--list", action="store_true",
                         help="summarise recorded outcomes: production recall and false-alarm rate")
    outcome.set_defaults(func=cmd_outcome)

    reset = sub.add_parser("reset", help="delete the local journal")
    reset.add_argument("--topic", help="only this topic constant, e.g. events")
    reset.add_argument("--yes", action="store_true")
    reset.set_defaults(func=cmd_reset)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        _print("")
        _print("interrupted")
        return 130
    except LayaUnavailable as exc:
        _print("Laya is unreachable: %s" % exc)
        _print("Start it with:  D:\\project\\laya\\service\\start.cmd")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
