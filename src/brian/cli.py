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
    claim_mark,
    claim_note,
    claimed_rounds,
    inherit_claim,
    claim_round_file,
    backfill_claims,
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
    load_settlements,
    unanswered_withdrawals,
    withdrawal_plan,
    unexplained_decisions,
    moved_settlements,
    settlement_summary,
    repair_legacy,
    moved_clearable,
    reaffirm_link,
    reaffirm_plan,
    settlement_moment,
    is_withdrawn,
    settle_link,
    settle_note,
    restore_settlement,
    preview_settlement_change,
    preview_reanchor_change,
    preview_withdrawal_change,
    reanchor_plan,
    reanchor_settlement,
    withdraw_settlement,)
from .laya.client import LayaClient, LayaError, LayaUnavailable
from .campaign import budget_text, campaign as campaign_result, claim_counts, format_campaign, span_text
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
    clearing_evidence,
    in_force_disputes,
    in_force_history,
    blocked_versions,
    refusal_history,
    latest_refusals,
    walks_phrase,
    last_queue_run,
    in_force_overrides,
    legacy_state_note,
    load_promotions,
    pending_walk_backfill,
    queue_runs,
    observe_in_force_disputes,
    stale_in_force_disputes,
    promotions_for_domain,
    provenance_block,
    record_clearing,
    backfill_walks,
    begin_rerun_run,
    finish_rerun_run,
    rerun_walks,
    walk_held,
    record_queue_run,
    revert_promotion,
    set_state_budget,
    repaired_paths,
    repair_plan,
    record_repair,
    evidence_split,
    prior_evidence,
    same_stream_path,
    set_stream_path,
    dead_record_paths,
    never_tested_at_origin,
    origin_queue,
    queue_history,
    blocked_dossier,
    stale_refusals,
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


def _claim_note_line(record: Dict[str, Any]) -> str:
    """The sentence printed before a re-measurement on a file a person claimed (round 153).

    It exists as its own function because the sentence has to be checked without Laya: the round that
    exposed the first cut printed this warning and then measured nothing, so the order of "warn" and
    "measure" is part of what the test holds (boundary 372).
    """
    note = claim_note(record)
    if not note:
        return ""
    return ("  careful: %s -- what this measures is evidence about that file, not about the one the round "
            "first measured (boundary 371)" % note)


def _walk_store(root: Path, entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Which record in the walk store a payload just handed over (round 143).

    Round 142 made the queue walk hand over the record it keeps, and the campaign summarised all of them; neither
    said *which* record it was reading, so a script holding the payload and a reader holding
    `var/evolve/queue_runs.json` had to line them up by timestamp. The ordinal is the store's own position (boundary
    342): it is read back from the store rather than counted on the side, so the two cannot drift.
    """
    try:
        runs = queue_runs(root)["runs"]
    except OSError:
        runs = []
    stamp = str(entry.get("at") or "")
    ordinal = 0
    for index, run in enumerate(runs):
        if str(run.get("at") or "") == stamp:
            ordinal = index + 1
    return {"path": "var/evolve/queue_runs.json", "ordinal": ordinal, "of": len(runs)}


def _write_payload(path: str, payload: Any, recorded: bool = True,
                   keys: Optional[Tuple[str, ...]] = None) -> bool:
    """Write a JSON payload the way every command writes it, and say where it went.

    Round 141: the settlement commands wrote their payload with a bare `open(...)`, printed nothing, and raised a
    traceback when the path could not be written -- after the decisions had already been recorded. Eight call sites
    did it eight ways. This is the one way: the parent directory is created (round 113 already did this for the round
    record), the file is renamed into place so a half-written payload cannot be mistaken for a whole one, and the
    the same words the non-settlement commands have always used (boundary 336).

    `recorded=False` is for the report pages (`--history`, `--campaign`, round 162): they change no book, so the
    failure sentence must say that only the export is missing rather than report a store change that never happened.
    """
    # Round 174 (item 160): a declared shape is a claim about *this* command, so the writer checks it where the
    # command builds a payload -- not on the page, and not only in a test that can drift with the page.
    if keys is not None and isinstance(payload, dict):
        missing = [key for key in keys if key not in payload]
        extra = [key for key in payload if key not in keys]
        if missing or extra:
            _print("payload shape     the payload this command built does not match the inventory:")
            if missing:
                _print("                  declared but missing: %s" % ", ".join(missing))
            if extra:
                _print("                  built but not declared: %s" % ", ".join(extra))
            _print("                  PAYLOAD_KEYS in src/brian/cli.py is the declaration; both sides move "
                   "together")
            return False
    target = Path(path)
    try:
        if not str(path).strip():
            raise ValueError("the path is empty")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(str(target) + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        temp.replace(target)
    except (OSError, ValueError) as error:
        # The decisions are already recorded: the payload is the only thing missing, and saying so is the whole
        # difference between a half-done command and a lost one (boundary 337).
        _print("cannot write the payload to %s: %s" % (path, error))
        if recorded:
            _print("                what this command recorded is already in the store: only the payload is missing")
        else:
            # Round 162: a page records nothing, so round 141's sentence would claim a store change that never
            # happened. The failure is still non-zero (boundary 337), but it must not describe work as done.
            _print("                nothing was recorded: this command only reports, so only its export is missing")
        return False
    _print("wrote %s" % path)
    return True


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


def _print_after_preview(root: Path, pairs: List[str], *, kind: str, origin: str) -> None:
    """Round 132: the printer, over a function the campaign page can call too (boundary 308)."""
    for line in _after_preview_lines(root, pairs, kind=kind, origin=origin):
        _print(line)


def _after_preview_lines(root: Path, pairs: List[str], *, kind: str, origin: str) -> List[str]:
    """Round 131: the counts the pages print, computed from a copy of the store instead of from a write.

    A preview that stops at "these three pairs" makes the reader run the command to learn whether it was worth
    running. Both plans are pure functions of the settlements list, so the after-state is exact -- and the one number
    that is *not* a function of that list is named instead of guessed (boundary 307).
    """
    try:
        stored = load_settlements(root)
        rounds_all = history(root, include_retired=True)
        wanted = [pair.split("=", 1) for pair in pairs if "=" in pair]
        if kind == "reanchor":
            # Round 139: a re-anchor changes what is *payable* without paying anything, so the preview has to run the
            # same copy the command would produce and count there (boundary 308).
            after = preview_reanchor_change(stored, rounds_all, [str(name) for name in pairs])
        elif kind == "drop":
            after = preview_withdrawal_change(stored, wanted, why="(preview)", by="")
        else:
            after = preview_settlement_change(stored, wanted, kind=kind, why="(preview)", by="")
        if kind == "drop":
            before_rows = reaffirm_plan(rounds_all, stored)
            after_rows = reaffirm_plan(rounds_all, after)
            key = "unpayable"
            before_queue = sum(int(row.get("waiting") or row.get("decisions") or 0)
                               for row in withdrawal_plan(rounds_all, stored))
            after_queue = sum(int(row.get("waiting") or row.get("decisions") or 0)
                              for row in withdrawal_plan(rounds_all, after))
            # Round 137: a dropped pair lands in "nobody has taken it up again", and if its record names another
            # successor it lands in the half of that queue no restore can pay -- so the preview has to say both.
            after_stuck = sum(int(row.get("unpayable") or 0) for row in withdrawal_plan(rounds_all, after))
        elif kind == "restore":
            before_rows = withdrawal_plan(rounds_all, stored)
            after_rows = withdrawal_plan(rounds_all, after)
            key = "decisions"
        elif kind == "reanchor":
            before_rows = reaffirm_plan(rounds_all, stored)
            after_rows = reaffirm_plan(rounds_all, after)
            key = "unpayable"
        else:
            runs = queue_runs(root)["runs"]
            before_rows = reaffirm_plan(rounds_all, stored, runs)
            after_rows = reaffirm_plan(rounds_all, after, runs)
            key = "stale"
    except (OSError, ValueError) as error:
        return ["after this run  the books could not be previewed: %s" % error]

    def total(rows: List[Mapping[str, Any]]) -> int:
        return sum(int(row.get(key) or 0) for row in rows)

    def here(rows: List[Mapping[str, Any]]) -> int:
        return sum(int(row.get(key) or 0) for row in rows
                   if same_stream_path(str(row.get("origin") or ""), origin))

    noun = {"restore": "unanswered withdrawal(s)", "drop": "decision(s) no re-take can pay",
            "reanchor": "decision(s) no re-take can pay"}.get(kind, "stale decision(s)")
    if kind == "reanchor":
        # Round 139: the two numbers a re-anchor moves are "no re-take can pay" and "still names a successor the
        # record has left". Paying one is not paying the other, and the page must not let the reader conflate them
        # (boundary 330).
        still = sum(len(row.get("pairs") or []) for row in reanchor_plan(rounds_all, after)
                    if same_stream_path(str(row.get("origin") or ""), origin))
        return ["after this run  %s would owe %d %s, and %d decision(s) would still name a successor their record "
                "has left (the ledger goes from %d to %d)"
                % (origin, here(after_rows), noun, still, total(before_rows), total(after_rows)),
                "                a re-anchored decision was not taken on its new successor: the page says so, and "
                "only a check measures it there (boundary 203)"]
    if kind == "drop":
        return ["after this run  %s would owe %d %s, and %d more withdrawal(s) would wait in the withdrawal queue "
                "(the queue goes from %d to %d%s)"
                % (origin, here(after_rows), noun, after_queue - before_queue, before_queue, after_queue,
                   ", and %d of them cannot be taken up again" % after_stuck if after_stuck else ""),
                "                dropping a decision does not settle anything: the pair becomes an unanswered "
                "withdrawal, which has its own queue, age and file grouping (boundary 318)"]
    return ["after this run  %s would owe %d %s, and the ledger would go from %d to %d"
            % (origin, here(after_rows), noun, total(before_rows), total(after_rows)),
            "                the repair bill is not previewed: it reads the queue's stored refusals, which this "
            "command does not touch -- run `--campaign` afterwards to see it (boundary 307)"]


PLAN_STEPS = ("restore", "drop", "retake")

# Round 148: the batch commands `--dry-run` can preview, in one table (boundary 356). Rounds 135 and 147 each added a
# batch command and updated one of the two lists -- the guard's condition, or the sentence it printed -- so the guard
# refused the very preview the change had just made possible, and the help text named two of five. Both are generated
# from this table now: a flag that is in it is accepted *and* printed by construction, which is the only version of
# "remember to update both places" that a person cannot forget.
BATCH_PREVIEWS: Tuple[Tuple[str, str], ...] = (
    ("--settle-reaffirm-file", "settle_reaffirm_file"),
    ("--settle-restore-file", "settle_restore_file"),
    ("--settle-drop-file", "settle_drop_file"),
    ("--settle-reanchor-file", "settle_reanchor_file"),
    ("--claim-round-file", "claim_round_file"),
    ("--backfill-claims", "backfill_claims"),
    ("--backfill-walks", "backfill_walks"),
)


# Round 164 (item 150): round 163 pinned the number of payload writers in a test, which keeps the rule honest for
# whoever runs the suite. This table is the same inventory as a page: which command writes what, and whether the
# shared writer's failure sentence may say "the record is already in the store" (boundaries 392/395).
PAYLOAD_WRITERS: Tuple[Tuple[str, str, str], ...] = (
    ("--set-domain-limit / --set-domain-drain", '{"recorded", "cleared", "in_force", "files"}', "records"),
    ("--history",
     '{"rounds", "links", "in_force_clearings", "stream_path_moves", "ceilings", "claim_counts", "gate"}',
     "reports"),
    ("--claim-round-file", '{"claimed", "claim_refused"}', "records, unless --dry-run"),
    ("--backfill-claims", '{"filled", "dry_run"}', "records, unless --dry-run"),
    ("--backfill-walks", '{"backfilled", "would_backfill", "dry_run"}', "records"),
    ("--backfill-walks (a dry run)", '{"backfilled": [], "would_backfill", "dry_run"}', "reports"),
    ("--unrecorded", '{"rounds", "unattributed", "matters", "payable", "out_of_reach", "ceilings", "why_empty" (no rounds|all placed|none deciding), "gate"}',
     "reports"),
    ("--campaign", "the campaign report (drift, debt, queue, claims, offer)", "reports"),
    ("--sensitivity", '{"bar", "bars", "rounds", "refused", "recorded", "unreadable", "rows", "gate"}',
     "reports"),
    ("--withheld", '{"bar", "withheld", "gate"}', "reports"),
    ("--audit", '{"limits", "ceiling_drift", "rules_hash", "moved", "entries", ...}', "reports"),
    ("--set-stream-path", '{"stream_path"}', "records"),
    ("--settle-link", '{"settled"}', "records"),
    ("--settle-note", '{"noted"}', "records"),
    ("--settle-reanchor", '{"reanchored", "reanchor_refused"}', "records"),
    ("--settle-drop", '{"withdrawn", "drop_refused", "drop_skipped"}', "records"),
    ("--settle-restore", '{"restored", "restore_refused"}', "records"),
    ("--settle-reaffirm", '{"reaffirmed", "reaffirm_refused", "reaffirm_skipped"}', "records"),
    ("--repair-links --apply", '{"repairs"}', "records"),
    ("--from-queue (a walk that measured nothing)", '{"store", "walks"}', "records"),
    ("--from-queue (a walk)", '{"store", "walks"}', "records"),
    ("run", '{"summary", "windows_judged", "rca_report_ids"}', "records"),
    ("evaluate", "the evaluation report plus its recommendation", "records"),
    ("dataset --summary-out", "the dataset summary", "reports"),
)

# A file-level command hands its records to the single form, which writes the one payload (round 141's rule): they
# belong to that row, not to an extra writer -- and the page must say so, or the inventory looks incomplete.
PAYLOAD_BATCH_FORMS: Tuple[str, ...] = (
    "--settle-reaffirm-file", "--settle-restore-file", "--settle-drop-file", "--settle-reanchor-file",
)

# Round 164 found, by writing the table, that `--backfill-walks` had no `--json-out` at all while its sibling
# `--backfill-claims` did. Round 165 closed the gap and this tuple is now empty -- it stays as the place a gap is
# named, so the next one is printed rather than left out (an empty tuple is a claim the page can make out loud).
PAYLOAD_NONE: Tuple[str, ...] = ()

# Round 174 (item 160): the keys each payload carries, declared once. `_write_payload` refuses a payload that
# disagrees with its declaration, so the column and the command cannot drift in either direction -- a row
# naming a key nothing writes, or a command writing a key no row names (boundary 416).
# Rows not listed here still describe their payload in prose; `format_payload_writers` says so on the page.
PAYLOAD_KEYS: Dict[str, Tuple[str, ...]] = {
    "--audit": ("ceiling_drift", "entries", "gate", "inconsistent", "limits", "limits_text", "moved",
                "relabelled", "retired", "rounds", "rules_hash", "settlements", "trials",
                "unreconstructable"),
    "--history": ("ceilings", "claim_counts", "gate", "in_force_clearings", "links", "rounds",
                  "stream_path_moves"),
    "--unrecorded": ("ceilings", "gate", "matters", "out_of_reach", "payable", "rounds", "unattributed",
                     "why_empty"),
    # Round 175 (item 161): derived from the two places that build this payload -- the 59 keys of
    # `Campaign.to_dict()` in src/brian/campaign.py and the 9 keys the campaign branch assigns beside the
    # write -- not copied from a run's output, because a run only shows the branches it took (boundary 419).
    "--claim-round-file": ("claim_refused", "claimed"),
    "--backfill-claims": ("dry_run", "filled"),
    "--backfill-walks": ("backfilled", "dry_run", "would_backfill"),
    "--backfill-walks (a dry run)": ("backfilled", "dry_run", "would_backfill"),
    "--set-stream-path": ("stream_path",),
    "--settle-link": ("settled",),
    "--settle-note": ("noted",),
    "--settle-reanchor": ("reanchor_refused", "reanchored"),
    "--settle-drop": ("drop_refused", "drop_skipped", "withdrawn"),
    "--settle-restore": ("restore_refused", "restored"),
    "--settle-reaffirm": ("reaffirm_refused", "reaffirm_skipped", "reaffirmed"),
    "--repair-links --apply": ("repairs",),
    "--from-queue (a walk)": ("store", "walks"),
    "--from-queue (a walk that measured nothing)": ("store", "walks"),
    "run": ("rca_report_ids", "summary", "windows_judged"),
    "--campaign": ("accepted", "backfilled_links", "blocked_dossier", "budget", "ceiling_drift", "ceilings_differ",
                "ceilings_unrecorded", "claim_counts", "claims", "comparisons_inconsistent",
                "comparisons_unreconstructable", "debt_plan", "domains", "domains_pooling_files",
                "frequent_retakes", "gate", "in_force_cleared", "in_force_cleared_history",
                "in_force_disputed", "in_force_stale", "in_force_untested", "limits",
                "measurements_not_repeated", "origin_queue", "queue_history", "queue_paid_by_domain",
                "queue_paid_families", "queue_paid_unstated", "queue_run", "reaffirm_plan",
                "reanchorable", "refusal_history", "refusals_carried", "refusals_from_retired",
                "refusals_withheld", "rejected", "repair_history", "repair_plan", "rerun_offer",
                "reruns", "retake_timeline", "rounds", "rounds_applied", "rounds_retired",
                "rounds_without_file_legacy", "rounds_without_file_this_build",
                "rounds_without_recorded_file", "rounds_without_stream", "settlement_summary",
                "settlements", "settlements_moved", "settlements_restored", "settlements_unanswered",
                "settlements_withdrawn", "stale_refusals", "trials", "verdicts_moved",
                "verdicts_relabelled", "versions_holding", "versions_never_checked",
                "versions_not_comparable", "versions_overdue", "versions_reverted", "walks",
                "withdrawal_plan", "withheld_due_for_check", "withheld_no_longer_holding",
                "withheld_oldest_evidence_days"),
}

# Round 173 (item 159): the three nothings `--unrecorded` can find, and what each one means. The page names them
# because `why_empty` is a value a script branches on, and a value nobody documents is a value somebody guesses
# at; `payload_writer_problems` checks that every one of them is assigned in exactly one place in the source.
WHY_EMPTY_REASONS: Tuple[Tuple[str, str], ...] = (
    ("no rounds", "the ledger holds no round yet, so there is no file to place and nothing to re-run"),
    ("all placed", "every round that still decides something says which file it measured"),
    ("none deciding", "rounds name no file, but none of them still decides anything"),
)


def _wrap_items(items: Sequence[str], width: int = 96) -> List[str]:
    """Round 176 (item 162): a list of names as lines that fit a terminal."""
    chunks: List[str] = []
    current = ""
    for item in items:
        piece = ("" if not current else ", ") + item
        if current and len(current) + len(piece) > width:
            chunks.append(current)
            current = item
        else:
            current = (current + piece).strip()
    if current:
        chunks.append(current)
    return chunks


def _key_chunks(keys: Tuple[str, ...], width: int = 96) -> List[str]:
    """Round 175 (item 161): the key list as lines that fit a terminal, one key never split."""
    chunks: List[str] = []
    current = ""
    for key in keys:
        piece = ("" if not current else " ") + '"%s",' % key
        if current and len(current) + len(piece) > width:
            chunks.append(current)
            current = piece.strip()
        else:
            current = (current + " " + piece.strip()).strip()
    if current:
        chunks.append(current)
    return chunks


def format_payload_writers() -> str:
    """The inventory as a page: what each command exports, and what the one writer says when it cannot write."""
    lines = ["brian payload writers: %d command(s), one writer" % len(PAYLOAD_WRITERS)]
    lines.append("")
    for command, payload, kind in PAYLOAD_WRITERS:
        # Round 174 (item 160): a declared row is rendered from the declaration, so the page cannot describe a
        # payload the writer would refuse -- prose rows keep their prose and are named as such below.
        # Round 175 (item 161): `--campaign` declares 68 keys, so a declared row wraps instead of running off
        # the terminal; the keys are still the declaration, in its order, one key per token.
        if command in PAYLOAD_KEYS:
            # Round 176 (item 162): an empty declaration is a claim too ("this payload has no keys"), and a
            # page that crashes on it turns a check into a traceback (boundary 422).
            chunks = _key_chunks(PAYLOAD_KEYS[command]) or ["{} (no keys declared)"]
            lines.append("  %-42s %-23s %s" % (command, kind, chunks[0]))
            for chunk in chunks[1:]:
                lines.append(" " * (2 + 42 + 23) + chunk)
        else:
            lines.append("  %-42s %-23s %s" % (command, kind, payload))
    lines.append("")
    lines.append("the file-level forms %s write" % ", ".join(PAYLOAD_BATCH_FORMS))
    lines.append("the record list of their single form: one writer, because the single form is the one that writes it.")
    lines.append("")
    if PAYLOAD_NONE:
        lines.append("%s takes --dry-run and exports nothing: it writes no payload at all, so a caller cannot"
                     % " ".join(PAYLOAD_NONE))
        lines.append("script it the way it can script its siblings (round 164 found the first one this way).")
    else:
        lines.append("every batch command that takes --dry-run exports a payload: round 164 found that "
                     "`--backfill-walks` did not,")
        lines.append("round 165 gave it one -- `{\"backfilled\": [...], \"would_backfill\": [], \"dry_run\": false}`.")
    lines.append("")
    lines.append("`--unrecorded` has three empty pages, and `why_empty` says which one the caller got; `null` means")
    lines.append("the page found work. A script branches on this value, so the page says what each one means:")
    for reason, meaning in WHY_EMPTY_REASONS:
        lines.append("  %-14s %s" % (reason, meaning))
    lines.append("")
    lines.append("Every one of them goes through _write_payload: the parent directory is created, the file is renamed")
    lines.append("into place, and the page says `wrote <path>` (boundary 336). A write that fails exits 2 and says which")
    lines.append("kind of failure it was (boundaries 390/394):")
    lines.append("  changed a book  what this command recorded is already in the store: only the payload is missing")
    lines.append("  only reported   nothing was recorded: this command only reports, so only its export is missing")
    lines.append("                  -- also what a `records, unless --dry-run` row says while it previews (boundary 395)")
    lines.append("")
    # Round 176 (item 162): nineteen declared rows made this sentence 438 characters, so the names wrap
    # like a key list does -- the page is the deliverable (boundary 420).
    lines.append("A payload's keys are declared for %d row(s), and each is checked where the payload is built:"
                 % len(PAYLOAD_KEYS))
    for chunk in _wrap_items(sorted(PAYLOAD_KEYS)):
        lines.append("  " + chunk)
    lines.append("_write_payload refuses a payload whose keys disagree with those declarations (boundary 416).")
    undeclared = len(PAYLOAD_WRITERS) - len(PAYLOAD_KEYS)
    lines.append("_write_payload refuses a payload whose keys disagree with the declaration (boundary 416);")
    lines.append("the %d row(s) not declared here describe their payload in prose, so a key can be added"
                 % undeclared)
    lines.append("there and no check notices (boundary 417).")
    lines.append("")
    lines.append("A command that starts or stops writing a payload fails tests/test_payload_inventory.py until this")
    lines.append("table and its count move together (boundary 392).")
    return "\n".join(lines)


def payload_writer_calls(text: str) -> List[str]:
    """Round 172 (item 158): the lines that hand a payload to the one writer, counted by one rule.

    `payload_writer_problems` and the tests both need this number, and two copies of a predicate is how a count
    drifts (boundary 412). The quoted form is excluded because this module's own checker contains the literal it
    searches for: a counter that counts its own text prints one too many, and "24 writers" would mean 23 calls.
    """
    return [line for line in text.splitlines()
            if "_write_payload(" in line and "def _write_payload" not in line
            and '"_write_payload("' not in line]


def _write_unrecorded_payload(args: argparse.Namespace, payload: Dict[str, Any]) -> bool:
    """Round 172 (item 158): `--unrecorded` writes one payload shape from two branches.

    The empty branch returns before the table, so it needs the write too; two calls of the low-level writer
    would inflate the inventory (which counts calls against rows), and one page needing two rows would be a
    worse answer than one writer that both branches use (boundary 413).
    """
    if not getattr(args, "json_out", None):
        return True
    return _write_payload(args.json_out, payload, recorded=False,
                          keys=PAYLOAD_KEYS["--unrecorded"])


def payload_writer_problems(source: Optional[str] = None) -> List[str]:
    """Round 165: the table says "N command(s), one writer" -- this evaluates that claim from the source.

    `tests/test_payload_inventory.py` compares the table with the calls, but a test only speaks to whoever runs the
    suite. A command that exits 2 says the same thing to CI, to a release script, and to a reader who wants to know
    before trusting the page (boundaries 392/396).
    """
    problems: List[str] = []
    text = source
    if text is None:
        try:
            text = Path(__file__).read_bytes().decode("utf-8")
        except OSError as error:  # pragma: no cover - the file is this module
            return ["cannot read my own source to check the inventory: %s" % error]
    calls = payload_writer_calls(text)
    if len(calls) != len(PAYLOAD_WRITERS):
        problems.append("the page lists %d payload writer(s) and this source has %d: the table and the calls have to "
                        "move together" % (len(PAYLOAD_WRITERS), len(calls)))
    named = " ".join(command for command, _payload, _kind in PAYLOAD_WRITERS)
    named += " " + " ".join(PAYLOAD_BATCH_FORMS) + " " + " ".join(PAYLOAD_NONE)
    missing = [flag for flag, _attribute in BATCH_PREVIEWS if flag not in named]
    if missing:
        problems.append("the page does not name %s, which --dry-run accepts" % ", ".join(missing))
    # Round 173 (item 159): a value the page names and the source never assigns is a promise nothing keeps; a
    # value assigned twice is two statements about the same nothing (boundary 414). The reasons live on the
    # page rather than in the table's command column, so this reads the page the caller sees.
    # Round 174 (item 160): a declared shape must be a row, must name its keys on the page, and must name keys
    # the source actually mentions -- a declaration nobody can find in the code is a plan, not an inventory.
    rendered = {row[0]: row[1] for row in PAYLOAD_WRITERS}
    for command, declared_keys in sorted(PAYLOAD_KEYS.items()):
        if command not in rendered:
            problems.append("the shape of %s is declared and the table has no row for it" % command)
            continue
        # The row is *rendered* from this declaration (see format_payload_writers), so re-reading the row back
        # would test the formatter against itself; what is checked here is that the source names each key at all
        # (boundary 418: a check that reads its own output back tests nothing, and looks diligent).
        for key in declared_keys:
            if '"%s"' % key not in text:
                problems.append("the key %r of %s appears nowhere in this source: it was declared from memory"
                                % (key, command))
    page = format_payload_writers()
    documented = {reason: meaning for reason, meaning in WHY_EMPTY_REASONS}
    # Only a line that *starts* with the assignment is one; the lines of this scanner quote the same token and
    # would otherwise be read as two more reasons (boundary 412, third time in this file).
    assigned = [line.strip().split('why = "', 1)[1].split('"', 1)[0]
                for line in text.splitlines() if line.strip().startswith('why = "')]
    counts: Dict[str, int] = {}
    for reason in assigned:
        counts[reason] = counts.get(reason, 0) + 1
    for reason, count in sorted(counts.items()):
        if count != 1:
            problems.append("the source assigns why_empty = %r in %d place(s); the page describes one meaning, so there has to be exactly one assignment" % (reason, count))
        meaning = documented.get(reason)
        if meaning is None:
            problems.append("the source can report why_empty = %r and the page does not say what it means"
                            % reason)
        elif meaning not in page:
            problems.append("the page names %r without saying what it means, so a caller still guesses" % reason)
    for reason in sorted(documented):
        if reason not in assigned:
            problems.append("the page documents the why_empty value %r and no branch of this source assigns it"
                            % reason)
    return problems


def previewing_flags(args: argparse.Namespace) -> List[str]:
    """Which batch commands this invocation asked for, in the table's own order (round 148)."""
    return [flag for flag, attribute in BATCH_PREVIEWS if getattr(args, attribute, None)]


