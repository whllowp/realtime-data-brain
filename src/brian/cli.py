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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bus import JournalBus, MemoryBus, open_bus
from .config import Settings, load_settings
from .contracts import Event, Lineage, dumps, loads
from .demo import DEFAULT_SEQUENCE, EXPECTED, LabeledWindow, flatten, generate
from .cost import format_ledger, ledger_from_traces
from .eval import Evaluator, format_report, gate_recommendation
from .evolve import (
    EVOLVE_DIR,
    EvolveRules,
    apply_overrides,
    audit,
    build_pool,
    catalogue,
    format_audit,
    format_rejudge,
    format_round,
    grade,
    history,
    label_free_cut,
    measure,
    persist,
    previously_rejected,
    reader_scores,
    rejudge,
    run_round,
)
from .laya.client import LayaClient, LayaError, LayaUnavailable
from .laya.gate import explain_thresholds
from .laya.qcatalog import CATALOG_VERSION, catalog_hash, describe as describe_catalog, validate_questions
from .llm.rca import RcaEngine
from .deploy import reload_capable
from .propose import Mode, Proposal, describe_proposal, format_proposal, propose, proposal_capable
from .promote import (
    FAILED,
    UNMEASURABLE,
    apply_promotion,
    claim_check,
    current_state,
    format_promotions,
    load_promotions,
    revert_promotion,
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


def cmd_evolve(args: argparse.Namespace, settings: Settings) -> int:
    """Run the improvement mechanism: propose, measure, select, and (only with --apply) integrate."""
    root = Path(".")
    directory = Path(EVOLVE_DIR)

    if args.history:
        rounds = history(root)
        if not rounds:
            _print("no rounds recorded yet in %s" % directory)
            return 0
        _print("=" * 78)
        _print("brian evolve - recorded rounds (oldest first)")
        _print("=" * 78)
        for index, record in enumerate(rounds, start=1):
            baseline_row = record.get("baseline") or {}
            measured = baseline_row.get("tokens_per_report")
            baseline = "n/a" if measured is None else "%.0f" % measured
            before_recall = baseline_row.get("recall_at_budget")
            baseline_recall = "n/a" if before_recall is None else "%.3f" % before_recall
            trials = record.get("trials") or []
            accepted = record.get("accepted") or []
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
            _print("%2d  %s  accepted=%-24s recall@b %s -> %s  tokens/report %5s -> %s%s"
                   % (index, record.get("finished_at") or record.get("started_at") or "?",
                      ",".join(accepted) or "-", baseline_recall, recall_after, baseline, after, mark))
        return 0

    if args.rejudge:
        path = Path(args.rejudge)
        if not path.exists():
            _print("no such round file: %s" % path)
            return 2
        rules = EvolveRules(min_gain=args.min_gain)
        try:
            result = rejudge(path, rules)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            _print("cannot rejudge %s: %s" % (path, error))
            return 2
        _print(format_rejudge(result))
        return 0

    if args.audit:
        rules = EvolveRules(min_gain=args.min_gain)
        try:
            result = audit(root, rules)
        except OSError as error:
            _print("cannot audit %s: %s" % (root / EVOLVE_DIR, error))
            return 2
        _print(format_audit(result))
        # The exit code is the part that makes this a check rather than a report: a rule change that
        # rewrites history should fail the build, or it will be noticed months later by whoever reads the
        # table last.
        return 1 if (args.strict and result.moved) else 0

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
        proposal = propose(rounds, mode=mode, source=source)
        if args.catalogue:
            proposal = Proposal(
                "catalogue",
                "forced by --catalogue: the fixed list, so the rule selection was not consulted",
                list(catalogue()), proposal.space, proposal.source)
        _print(format_proposal(proposal))
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
        promotions = [item for item in load_promotions(root)
                      if item.pending and (not wanted or item.version == wanted)]
        if not promotions:
            _print("no unverified promotions: every version file has already been re-checked.")
            return 0
        settings, streams, source, _overrides = _evolve_streams(args, settings)
        rules = EvolveRules(min_gain=args.min_gain)
        pool = build_pool(settings, streams)
        _print("verifying %d promotion(s) on %s" % (len(promotions), source))
        for promotion in promotions:
            if promotion.candidate is None:
                write_verification(root, promotion, {
                    "checked_at": _stamp(), "stream": source, "verdict": UNMEASURABLE,
                    "reason": "the version file does not name the change, so it cannot be re-measured",
                })
                _print("  v%03d  %s  unmeasurable" % (promotion.version, promotion.change))
                continue
            measurement = measure(settings, promotion.candidate, streams, pool,
                                  budget=rules.fpr_budget)
            verdict, reason = claim_check(promotion.claim, measurement, rules)
            payload = {"checked_at": _stamp(), "stream": source, "verdict": verdict,
                       "reason": reason, "measurement": measurement.to_dict()}
            if verdict == FAILED:
                # The promise stopped paying out, so the configuration stops being the deployed one.
                # The file is kept, because a retracted claim is part of the record, not a hole in it.
                revert_promotion(root, promotion)
                write_verification(root, promotion, payload, reverted_at=_stamp())
                _print("  v%03d  %s  FAILED and reverted: %s" % (promotion.version,
                                                                 promotion.change, reason))
            else:
                write_verification(root, promotion, payload)
                apply_promotion(root, promotion)
                _print("  v%03d  %s  %s" % (promotion.version, promotion.change, verdict))
        _print("")
        _print(format_promotions(load_promotions(root), current_state(root)))
        return 0

    settings, streams, source, base_overrides = _evolve_streams(args, settings)

    known = {candidate.name: candidate for candidate in catalogue()}
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
        proposal = propose(history(root), mode=mode, source=source) if not args.catalogue else None
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

    rules = EvolveRules(min_gain=args.min_gain)
    prior = previously_rejected(root, rules)
    if prior:
        _print("Previously rejected (re-judged under today's rules, so a retired rule cannot poison this):")
        for name, reason in sorted(prior.items()):
            _print("  %-22s %s" % (name, reason))
        _print("")

    totals = summarise(streams)
    for section, values in base_overrides.items():
        _print("base override   %s: %s" % (section, ", ".join(
            "%s=%r" % (key, value) for key, value in sorted(values.items()))))
    _print("evolving over %d recording(s), %d windows (%d scored, %d anomalous, %d events)"
           % (totals["streams"], totals["windows"], totals["scored"], totals["anomalous"],
              totals["events"]))
    _print("measuring baseline…")
    result = run_round(settings, streams, selected=selected, rules=rules, source=source,
                       proposal=proposal_record)
    _print("")
    _print(format_round(result))

    written = persist(root, result, write_versions=args.apply)
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
            applied = apply_promotion(root, fresh[0])
            _print("in force: %s" % ", ".join("%s=%s" % item for item in
                                             sorted(current_state(root)["env"].items())))
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
    evolve.add_argument("--apply", action="store_true",
                        help="also write a version file per accepted change "
                             "(the round itself is always recorded)")
    evolve.add_argument("--grade", action="store_true",
                        help="print the L1-L5 grading and exit")
    evolve.add_argument("--history", action="store_true",
                        help="print every round recorded so far and exit")
    evolve.add_argument("--rejudge", default="",
                        help="re-decide a recorded round file under today's rules and exit")
    evolve.add_argument("--audit", action="store_true",
                        help="re-decide every recorded round under today's rules and exit")
    evolve.add_argument("--verify", action="store_true",
                        help="re-measure every unverified promotion and revert the ones whose claim "
                             "no longer holds, then exit")
    evolve.add_argument("--verify-version", type=int, default=0,
                        help="with --verify: only that version number (default: everything unverified)")
    evolve.add_argument("--strict", action="store_true",
                        help="with --audit: exit 1 when any recorded verdict would be decided differently")
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