def previews_sentence() -> str:
    """The family as one phrase -- "a, b, c or d" -- so the guard, the help and any page say the same thing."""
    flags = [flag for flag, _attribute in BATCH_PREVIEWS]
    if not flags:
        return "no command"
    if len(flags) == 1:
        return flags[0]
    return ", ".join(flags[:-1]) + " or " + flags[-1]


def _plan_counts(rounds_all: List[Mapping[str, Any]], stored: List[Mapping[str, Any]],
                 runs: Sequence[Mapping[str, Any]], target: str) -> Tuple[int, int, int]:
    """(unanswered withdrawal(s), payable stale decision(s), decision(s) no re-take can pay) on one file.

    Round 136: the three numbers the what-if speaks in, computed from one store. Every step of a plan is "run one
    of the batch commands on a copy", so a step's effect is exactly this function called twice (boundary 308).
    """
    withdrawal_rows = [row for row in withdrawal_plan(rounds_all, stored)
                       if same_stream_path(str(row.get("origin") or ""), target)]
    reaffirm_rows = [row for row in reaffirm_plan(rounds_all, stored, runs)
                     if same_stream_path(str(row.get("origin") or ""), target)]
    return (sum(int(row.get("waiting") or row.get("decisions") or 0) for row in withdrawal_rows),
            sum(int(row.get("stale") or 0) for row in reaffirm_rows),
            sum(int(row.get("unpayable") or 0) for row in reaffirm_rows))


def _step_pairs(rounds_all: List[Mapping[str, Any]], stored: List[Mapping[str, Any]],
                runs: Sequence[Mapping[str, Any]], target: str, step: str) -> List[str]:
    """The pairs one batch command would hand over, read from the plan that command would read."""
    if step == "restore":
        rows = [row for row in withdrawal_plan(rounds_all, stored)
                if same_stream_path(str(row.get("origin") or ""), target)]
        entries = rows[0].get("records") if rows else []
    else:
        rows = [row for row in reaffirm_plan(rounds_all, stored, runs)
                if same_stream_path(str(row.get("origin") or ""), target)]
        key = "pairs" if step == "retake" else "unpayable_pairs"
        entries = rows[0].get(key) if rows else []
    return ["%s=%s" % (str(item.get("record") or ""), str(item.get("successor") or "")) for item in entries or []]


def _run_steps(rounds_all: List[Mapping[str, Any]], stored: List[Mapping[str, Any]],
               runs: Sequence[Mapping[str, Any]], target: str,
               steps: Sequence[str]) -> Tuple[List[Mapping[str, Any]], List[Tuple[str, int, Tuple[int, int, int]]]]:
    """Apply each step to a copy of the store, and record what it handed over and what was left behind."""
    trace: List[Tuple[str, int, Tuple[int, int, int]]] = []
    current = list(stored)
    for step in steps:
        pairs = _step_pairs(rounds_all, current, runs, target, step)
        wanted = [pair.split("=", 1) for pair in pairs]
        if step == "drop":
            current = preview_withdrawal_change(current, wanted, why="(preview)", by="")
        else:
            current = preview_settlement_change(current, wanted,
                                                kind="restore" if step == "restore" else "reaffirm",
                                                why="(preview)", by="")
        trace.append((step, len(pairs), _plan_counts(rounds_all, current, runs, target)))
    return current, trace


def _print_what_if_plan(root: Path, target: str, steps: Sequence[str]) -> None:
    """Round 136: "what if I did these, in this order" -- the one question the three answers cannot answer apart.

    Dropping a decision turns a pair no re-take can pay into an unanswered withdrawal; restoring one turns an
    unanswered withdrawal back into a settled pair. So the same commands, reordered, end in different books, and a
    page that quotes each command's own after-state is answering three questions nobody can add up (boundary 321).
    """
    try:
        stored = load_settlements(root)
        rounds_all = history(root, include_retired=True)
        runs = queue_runs(root)["runs"]
    except (OSError, ValueError) as error:
        _print("what if          the plans could not be read: %s" % error)
        return
    start = _plan_counts(rounds_all, stored, runs, target)
    _print("what if          %s owes %d unanswered withdrawal(s), %d stale decision(s) and %d decision(s) no "
           "re-take can pay" % ((target,) + start))
    _print("                 in the order: %s" % ", ".join(steps))
    _finished, trace = _run_steps(rounds_all, stored, runs, target, steps)
    for step, count, counts in trace:
        _print("                   after %-8s %d pair(s) -> %d unanswered withdrawal(s), %d stale decision(s), "
               "%d cannot be re-taken" % (step, count, counts[0], counts[1], counts[2]))
    reversed_counts = _plan_counts(rounds_all, _run_steps(rounds_all, stored, runs, target,
                                                          list(reversed(steps)))[0], runs, target)
    if reversed_counts != start and reversed_counts != trace[-1][2]:
        _print("                 the order matters: done the other way round it would end at %d unanswered "
               "withdrawal(s), %d stale decision(s) and %d it cannot re-take (boundary 322)" % reversed_counts)
    _print("                 nothing here is written: every step runs on a copy of the store, so the order is the "
           "only thing that changed (boundary 321)")


def _print_what_if(root: Path, target: str, steps: Sequence[str] = ()) -> None:
    """Round 132: "what if I paid this file off", asked from the page instead of from the command that pays it.

    Both families are asked, because a file can owe both and paying one does not touch the other. The numbers come
    from `_after_preview_lines`, the same function the dry runs print, so the page and the command cannot drift
    (boundary 308) -- and the block says that it wrote nothing, because a recomputation that looks like a receipt is
    the one mistake this whole thread keeps finding.
    """
    if steps:
        _print_what_if_plan(root, target, steps)
        return
    try:
        stored = load_settlements(root)
        rounds_all = history(root, include_retired=True)
        runs = queue_runs(root)["runs"]
        withdrawal_rows = [row for row in withdrawal_plan(rounds_all, stored)
                           if same_stream_path(str(row.get("origin") or ""), target)]
        reaffirm_rows = [row for row in reaffirm_plan(rounds_all, stored, runs)
                         if same_stream_path(str(row.get("origin") or ""), target)]
        reanchor_rows = [row for row in reanchor_plan(rounds_all, stored)
                         if same_stream_path(str(row.get("origin") or ""), target)]
    except (OSError, ValueError) as error:
        _print("what if          the plans could not be read: %s" % error)
        return
    restore_pairs = ["%s=%s" % (str(entry.get("record") or ""), str(entry.get("successor") or ""))
                     for entry in (withdrawal_rows[0].get("records") if withdrawal_rows else [])]
    retake_pairs = ["%s=%s" % (str(item.get("record") or ""), str(item.get("successor") or ""))
                    for item in (reaffirm_rows[0].get("pairs") if reaffirm_rows else [])]
    drop_pairs = ["%s=%s" % (str(item.get("record") or ""), str(item.get("successor") or ""))
                  for item in (reaffirm_rows[0].get("unpayable_pairs") if reaffirm_rows else [])]
    # Round 139: re-anchoring is the fourth answer to "what can this file still owe", and it is neither paying a
    # withdrawal, nor paying a stale decision, nor dropping one -- it changes what is payable without paying anything
    # (boundary 331). Asking three of four families is how "nothing to pay" came out wrong twice already.
    reanchor_pairs = [str(item.get("record") or "")
                      for item in (reanchor_rows[0].get("pairs") if reanchor_rows else [])]
    _print("what if          %s owes %d unanswered withdrawal(s), %d stale decision(s) and %d decision(s) no "
           "re-take can pay"
           % (target, sum(int(row.get("waiting") or row.get("decisions") or 0) for row in withdrawal_rows),
              sum(int(row.get("stale") or 0) for row in reaffirm_rows),
              sum(int(row.get("unpayable") or 0) for row in reaffirm_rows)))
    if not restore_pairs and not retake_pairs and not drop_pairs and not reanchor_pairs:
        _print("                 nothing to pay: no plan lists that file, so no batch command would change it")
        return
    if restore_pairs:
        _print("                 paying the withdrawals (%d pair(s), `--settle-restore-file %s`):"
               % (len(restore_pairs), target))
        for line in _after_preview_lines(root, restore_pairs, kind="restore", origin=target):
            _print("                   " + line.strip())
    if withdrawal_rows and withdrawal_rows[0].get("unpayable"):
        # Round 137: `restore_settlement` refuses a withdrawal whose record names another successor, so the queue
        # has a half no restore can pay -- and this question owes the reader both halves (boundary 311 again).
        _print("                 NONE of that reaches %d withdrawal(s) here: the records name other successors, so "
               "no restore can take them up again -- `brian evolve --settle-drop-file %s --settle-why TEXT`"
               % (int(withdrawal_rows[0].get("unpayable") or 0), target))
    if drop_pairs:
        # Round 135: round 133 stopped counting unpairable pairs as `stale`, which made the page honest and this
        # question false -- "paying it all" silently meant "paying the payable part" (boundary 319).
        _print("                 paying the decisions no re-take can pay (%d pair(s), `--settle-drop-file %s`):"
               % (len(drop_pairs), target))
        for line in _after_preview_lines(root, drop_pairs, kind="drop", origin=target):
            _print("                   " + line.strip())
    if reanchor_pairs:
        _print("                 re-anchoring the decisions whose record moved (%d record(s), "
               "`--settle-reanchor-file %s`):" % (len(reanchor_pairs), target))
        for line in _after_preview_lines(root, reanchor_pairs, kind="reanchor", origin=target):
            _print("                   " + line.strip())
    if retake_pairs:
        _print("                 paying the re-takes (%d pair(s), `--settle-reaffirm-file %s`):"
               % (len(retake_pairs), target))
        for line in _after_preview_lines(root, retake_pairs, kind="reaffirm", origin=target):
            _print("                   " + line.strip())
    _print("                 nothing here is written: this is the same recomputation the dry runs print (boundary 308)")


def _print_repair_delta(entry: Mapping[str, Any]) -> None:
    """Say what one repair changed about the repair plan, in the two numbers the plan is ranked by (round 111)."""
    before = entry.get("before") or {}
    after = entry.get("after") or {}
    freed = [str(item) for item in entry.get("freed") or [] if str(item)]
    if freed:
        _print("repair plan      %d file(s) off the table: %s -- cost %d -> %d"
               % (len(freed), ", ".join(freed), int(before.get("cost") or 0), int(after.get("cost") or 0)))
    else:
        _print("repair plan      nothing left the table: cost %d -> %d, %d file(s) still owing"
               % (int(before.get("cost") or 0), int(after.get("cost") or 0), int(after.get("files") or 0)))


def _print_repair_plan(plan: List[Dict[str, Any]]) -> None:
    """Say which origin file to open first, and the two numbers that decided it (round 110)."""
    if not plan:
        return
    first = plan[0]
    _print("repair order     %s costs %d walk(s) of refusals and holds %d promise(s) behind it -- start there (%s)"
           % (str(first.get("origin") or "(unnamed)"), int(first.get("walks_blocked") or 0),
              int(first.get("held") or 0), str(first.get("remedy") or "")))
    for row in plan[1:]:
        _print("                 then %s (%d walk(s), %d promise(s) held)"
               % (str(row.get("origin") or "(unnamed)"), int(row.get("walks_blocked") or 0),
                  int(row.get("held") or 0)))


def _refusal_phrase(history: Mapping[Any, Mapping[str, Any]], version: int,
                    blocked: Mapping[Any, Mapping[str, Any]]) -> str:
    """Why and when this promise was refused, as one parenthesis (round 109).

    The notice under the queue used to print only the stamp of the last refusal, which answers "when" and not "how
    often, and in which walks" -- the two facts that decide whether repairing the books is worth a round.
    """
    seen = history.get(version) or {}
    walked = walks_phrase([int(item) for item in seen.get("refused_walks") or []])
    at = str((blocked.get(version) or {}).get("at") or "unknown")
    if not walked:
        return "last refused at %s" % at
    return "refused in %s, %d time(s), last at %s" % (walked, int(seen.get("count") or 0), at)


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
                 page_problems: int = 0,
                 broken_links: int = 0, in_force: int = 0,
                 in_force_stale: int = 0, in_force_untested: int = 0,
                 unexplained_stale: int = 0,
                 unexplained_days: Optional[float] = None,
                 restaled: int = 0, max_restaled: Optional[int] = None,
                 per_file_restaled: int = 0, max_restaled_per_file: Optional[int] = None,
                 unanswered: int = 0, unanswered_days: Optional[float] = None,
                 per_file_unanswered: int = 0,
                 max_unanswered_per_file: Optional[int] = None,
                 unanswered_file: str = "",
                 into: Optional[Dict[str, Any]] = None) -> int:
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
    # Round 169 (item 155): the flag decides the return code, not whether the facts are read. `say` keeps a
    # report silent while the same table is computed, so a reader can ask what a check would find.
    say = _print if strict else (lambda *a, **k: None)
    charges: List[Dict[str, Any]] = []

    def _charge(rule: str, count: Any, code: int) -> None:
        charges.append({"rule": rule, "count": int(count or 0), "code": int(code)})

    def _done() -> int:
        # Round 168 (item 154): the exit code is --exactly-- the list of charges, so a script that reads the
        # list can compute the code the command will return (boundary 404).
        # Round 169 (item 155): a report keeps the same list under `would_charge` and charges nothing -- the
        # two fields must not be two readings of different flags (boundary 406).
        if into is not None:
            into["would_charge"] = list(charges)
            into["charges"] = [] if not strict else list(charges)
            # Round 171 (item 157): "the gate charged nothing" and "this page never ran the gate" are
            # two facts, and a payload with no gate key at all is a third. Every report that runs the
            # checks says so in its payload; the pages that only read the ledger say that instead.
            into["checked"] = True
        if not strict:
            # Round 170 (item 156): a reader at a terminal has no JSON to read. One line, from the same list
            # the payload carries, so the page and `would_charge` cannot disagree (boundaries 406/407).
            if charges:
                _print("")
                _print("next check        would charge %d item(s): %s"
                       % (len(charges), ", ".join(
                           "%s (%s)" % (item["rule"], item["count"]) if item["count"] > 1
                           else str(item["rule"]) for item in charges)))
                _print("                  run the same command with --strict to charge it")
            return 0
        if any(item["code"] == 2 for item in charges):
            return 2
        return 1 if charges else 0

    # Round 168 (item 154): the two counts that are already decided by the caller are recorded first, because a
    # build defect returns early and would otherwise list itself as the only charge -- a reader would read "one
    # defect" where the ledger also has a moved verdict (boundary 405).
    if moved:
        _charge("moved verdict", moved, 1)
    if due:
        _charge("refusal past its horizon", due, 1)
    if broken_links:
        # Round 81: the two ends of a replacement relation are written by two different commands, and
        # neither checks the other. A ledger whose books contradict each other cannot be repaired by
        # re-deciding under today's rules, so this is a build defect (exit 2), like `inconsistent`.
        say("")
        say("%d replacement link(s) disagree: the retired record and the successor name different "
               "rounds," % broken_links)
        say("or one end points at a round the ledger does not hold. The two ends cannot both be true, and")
        say("no re-deciding settles it, so --strict exits 2. `--audit` names each one.")
    if inconsistent:
        say("")
        say("%d comparison(s) cannot be reconstructed in records that carry the geometry and still lack "
               "the reference:" % inconsistent)
        say("that is a defect in this build, so --strict exits 2.")
        _charge("unreconstructable comparison", inconsistent, 2)
        return _done()
    if in_force:
        # Round 85: the same contradiction, one step further along -- a version running on top of it. The exit
        # code is the one `broken_links` already returns; a second `return 2` for the same finding would not
        # change the answer, it would only look like an independent check.
        say("")
        say("%d version(s) in force sit on a replacement link whose two ends contradict each other, so a"
               % in_force)
        say("deployed configuration inherits the contradiction. `--audit` names the version and the link.")
    if in_force_stale:
        # Round 86: a copied end is a debt with exactly one remedy -- measuring the deployed version again -- so
        # an aged one is charged, and the charge names that command. Before the grace period it stays a report:
        # the note is new, and a check that fails on the first sighting teaches nothing about what changed.
        say("")
        say("%d version(s) in force have sat on a copied record for longer than the %.0f-day grace period"
               % (in_force_stale, IN_FORCE_GRACE_DAYS))
        say("with no re-measurement that could test the claim, so --strict exits 1:")
        say("`brian evolve --verify --verify-version <n> --stream-file <the file that version measured> --accept-copied`")
        say("clears it, because the copy is about the record, not about the number.")
        if in_force_untested:
            # Round 89: the command above was already run on at least one of these, and it measured nothing
            # comparable. Naming it again without saying so is the charge that can never be cleared.
            say("%d of them were measured and the check could not test the claim: measuring again on the"
                   % in_force_untested)
            say("same stream will compare nothing again. Use the file that version recorded, or retire the")
            say("version whose claim cannot be checked (boundary 188).")
    if unexplained_stale:
        # Round 117: a decision with no reason and no note is answered by one command, and nothing else in the
        # loop will ever fill it in -- so an old one is debt, charged only when the caller sets the age cap
        # (a default would be red on every ledger whose settlements predate --settle-why).
        say("")
        say("%d decision(s) have had no explanation for longer than the %.0f day(s) asked for:"
               % (unexplained_stale, float(unexplained_days or 0.0)))
        say("  a decision with no reason and no note is debt nobody can review, and `--settle-note "
               "RECORD=SUCCESSOR`")
        say("  with `--settle-why TEXT --settle-by NAME` is the command that pays it: --strict exits 1 "
               "(boundary 266).")
    if restaled:
        # Round 118 reported "the books moved"; round 119 is the command that clears it, so what is left is
        # chargeable -- and only the clearable kind: a decision whose pair is gone has nothing to re-affirm
        # (boundaries 271/275).
        say("")
        say("%d decision(s) rest on books that have moved since they were taken, above the %d asked for:"
               % (restaled, int(max_restaled or 0)))
        say("  a claim arrived on the same successor after the decision, so it covers a pair the ledger no")
        say("  longer describes: `--settle-reaffirm RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME`")
        say("  re-takes it against today's books: --strict exits 1 (boundary 273).")
    if per_file_restaled:
        say("%d decision(s) on one stream file rest on books that have moved, above the %d asked for for a "
               "single file:" % (per_file_restaled, int(max_restaled_per_file or 0)))
        say("  the cap is per file because the work is per file: eleven stale decisions on eleven files is eleven")
        say("  afternoons, and eleven on one file is one. Re-take that file's decisions with")
        say("  `brian evolve --settle-reaffirm RECORD=SUCCESSOR --settle-why TEXT`, which `brian evolve")
        say("  --campaign` lists one command per pair (boundary 279).")
    if per_file_unanswered:
        say("%d withdrawal(s) sit on one stream file, above the %d asked for for a single file:"
               % (per_file_unanswered, max_unanswered_per_file if max_unanswered_per_file is not None else 0))
        say("  the file is %s, and each of those pairs is one nobody has ruled on since it was taken back."
               % (unanswered_file or "(unattributed)"))
        say("  A cap in decisions says the debt is large; a reader pays in afternoons, and eleven withdrawals on")
        say("  eleven files is eleven of them while eleven on one file is one (boundary 279, applied here).")
        say("  `--settle-restore RECORD=SUCCESSOR --settle-why TEXT` decides one again, and the withdrawal stays")
        say("  on the record: --strict exits 1 until this file's count is back inside the cap (boundary 299).")
    if unanswered:
        # Round 127: unlike the unexplained decisions, this one has a command that clears it, so it is charged
        # (boundary 266's condition) -- and the pair is a standing contradiction while it waits (boundary 294).
        say("%d decision(s) have been withdrawn for longer than the %d day(s) asked for:" % (unanswered,
                                                                                             int(unanswered_days or 0)))
        say("  each of those pairs is a contradiction nobody has ruled on: the withdrawal says the decision no")
        say("  longer stands, and nothing has taken its place, so `--audit` charges it and")
        say("  `--settle-restore RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME` is the command that pays it:")
        say("  the pair is decided again, and the withdrawal stays on the record (boundary 297).")
    if broken_links:
        # Round 168 (item 154): the early exit names its charge like every later one, so a report can say which
        # rule turned the code to 2 (boundary 404).
        _charge("replacement link", broken_links, 2)
        return _done()
    if unreconstructable:
        say("")
        say("%d comparison(s) predate the geometry field and cannot be replayed; re-running those rounds "
               "writes records that can be." % unreconstructable)
        say("They do not fail the check (their rules still move the exit code), but --strict has not "
               "verified them.")
    if due:
        say("")
        say("%d refusal(s) are older than the %.0f-day horizon and were not measured again%s:"
               % (due, horizon,
                  "" if oldest_days is None else " (oldest evidence %.0f day(s) old)" % oldest_days))
        say("a refusal nobody re-tests is an assertion the loop cannot lose, so --strict exits 1.")
        say("pass --verify-withheld to re-measure them.")
    if legacy_unrecorded:
        # Round 50, same seam as round 33: a round written before the field existed is history nobody can
        # back-fill (writing a path in would invent a measurement), so it is a warning even when a cap is
        # set -- otherwise the gate is red until 40 rounds have been re-run, which is how a gate stops being
        # read. One line, because this debt only falls as new rounds are written.
        say("")
        say("%d round(s) predate the stream-file field, so which sampling their totals belong to is not "
               "recorded." % legacy_unrecorded)
        say("Re-running a round writes a record that says; --unrecorded lists the ones that still decide "
               "something.")
    if unrecorded:
        # Round 50 learned this from a failing test: a round this build writes without a file is not
        # automatically a defect. `brian evolve` with no --stream-file measures the generated demo stream,
        # which has no file to name -- so charging this would leave the demo domain permanently red, the
        # failure round 33 fixed. Named as a warning; the share cap is what a deployment can ask for.
        say("")
        say("%d round(s) were written by this build and say no stream file: a round measured on the "
               "generated stream has none to name." % unrecorded)
        say("Pass --stream-file to make a round's totals attributable to a sampling.")
    if max_unrecorded is not None and unrecorded_share > max_unrecorded:
        say("")
        say("%.0f%% of the recorded rounds cannot be placed on a sampling, above the %.0f%% asked for:"
               % (100.0 * unrecorded_share, 100.0 * max_unrecorded))
        say("re-run the rounds whose totals still decide something, on the file each one measured.")
    if trend_rise > trend_tolerance:
        # Round 56: a debt that grows while the loop is not paying it is the failure the whole queue exists
        # for, and until now the page only printed it. It is a warning rather than a failure by default
        # because this build writes nameless rounds on purpose when it measures the generated demo stream
        # (round 50), so a blanket "a rise fails" would keep the demo domain red forever -- the exit code a
        # check can never clear is the check nobody runs (round 33). The opt-in cap makes it chargeable.
        window = span_text(trend_days)
        rose = max_rise is not None and trend_rise > max_rise
        say("")
        say("%.1f point(s) of the unrecorded share came back in the last %s: %d round(s) were re-measured "
               "and %d record(s) arrived." % (trend_rise, window, trend_paid, trend_arrived))
        if rose:
            say("the share rose by more than the %.1f point(s) asked for, so --strict exits 1."
                   % max_rise)
            say("re-run the queue, or write the rounds with --stream-file so they name a sampling.")
        elif max_rise is not None:
            say("the share rose by less than the %.1f point(s) asked for, so --strict does not charge it."
                   % max_rise)
        else:
            say("a share that grows while nothing is paid is a debt nobody is working on; pass "
                   "--max-share-rise to make it fail the check.")
    # Round 58: the per-domain share is the number a reader of one row actually faces, and until now only the
    # ledger-wide share could be required. The paragraph prints the worst domain either way; the exit code
    # only moves when that domain's rounds can be re-run, so the gate cannot be red for a debt no command pays.
    debt_capped = False
    if max_debt is not None and worst is not None and float(worst.get("share") or 0.0) > max_debt:
        debt_capped = (payable is not None
                       and float(payable.get("share") or 0.0) > max_debt
                       and str(payable.get("domain") or "") == str(worst.get("domain") or ""))
        say("")
        say("%s has %.0f%% of its rounds unrecorded (%d of %d), above the %.0f%% asked for:"
               % (worst.get("domain"), 100.0 * float(worst.get("share") or 0.0),
                  int(worst.get("without") or 0), int(worst.get("rounds") or 0), 100.0 * max_debt))
        if debt_capped:
            say("a row whose rounds are mostly unattributable cannot be read as a measurement, so "
                   "--strict exits 1.")
            say("%d of those round(s) can be paid by re-running them on the file they measured."
                   % int(payable.get("ready") or 0))
        else:
            say("no recorded round on it can be re-run (its candidates or its file are not in the record), "
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
        say("")
        for domain, minutes, ceiling in over:
            say("the next queue run would spend about %.1f minute(s) on %s, above the %.1f asked for:"
                   % (minutes, domain, ceiling))
        say("lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.")
    elif (max_minutes is not None and budget_minutes is not None and budget_minutes > max_minutes):
        minutes_capped = True
        say("")
        say("the next queue run would spend about %.1f minute(s), above the %.1f asked for:"
               % (budget_minutes, max_minutes))
        say("lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.")
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
        say("")
        for domain, minutes, ceiling in drain_over:
            say("emptying the queue would spend about %.1f minute(s) on %s, above the %.1f asked for:"
                   % (minutes, domain, ceiling))
        say("raise the ceiling, or drain the queue over several runs with --rerun-limit.")
    elif max_drain is not None and drain_minutes is not None and drain_minutes > max_drain:
        minutes_capped = True
        say("")
        say("emptying the queue would spend about %.1f minute(s), above the %.1f asked for:"
               % (drain_minutes, max_drain))
        say("raise the ceiling, or drain the queue over several runs with --rerun-limit.")
    # A check must not fail for something it does not print: the rise is only chargeable when it is large
    # enough to be printed at all, or the reader would get exit 1 with no paragraph explaining it.
    rose = max_rise is not None and trend_rise > max_rise and trend_rise > trend_tolerance
    capped = max_unrecorded is not None and unrecorded_share > max_unrecorded
    # Round 68: a recorded round judged under other ceilings is not a wrong record -- the limits really were
    # those -- so this is chargeable only when asked for, the same way a debt that history created is
    # (boundary 123). The flag names a *count*, because the reader is deciding whether to re-decide rounds.
    drift_capped = (max_limit_drift is not None and limit_drift > float(max_limit_drift))
    if drift_capped:
        say("")
        say("%d recorded round(s) were judged under ceilings that differ from today's, above the %d "
               "asked for:" % (limit_drift, int(max_limit_drift)))
        say("their verdicts were reached under other limits, so --strict exits 1 until they are re-decided")
        say("with the ceilings in force now, or the cap is raised.")
    # Round 72: the ceiling debt may be capped, but only when the queue can pay it. A share that no command can
    # lower is the always-red gate (boundary 126), so the cap is asked for explicitly and the exit code moves
    # only when `ceiling_payable` covers every missing round -- the same shape as the per-domain debt and the
    # minutes cap. When it cannot be paid the paragraph still prints: silence would read as "nothing owed".
    ceiling_share = (float(ceiling_missing) / float(ceiling_rounds)) if ceiling_rounds else 0.0
    if max_ceiling_debt is not None and ceiling_missing and ceiling_share > float(max_ceiling_debt):
        say("")
        say("%d of %d round(s) do not record the ceilings they were judged under (%.0f%%), above the %.0f%% "
               "asked for:" % (ceiling_missing, ceiling_rounds, 100.0 * ceiling_share,
                               100.0 * float(max_ceiling_debt)))
        if ceiling_payable >= ceiling_missing:
            say("re-running the queue would leave none, so --strict exits 1 until it is paid.")
            say("run `brian evolve --rerun-unrecorded` to measure them again with their ceilings.")
        else:
            say("only %d of them can be re-run, so --strict does not charge it: a gate that can never be "
                   "cleared is a gate nobody runs." % max(0, int(ceiling_payable)))
            say("nothing was charged; the debt is on the page above.")
    # Round 166 (item 152): `--payload-writers --check` is a command somebody has to remember to run. A gate that
    # does not call it lets a table that no longer describes the source land on a fully green `--strict`. This is a
    # build defect, not a debt (round 33's rule): nothing in the ledger can pay it, so it is simply exit 2.
    page_capped = bool(page_problems)
    if strict and page_capped:
        say("payload page      %d problem(s): the --payload-writers table does not describe this source"
               % int(page_problems))
        say("                  run `brian evolve --payload-writers --check` for the list (boundary 397)")
    ceiling_capped = (max_ceiling_debt is not None and ceiling_missing
                      and ceiling_share > float(max_ceiling_debt) and ceiling_payable >= ceiling_missing)
    # Round 168 (item 154): every charge the gate reads, in the order the docstring ranks them. `inconsistent`
    # is charged above, on its own early exit; the builder defect and the debts share this table.
    for rule, count, code in (
            ("replacement link", broken_links, 2),
            ("version in force on a broken link", in_force, 2),
            ("payload page", page_problems, 2),
            ("unrecorded share", capped, 1),
            ("standing refusal", rose, 1),
            ("per-domain debt", debt_capped, 1),
            ("queue minutes", minutes_capped, 1),
            ("ceiling drift", drift_capped, 1),
            ("ceiling debt", ceiling_capped, 1),
            ("copied end in force", in_force_stale, 1),
            ("unexplained decision", unexplained_stale, 1),
            ("re-taken decision", restaled, 1),
            ("re-taken decision on one file", per_file_restaled, 1),
            ("unanswered withdrawal", unanswered, 1),
            ("unanswered withdrawal on one file", per_file_unanswered, 1)):
        if count:
            _charge(rule, count, code)
    return _done()

def report_footer(checked: bool, *, asked: bool = False,
                  command: str = "brian evolve --campaign --strict",
                  strict_command: str = "brian evolve --audit --strict") -> Dict[str, Any]:
    """Round 171 (item 157): the gate's line on a report page, and the payload entry that goes with it.

    `--audit`, `--campaign` and `--sensitivity` compute the charges (through `_strict_exit`, which marks the
    dict `checked`); a page that only reads the ledger cannot, and the caps it *does* know only mark rows
    (`--history` prints `[past the cap: --strict exits 1]`). Silence there would read as "everything is fine",
    so the page says what it did -- charges nothing -- and names the command that turns its marks into an
    exit. `asked` records that the reader passed `--strict`: a flag that lands on a page which never charges
    must be acknowledged, not ignored (boundary 411), and refusing it would break the marks this page exists
    to show.
    """
    if checked:
        return {"asked": bool(asked), "checked": True}
    _print("%-17s %s" % ("--strict" if asked else "gate",
                         "this page reads the ledger and charges nothing; `%s` turns the marks above into an "
                         "exit" % strict_command if asked
                         else "this page reads the ledger and charges nothing; `%s` is the page that does"
                              % command))
    return {"asked": bool(asked), "checked": False,
            "command": strict_command if asked else command}


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


def _paid_round_names(root: Path, walk: int) -> List[str]:
    """The rounds whose `queue` block names this invocation (round 144).

    Read back from the records rather than accumulated in the loop: the loop counts what it paid, and a round
    that recorded itself but failed to retire would be counted twice by a second accumulator and once here.
    """
    names = []
    for record in history(root, include_retired=True):
        entry = record.get("queue")
        if isinstance(entry, Mapping) and int(entry.get("walk") or 0) == int(walk or 0):
            names.append(str(record.get("round") or ""))
    return [name for name in names if name]


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


def _rerun_offer(root: Path) -> Dict[str, Any]:
    """Round 149: what the re-run queue could pay today, and the one command that pays it (boundary 360).

    Round 147 read the real ledger's retirement marks back into the store, and the page has printed `0 by the queue,
    17 typed by hand` ever since. That is a fact about the past -- how the last payments were made -- and a reader
    cannot do anything with it. So the page now also carries the count the queue could pay and the command that pays
    it; and when the store records hand payments but not one queued walk, it says that in the same breath, because
    "the queue has never run here" is the part an operator needs and it is invisible in a count of walks.
    """
    matters = _unrecorded_matters(root)
    # Round 150: "could pay today" is a claim about what a command can do, so it counts only the rounds that have
    # exactly one candidate file -- the same filter `--rerun-unrecorded` applies before it measures anything
    # (boundary 363). Round 149 counted every unrecorded round, which made the page promise work no command can
    # perform: a round with no file, or with two, is skipped by the very queue the sentence recommends.
    file_rounds = [item for item in matters if len(item[3]) == 1]
    unplaceable = len(matters) - len(file_rounds)
    ceiling_rounds = _ceiling_matters(root)
    total = len(file_rounds) + len(ceiling_rounds)
    try:
        walks = rerun_walks(root)
    except OSError:
        walks = {}
    queued = int(walks.get("queued") or 0)
    hand = int(walks.get("hand") or 0)
    if not total:
        if unplaceable:
            text = ("nothing for the queue to pay today: %d round(s) keep no unique file, so no command can pay them"
                    % unplaceable)
        else:
            text = ("nothing for the queue to pay today: every round that still decides something records the file "
                    "and the ceilings it was measured under")
        command = ""
    else:
        parts = []
        if file_rounds:
            parts.append("%d from the file queue" % len(file_rounds))
        if ceiling_rounds:
            parts.append("%d that named their file but never recorded their ceilings" % len(ceiling_rounds))
        command = _rerun_command(bool(ceiling_rounds))
        text = "%d round(s) the queue could pay today (%s): %s" % (total, ", ".join(parts), command)
        if unplaceable:
            text += ("; %d more keep no unique file, so no command can pay them" % unplaceable)
        if hand and not queued:
            text += ("; the queue has paid none of this store's %d payment(s): every one was typed by hand, so the "
                     "store says nothing about what the queue would do" % hand)
    return {"rounds": total, "file_rounds": len(file_rounds), "ceiling_rounds": len(ceiling_rounds),
            "unplaceable": unplaceable, "command": command, "text": text}


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


def _rerun_command(with_ceilings: bool) -> str:
    """The one command that pays the re-run queue, in one place (round 149).

    Two lines of the campaign page name this command -- the ceiling debt's remedy and the queue's own offer -- and
    the round's own lesson is that a hand-written second copy drifts. So there is one builder, and a test compares
    the two lines rather than trusting them to agree.
    """
    return "brian evolve --rerun-unrecorded %s--rerun-limit 0" % ("--with-ceilings " if with_ceilings else "")


def _ceiling_command(payable: int, named: int) -> Optional[str]:
    """The command that lowers the ceiling debt, or None when no command can (round 74).

    Two families, two commands: a round whose *file* is unknown is paid by the plain queue, a round that named
    its file needs `--with-ceilings`. The sentence on the page names one command, so it names the one that pays
    the family which actually owes something -- and names nothing when both counts are zero, because a command
    that would measure nothing is not a next step (round 59's lesson about true next steps).
    """
    if int(named) > 0:
        return _rerun_command(True)
    if int(payable) > 0:
        return _rerun_command(False)
    return None


def _queue_entry(kind: str, domain: str, name: str, largest_start: Optional[Mapping[str, Any]],
                 progress: Mapping[str, Any], shape: Mapping[str, Any], walk: int = 0) -> Dict[str, Any]:
    """What one invocation of the re-run queue records in the round it paid (round 58, extended in round 78).

    The record lives in the round because the terminal is gone by the time a reader asks why the ledger moved.
    Round 78 adds two facts a reader cannot recover otherwise: which family this entry paid (`file` or
    `ceiling`), and the shape of the invocation that paid it -- number of families and rounds per family, which
    is not the same as the number of rounds (boundary 163: "two families of one round" and "one family of two
    rounds" push different books).
    """
    entry: Dict[str, Any] = {"paid": domain, "paid_round": name, "family": kind, "shape": dict(shape)}
    # Round 144: which invocation of the re-run queue paid for this round. The family says who paid; the ordinal
    # says which walk, and `var/evolve/rerun_runs.json` holds the entry it names (boundary 345). 0 means the
    # record was written before this store existed.
    entry["walk"] = int(walk or 0)
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


def format_history_paths(items: List[Mapping[str, Any]]) -> List[str]:
    """The `stream paths` block of `--history` (round 94, extended in round 96).

    Extracted so the block can be tested without measuring anything: both facts it prints -- that a hand repair
    moved the measured file, and whether the file the promise was born with has come back -- are decided by the
    ledger alone. A page about records should not need Laya to be checked.
    """
    if not items:
        return []
    lines = ["stream paths     %d record(s) have had their measured file rewritten by hand:" % len(items)]
    for item in items:
        origin = str(item.get("first_from") or "")
        # Round 97: a returned record is one round trip, not two unrelated moves. The chain is printed whole --
        # origin -> substitute -> origin -- because a reader who sees only the last pair cannot tell a return from
        # a second repair (boundary 204's rule, applied to the direction of the move).
        chain = [origin or "(unrecorded)"] + [str(move.get("to") or "(unrecorded)")
                                              for move in item.get("moves") or []]
        if item.get("returned"):
            when = "round trip: %d move(s), returned %s" % (len(item.get("moves") or []),
                                                            str(item.get("returned_at") or "unknown"))
            back = ""
        else:
            when = "%d move(s), last %s" % (len(item.get("moves") or []), str(item.get("at") or "unknown"))
            back = "  (the original file is back: %s)" % origin if item.get("origin_exists") else ""
        lines.append("  v%03d  %s  (%s)%s%s"
                     % (int(item.get("version") or 0), " -> ".join(chain), when,
                        "  [in force]" if item.get("in_force") else "", back))
    return lines


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


def _round_names_walk(root: Path, name: str) -> Optional[int]:
    """Which walk a round record says paid it, `0` when it says nothing, or None when it is not readable (round 146).

    A hand retirement must not bill a second time for a payment the queue already recorded: the successor round
    carries `queue.walk` in that case, and that number is the one the store already holds.
    """
    path = root / "var" / "evolve" / name
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    block = record.get("queue")
    if not isinstance(block, dict):
        return 0
    return int(block.get("walk") or 0)


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
    # Round 117: one reading of the settlements, used by the history page and by the gate.
    try:
        unexplained_rows = unexplained_decisions(load_settlements(root))
    except OSError:
        unexplained_rows = []
    # Round 117: a decision nobody explained becomes debt only past the age the caller asked for, because a
    # ledger whose settlements predate --settle-why would otherwise be red on every run (boundary 154's rule).
    _unexplained_cap = getattr(args, "max_unexplained_days", None)
    # Round 119: round 118's "the books moved" is now clearable, so it is chargeable -- but only the clearable
    # kind. A decision whose pair was retargeted or deleted cannot be re-affirmed, so charging it would be a gate
    # nobody can clear (boundaries 154/171/266/271/275).
    try:
        _moved_rows = moved_clearable(moved_settlements(history(root, include_retired=True),
                                                        load_settlements(root)))
    except (OSError, ValueError):
        _moved_rows = []
    _moved_cap = getattr(args, "max_moved", None)
    _restaled = len(_moved_rows) if (_moved_cap is not None and len(_moved_rows) > int(_moved_cap)) else 0
    # Round 121: the same debt capped in the unit a reader pays in. Eleven stale decisions spread over eleven files
    # are eleven afternoons of work; eleven on one file are one -- a total cannot tell them apart (boundary 279).
    _per_file_cap = getattr(args, "max_moved_per_file", None)
    try:
        _plan_rows = reaffirm_plan(history(root, include_retired=True), load_settlements(root),
                                   queue_runs(root)["runs"])
    except (OSError, ValueError):
        _plan_rows = []
    _worst_file = max(_plan_rows, key=lambda row: (int(row.get("stale") or 0), str(row.get("origin") or "")),
                      default={})
    _worst_stale = int(_worst_file.get("stale") or 0)
    _per_file_restaled = _worst_stale if (_per_file_cap is not None and _worst_stale > int(_per_file_cap)) else 0
    unexplained_stale = ([row for row in unexplained_rows
                          if float(row.get("age_days") or 0.0) >= float(_unexplained_cap)]
                         if _unexplained_cap is not None else [])
    # Round 127: a pair withdrawn and never taken up again is a contradiction nobody has ruled on, and round 126
    # built the command that rules on it -- which is what makes it chargeable (boundaries 266/271).
    try:
        _withdrawn_rows = unanswered_withdrawals(load_settlements(root))
    except OSError:
        _withdrawn_rows = []
    _withdrawn_cap = getattr(args, "max_withdrawn_days", None)
    _unanswered = ([row for row in _withdrawn_rows
                    if float(row.get("age_days") or 0.0) >= float(_withdrawn_cap)]
                   if _withdrawn_cap is not None else [])
    # Round 128: the same debt in the unit a reader pays in -- the stream file (boundary 279, applied here).
    try:
        _withdrawn_plan = withdrawal_plan(history(root, include_retired=True), load_settlements(root))
    except (OSError, ValueError):
        _withdrawn_plan = []
    _withdrawn_per_file = getattr(args, "max_withdrawn_per_file", None)
    _worst_withdrawn = max(_withdrawn_plan,
                           key=lambda row: (int(row.get("decisions") or 0), str(row.get("origin") or "")),
                           default={})
    _per_file_unanswered = len(_worst_withdrawn.get("records") or [])
    _per_file_charged = (_per_file_unanswered if (_withdrawn_per_file is not None and
                                                 _per_file_unanswered > int(_withdrawn_per_file)) else 0)
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
    # Round 91: which cited files are gone is a fact about this checkout, not about the write, so it is
    # computed once here and carried into both pages that print it.
    in_force_evidence = clearing_evidence(in_force_history_rows, root)
    in_force_record = dict(in_force_record, evidence_missing=in_force_evidence)
    # Round 93: which promises name a stream file this checkout cannot open. Computed here from the checkout,
    # never stored: the answer changes when a file is moved, not when a clearing is written.
    in_force_record = dict(in_force_record, dead_record_paths=dead_record_paths(root))
    # Round 101: the origin queue travels with the page and the JSON payload, so a script that wants to run it
    # reads the same list `--from-queue` walks instead of parsing the sentence above.
    try:
        origin_queue_state = never_tested_at_origin(repaired_paths(root))
    except OSError:
        origin_queue_state = {"records": 0, "by_file": {}, "versions": []}
    # Round 94: the promises whose measured file was rewritten by hand. Computed from the version records, so a
    # `--set-stream-path` shows up on the next page without anyone having to remember to write it down.
    in_force_record = dict(in_force_record, repaired_paths=repaired_paths(root))

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

    if getattr(args, "max_moved_per_file", None) is not None and not getattr(args, "strict", False):
        # Round 121: same rule as the other caps -- a flag the gate never reads looks armed.
        _print("--max-moved-per-file caps a --strict charge; without --strict nothing reads it")
        return 2
    if getattr(args, "max_moved", None) is not None and not getattr(args, "strict", False):
        # Round 119: same rule as --max-unexplained-days -- a cap nobody reads is a flag that looks armed.
        _print("--max-moved caps a --strict charge; without --strict nothing reads it")
        return 2
    if getattr(args, "max_unexplained_days", None) is not None and not getattr(args, "strict", False):
        # Round 117: same shape as --max-ceiling-debt. The page prints the ages either way; only the gate fails.
        _print("--max-unexplained-days caps a --strict charge; without --strict nothing reads it")
        return 2
    if getattr(args, "max_withdrawn_per_file", None) is not None and not getattr(args, "strict", False):
        # Round 128: the per-file cap is armed the same way the per-decision one is, or it looks armed and is not.
        _print("--max-withdrawn-per-file caps a --strict charge; without --strict nothing reads it")
        return 2
    if getattr(args, "max_withdrawn_days", None) is not None and not getattr(args, "strict", False):
        # Round 127: same rule again -- a cap the gate never reads is a flag that looks armed.
        _print("--max-withdrawn-days caps a --strict charge; without --strict nothing reads it")
        return 2

    _plan_steps = [str(step).strip() for step in str(getattr(args, "plan", "") or "").split(",")
                   if str(step).strip()]
    if _plan_steps and not getattr(args, "what_if_records", None):
        # Round 136: only the page's what-if steps through a plan. Anywhere else it would be ignored, and a flag
        # that looks like a question must not be answered with silence (boundary 305's rule, third appearance).
        _print("--plan steps --campaign --what-if-records through the batch commands; without that question nothing "
               "reads it")
        return 2
    unknown_steps = [step for step in _plan_steps if step not in PLAN_STEPS]
    if unknown_steps:
        _print("--plan takes some of %s in the order to run them; %s is not one of them"
               % (", ".join(PLAN_STEPS), ", ".join(unknown_steps)))
        return 2
    if getattr(args, "what_if_records", None) and not getattr(args, "campaign", False):
        # Round 132: only the campaign branch reads it. Anywhere else the question would be ignored, and a flag that
        # looks like a question must not be answered with silence (boundary 305 again).
        _print("--what-if-records asks the campaign page a question; without --campaign nothing prints it")
        return 2
    if getattr(args, "dry_run", False) and not previewing_flags(args):
        # Round 130: `--dry-run` exists for the batch commands. Anywhere else it would be silently ignored, and
        # a flag that looks like a safety belt must not be decorative (rounds 103/117/120/127/128, boundary 305).
        # Round 148: the condition and the sentence are one table's two readings (boundary 356) -- rounds 135 and
        # 147 both shipped a batch command the guard refused to preview, and the help text named two of five.
        _print("--dry-run previews %s; without one of those the command would go ahead and record nothing you "
               "could see first" % previews_sentence())
        return 2
    if getattr(args, "from_queue", None) is not None and not getattr(args, "verify", False):
        # Round 103: only the verify branch reads `--from-queue`. Without it the command measures a fresh round and
        # the flag is silently inert -- which is how a queue walk was mistaken for one (the run landed in a round
        # record instead). A flag one mode reads must refuse to be ignored by the others.
        _print("--from-queue walks the origin queue through --verify; without --verify it would be ignored and a "
               "round would be measured instead. Add --verify.")
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
    # Round 151: the third part of the debt -- rounds no command can lower, because they name no single file or
    # no longer decide anything. It is computed here, with the other two, so the campaign sentence, the
    # --unrecorded page and its payload cannot disagree about how the debt splits (boundary 366).
    ceiling_unpayable = max(0, ceiling_missing - ceiling_file_payable - ceiling_named)
    # Round 166: the page's own claim, read by the gate rather than only by whoever remembers --check.
    writer_problems = payload_writer_problems()
    page_problems = len(writer_problems)
    # Round 167: `asked` is not the verdict -- it says whether a verdict was asked for at all, because a
    # report command runs the same code without one (boundary 401).
    gate_report = {"asked": bool(getattr(args, "strict", False)),
                   "page_problems": [str(item) for item in writer_problems]}

    # Round 153: the repairs round 152 made are visible in each record's stream_path_history, and a claim the
    # geometry never confirmed is a fact a reader of the page needs -- the row rests on a person's assertion
    # (boundary 371). Computed with the other pre-branch counts for the same reason.
    claim_rows = claimed_rounds(live_records)
    # Round 161 (item 147): one computation, three readers -- the page prints it, the payload carries it,
    # and the test that compares both pages' sentences reads it (boundary 385). The population is named
    # here because this page counts live rounds, while `--history` counts every row it lists (boundary 386).
    campaign_counts = claim_counts(live_records, population="live round(s)")

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
            # Round 163 (item 149): the payload writers were counted, and eight of them still wrote the file
            # themselves -- the round-141 rule had only reached the settlement commands (boundary 389).
            if not _write_payload(args.json_out, payload):
                return 2
        return 0

    if getattr(args, "check", False) and not getattr(args, "payload_writers", False):
        _print("--check reads the --payload-writers page; without it there is nothing to check")
        return 2

    if getattr(args, "payload_writers", False):
        # Round 164 (item 150): the inventory round 163 pinned in a test, on a page -- so the rule lives where a
        # reader looks for it, not only where the suite runs (boundary 392). Round 165 makes that page checkable by
        # the command itself, so CI can read the claim without running the suite (boundary 396).
        if getattr(args, "check", False):
            problems = payload_writer_problems()
            if problems:
                for problem in problems:
                    _print("inconsistent      %s" % problem)
                _print("checked           %d problem(s): the page does not describe this source (boundary 392)"
                       % len(problems))
                return 2
            _print("checked           the page and the source agree: %d writer(s), %d file-level form(s), "
                   "%d named gap(s), %d why_empty value(s)"
                   % (len(PAYLOAD_WRITERS), len(PAYLOAD_BATCH_FORMS), len(PAYLOAD_NONE),
                      len(WHY_EMPTY_REASONS)))
            return 0
        _print(format_payload_writers())
        return 0

    if args.history:
        # Round 127: a withdrawal with no answer is the debt the gate charges, so the page that shows the ledger
        # must show it -- with the age, and with the command, because "somebody withdrew this" alone is a rumour.
        if _withdrawn_rows:
            _print("withdrawn       %d decision(s) were withdrawn and nobody has taken them up again:"
                   % len(_withdrawn_rows))
            for row in _withdrawn_rows:
                age = row.get("age_days")
                past = (_withdrawn_cap is not None and age is not None
                        and float(age) >= float(_withdrawn_cap))
                _print("  %s -> %s  (withdrawn %s, %s day(s) ago)%s"
                       % (row.get("record"), row.get("successor"), row.get("at"),
                          "?" if age is None else "%.1f" % float(age),
                          "  [past the cap: --strict exits 1]" if past else ""))
            _print("  take one up again with `brian evolve --settle-restore RECORD=SUCCESSOR --settle-why TEXT`:")
            _print("  the pair is decided again, and the withdrawal stays on the record (boundary 297).")
        # Round 118: the same rows the audit page prints, so a reader who opens the ledger never reads a decision
        # as settled while the books it settled have moved (boundary 264).
        try:
            moved_rows = moved_settlements(history(root, include_retired=True), load_settlements(root))
        except OSError:
            moved_rows = []
        if moved_rows:
            _print("settled         %d decision(s) rest on books that have moved since they were taken:"
                   % len(moved_rows))
            for item in moved_rows:
                # Round 119: the row says whether this decision has ever been re-taken and when. A reader who
                # cannot tell "never re-confirmed" from "re-confirmed twice" cannot judge the debt.
                taken = int(item.get("reaffirmed") or 0)
                if taken:
                    mark = "  [re-taken %d time(s), last on %s]" % (taken, item.get("effective_at") or "(no date)")
                else:
                    mark = "  [never re-taken: only --settle-reaffirm moves the date it speaks about]"
                _print("  %-26s -> %-26s  (%s)%s"
                       % (item["record"] or "(unrecorded)", item["successor"] or "(unrecorded)",
                          "; ".join(str(reason) for reason in item.get("reasons") or []), mark))
            _print("                a decision is a statement about the books as they stood; when they move, "
                   "take it again")
        if unexplained_rows:
            # Round 117: the count is on the page whether or not a cap exists, because "3 decisions nobody can
            # review" is the fact a reader of the ledger needs, and the cap only decides when it becomes an exit.
            _print("decisions       %d settled decision(s) have no explanation: no reason was recorded with "
                   "them, and no note was added since" % len(unexplained_rows))
            for row in unexplained_rows:
                past = (_unexplained_cap is not None
                        and float(row.get("age_days") or 0.0) >= float(_unexplained_cap))
                _print("  %-26s -> %-26s  (decided %s, %.1f day(s) ago)%s"
                       % (row["record"] or "(unrecorded)", row["successor"] or "(unrecorded)",
                          row["at"] or "(no date)", float(row.get("age_days") or 0.0),
                          "  [past the cap: --strict exits 1]" if past else ""))
            _print("                `brian evolve --settle-note RECORD=SUCCESSOR --settle-why TEXT "
                   "--settle-by NAME` explains one")
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
        # Round 112: a settled pair is a decision, not a disagreement, and this page must read it the same way
        # `--audit` does -- otherwise the audit stops charging a pair that this page still marks as broken, and two
        # pages disagreeing about one ledger is the defect this campaign keeps finding (boundary 252).
        settled_rows = load_settlements(root)
        link_rows = replacement_links(rounds, settled_rows)
        # Round 134: the row that carries a settled decision also says when no re-take can pay it. The plan is the
        # one computation that knows (round 133), so the page asks it rather than re-deriving the rule (boundaries
        # 264/308) -- and the reader sees it without opening `--campaign` first (boundary 315).
        _unpayable_rows: Dict[Tuple[str, str], str] = {
            (str(item.get("record") or ""), str(item.get("successor") or "")): str(item.get("names") or "(nothing)")
            for row in reaffirm_plan(rounds, settled_rows)
            for item in (row.get("unpayable_pairs") or [])
        }
        # Round 83: an end written by a repair agrees with the other side by construction, so it is counted
        # apart from the links the queue wrote. "Every link agrees" is true of both, and means different
        # things; a page that prints only the first sentence lets a copied statement read as a witness.
        repaired_links = backfilled_links(rounds)
        link_legacy = [item for item in link_rows if item.get("kind") == "legacy"]
        link_defects = [item for item in link_rows if item.get("kind") in ("mismatch", "unretired", "dangling")]
        # Round 105: the ledger's view marks a broken link on the rows it names; the queue's view is different --
        # someone walked the queue, tried this promise, and the walk ended on a build defect. A reader asking "why is
        # this row never measured" needs the second sentence, and only `blocked_versions` knows it.
        blocked_rows = blocked_versions(root)
        # Round 107: the row's job is to say the queue tried and was refused, and when. A promise refused twice and
        # then repaired needs more than the standing refusal: how many walks refused it, and the first and last
        # stamp, so "this version was refused" and "it has not been refused for a while" read apart.
        refusal_rows = refusal_history(root)
        # Round 107: the counts, the first and the last belong on one header line, the way the link and ceiling
        # headers carry their own counts. A long mark repeated on every row that produced the version is not
        # emphasis -- round 105 already learned that with the contradiction sentence (boundary 235).
        for version in sorted(refusal_rows):
            entry = refusal_rows.get(version) or {}
            _print("refusals          v%03d was refused by the queue %d time(s) among %d recorded walk(s): "
                   "walk %s to %s, first %s, last %s%s"
                   % (version, int(entry.get("count") or 0), int(entry.get("walks") or 0),
                      str(entry.get("first_walk") or "?"), str(entry.get("last_walk") or "?"),
                      str(entry.get("first_at") or "?"), str(entry.get("last_at") or "?"),
                      ", and the refusal still stands" if entry.get("standing")
                      else ", and it no longer stands"))
        # Round 108: the state of the queue as the last walk left it, which no per-promise history can state.
        latest = latest_refusals(root)
        if int(latest.get("walk") or 0):
            refused_now = [int(item) for item in latest.get("versions") or []]
            _print("refusals          the last walk (%d of %d) refused %s"
                   % (int(latest.get("walk") or 0), int(latest.get("walks") or 0),
                      ", ".join("v%03d" % version for version in refused_now) if refused_now
                      else "no promise"))
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
                if settled_rows:
                    _print("links             every replacement link that no decision settled agrees with the "
                           "other end it names")
                else:
                    _print("links             every replacement link agrees with the other end it names")
        if settled_rows:
            # Printed after the sentence it qualifies: "every link agrees" is false here, and "2 links disagree" is
            # misleading, because a person decided them and `--strict` no longer charges either (boundary 251).
            _print("links             %d pair(s) were settled by a recorded decision, not by agreement: %s"
                   % (len(settled_rows),
                      ", ".join("%s -> %s" % (str(entry.get("record")), str(entry.get("successor")))
                                for entry in settled_rows)))
            # Round 115: the settled row is where a reader looks for "was this decision ever explained", so the
            # notes belong here, with their author -- and a decision explained twice says so (boundary 241: the
            # count on one line, the pairs named, never repeated per row).
            annotated = [entry for entry in settled_rows if entry.get("notes")]
            if annotated:
                _print("links             %d of them were explained later: %s"
                       % (len(annotated),
                          ", ".join("%s -> %s (%d note(s), last by %s on %s)"
                                    % (str(entry.get("record")), str(entry.get("successor")),
                                       len(entry.get("notes") or []),
                                       str(((entry.get("notes") or [{}])[-1]).get("by") or "")
                                       or "no one named",
                                       str(((entry.get("notes") or [{}])[-1]).get("at") or ""))
                                    for entry in annotated)))
        _print("ceilings          %d of %d round(s) record the wait ceilings they were judged under; %d do not"
               % (len(recorded_limits), len(rounds), len(rounds) - len(recorded_limits)))
        # Round 159: the rows below carry the mark since round 158; a reader who has to scroll the whole list to
        # count them has been given a detail and not a fact. The header counts, and it counts the retired rows
        # apart: a retired round's assertion lives on in its successor, so the total is a count of rows that rest
        # on a claim, not a count of people's assertions (boundary 383).
        # Round 160: the counting lives in `campaign.claim_counts`, so this page and `--campaign` read the same
        # function. The population differs -- this page lists every row, the campaign only the live rounds -- so
        # the sentence names the population it counted (boundary 386).
        history_counts = claim_counts(rounds)
        if not history_counts.empty:
            _print("claims            %s" % history_counts.sentence())
            retired_claims = history_counts.retired_sentence()
            if retired_claims:
                _print("claims            %s" % retired_claims)
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
        # Round 90: the campaign counts clearings; this page is where a reader asks which measurement earned
        # one. The chain is printed per version because that is the unit a reader can act on: re-check it, or
        # stop trusting it. The rows are the ones the pre-branch already loaded, so the two pages cannot
        # disagree about what the store holds.
        clearing_rows: List[Dict[str, Any]] = list(in_force_history_rows)
        if clearing_rows:
            chains: Dict[int, List[Dict[str, Any]]] = {}
            for entry in clearing_rows:
                chains.setdefault(int(entry.get("version") or 0), []).append(entry)
            evidence = in_force_evidence
            _print("clearings        %d recorded clearing(s); each names the measurement that earned it:"
                   % len(clearing_rows))
            if int(evidence.get("missing") or 0):
                _print("evidence         %d of them name a file this checkout cannot open: %s"
                       % (int(evidence.get("missing") or 0),
                          ", ".join(str(name) for name in evidence.get("missing_files") or [])))
            for version in sorted(chains):
                for entry in sorted(chains[version], key=lambda item: str(item.get("checked_at") or "")):
                    mark = ""
                    path = str(entry.get("stream_path") or "")
                    if path and not (root / path).is_file():
                        mark = "  [evidence missing]"
                    # Round 92: the entry names the file that was read; the file the caller asked for is a
                    # different fact, and only the pair tells a reader whether the verdict describes the stream
                    # they think it does (boundary 197).
                    if entry.get("substituted_from"):
                        mark += "  [substituted for %s]" % str(entry.get("substituted_from"))
                    # Round 100: the first verdict ever taken where the promise was made is a different
                    # event from another re-check, and the chain is where a reader goes to see the order.
                    if entry.get("first_original"):
                        mark += "  [first verdict at the origin]"
                    # Round 102: say how the check was started. A queued run and a hand-typed one leave the same
                    # verdict behind; only the record can tell a reader whether anyone had to retype an identifier.
                    if entry.get("queued") is True:
                        mark += "  [from the queue]"
                    _print("  v%03d  attempt %-2d %s  %-13s %s%s"
                           % (version, int(entry.get("attempt") or 0),
                              str(entry.get("checked_at") or ""), str(entry.get("verdict") or ""),
                              path or "(unnamed stream)", mark))
        # Round 94: a version file can have its measured file rewritten by hand (`--set-stream-path`). The chain
        # belongs on this page: it changes what every later verdict is about, and a reader who cannot see the move
        # will read the promise as having been made on the file it now names (boundary 203).
        path_moves = repaired_paths(root)
        for line in format_history_paths(path_moves):
            _print(line)
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
            # A record lists what it applied as labels (`v020`), not as numbers, so the digits are read out of the
            # label rather than casting it: `int("v020")` is a crash on a page about records (round 71's shape).
            produced_versions = set()
            if isinstance(applied, list):
                for item in applied:
                    digits = "".join(character for character in str(item) if character.isdigit())
                    if digits:
                        produced_versions.add(int(digits))
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
            # Round 158: and whether the file was a person's assertion rather than a measured choice. The
            # re-measurement says it before it runs and `--campaign` counts it; the history page is where a
            # reader lands after asking "why does this round look like that?", so the mark belongs here too.
            mark += claim_mark(record)
            # Round 79: which book the queue paid when it produced this round. The record has said so since
            # round 78; without this the fact was only readable by opening the JSON, and a page listing
            # finished rounds is exactly where a reader asks "what paid for this one".
            queue_entry = record.get("queue")
            if isinstance(queue_entry, Mapping) and queue_entry.get("family"):
                walk = int(queue_entry.get("walk") or 0)
                if walk and walk_held(root, walk) is None:
                    # Round 145: the store keeps 20 entries, so an older round can name a walk that has been
                    # trimmed away. The number stays on the row (it is what the record says) and the row says it
                    # can no longer be opened -- a dangling ordinal that reads like a live one is the defect.
                    mark += "  [paid by the %s queue, walk %d, no longer in the store]" % (
                        queue_entry.get("family"), walk)
                elif walk:
                    # Round 144: "the file queue paid" does not say which run of it, and a queue run twelve
                    # times has twelve entries in `rerun_runs.json`. The ordinal is what makes the sentence
                    # checkable (boundary 342's rule, one book over).
                    mark += "  [paid by the %s queue, walk %d]" % (queue_entry.get("family"), walk)
                else:
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
                sentence = "  [broken link: %s with %s]" % (item.get("kind"), ", ".join(others))
                # Round 105: one row can be named by two findings (each nominator offends on its own), and the same
                # sentence twice reads as two separate defects. The row says it once; the header says how many.
                if others and sentence not in mark:
                    mark += sentence
            # Round 112: the row a settled pair names says so, because the header's count is no substitute for
            # finding out which rounds it was about. A reader who lands on the retired row sees who decided.
            for entry in settled_rows:
                pair = (str(entry.get("record") or ""), str(entry.get("successor") or ""))
                if own_name not in pair:
                    continue
                other = pair[1] if own_name == pair[0] else pair[0]
                # Round 125: a withdrawn decision no longer stands, so the row must not call it a decision the books
                # agree with -- and since it will not be re-taken either, neither of the two later marks applies.
                if is_withdrawn(entry):
                    taken = (entry.get("withdrawn") or [{}])[-1]
                    sentence = ("  [the decision on %s was withdrawn on %s: the pair is charged again]"
                                % (other, str(taken.get("at") or "")))
                    if sentence not in mark:
                        mark += sentence
                    continue
                # Round 126: the pair is decided again by a person. Both moments go on the row, because "decided"
                # and "decided after somebody took the first decision back" are different histories (boundary 295).
                if entry.get("redecisions") and own_name == pair[0]:
                    taken = (entry.get("withdrawn") or [{}])[-1]
                    back = (entry.get("redecisions") or [{}])[-1]
                    sentence = ("  [withdrawn on %s, taken up again on %s]"
                                % (str(taken.get("at") or ""), str(back.get("at") or "")))
                    if sentence not in mark:
                        mark += sentence
                # Round 116: the row says whether the decision was ever explained, so "no reason" stops being a
                # sentence somewhere above the list and becomes a state a reader can find on the row itself.
                explained_here = ("and no reason was recorded" if not str(entry.get("why") or "").strip()
                                  and not entry.get("notes")
                                  else "by a recorded decision")
                sentence = "  [settled with %s %s]" % (other, explained_here)
                if sentence not in mark:
                    mark += sentence
                # Round 124: a decision re-taken repeatedly keeps going stale, and repeating a re-take is evidence
                # about the decision (boundary 288). The row says how many times; the `re-takes` block above the
                # list prints each one with what it covered.
                retakes = len([note for note in entry.get("reaffirmations") or [] if isinstance(note, Mapping)])
                # The decision settles the *record*; its successor was never re-taken, so the mark sits on the
                # record's own row only (round 124's demo printed it on both ends, which read as six re-takes of
                # a round that was never a decision).
                if retakes >= 3 and own_name == pair[0]:
                    sentence = "  [re-taken %d time(s): the decision keeps going stale]" % retakes
                    if sentence not in mark:
                        mark += sentence
                if entry.get("successor_history") and own_name == pair[0]:
                    # Round 138: a re-anchored decision was never taken on its new successor, so the row has to say
                    # the target moved -- otherwise a repaired target reads as the target the decision was made
                    # on (boundary 203, and round 93's rule for a repaired path one field over).
                    move = (entry.get("successor_history") or [{}])[-1]
                    sentence = ("  [re-anchored on %s: %s -> %s]"
                                % (str(move.get("at") or "?"), str(move.get("from") or "?"),
                                   str(move.get("to") or "?")))
                    if sentence not in mark:
                        mark += sentence
                # Round 134: a pair no re-take can pay says so on its own row, with the successor the record names
                # now -- the `--campaign` line already names it, and a reader who lands here should not have to
                # guess why the re-take command will refuse this decision.
                names_now = _unpayable_rows.get(pair)
                if names_now and own_name == pair[0]:
                    sentence = "  [cannot be re-taken: the record names %s]" % names_now
                    if sentence not in mark:
                        mark += sentence
            # Round 107: the row is about what happened to this promise, not only about what is still true of it. A
            # refusal the ledger has since answered is history a reader searching for "why was this never measured"
            # still needs, so the loop walks every refused promise and says which kind it is.
            for version in sorted(refusal_rows):
                if version not in produced_versions:
                    continue
                blocked = blocked_rows.get(version)
                # The long sentence (which record's book contradicts this promise) belongs on the campaign page,
                # where it is printed once per origin file. On a row it would repeat on every record that produced
                # the version and bury the column it sits in -- the row's job is to say the queue tried and was
                # refused, and when.
                # The row carries the short fact; the header above carries the counts and the stamps.
                mark += "  [refused by the queue]" if not blocked else "  [refused by the queue, still standing]"
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
        # Round 171 (item 157): the row counts are printed above; this line says what the page did about the
        # checks, and the payload carries it so a script can tell "nothing charged" from "nothing checked".
        history_gate = report_footer(False, asked=bool(getattr(args, "strict", False)))
        if getattr(args, "json_out", None) and not _write_payload(args.json_out, recorded=False, payload={
                "rounds": history_rows,
                "links": link_rows,
                "in_force_clearings": clearing_rows,
                "stream_path_moves": path_moves,
                "ceilings": {"missing": ceiling_missing, "rounds": ceiling_rounds,
                             "payable": ceiling_payable, "by_domain": ceiling_rows},
                # Round 161 (item 147): the header above counts the claim rows; the payload carried the rows
                # but not their counts, so a script had to recount them -- and a recount is a second
                # computation that can disagree with the sentence printed beside it (boundary 385).
                "claim_counts": history_counts.to_dict(),
                "gate": history_gate,
            }, keys=PAYLOAD_KEYS["--history"]):
                # Round 161 (item 148): this page wrote its payload with a bare Path.write_text and printed the
                # line itself, so a missing directory raised FileNotFoundError -- the exact shape round 141
                # collected out of the settlement commands (boundaries 336/337). The page is a report, not a
                # record: if only the payload is missing, say so and leave a non-zero exit.
                return 2
        return 0

    if getattr(args, "claim_round_file", None):
        # Round 152 (item 138): the out-of-reach half of the debt was a diagnosis without a verb. A person who
        # knows which file a round measured can now record it; the geometry check says whether the record and
        # the file agree, and `--force-claim` is the only way past a disagreement -- a repair that moves a row
        # into the queue's reach without making that true is worse than the debt it hides (boundary 368).
        shapes = dict(_stream_library(root))
        records = {str(row.get("round")): row for row in history(root, include_retired=True)}
        claims: List[Dict[str, Any]] = []
        refused = 0
        for pair in [str(item) for item in args.claim_round_file]:
            name, _sep, claimed = pair.partition("=")
            name = name.strip()
            claimed = claimed.strip().replace("\\", "/")
            shape = shapes.get(claimed)
            geometry = "unknown" if shape is None else (
                "agrees" if _matching_streams(records.get(name, {}), [(claimed, shape)]) == [claimed]
                else "disagrees")
            try:
                done = claim_round_file(root, [pair], force=bool(getattr(args, "force_claim", False)),
                                        geometry_of=lambda _path, verdict=geometry: verdict,
                                        dry_run=bool(getattr(args, "dry_run", False)),
                                        why=str(getattr(args, "claim_why", "") or ""))
            except (OSError, ValueError) as error:
                _print("cannot claim that record: %s" % error)
                refused += 1
                continue
            for item in done:
                claims.append(item)
                if getattr(args, "dry_run", False):
                    _print("would claim      %s  now names %s (geometry %s)" % (item["round"], item["to"],
                                                                              item["geometry"]))
                elif item.get("changed"):
                    _print("claimed          %s  now names %s (geometry %s)" % (item["round"], item["to"],
                                                                              item["geometry"]))
                else:
                    _print("claimed          %s  already named it, nothing to repair" % item["round"])
        if getattr(args, "dry_run", False):
            _print("nothing was written; drop --dry-run to record the claim (boundary 304).")
        elif claims:
            _print("claimed          %d record(s) repaired; each keeps the file it named before in "
                   "stream_path_history, so the repair is visible in the record (boundary 369)."
                   % sum(1 for item in claims if item.get("changed")))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"claimed": claims, "claim_refused": refused},
                keys=PAYLOAD_KEYS["--claim-round-file"],
                # Round 164: a preview of a repairing command still repairs nothing, so the failure sentence must
                # follow this branch's own behaviour rather than the command's category (boundary 395).
                recorded=not getattr(args, "dry_run", False)):
            return 2
        return 2 if refused else 0

    if getattr(args, "backfill_claims", False):
        # Round 156 (item 142): round 155 carries the chain forward, but the records round 154 wrote have only
        # `inherited_from` and a refreshed `at` -- so their chain starts counting at 155 and `claim_since`
        # would fall back to the re-measurement's moment, which is the confusion the field exists to prevent.
        dry = bool(getattr(args, "dry_run", False))
        try:
            rows = backfill_claims(root, dry_run=dry)
        except OSError as error:
            _print("cannot read the records: %s" % error)
            return 2
        for row in rows:
            if dry:
                _print("would fill       %s  %d entr(y/ies): %d dated from the record they point at, %d unknown"
                       % (row["round"], row["entries"], row["derived"], row["unknown"]))
            else:
                _print("filled           %s  %d entr(y/ies): %d dated from the record they point at, %d unknown"
                       % (row["round"], row["entries"], row["derived"], row["unknown"]))
        if dry:
            _print("nothing was written; drop --dry-run to complete the chain (boundary 304).")
        elif rows:
            _print("filled           %d record(s); each entry says backfilled, and a date this command could not "
                   "derive stays unknown rather than repeating the re-measurement's moment (boundary 378)."
                   % len(rows))
        else:
            _print("nothing to backfill: every claim chain already carries its origin, its first moment and "
                   "its hop count")
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"filled": rows, "dry_run": dry}, recorded=not dry,
                keys=PAYLOAD_KEYS["--backfill-claims"]):
            return 2
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
        # Round 150: the command prints the page's own sentence before the first measurement (round 60's rule --
        # one place computes, both places print), so what this invocation is about to pay is not something the
        # reader has to add up from the three counts above it.
        _print("offer             %s" % _rerun_offer(root)["text"])
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
        # Round 144: this invocation's place in the re-run queue's own store, reserved before the first measurement
        # so every round it pays can name it. `0` means the store could not be written; the rounds then record
        # "no walk", which is what happened rather than a number nobody can look up.
        # Round 144: this invocation's place in the re-run queue's own store, reserved before the first measurement
        # so every round it pays can name it. `0` means the store could not be written; the rounds then record
        # "no walk", which is what happened rather than a number nobody can look up.
        #
        # Only an invocation with work to do gets a slot: a command that finds nothing payable spends no
        # measurement, so there is no walk for a later reader to look up, and a store of empty entries would make
        # "walk 12" mean "somebody typed the command twelve times before it had anything to do" (boundary 346).
        walk_no = 0
        if list(ready[:limit]) or list(ceiling_ready[:ceiling_limit]):
            try:
                opened = begin_rerun_run(root, {
                    "limit": limit, "ceiling_limit": ceiling_limit if ceiling_ready else None,
                    "families": [name for name, opened_ in (("file", bool(ready)),
                                                            ("ceiling", bool(ceiling_ready))) if opened_],
                    "with_ceilings": bool(ceiling_ready),
                })
                walk_no = int(opened.get("walk") or 0)
            except OSError:
                walk_no = 0
        if walk_no:
            _print("walk              %d of the re-run queue's recorded invocation(s); each round it pays names it"
                   % walk_no)
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
            # Round 153: only after the record is known to be re-measurable. A warning
            # about a measurement that will not happen is a page describing work it is
            # not doing (boundary 372).
            note = _claim_note_line(record)
            if note:
                _print(note)
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
            # Round 154: a re-measurement writes a new record. If the file it measured was claimed by a
            # person and the geometry disagreed, that fact belongs on the new record too -- otherwise the
            # claim disappears right after a measurement that stood on it (boundary 373).
            result.stream_path_history = inherit_claim(record, target_file)
            # Round 68: a re-run is judged under the same ceilings as any other round, so it records them.
            result.limits = _limits_view(minute_ceiling, minute_ceilings, flag_run_ceilings,
                                         drain_ceiling, drain_ceilings, flag_drain_ceilings)
            # Round 58: the record of what this entry of the queue paid belongs in the round it paid, not in
            # the terminal that ran it. `largest_at_start` is the domain a reader would have expected this
            # command to aim at, so a later reader can tell an aimed payment from an incidental one.
            # Round 78: the record also says which family this entry paid and what the invocation opened --
            # a round paid by the ceiling queue and one paid by the file queue are different work, and the
            # record is the only place that survives the terminal (boundary 163).
            result.queue = _queue_entry(kind, domain, name, largest_start, progress, queue_shape,
                                      walk=walk_no)
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
        if walk_no:
            try:
                finished = finish_rerun_run(root, walk_no, {
                    "paid": paid_now, "paid_ceilings": paid_ceilings, "failures": failures,
                    "stopped": stopped, "rounds": sorted(_paid_round_names(root, walk_no)),
                })
            except OSError:
                finished = None
            if finished is None:
                _print("walk              the invocation it reserved is no longer in the store, so the rounds "
                       "it paid name an entry that is gone")
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
        # Round 150: the closing line answers the opening one, from the same function, so "what it promised" and
        # "what is left" cannot be two different accounts (boundary 364). The label says which moment this is,
        # because the same sentence at two moments otherwise reads as two numbers that contradict each other
        # (boundary 365).
        _print("left to pay       %s" % _rerun_offer(root)["text"])
        return 2 if failures else 0

    if getattr(args, "backfill_walks", False):
        # Round 147: the ledger's retirement marks are the only record of the payments made before the store
        # existed. Reading them back is not the same as having recorded them, and the entry says which it is.
        pairs: List[Any] = []
        for record in history(root, include_retired=True):
            retired_block = record.get("retired") if isinstance(record, dict) else None
            if not isinstance(retired_block, dict):
                continue
            successor = str(retired_block.get("re_measured_as") or "")
            if successor:
                pairs.append((str(record.get("round") or ""), successor,
                              str(retired_block.get("at") or "")))
        if not pairs:
            _print("nothing to backfill: no round in the ledger names the re-run that replaced it")
            return 0
        # Round 165: what this run would write, not everything the ledger ever named -- the same list the run uses.
        # Two different nothings (boundary 367): a ledger that names no payment, and a store that already holds
        # every payment the ledger names. The first is `if not pairs` above; this is the second.
        pairs = pending_walk_backfill(root, pairs)
        if not pairs:
            _print("nothing to backfill: every payment the ledger names already has a walk in the store")
            if getattr(args, "json_out", None):
                _print("no payload        nothing was backfilled, so there is no walk to hand over (boundary 339).")
            return 0
        if getattr(args, "dry_run", False):
            would = [{"round": record, "successor": successor, "at": at or ""}
                     for record, successor, at in pairs]
            for row in would:
                _print("would backfill    %s -> %s (the payment is dated %s)"
                       % (row["round"], row["successor"], row["at"] or "unknown"))
            _print("nothing was written; drop --dry-run to write the walks the ledger already names (boundary 304).")
            if getattr(args, "json_out", None) and not _write_payload(
                    args.json_out, {"backfilled": [], "would_backfill": would, "dry_run": True}, recorded=False,
                    keys=PAYLOAD_KEYS["--backfill-walks (a dry run)"]):
                return 2
            return 0
        try:
            written = backfill_walks(root, pairs)
        except OSError as error:
            _print("cannot write the re-run store: %s" % error)
            return 2
        if not written:
            _print("nothing to backfill: every payment the ledger names already has a walk in the store")
            if getattr(args, "json_out", None):
                _print("no payload        nothing was backfilled, so there is no walk to hand over (boundary 339).")
            return 0
        for entry in written:
            _print("backfilled        %s -> walk %d, dated %s"
                   % (entry["rounds"][0], entry["walk"], entry["at"]))
        _print("backfilled        %d payment(s) the ledger recorded and the store never saw; each entry says so, "
               "and each keeps the moment of the payment (boundary 354)" % len(written))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"backfilled": written, "would_backfill": [], "dry_run": False},
                keys=PAYLOAD_KEYS["--backfill-walks"]):
            return 2
        return 0

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
            if successor:
                # Round 146: a retirement that names a successor is a payment, whether the queue made it or a
                # person typed both commands. Recording only the queue's payments made the store's count say
                # "the loop paid this" when a human had. The successor already exists and was written before this
                # command, so it cannot name the walk -- the page says that instead of pretending otherwise.
                paid_walk = _round_names_walk(root, str(successor))
                if paid_walk is None:
                    pass
                elif paid_walk:
                    _print("walk              already recorded: %s was paid by the queue's walk %d, so this "
                           "retirement does not pay for it a second time" % (successor, paid_walk))
                else:
                    try:
                        hand_walk = begin_rerun_run(root, {"hand": True, "limit": 0, "families": []})["walk"]
                        finish_rerun_run(root, hand_walk, {"paid": 1, "rounds": [str(successor)]})
                    except OSError:
                        hand_walk = 0
                    if not hand_walk:
                        _print("walk              this retirement could not be recorded as a walk: the store is "
                               "unwritable, and the retirement itself stands")
                    else:
                        _print("walk              %d, typed by hand: the round it paid was written before this "
                               "command, so it cannot name the walk (boundary 351)" % hand_walk)
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
        # Round 172 (item 158): one dict for the empty payload and the full one. Two copies of seven keys
        # is how the two pages drifted apart in round 160 (boundary 385).
        unrecorded_ceilings = {"missing": ceiling_missing, "rounds": ceiling_rounds,
                               "payable": ceiling_payable,
                               # Round 151: the same three parts the page names, so a consumer subtracting
                               # "missing - payable" by hand cannot mistake a repair for a queue debt.
                               "named": ceiling_named,
                               "unpayable": ceiling_unpayable,
                               "after": max(0, ceiling_missing - ceiling_payable),
                               "by_domain": ceiling_rows}
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
            # Round 172 (item 158): this page used to return here, before its `_write_payload` call, so a
            # script asking "is there work?" got no file at all -- and a missing file reads exactly like a
            # failed run (round 162's shape, boundary 406). It now always writes the payload, and the three
            # empty ledgers get three sentences: they are three different facts.
            _print("")
            if not rounds:
                why = "no rounds"
                _print("nothing recorded: this ledger holds no round yet, so there is no file to place and "
                       "nothing to re-run.")
            elif not unknown:
                why = "all placed"
                _print("Every round that still decides something says which file it measured.")
            else:
                why = "none deciding"
                _print("nothing to re-run: %d round(s) name no file, and none of them still decides anything "
                       "-- they are retired, or their promise is answered." % unknown)
            unrecorded_gate = report_footer(False)
            if not _write_unrecorded_payload(args, payload={"rounds": len(rounds), "unattributed": unknown,
                           "matters": [], "payable": [], "out_of_reach": [],
                           "ceilings": unrecorded_ceilings,
                           # Round 172: the empty page says which nothing this is -- "no rounds", "all placed"
                           # or "none deciding" -- because three empty lists spell none of them (boundary 410).
                           "why_empty": why,
                           "gate": unrecorded_gate}):
                return 2
            return 0
        # Round 151 (item 137): the table mixed two different situations in one list. A round with one
        # candidate file can be paid by the queue; a round with none, or with several, cannot be re-run by any
        # command as the record stands -- the missing thing there is a record, not a measurement. Two tables,
        # because a reader who runs the queue wants to know which rows the command will actually take.
        payable_rows = [row for row in matters if len(row[3]) == 1]
        reachless_rows = [row for row in matters if len(row[3]) != 1]
        if payable_rows:
            _print("")
            _print("payable           %d round(s) name exactly one candidate file, so the queue can re-run them"
                   % len(payable_rows))
        if reachless_rows:
            _print("")
            _print("out of reach      %d round(s) name no single file (none, or several), so no command can "
                   "re-run them while the record stands" % len(reachless_rows))
        _print("")
        _print("%-16s %-32s %-34s %s" % ("domain", "round", "likely file", "why it still matters"))
        _print("-" * 130)
        # Round 71: a re-run writes a record that carries its own ceilings, so this list is also the list of
        # rounds that would let the default --max-limit-drift cap arm. Marked per row rather than summarised,
        # because the reader is deciding which rows to run, not how many.
        lacking = {str(record.get("round") or ""): not record.get("limits")
                   for record in history(root, include_retired=False)}
        for domain, name, why, files in payable_rows + reachless_rows:
            note = "  [+ceilings]" if lacking.get(str(name)) else ""
            _print("%-16s %-32s %-34s %s%s" % (domain, name, _candidate_label(files), why, note))
        _print("")
        if reachless_rows:
            _print("Re-run one of the reachable rows with `brian evolve --stream-file <file>`: the new record")
            _print("names the file, and this list shrinks. The out-of-reach rows stay: no command re-measures a")
            _print("round that names no single file -- unless a person records the file: `brian evolve")
            _print("--claim-round-file <round>=<file>` moves the row out of this list, and the ceiling debt")
            _print("it owes becomes payable, because the queue can finally find the file (boundary 370).")
        else:
            _print("Re-run one with `brian evolve --stream-file <file>`: the new record names the file, and this")
            _print("list shrinks.")
        _print("The likely-file column is matched by geometry, not written anywhere: a file whose")
        _print("summary disagrees with the round's is never proposed, and a round that matches none is shown")
        _print("as such rather than given the nearest name (boundary 102).")
        # Round 171 (item 157): a list of work, not a verdict -- the row reader still needs to know that
        # nothing on this page charged anything (boundary 410).
        unrecorded_gate = report_footer(False)
        if not _write_unrecorded_payload(args, payload={"rounds": len(rounds), "unattributed": unknown,
                       "matters": [{"domain": d, "round": n, "why": w, "files": f}
                                   for d, n, w, f in matters],
                       # Round 151: the same split the page prints, so a consumer does not re-derive the rule.
                       "payable": [{"domain": d, "round": n, "why": w, "files": f}
                                   for d, n, w, f in payable_rows],
                       "out_of_reach": [{"domain": d, "round": n, "why": w, "files": f}
                                        for d, n, w, f in reachless_rows],
                       # Round 172 (item 158): the same dict the empty page carries -- computed once.
                       "ceilings": unrecorded_ceilings,
                       # Round 172 (item 158): null when there is work; the empty page names which of the
                       # three "nothing"s it is, so a script does not have to infer it from three empty lists.
                       "why_empty": None,
                       "gate": unrecorded_gate}):
                return 2
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
                                     in_force_stale=in_force_stale_rows, in_force_history=in_force_record,
                                     queue_run=last_queue_run(root), queue_history=queue_history(root),
                                     blocked_dossier=blocked_dossier(root),
                                     stale_refusals=stale_refusals(root),
                                     refusal_history=refusal_history(root))
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
        # Round 151 (item 137): the third count is the part of the debt no command can lower -- a round that
        # names no single file, or no longer decides anything. The gate charges only what is payable (boundary
        # 154), and the sentence now says so instead of leaving the reader to subtract (boundary 366).
        ceiling_note = format_ceiling_shortfall(ceiling_rows, payable=ceiling_file_payable,
                                                named=ceiling_named, unpayable=ceiling_unpayable,
                                                repair="brian evolve --claim-round-file ROUND=FILE",
                                                command=_ceiling_command(ceiling_file_payable, ceiling_named))
        # Round 149: `0 by the queue` is a fact about the past; the page also says what the queue could pay
        # today and names the command, computed once so the page and the payload print one sentence (boundary 360).
        rerun_offer = _rerun_offer(root)
        _print(format_campaign(result, expiry_days=args.expiry_days, rules=rules,
                               refusal_expiry_days=horizon, queue=_rerun_progress(root),
                               trend=trend, debt=_largest_debt(root), plan=_debt_plan(root),
                               budget=budget, limits=_limits_text(limits_view),
                               ceiling_note=ceiling_note, rerun_offer=rerun_offer,
                               claims=claim_rows,
                               claim_counts=campaign_counts))
        if getattr(args, "what_if_records", None):
            # Round 132: a reader looking at "this file owes two" should not have to conjure the batch commands to
            # ask what paying them would leave. The block is a recomputation, never a write.
            _print_what_if(root, str(args.what_if_records).strip(), _plan_steps)
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
            # Round 101: the queue the page counts, in the form a script can execute.
            payload["origin_queue"] = origin_queue_state
            # Round 149: what the queue could pay today travels with the page that printed it.
            payload["rerun_offer"] = rerun_offer
            # Round 153: which rows rest on a person's claim, and what the
            # geometry said about it, travels with the page.
            payload["claims"] = claim_rows
            # Round 161 (item 147): the same counts the line above printed, in the form a script reads.
            payload["claim_counts"] = campaign_counts.to_dict()
            # Round 167 (item 153): the same block as the audit page, from the same value.
            payload["gate"] = gate_report
        # Round 168 (item 154): the gate runs first, so the report can carry the verdict it reached -- a file
        # written before the check says "nothing is wrong" while the exit code says otherwise (boundary 402).
        gate_code = _strict_exit(args.strict, result.moved, result.unreconstructable, result.inconsistent,
                            page_problems=page_problems,
                            due=result.withheld_due,
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
                            in_force_untested=sum(1 for row in in_force_stale_rows if row.get("untested")),
                            unexplained_stale=len(unexplained_stale),
                            unexplained_days=_unexplained_cap,
                            restaled=_restaled, max_restaled=_moved_cap,
                            per_file_restaled=_per_file_restaled, max_restaled_per_file=_per_file_cap,
                            unanswered=len(_unanswered), unanswered_days=_withdrawn_cap,
                            per_file_unanswered=_per_file_charged,
                            max_unanswered_per_file=_withdrawn_per_file,
                            unanswered_file=str(_worst_withdrawn.get("origin") or ""),
                            into=gate_report)
        gate_report["exit"] = gate_code
        if args.json_out and not _write_payload(args.json_out, payload, recorded=False,
                                               keys=PAYLOAD_KEYS["--campaign"]):
            return 2
        return gate_code

    if args.sensitivity:
        rules = _rules_from_args(args)
        bars = [value for value in (args.sensitivity_bar or [])] or None
        try:
            result = refusal_sensitivity(root, rules, bars=bars)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_sensitivity(result))
        # Round 171 (item 157): this page re-decides stored refusals under a ladder of bars; it records
        # nothing and charges nothing, so it says which page would (boundary 410).
        sensitivity_gate = report_footer(False)
        if args.json_out and not _write_payload(
                args.json_out, dict(result.to_dict(), gate=sensitivity_gate), recorded=False):
            return 2
        return 0

    if args.withheld:
        rules = _rules_from_args(args)
        try:
            records = withheld_records(root)
        except OSError as error:
            _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_withheld(records, rules))
        # Round 171 (item 157): the same page family as --sensitivity -- it reads the refusals and
        # charges nothing, so it names the page that does (boundary 410).
        withheld_gate = report_footer(False)
        if args.json_out:
            payload = {"bar": rules.replication_bar,
                       "withheld": [record.to_dict() for record in records],
                       "gate": withheld_gate}
            if not _write_payload(args.json_out, payload, recorded=False):
                return 2
        return 0

    if args.audit:
        rules = _rules_from_args(args)
        horizon = float(getattr(args, "refusal_expiry_days", REFUSAL_EXPIRY_DAYS))
        try:
            result = audit(root, rules, in_force=in_force_rows,
                           in_force_cleared=in_force_cleared, in_force_stale=in_force_stale_rows,
                           unanswered=_withdrawn_rows)
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
                       # Round 116: a decision's account belongs in the machine-readable judgement too; the page
                       # prints four states and a script that sees only `links` would read them as one.
                       "settlements": settlement_summary(load_settlements(root)),
                       # Round 167 (item 153): an exit code whose reason exists only on stdout is
                       # unexplained to a script. The gate's own input travels with the report
                       # (boundary 402).
                       "gate": gate_report,
                       "entries": result.entries}
        # Round 168 (item 154): same order as the campaign page -- check, then export.
        gate_code = _strict_exit(args.strict, result.moved, result.cross, result.inconsistent,
                            page_problems=page_problems,
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
                                                  if row.get("untested")),
                            unexplained_stale=len(unexplained_stale),
                            unexplained_days=_unexplained_cap,
                            restaled=_restaled, max_restaled=_moved_cap,
                            per_file_restaled=_per_file_restaled, max_restaled_per_file=_per_file_cap,
                            unanswered=len(_unanswered), unanswered_days=_withdrawn_cap,
                            per_file_unanswered=_per_file_charged,
                            max_unanswered_per_file=_withdrawn_per_file,
                            unanswered_file=str(_worst_withdrawn.get("origin") or ""),
                            into=gate_report)
        gate_report["exit"] = gate_code
        if args.json_out and not _write_payload(args.json_out, payload, recorded=False,
                                               keys=PAYLOAD_KEYS["--audit"]):
            return 2
        return gate_code

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

    if getattr(args, "set_stream_path", None):
        pair = str(args.set_stream_path)
        if "=" not in pair:
            _print("--set-stream-path takes VERSION=FILE, for example --set-stream-path v020=data/smd/stream.json")
            return 2
        label, target = pair.split("=", 1)
        digits = "".join(ch for ch in label if ch.isdigit())
        if not digits:
            _print("--set-stream-path needs a version number in the label, not %r" % label)
            return 2
        plan_before = repair_plan(root)
        try:
            change = set_stream_path(root, int(digits), target)
        except ValueError as error:
            _print("  %s" % error)
            return 2
        if not change["changed"]:
            _print("stream path     v%03d already says %s; nothing to change" % (change["version"], change["to"]))
        else:
            # Round 93: the repair is written into the record, not into a log -- a reader of the version file
            # must see that the file moved and what it moved from.
            _print("stream path     v%03d  %s -> %s" % (change["version"],
                                                        change["from"] or "(unrecorded)", change["to"]))
            _print("                recorded in the version's stream_path_history, so the change is visible")
            _print("                to anyone who reads the promise. Nothing was re-measured by this call.")
            if change.get("returned"):
                # Round 97: the trip back ends here. Saying so at the moment it happens keeps a reader from
                # counting it as a second repair, and from reading the old verdicts as if they had been taken
                # on the file the record now names (boundary 211).
                _print("                this one is a return: the record is back on the file the promise was")
                _print("                born with, while the evidence it holds was measured on the substitute.")
        if change["changed"]:
            # Round 111: a repair that cannot say whether it moved the plan leaves the reader ranking twice.
            _print_repair_delta(record_repair(root, "set-stream-path", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"stream_path": change}, keys=PAYLOAD_KEYS["--set-stream-path"]):
            return 2
        return 0

    if getattr(args, "settle_link", None):
        # Round 112: a contradiction is two claims and no evidence, so no measurement settles it and the only
        # repair commands edit other books. This records the decision instead, and then says what the decision
        # did to the plan -- the same before/after every other repair writes (round 111).
        pair = str(args.settle_link or "")
        if "=" not in pair:
            _print("--settle-link takes RECORD=SUCCESSOR, for example `--settle-link round-old.json=round-new.json`")
            return 2
        record, successor = pair.split("=", 1)
        plan_before = repair_plan(root)
        try:
            decision = settle_link(root, record.strip(), successor.strip(),
                                   why=str(args.settle_why or ""), by=str(getattr(args, "settle_by", "") or ""))
        except (OSError, ValueError) as error:
            _print("cannot settle that link: %s" % error)
            # Round 115: a refusal that says "you need a reason" and stops there hides the fact that the old
            # records are the ones that need explaining. The hint counts them, because "N of your decisions
            # have no reason" is the sentence that sends a reader to the right command (boundary 129).
            if "needs a reason" in str(error):
                unexplained = [item for item in load_settlements(root)
                               if not str(item.get("why") or "").strip()]
                if unexplained:
                    _print("hint            %d recorded decision(s) have no reason at all; `--settle-note "
                           "RECORD=SUCCESSOR --settle-why TEXT` explains one without moving its date"
                           % len(unexplained))
            return 2
        if not decision["changed"]:
            _print("settled         %s -> %s was already recorded on %s; nothing to change"
                   % (decision["record"], decision["successor"], decision["at"]))
        else:
            _print("settled         %s -> %s" % (decision["record"], decision["successor"]))
            _print("                recorded in var/evolve/settlements.json: this is a decision, not a measurement,")
            _print("                and `--audit` prints it apart from the links that agree by checking.")
            _print("                because: %s" % decision["why"])
            # Round 113: the record names a decider only when the caller did. A machine account is not a person,
            # so the empty case is printed as empty and the flag that fills it is named.
            _print("                decided by: %s"
                   % (decision["by"] if decision.get("by")
                      else "(not recorded -- pass --settle-by NAME to attribute this decision)"))
            _print_repair_delta(record_repair(root, "settle-link", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"settled": decision}, keys=PAYLOAD_KEYS["--settle-link"]):
            return 2
        return 0

    if getattr(args, "settle_note", None):
        # Round 114: the decision is already recorded; what is missing is the sentence explaining it. Appending a
        # note keeps the date and the successor where they are, so the record cannot be rewritten into a decision
        # that looks like it was explained all along (boundary 257).
        pair = str(args.settle_note or "")
        if "=" not in pair:
            _print("--settle-note takes RECORD=SUCCESSOR, for example "
                   "`--settle-note round-old.json=round-new.json`")
            return 2
        record, successor = pair.split("=", 1)
        plan_before = repair_plan(root)
        try:
            noted = settle_note(root, record.strip(), successor.strip(),
                                why=str(args.settle_why or ""),
                                by=str(getattr(args, "settle_by", "") or ""))
        except (OSError, ValueError) as error:
            _print("cannot note that decision: %s" % error)
            return 2
        if not noted["changed"]:
            _print("noted           %s -> %s already ends with that explanation; nothing to change"
                   % (noted["record"], noted["successor"]))
        else:
            note = noted["note"]
            _print("noted           %s -> %s" % (noted["record"], noted["successor"]))
            _print("                the decision keeps its date (%s) and its successor; the explanation is "
                   "appended" % noted["at"])
            _print("                because: %s" % note["why"])
            _print("                explained by: %s"
                   % (note["by"] if note.get("by") else "(not recorded -- pass --settle-by NAME)"))
            _print("                `--audit` prints this apart from a reason the decision came with.")
            # Round 114: the amendment is a repair too, and the plan's own numbers are how it reports itself
            # (round 111) -- "nothing changed in the plan" is the honest answer here and must print.
            _print_repair_delta(record_repair(root, "settle-note", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"noted": noted}, keys=PAYLOAD_KEYS["--settle-note"]):
            return 2
        return 0

    if getattr(args, "settle_reaffirm_file", None):
        # Round 122: a file with several stale decisions needs one command, not one per pair -- pasting eleven
        # pairs is how the reason drifts and how one gets skipped (boundary 277, one level up). The batch resolves
        # its pairs from the plan, so the list `--campaign` prints is the list this command runs (round 101).
        target = str(args.settle_reaffirm_file or "").strip()
        if not target:
            _print("--settle-reaffirm-file takes the stream file the decisions are about, for example "
                   "`--settle-reaffirm-file data/smd/stream-two.json`")
            return 2
        try:
            plan_rows = reaffirm_plan(history(root, include_retired=True), load_settlements(root),
                                      queue_runs(root)["runs"])
        except (OSError, ValueError) as error:
            _print("cannot read the re-take plan: %s" % error)
            return 2
        match = [row for row in plan_rows if same_stream_path(str(row.get("origin") or ""), target)]
        if not match:
            _print("no stale decision is recorded against %s: `brian evolve --campaign` lists the files that "
                   "hold one" % target)
            return 2
        row = match[0]
        # Round 123: the two kinds are two jobs. A first reading has to read the whole record; a repair after
        # movement only has to compare what arrived with what the last confirmation covered. Taking them together
        # under one reason made the expensive job invisible and let one explanation stand for both (boundary 285).
        kind = str(getattr(args, "settle_kind", None) or "all").strip().lower()
        if kind not in ("never", "again", "all"):
            _print("--settle-kind takes never, again or all, and %s is none of them: `never` is a decision nobody "
                   "has read the books for, `again` is one the books moved under after the last reading" % kind)
            return 2
        wanted = [item for item in row.get("pairs") or []
                  if kind == "all" or str(item.get("kind") or "") == kind]
        skipped = len(row.get("pairs") or []) - len(wanted)
        pairs = ["%s=%s" % (str(item.get("record") or ""), str(item.get("successor") or ""))
                 for item in wanted]
        if not pairs:
            if kind != "all":
                _print("%s has no stale decision of the kind `%s`: it owes %d decision(s) of the other kind -- run "
                       "`--settle-reaffirm-file %s` without --settle-kind to pay them"
                       % (row.get("origin") or target, kind, skipped, row.get("origin") or target))
                return 0
            _print("%s has no stale decision to re-take: its decisions agree with the books as they stand"
                   % (row.get("origin") or target))
            return 0
        dry = bool(getattr(args, "dry_run", False))
        # Round 130: a batch writes N records at once, so the plan comes before the write (round 83's rule). The
        # preview lists the pairs by name because "3 decisions on that file" is not something the reader can check.
        _print("%s  %d decision(s) on %s in one %s (%d never re-taken, %d re-taken before and moved again)"
               % ("would re-take" if dry else "re-taking     ", len(pairs), row.get("origin") or target,
                  "dry run: nothing was recorded" if dry else "command",
                  int(row.get("never") or 0), int(row.get("again") or 0)))
        if skipped:
            _print("leaving        %d decision(s) of the other kind alone: `--settle-kind %s` was asked for, and "
                   "the two kinds are different work (boundary 285)" % (skipped, kind))
        if dry:
            for pair in pairs:
                record = pair.split("=", 1)[0]
                entry = next((item for item in wanted if str(item.get("record") or "") == record), {})
                _print("  %s  (%s)" % (pair, str(entry.get("kind") or "never")))
            _print("nothing was written; drop --dry-run to re-take them (boundary 304).")
            _print("no payload      a preview writes nothing, not even the file --json-out names (boundary 304).")
            _print_after_preview(root, pairs, kind="reaffirm", origin=str(row.get("origin") or target))
            return 0
        inner = argparse.Namespace(**vars(args))
        inner.settle_reaffirm_file = None
        inner.settle_reaffirm = pairs
        inner.settle_reaffirm_skipped = skipped
        return cmd_evolve(inner, settings)

    if getattr(args, "settle_drop_file", None):
        # Round 134: the sibling of `--settle-restore-file`. Round 133 names the pairs no re-take can pay and spells
        # one `--settle-drop` per pair; a file owing three of them asked the same reason three times, which is how a
        # reason comes to be retyped differently (boundary 315). The pairs come from the same plan `--campaign`
        # prints (boundary 283) and the walk is the single-pair branch below (boundary 284).
        target = str(args.settle_drop_file or "").strip()
        if not target:
            _print("--settle-drop-file takes the stream file whose decisions cannot be re-taken")
            return 2
        plan_rows = reaffirm_plan(history(root, include_retired=True), load_settlements(root))
        match = [row for row in plan_rows if same_stream_path(str(row.get("origin") or ""), target)]
        pairs = ["%s=%s" % (str(item.get("record") or ""), str(item.get("successor") or ""))
                 for item in (match[0].get("unpayable_pairs") or [])] if match else []
        if not pairs:
            _print("no decision recorded against %s cannot be re-taken: `brian evolve --campaign` lists the files "
                   "that hold one" % target)
            return 2
        if getattr(args, "dry_run", False):
            # Round 135: the third batch command was the one `--dry-run` did not know. It says what it would hand
            # over, in the shape the other two families use, and writes nothing (boundaries 304/305).
            _print("would withdraw %d decision(s) on %s in one dry run: nothing was recorded"
                   % (len(pairs), match[0].get("origin") or target))
            for item in match[0].get("unpayable_pairs") or []:
                _print("  %s=%s  (the record now names %s)"
                       % (str(item.get("record") or ""), str(item.get("successor") or ""),
                          str(item.get("names") or "(nothing)")))
            for line in _after_preview_lines(root, pairs, kind="drop", origin=target):
                _print("  " + line.strip())
            _print("nothing was written; drop --dry-run to withdraw them (boundary 304).")
            _print("no payload      a preview writes nothing, not even the file --json-out names (boundary 304).")
            return 0
        _print("dropping       %d decision(s) on %s in one command" % (len(pairs), match[0].get("origin") or target))
        inner = argparse.Namespace(**vars(args))
        inner.settle_drop_file = None
        inner.settle_drop = pairs
        # Round 140: a batch writes the payload its single form writes, naming every pair it withdrew, and says so when
        # it writes none. Round 134 had nulled `json_out` here, so the flag produced nothing in silence while
        # `--settle-restore-file` produced a list -- one family, two answers (boundary 333).
        inner.json_out = getattr(args, "json_out", None)
        return cmd_evolve(inner, settings)

    if getattr(args, "settle_reanchor_file", None):
        # Round 139: a retargeted file owes the same step once per decision. The batch resolves its records from
        # `reanchor_plan`, so the list the page prints is the list this command runs (round 101), and it recurses into
        # the single-record branch so refusals, output and the JSON payload stay one implementation (boundary 284).
        file_target = str(args.settle_reanchor_file or "").strip()
        if not file_target:
            _print("--settle-reanchor-file takes the stream file those records were measured on, for example "
                   "`--settle-reanchor-file data/smd/stream-two.json`")
            return 2
        try:
            plan_rows = reanchor_plan(history(root, include_retired=True), load_settlements(root))
        except (OSError, ValueError) as error:
            _print("cannot read the re-anchor plan: %s" % error)
            return 2
        match = [row for row in plan_rows if same_stream_path(str(row.get("origin") or ""), file_target)]
        if not match:
            _print("no decision on %s still names a successor its record has left: `brian evolve --campaign` "
                   "lists the files that hold one" % file_target)
            return 2
        row = match[0]
        records = [str(item.get("record") or "") for item in row.get("pairs") or []]
        dry = bool(getattr(args, "dry_run", False))
        _print("%s  %d decision(s) on %s in one %s"
               % ("would re-anchor" if dry else "re-anchoring  ", len(records), row.get("origin") or file_target,
                  "dry run: nothing was recorded" if dry else "command"))
        if dry:
            for item in row.get("pairs") or []:
                _print("  %s  (%s -> %s)" % (str(item.get("record") or ""), str(item.get("from") or "?"),
                                             str(item.get("to") or "?")))
            _print("nothing was written; drop --dry-run to re-anchor them (boundary 304).")
            _print("no payload      a preview writes nothing, not even the file --json-out names (boundary 304).")
            _print_after_preview(root, records, kind="reanchor", origin=str(row.get("origin") or file_target))
            return 0
        inner = argparse.Namespace(**vars(args))
        inner.settle_reanchor_file = None
        inner.settle_reanchor = records
        # Round 140: same rule as the other batches -- the payload the single command writes, carrying every record
        # this one paid, and a preview that writes none says so instead of going quiet (boundary 333).
        inner.json_out = getattr(args, "json_out", None)
        inner.dry_run = False
        return cmd_evolve(inner, settings)

    if getattr(args, "settle_reanchor", None):
        # Round 138: round 133 stops counting a pair whose record names another successor, and round 137 makes the
        # restore door refuse it. Both are right, and together they left the decision with no forward step: the old
        # target stayed unpairable for ever and the only alternatives were "make a brand-new decision" (which leaves
        # the old one behind) or "drop it" (which throws the decision away). This moves the decision to the successor
        # the ledger says the record's measurement went to, keeping the old target in `successor_history`.
        # Round 139: several records in one command, one reason for the batch, and a refused record does not stop the
        # rest -- the same shape the other batches took (boundaries 277/303/316).
        raw_reanchor = getattr(args, "settle_reanchor", None)
        wanted = [str(item).strip() for item in
                  (raw_reanchor if isinstance(raw_reanchor, (list, tuple)) else [raw_reanchor])
                  if str(item).strip()]
        if not wanted:
            _print("--settle-reanchor takes the record whose decision moved, for example "
                   "`--settle-reanchor round-old.json`")
            return 2
        plan_before = repair_plan(root)
        done: List[Dict[str, Any]] = []
        refused = 0
        for record in wanted:
            try:
                change = reanchor_settlement(root, record, why=str(args.settle_why or ""),
                                             by=str(getattr(args, "settle_by", "") or ""))
            except (OSError, ValueError) as error:
                _print("cannot re-anchor that decision: %s" % error)
                refused += 1
                continue
            done.append(change)
            if not change["changed"]:
                _print("re-anchored    %s already names %s, which is what the record names: nothing to change"
                       % (change["record"], change["to"]))
                continue
            _print("re-anchored    %s  %s -> %s" % (change["record"], change["from"], change["to"]))
            _print("                the decision now names what the record names, and the earlier target stays in "
                   "successor_history")
            _print("                because: %s" % str(args.settle_why or "").strip())
            _print("                re-anchored by: %s"
                   % (str(args.settle_by or "").strip() or "(not recorded -- pass --settle-by NAME)"))
            _print("                a re-anchored decision was not taken on the new successor: the page says so, "
                   "and only a check can measure it there (boundary 203)")
        if len(wanted) > 1:
            _print("re-anchored    %d of %d decision(s), %d needed no change, %d refused"
                   % (sum(1 for item in done if item["changed"]), len(wanted),
                      sum(1 for item in done if not item["changed"]), refused))
        if done and any(item["changed"] for item in done):
            _print_repair_delta(record_repair(root, "settle-reanchor", plan_before, repair_plan(root)))
        out = getattr(args, "json_out", None)
        if out and not _write_payload(out, {"reanchored": done, "reanchor_refused": refused},
                                      keys=PAYLOAD_KEYS["--settle-reanchor"]):
            return 2
        return 2 if refused else 0

    if getattr(args, "settle_drop", None):
        # Round 125: round 124 names the decisions that keep needing a re-take, and the honest answer to one of them
        # is often "this decision is wrong". Until now the only ways out were another re-take or editing the store by
        # hand, which leaves no trace of who decided. A withdrawal keeps the decision and the reason it stopped
        # standing -- and it does not make the contradiction disappear, because `replacement_links` reads only the
        # decisions still standing, so the pair is charged again (boundary 292).
        # Round 134: several pairs in one command, so one reason covers the batch and a refused pair does not stop
        # the rest (boundary 303) -- the JSON payload is a list for the same reason it is a list everywhere else.
        raw_drop = getattr(args, "settle_drop", None)
        wanted = [str(item).strip() for item in (raw_drop if isinstance(raw_drop, (list, tuple)) else [raw_drop])
                  if str(item).strip()]
        if not wanted:
            _print("--settle-drop takes RECORD=SUCCESSOR, for example "
                   "`--settle-drop round-old.json=round-new.json`")
            return 2
        bad = next((item for item in wanted if "=" not in item), "")
        if bad:
            _print("--settle-drop takes RECORD=SUCCESSOR, and %s has no = in it" % bad)
            return 2
        plan_before = repair_plan(root)
        done: List[Dict[str, Any]] = []
        refused = 0
        skipped = 0
        for item in wanted:
            record, successor = (part.strip() for part in item.split("=", 1))
            try:
                decision = withdraw_settlement(root, record, successor, why=str(args.settle_why or ""),
                                               by=str(getattr(args, "settle_by", "") or ""))
            except (OSError, ValueError) as error:
                _print("cannot withdraw that decision: %s" % error)
                refused += 1
                continue
            if not decision["changed"]:
                _print("withdrawn      %s -> %s was already withdrawn on %s; nothing to change"
                       % (record, successor, decision["at"]))
                skipped += 1
                continue
            done.append(decision)
            _print("withdrawn      %s -> %s" % (record, successor))
            _print("                the decision stays in the store, with this withdrawal on %s" % decision["at"])
            _print("                because: %s" % str(args.settle_why or "").strip())
            _print("                withdrawn by: %s"
                   % (str(getattr(args, "settle_by", "") or "").strip()
                      or "(not recorded -- pass --settle-by NAME)"))
            waiting = [row for row in replacement_links(history(root, include_retired=True),
                                                        load_settlements(root))
                       if record in {str(row.get("claimant") or ""), str(row.get("round") or "")}]
            if waiting:
                _print("                the contradiction it settled is back: `--audit` charges it again until a new "
                       "decision is taken (boundary 292)")
            else:
                _print("                no contradiction is waiting on that pair: the decision simply stops applying")
        if len(wanted) > 1:
            _print("withdrawn      %d of %d decision(s), %d already withdrawn, %d refused"
                   % (len(done), len(wanted), skipped, refused))
        _print_repair_delta(record_repair(root, "settle-drop", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"withdrawn": done, "drop_refused": refused, "drop_skipped": skipped},
                keys=PAYLOAD_KEYS["--settle-drop"]):
            return 2
        return 2 if refused else 0

    if getattr(args, "settle_restore_file", None):
        # Round 129: the sibling of `--settle-reaffirm-file`. Round 128 groups the withdrawal debt by stream file and
        # printed a command that paid one pair, so the file grouping bought the reader nothing (boundary 301). The
        # pairs are resolved from the same plan the page prints (boundary 283), and the walk itself is the single-pair
        # branch below: one reason, per-pair blocks, a refused pair that does not stop the rest, one JSON list.
        target = str(args.settle_restore_file or "").strip()
        if not target:
            _print("--settle-restore-file takes the stream file the withdrawn decisions are about, for example "
                   "`--settle-restore-file data/smd/stream-real.json`")
            return 2
        try:
            plan_rows = withdrawal_plan(history(root, include_retired=True), load_settlements(root))
        except (OSError, ValueError) as error:
            _print("cannot read the withdrawal plan: %s" % error)
            return 2
        match = [row for row in plan_rows if same_stream_path(str(row.get("origin") or ""), target)]
        pairs = ["%s=%s" % (str(entry.get("record") or ""), str(entry.get("successor") or ""))
                 for entry in (match[0].get("records") or [])] if match else []
        if not pairs:
            if match and int(match[0].get("unpayable") or 0):
                _print("%d withdrawal(s) on %s cannot be taken up again: the records name other successors -- "
                       "`brian evolve --settle-drop-file %s --settle-why TEXT`"
                       % (int(match[0].get("unpayable") or 0), match[0].get("origin") or target, target))
                return 2
            _print("no unanswered withdrawal is recorded against %s: `brian evolve --campaign` lists the files "
                   "that hold one" % target)
            return 2
        if match and int(match[0].get("unpayable") or 0):
            leaving = int(match[0].get("unpayable") or 0)
            _print("leaving        %d withdrawal(s) no restore can pay: the records name other successors -- "
                   "`brian evolve --settle-drop-file %s --settle-why TEXT`"
                   % (leaving, match[0].get("origin") or target))
        dry = bool(getattr(args, "dry_run", False))
        _print("%s  %d decision(s) on %s in one %s"
               % ("would take up" if dry else "re-taking     ", len(pairs), match[0].get("origin") or target,
                  "dry run: nothing was recorded" if dry else "command"))
        if dry:
            by_record = {str(entry.get("record") or ""): entry for entry in (match[0].get("records") or [])}
            for pair in pairs:
                entry = by_record.get(pair.split("=", 1)[0]) or {}
                _print("  %s  (withdrawn %s, %.1f day(s) ago)"
                       % (pair, str(entry.get("at") or "?"), float(entry.get("age_days") or 0.0)))
            _print("nothing was written; afterwards that file would owe no unanswered withdrawal. Drop --dry-run to "
                   "take them up again (boundary 304).")
            _print("no payload      a preview writes nothing, not even the file --json-out names (boundary 304).")
            _print_after_preview(root, pairs, kind="restore", origin=str(match[0].get("origin") or target))
            return 0
        inner = argparse.Namespace(**vars(args))
        inner.settle_restore_file = None
        inner.settle_restore = pairs
        return cmd_evolve(inner, settings)

    if getattr(args, "settle_restore", None):
        # Round 126: round 125 made "this decision is wrong" recordable and left the operator one command short.
        # `--settle-link` refuses a withdrawn pair and `--settle-reaffirm` refuses it too, so the contradiction a
        # person had correctly withdrawn could only be settled by editing the store by hand. This records the new
        # decision: the withdrawal stays, the re-decision says what is different, and the pair is decided again.
        # Round 129: several pairs in one command, so the file form above has a body to reuse (boundary 284).
        raw = args.settle_restore
        pairs = [str(item) for item in (raw if isinstance(raw, (list, tuple)) else [raw]) if str(item).strip()]
        if not pairs:
            _print("--settle-restore takes RECORD=SUCCESSOR, for example "
                   "`--settle-restore round-old.json=round-new.json`")
            return 2
        bad = [pair for pair in pairs if "=" not in pair]
        if bad:
            _print("--settle-restore takes RECORD=SUCCESSOR, and %s has no = in it" % bad[0])
            return 2
        plan_before = repair_plan(root)
        done: List[Dict[str, Any]] = []
        refused = 0
        for pair in pairs:
            record, successor = pair.split("=", 1)
            record, successor = record.strip(), successor.strip()
            try:
                decision = restore_settlement(root, record, successor, why=str(args.settle_why or ""),
                                              by=str(getattr(args, "settle_by", "") or ""))
            except (OSError, ValueError) as error:
                _print("cannot take that decision up again: %s" % error)
                refused += 1
                continue
            done.append(decision)
            if not decision["changed"]:
                _print("taken up again  %s -> %s was already taken up again on %s; nothing to change"
                       % (record, successor, decision["at"]))
                continue
            _print("taken up again  %s -> %s" % (record, successor))
            _print("                the decision stands again from %s, and the withdrawal stays on record"
                   % decision["at"])
            _print("                because: %s" % str(args.settle_why or "").strip())
            _print("                restored by: %s"
                   % (str(getattr(args, "settle_by", "") or "").strip()
                      or "(not recorded -- pass --settle-by NAME)"))
            # Round 126: a decision is exactly the thing allowed to silence a contradiction, so the page must say
            # that this one does -- and name what would undo it again if it turns out wrong too (boundary 294).
            _print("                the contradiction it settles is muted while it stands: `--audit` stops "
                   "charging that pair, and `--settle-drop` takes it back if this decision turns out wrong too "
                   "(boundary 294)")
        changed = sum(1 for item in done if item.get("changed"))
        if len(pairs) > 1:
            _print("re-taken       %d of %d decision(s), %d was already standing, %d refused"
                   % (changed, len(pairs), len(done) - changed, refused))
        if done:
            _print_repair_delta(record_repair(root, "settle-restore", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"restored": done, "restore_refused": refused},
                keys=PAYLOAD_KEYS["--settle-restore"]):
            return 2
        return 2 if refused else 0

    if getattr(args, "settle_reaffirm", None):
        # Round 119: round 118 can say the books moved under a decision but not re-take it, so the decision stayed
        # stale for ever and the gate could never charge it. This branch re-reads today's books and records what
        # it saw -- the claims standing on that successor now -- because a decision that does not name what it
        # covered is exactly the defect round 118 exists to catch (boundary 274).
        # Round 120: several pairs in one command. The reason belongs to the action ("I read today's books"); what
        # each decision covered belongs to the pair, and typing the same reason eleven times is how it drifts.
        raw = args.settle_reaffirm
        pairs = [str(item) for item in (raw if isinstance(raw, (list, tuple)) else [raw]) if str(item).strip()]
        if not pairs:
            _print("--settle-reaffirm takes RECORD=SUCCESSOR, for example "
                   "`--settle-reaffirm round-old.json=round-new.json`")
            return 2
        bad = [pair for pair in pairs if "=" not in pair]
        if bad:
            _print("--settle-reaffirm takes RECORD=SUCCESSOR, and %s has no = in it" % bad[0])
            return 2
        plan_before = repair_plan(root)
        done: List[Dict[str, Any]] = []
        refused = 0
        for pair in pairs:
            record, successor = pair.split("=", 1)
            record, successor = record.strip(), successor.strip()
            seen = sorted({str(row.get("round") or "") for row in history(root, include_retired=True)
                           if str((row.get("retired") or {}).get("re_measured_as") or "") == successor
                           and str(row.get("round") or "") != record})
            # Round 120: what the books looked like *before* this call. Asking afterwards would always find them
            # unchanged -- the call itself is what covers the claims (boundary 276).
            stored = load_settlements(root)
            moment = settlement_moment(stored, record, successor)
            moved_before = [row for row in moved_settlements(history(root, include_retired=True), stored)
                            if str(row.get("record") or "") == record
                            and str(row.get("successor") or "") == successor]
            try:
                again = reaffirm_link(root, record, successor, why=str(args.settle_why or ""),
                                      by=str(getattr(args, "settle_by", "") or ""), claimants=seen)
            except (OSError, ValueError) as error:
                # One refused pair must not stop the rest: the queue is a backlog, and a decision whose books moved
                # under it is a reason to skip that pair, not to abandon the walk (boundary 277).
                _print("cannot re-affirm that decision: %s" % error)
                refused += 1
                continue
            done.append(again)
            if not again["changed"]:
                _print("reaffirmed     %s -> %s was already re-taken on %s; nothing to change"
                       % (again["record"], again["successor"], again["at"]))
                continue
            _print("reaffirmed     %s -> %s" % (again["record"], again["successor"]))
            _print("                the decision keeps its first date; this re-affirmation is on %s"
                   % again["at"])
            _print("                what it covered: %s"
                   % (", ".join(seen) if seen else "no other claim stood on that successor"))
            if not moved_before:
                # Two facts, and the page must say which one it did: a re-take answers a claim that arrived after
                # the decision; a confirmation says the books have not moved under it since (boundary 276).
                _print("                nothing had moved since %s: this records a fresh confirmation, not a re-take"
                       % (moment or "the decision was taken"))
            _print("                because: %s" % str(args.settle_why or "").strip())
            _print("                re-affirmed by: %s"
                   % (str(getattr(args, "settle_by", "") or "").strip()
                      or "(not recorded -- pass --settle-by NAME)"))
        changed = sum(1 for item in done if item.get("changed"))
        if len(pairs) > 1:
            # The count is the headline when several pairs were asked for: one line per pair is a transcript, and a
            # transcript without a total makes the reader count.
            _print("re-taken       %d of %d decision(s), %d kept its date (already re-taken), %d refused"
                   % (changed, len(pairs), len(done) - changed, refused))
        if done:
            _print("                `--audit` stops reporting these as resting on books that have moved.")
            _print_repair_delta(record_repair(root, "settle-reaffirm", plan_before, repair_plan(root)))
        if getattr(args, "json_out", None) and not _write_payload(
                args.json_out, {"reaffirmed": done, "reaffirm_refused": refused,
                                "reaffirm_skipped": int(getattr(args, "settle_reaffirm_skipped", 0) or 0)},
                keys=PAYLOAD_KEYS["--settle-reaffirm"]):
            return 2
        return 2 if refused else 0

    if getattr(args, "repair_links", False):
        # Round 83: a `legacy` end is repaired by copying the retirement's claim into the successor record.
        # Without `--apply` this only prints what it would write: a command that edits recorded history has
        # to be able to show the edit first (boundary 175).
        rounds = history(root, include_retired=True)
        plan_before = repair_plan(root)
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
            # Round 111: the same before/after the other repair writes, because "did it help" is the same question.
            _print_repair_delta(record_repair(root, "repair-links", plan_before, repair_plan(root)))
            _print("")
            _print("a repaired end records `%s`, so the two ends agree by copying; --audit counts these"
                   % BACKFILL_KEY)
            _print("separately and does not treat them as independent confirmation.")
        json_out = getattr(args, "json_out", None)
        if json_out and not _write_payload(json_out, {"repairs": rows},
                                           keys=PAYLOAD_KEYS["--repair-links --apply"]):
            return 2
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
        # Round 101: the queue as an action. `--from-queue N` measures the first N promises that hold no verdict
        # on the file they were born with, each on that file, in the queue's order. Every one of them is a check
        # the page already named -- the point is that nobody has to retype a version label or a path, because a
        # retyped path is how a check lands on the substitute (round 99, boundary 218).
        # Round 102: `None` means "this command was not asked to walk the queue", `0` means "walk it only in
        # imagination" -- a plan is the first thing a batch command should be able to show (boundary 175).
        from_queued = getattr(args, "from_queue", None)
        from_queue = int(from_queued or 0)
        if from_queued is not None and (args.verify_withheld or getattr(args, "withheld_stream", None)):
            _print("--from-queue runs the origin queue; --verify-withheld re-measures refusals. "
                   "Pass one, not both: they answer different questions about different records.")
            return 2
        if from_queued is not None:
            if int(getattr(args, "verify_version", 0) or 0):
                _print("--from-queue already names the versions to check; drop --verify-version so the queue "
                       "decides which promises are measured.")
                return 2
            try:
                queued = origin_queue(root)
            except OSError as error:
                _print("cannot read %s: %s" % (root / EVOLVE_DIR, error))
                return 2
            runnable = [row for row in queued if row.get("origin_exists")]
            skipped = len(queued) - len(runnable)
            skipped_origins = [str(item.get("origin") or "") for item in queued if not item.get("origin_exists")]
            # Round 104: a record whose last walk was refused by a contradictory ledger will be refused the same way
            # now, because nothing about the books has moved. It is listed and counted, not measured again -- unless
            # the caller has just repaired the ledger, which `--retry-blocked` says.
            blocked = {} if getattr(args, "retry_blocked", False) else blocked_versions(root)
            # Round 109: one history for both notices -- the "still blocked" line and the plan's marks -- so the two
            # cannot disagree about which walk refused what.
            walked_history = refusal_history(root)
            held_back = [row for row in runnable if int(row.get("version") or 0) in blocked]
            runnable = [row for row in runnable if int(row.get("version") or 0) not in blocked]
            if held_back:
                _print("still blocked    %d promise(s) ended on a build defect last time and nothing has changed "
                       "since: %s" % (len(held_back),
                                      ", ".join("v%03d (%s)"
                                                % (int(row.get("version") or 0),
                                                   _refusal_phrase(walked_history, int(row.get("version") or 0),
                                                                   blocked))
                                                for row in held_back)))
                _print("                 fix the books, or pass --retry-blocked to measure them again anyway")
            if not runnable:
                # Round 104: two different reasons leave nothing to measure, and naming the wrong one sends the reader
                # to look for a file that is sitting right there.
                if held_back:
                    _print("no promise in the queue is left to measure: %d record(s) still owe a check where they "
                           "were made, and the last walk's refusal still stands (fix the books, or pass "
                           "--retry-blocked)." % len(held_back))
                else:
                    _print("no promise in the queue can be measured here: %d record(s) hold no verdict on the file "
                           "they were born with, and none of those files is in this checkout." % len(queued))
                _print_repair_plan(repair_plan(root))
                # A walk that measured nothing is still a walk: it is the observation behind "nobody has measured
                # this", and leaving it out would let a queue that is stuck read as a queue nobody visits.
                empty_walk = {"limit": from_queue, "runnable": 0, "measured": 0, "skipped": skipped,
                              "skipped_origins": skipped_origins, "cleared": 0, "worst": 0,
                              "stopped_at": 0, "attempted": []}
                try:
                    empty_entry = record_queue_run(root, empty_walk)
                except OSError:
                    empty_entry = empty_walk
                # Round 142: a walk that measured nothing is still a walk, and what it hands over is the record it
                # just wrote -- the same numbers the campaign page reads (boundary 340).
                if getattr(args, "json_out", None):
                    _write_payload(args.json_out, {"store": _walk_store(root, empty_entry),
                                                   "walks": [empty_entry]},
                                   keys=PAYLOAD_KEYS["--from-queue (a walk that measured nothing)"])
                return 0
            if from_queued is not None and not from_queue:
                # Round 102: a plan, not a measurement. It lists exactly the records `--from-queue N` would walk, in
                # the queue's order, so the caller can read the shape before spending anything.
                chosen = runnable
                _print("from queue       %d of %d runnable promise(s) hold no verdict on the file they were born "
                       "with; --from-queue 0 only lists them" % (len(chosen), len(runnable)))
                # Round 109: the plan carries each promise's refusal history, so "is it worth a round to repair the
                # books" is a question this page can answer -- it used to take the campaign page as well.
                for row in chosen:
                    version = int(row.get("version") or 0)
                    seen = walked_history.get(version) or {}
                    mark = ""
                    if int(seen.get("count") or 0):
                        mark = "  [refused by the books in %s%s]" % (
                            walks_phrase([int(item) for item in seen.get("refused_walks") or []]) or "an unnamed walk",
                            ", and the refusal still stands" if seen.get("standing")
                            else ", since repaired")
                    _print("  v%03d  <-  %s  (never measured where this promise was made)%s"
                           % (version, str(row.get("origin") or ""), mark))
                if skipped:
                    _print("                 %d left out: the file they were born with is not in this checkout"
                           % skipped)
                _print_repair_plan(repair_plan(root))
                _print("")
                _print("nothing was measured. Run the same command with --from-queue N to measure the first N of "
                       "them on the files they were born with.")
                if getattr(args, "json_out", None):
                    # Round 142: a plan records no walk, so there is nothing to hand over -- and saying that is the
                    # difference between "no file" and "the flag was ignored" (boundary 336).
                    _print("no payload      a plan measures nothing, so there is no walk to hand over (boundary 339).")
                return 0
            chosen = runnable[:max(1, from_queue)]
            _print("from queue       %d of %d runnable promise(s) hold no verdict on the file they were born "
                   "with; measuring each on its own origin" % (len(chosen), len(runnable)))
            if skipped:
                _print("                 %d left out: the file they were born with is not in this checkout"
                       % skipped)
            # Round 103: count the clearings the queue itself writes, so the run record can separate "measured
            # three promises" from "answered three disputes".
            queued_before = 0
            try:
                queued_before = sum(1 for item in in_force_history(root)[0] if item.get("queued") is True)
            except OSError:
                queued_before = 0
            worst = 0
            measured_now: List[int] = []
            codes: Dict[int, int] = {}
            # Round 106: what refused each version, recorded with the walk so a later one can compare the sentence.
            refused: Dict[int, str] = {}
            stopped_at = 0
            remaining: List[int] = []
            for index, row in enumerate(chosen):
                version = int(row.get("version") or 0)
                origin = str(row.get("origin") or "")
                _print("")
                _print("---- v%03d  <-  %s  (never measured where this promise was made)" % (version, origin))
                inner = argparse.Namespace(**vars(args))
                inner.from_queue = None
                # Round 102: the inner check is told it was started by the queue, so the clearing it writes says so.
                inner.queued_run = True
                inner.verify_version = version
                # The file is the point: it is the record's own origin, not the record's current path and not
                # whatever `--stream-file` happened to say.
                inner.stream_file = origin
                # Round 142: the inner record writes no payload of its own -- the walk writes one for all of them,
                # below. Nulling it here is what makes that one payload possible, so it is no longer silent work.
                inner.json_out = None
                code = cmd_evolve(inner, settings)
                worst = max(worst, code)
                measured_now.append(version)
                codes[version] = code
                if code == 2:
                    block = provenance_block(root, version)
                    refused[version] = str(block[1]) if block else ""
                if worst == 2:
                    # Round 103: exit 2 means the books for this record contradict each other, and that is not a
                    # defect the next promise in the queue can measure away. Stop, name what was left, and let the
                    # caller fix the books first.
                    stopped_at = version
                    remaining = [int(item.get("version") or 0) for item in chosen[index + 1:]]
                    break
            cleared_here = 0
            try:
                cleared_here = sum(1 for item in in_force_history(root)[0]
                                   if item.get("queued") is True) - queued_before
            except OSError:
                cleared_here = 0
            walk = {
                "limit": from_queue,
                "runnable": len(runnable),
                "measured": len(measured_now),
                "skipped": skipped,
                "skipped_origins": [str(item.get("origin") or "") for item in queued
                                    if not item.get("origin_exists")],
                "cleared": max(0, cleared_here),
                "worst": worst,
                "stopped_at": stopped_at,
                # Round 106: the refusal carries its reason, so a later walk can tell a refusal that still
                # stands from one the ledger has already answered.
                "attempted": [{"version": version, "code": codes.get(version, 0),
                               "why": refused.get(version, "")} for version in measured_now],
            }
            try:
                walk_entry = record_queue_run(root, walk)
            except OSError:
                walk_entry = walk
            # Round 142: the walk used to null `json_out` for every inner record (one file per record would overwrite
            # itself) and hand the caller nothing. One record for the whole walk is what it owes, and it is the record
            # the store just kept (boundary 340).
            if getattr(args, "json_out", None) and not _write_payload(
                    args.json_out, {"store": _walk_store(root, walk_entry), "walks": [walk_entry]},
                keys=PAYLOAD_KEYS["--from-queue (a walk)"]):
                worst = max(worst, 2)
            _print("")
            if stopped_at:
                # Round 103: a walk that ends on a build defect must say what it left behind -- and when the answer
                # is "nothing", it says that instead of counting zero objects (boundary 226's subject, one line over).
                if remaining:
                    _print("stopped          v%03d exited 2, and a contradiction is not something the next "
                           "promise measures away. %d promise(s) were left in the queue: %s"
                           % (stopped_at, len(remaining),
                              ", ".join("v%03d" % item for item in remaining)))
                else:
                    _print("stopped          v%03d exited 2, and no other promise was waiting behind it: a "
                           "contradiction is not something the next promise measures away" % stopped_at)
            _print("%d promise(s) measured on the file they were born with. A copied end is measured, never "
                   "applied; a contradiction is still refused." % len(measured_now))
            return worst
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
        substituted_from = ""
        if args.stream_file and not Path(args.stream_file).is_file():
            # Round 91 said the file is missing. Round 92 asks the next question: does the record name one that
            # is here? The promise records the file it was measured on, so the check can run there -- and must
            # say that it did, because a verdict is about a file (boundary 197). Only when every version to
            # check agrees on one recorded file: choosing between several would decide which promise gets
            # graded, which round 47 already refuses to do by convenience.
            named = str(args.stream_file)
            known = [item for item in load_promotions(root)
                     if item.version == wanted or (not wanted and item.due(days=args.expiry_days))]
            recorded_path, unrecorded, _by_path = _promotion_stream(known) if known else ("", [], {})
            usable = [recorded_path] if recorded_path and Path(recorded_path).is_file() else []
            if len(usable) == 1 and not unrecorded:
                _print("stream file    %s  (substituted: %s does not exist in this checkout)"
                       % (usable[0], named))
                substituted_from = named
                args.stream_file = usable[0]
            else:
                _print("cannot read %s: no such file" % named)
                # The diagnostic names the reason substitution failed, because "nothing to stand in" has three
                # different fixes: pass a file (no version records one), restore a file (the recorded one is
                # missing too), or resolve which promise is being graded (versions disagree or say nothing).
                recorded = sorted({str(item.stream_path or "") for item in load_promotions(root)} - {""})
                if unrecorded:
                    _print("the recorded file cannot stand in: %d version(s) to check do not say which file "
                           "they were measured on: %s"
                           % (len(unrecorded), ", ".join("v%03d" % item.version for item in unrecorded)))
                elif len(recorded) > 1:
                    _print("the versions record different files, and picking one would decide which promise "
                           "gets graded: %s" % ", ".join(recorded))
                elif recorded:
                    _print("the file the promise was measured on is also missing: %s" % recorded[0])
                else:
                    _print("no version records which file it was measured on, so there is nothing to stand in")
                _print("a measurement on a file this checkout cannot open answers nothing, so nothing was cleared:")
                _print("pass --stream-file naming a file that still exists, or restore the one above.")
                return 2
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
        measured_versions: List[Tuple[int, str, str, str, bool]] = []
        for promotion in promotions:
            # Round 95: round 94 put the repair on the version table; the check has to say it too, and say it
            # before measuring, because the number it records (`stream_path_moves`) describes where it stood.
            path_moves = promotion.path_moves
            checked_here = str(getattr(args, "stream_file", None) or "")
            # Round 98: the verdict about to be written says where it stands and where the evidence stood
            # before it, so a reader never has to infer either from the order of the file's keys.
            prior_rows = [row for row in prior_evidence(promotion) if row.get("path") or row.get("at")]
            prior_paths = prior_rows
            # Round 99: what the evidence looked like *before* this check, so the verdict about to be written
            # can say whether it is the first one ever taken where the promise was made (boundary 213).
            prior_split = evidence_split(prior_rows, promotion.path_origin)
            on_original = bool(path_moves) and same_stream_path(checked_here, promotion.path_origin)
            # Round 97: after a return the record names the original again, so "origin -> current" prints the
            # same file twice. The chain of the trip is the honest description, and whether the trip is closed
            # decides which sentence about the check is true.
            back_home = bool(path_moves) and same_stream_path(promotion.path_origin, promotion.path_current)
            if path_moves:
                chain = " -> ".join([promotion.path_origin or "(unknown)"] +
                                    [str(move.get("to") or "(unknown)")
                                     for move in promotion.stream_path_history])
                _print("  v%03d  %s  path rewritten %d time(s) by hand%s: %s"
                       % (promotion.version, promotion.change, path_moves,
                          " and back on the original" if back_home else "", chain))
                if on_original and back_home:
                    # Round 97: the record already points at the file this check is standing on. Saying it
                    # "still points at the repair" would contradict the line above it (boundary 212).
                    # Round 99: "the verdicts it already holds were taken on the substitute" is only true when
                    # none of them was taken here. A record can hold both kinds, and then the count is the fact.
                    if prior_split["original"]:
                        _print("        this check measures the file the record names again after the trip back; "
                               "%d of the %d verdict(s) it already holds were taken here, %d elsewhere "
                               "(boundary 212)"
                               % (prior_split["original"], prior_split["total"],
                                  prior_split["elsewhere"] + prior_split["unplaced"]))
                    else:
                        _print("        this check measures the file the record names again after the trip back; "
                               "the verdicts it already holds were taken on the substitute (boundary 212)")
                elif on_original:
                    # Round 96: the same page must not claim the check is standing on the repair when it is
                    # standing on the file the promise was born with (boundary 190: the sentence and the
                    # mechanism move together).
                    _print("        this check measures the original file the promise first named, while the "
                           "record still points at the repair (boundary 209)")
                else:
                    _print("        this check measures the repaired location, not the file the promise first "
                           "named (boundary 203)")
                if on_original and prior_split["total"] and not prior_split["original"]:
                    _print("        this is the first verdict taken on the file the promise was born with: all "
                           "%d it holds were measured elsewhere (boundary 213)" % prior_split["total"])
                elif not on_original and not prior_split["original"] and promotion.path_origin \
                        and (root / promotion.path_origin).is_file():
                    # Round 100: the queue, one record at a time. This check will not touch the place the
                    # promise was made, and that place exists here, so the page names what measuring there
                    # would buy instead of quietly recording another verdict somewhere else (boundary 218).
                    _print("        this promise has never been measured on the file it was born with (%s), "
                           "which exists here:" % promotion.path_origin)
                    _print("        measuring there is what would test the promise where it was made "
                           "(boundary 218)")
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
                    # Even a check that could not run stands on a location: the repair is a fact about the
                    # record, not about the verdict (boundary 205).
                    "stream_path_moves": path_moves,
                    "on_original_path": on_original,
                    # Round 98: a verdict is about a file, so it names the file it stood on and what the
                    # record held before it. Without this the pages can only count "on the repair" twice over.
                    "checked_path": checked_here,
                    "prior_evidence_paths": prior_paths,
                    "prior_evidence_split": prior_split,
                })
                _print("  v%03d  %s  unmeasurable" % (promotion.version, promotion.change))
                continue
            measurement = measure(settings, promotion.candidate, streams, pool,
                                  budget=rules.fpr_budget)
            verdict, reason = claim_check(promotion.claim, measurement, rules,
                                          recorded_stream=promotion.stream,
                                          checked_stream=checked_stream)
            payload = {"checked_at": _stamp(), "stream": source, "verdict": verdict,
                       "reason": reason, "measurement": measurement.to_dict(),
                       "stream_path_moves": path_moves, "on_original_path": on_original,
                       "checked_path": checked_here,
                       "prior_evidence_paths": prior_paths,
                       "prior_evidence_split": prior_split,
                       "first_original_measurement": bool(on_original and prior_split["total"]
                                                          and not prior_split["original"])}
            if copied:
                payload["copied_end"] = {"kind": block[0], "detail": block[1]}
            if verdict == FAILED:
                # The promise stopped paying out, so the configuration stops being the deployed one.
                # The file is kept, because a retracted claim is part of the record, not a hole in it.
                # Retracting is not stacking, so a copied end does not block it.
                revert_promotion(root, promotion, domain=domain)
                write_verification(root, promotion, payload, reverted_at=_stamp())
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason,
                                          bool(payload.get("first_original_measurement"))))
                _print("  v%03d  %s  FAILED and reverted: %s" % (promotion.version,
                                                                 promotion.change, reason))
            elif verdict == VERIFIED:
                write_verification(root, promotion, payload)
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason,
                                          bool(payload.get("first_original_measurement"))))
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
                measured_versions.append((promotion.version, payload["checked_at"], verdict, reason, False))
                _print("  v%03d  %s  unmeasurable: %s" % (promotion.version, promotion.change, reason))
                hint = rescale_hint(promotion, promotion.stream, checked_stream)
                if hint:
                    _print("        hint  %s" % hint)
                note = shape_note(payload)
                if note:
                    _print(note)
        cleared_now: List[Dict[str, Any]] = []
        # Round 90: the entry names the file this run measured, because that is the file a reader has to
        # open to audit the verdict. `--stream-file` may be empty (the demo stream), and an empty path is
        # recorded as empty rather than guessed.
        checked_path = str(getattr(args, "stream_file", None) or "")
        for version, checked_at, verdict, why, first_original in measured_versions:
            cleared_now.extend(record_clearing(root, version, now=checked_at, verdict=verdict,
                                               reason="measured again by --verify",
                                               tested=verdict != UNMEASURABLE,
                                               attempt_reason=why, stream_path=checked_path,
                                               substituted_from=substituted_from,
                                               first_original=first_original,
                                               queued=bool(getattr(args, "queued_run", False))))
        answered = [entry for entry in cleared_now if entry.get("tested") is not False]
        attempted = [entry for entry in cleared_now if entry.get("tested") is False]
        if answered:
            _print("")
            _print("cleared         %d in-force observation(s) were answered by this check:" % len(answered))
            for entry in answered:
                age = entry.get("age_days")
                _print("  %-16s v%03d  %-13s stood %s, answered on %s"
                       % (entry["domain"], int(entry["version"]), entry["kind"],
                          "an unknown time" if age is None else "%.1f day(s)" % float(age),
                          entry.get("stream_path") or "(unnamed stream)"))
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

    if args.json_out and not _write_payload(args.json_out, {
            "summary": summary, "windows_judged": judged,
            "rca_report_ids": [r.rca_id for r in reports]}, keys=PAYLOAD_KEYS["run"]):
        return 2
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
        if not _write_payload(args.json_out, payload):
            return 2
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
    if args.summary_out and not _write_payload(args.summary_out, summary):
        return 2
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
    evolve.add_argument("--max-withdrawn-days", type=float, default=None, metavar="DAYS",
                        help="with --strict: charge decisions withdrawn for longer than DAYS with no new decision")
    evolve.add_argument("--max-withdrawn-per-file", type=int, default=None, metavar="N",
                        help="with --strict: charge when one stream file holds more than N unanswered withdrawals")
    evolve.add_argument("--max-unexplained-days", type=float, default=None, metavar="DAYS",
                        help="with --strict: fail when a decision has had no reason and no note for longer "
                             "than DAYS (default off)")
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
    evolve.add_argument("--set-stream-path", metavar="VERSION=FILE", default=None,
                        help="record that a version was measured on FILE, when its record names a file this "
                             "checkout does not have; writes stream_path_history")
    evolve.add_argument("--settle-link", metavar="RECORD=SUCCESSOR", default=None,
                        help="record that RECORD's measurement went into SUCCESSOR, so `--audit` stops charging "
                             "that pair as a contradiction; a decision, not a measurement")
    evolve.add_argument("--settle-why", metavar="TEXT", default="",
                        help="with --settle-link: why the pair was decided; required for a new settlement")
    # Round 113: who decided. Optional on purpose -- an empty decider is printed as "not recorded" rather than
    # filled with the local account, which is shared by whoever is at the keyboard (boundary 254).
    evolve.add_argument("--settle-note", metavar="RECORD=SUCCESSOR", default=None,
                        help="append the explanation a recorded settlement lacked: the decision keeps its date "
                             "and successor; needs --settle-why")
    evolve.add_argument("--plan", default="", metavar="STEPS",
                        help="with --campaign --what-if-records: run the batch commands in this order and print "
                             "each step's books (restore, drop, retake)")
    evolve.add_argument("--what-if-records", default=None, metavar="STREAM",
                        help="with --campaign: print what that stream file would still owe if its unresolved "
                             "decisions were paid off now (a recomputation; nothing is written)")
    evolve.add_argument("--dry-run", action="store_true",
                        help="with %s: print what the batch command would do, and write nothing"
                             % previews_sentence())
    evolve.add_argument("--settle-reaffirm-file", default=None, metavar="STREAM",
                        help="re-take every stale decision on one stream file in a single command, with the same "
                             "--settle-why and --settle-by; the pairs come from the plan `--campaign` prints")
    evolve.add_argument("--settle-kind", default=None,
                        help="with --settle-reaffirm-file: `never` pays only the decisions nobody has read the "
                             "books for, `again` only the ones the books moved under after the last reading")
    evolve.add_argument("--settle-drop-file", default=None, metavar="STREAM",
                        help="withdraw every decision on STREAM that cannot be re-taken in one command, resolved "
                             "from the same plan --campaign prints; needs --settle-why")
    evolve.add_argument("--settle-reanchor", default=None, metavar="RECORD", nargs="+",
                        help="move one or more decisions to the successor their record now names, keeping the "
                             "earlier target in successor_history")
    evolve.add_argument("--settle-reanchor-file", default=None, metavar="STREAM",
                        help="re-anchor every decision on that stream file that still names a successor its record "
                             "has left")
    evolve.add_argument("--settle-drop", metavar="RECORD=SUCCESSOR", nargs="+", default=None,
                        help="withdraw a recorded decision: it keeps its date and gains the reason it stopped "
                             "standing, and the contradiction it settled is charged again; needs --settle-why")
    evolve.add_argument("--settle-restore", metavar="RECORD=SUCCESSOR", nargs="+", default=None,
                        help="take a withdrawn decision up again as a new decision: the withdrawal stays on record, "
                             "and the pair is decided again from this moment; takes one or more "
                             "RECORD=SUCCESSOR pairs; needs --settle-why")
    evolve.add_argument("--settle-restore-file", default=None, metavar="STREAM",
                        help="take every unanswered withdrawal recorded against STREAM up again in one command, "
                             "resolved from the same plan --campaign prints; needs --settle-why")
    evolve.add_argument("--settle-reaffirm", metavar="RECORD=SUCCESSOR", nargs="+", default=None,
                        help="re-take decisions against today's books: keeps each first date, records the claims "
                             "it covered; takes one or more RECORD=SUCCESSOR pairs; needs --settle-why")
    evolve.add_argument("--max-moved-per-file", type=int, default=None, metavar="N",
                        help="with --strict: charge exit 1 when any one stream file holds more than N stale "
                             "decisions, which is the unit a reader pays in")
    evolve.add_argument("--max-moved", type=int, default=None, metavar="N",
                        help="with --strict: fail when more than N decisions rest on books that have moved and "
                             "could be re-taken with --settle-reaffirm")
    evolve.add_argument("--settle-by", default="", metavar="NAME",
                        help="with --settle-link: who made the decision (recorded in settlements.json)")
    evolve.add_argument("--repair-links", action="store_true",
                        help="write the missing end of a legacy replacement link; plan only without --apply")
    evolve.add_argument("--history", action="store_true",
                        help="print every round recorded so far and exit")
    evolve.add_argument("--rejudge", default="",
                        help="re-decide a recorded round file under today's rules and exit")
    evolve.add_argument("--audit", action="store_true",
                        help="re-decide every recorded round under today's rules and exit")
    evolve.add_argument("--claim-round-file", dest="claim_round_file", nargs="+", metavar="ROUND=FILE",
                        help="record the file a round measured, for a round whose record names none; the file "
                             "must agree with the geometry the round recorded unless --force-claim")
    evolve.add_argument("--backfill-claims", dest="backfill_claims", action="store_true",
                        help="complete the claim chain (claim_origin / claim_since / claim_hops) on the records "
                             "written before it existed; every entry it touches says backfilled, and a date it "
                             "cannot derive says unknown instead of guessing")
    evolve.add_argument("--force-claim", dest="force_claim", action="store_true",
                        help="with --claim-round-file: record a file the round's geometry does not point at")
    evolve.add_argument("--claim-why", dest="claim_why", default="",
                        help="with --claim-round-file: why the record names this file, kept with the repair")
    evolve.add_argument("--backfill-walks", dest="backfill_walks", action="store_true",
                        help="write a walk for every payment the ledger records but the re-run store never saw "
                             "(rounds retired before the store existed; each entry says it was written after the "
                             "fact)")
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
    evolve.add_argument("--from-queue", type=int, default=None, metavar="N",
                        help="with --verify: measure the first N promises that hold no verdict on the file "
                             "they were born with, each on that file, in the queue's order")
    evolve.add_argument("--retry-blocked", action="store_true",
                        help="with --verify --from-queue: measure again the promises whose last walk was refused "
                             "by the ledger, instead of listing them as still blocked")
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
    evolve.add_argument("--check", action="store_true",
                        help="with --payload-writers: exit 2 when the page and the source disagree")
    evolve.add_argument("--payload-writers", dest="payload_writers", action="store_true",
                        help="list the commands that export a payload, and what the writer says when it cannot")
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
