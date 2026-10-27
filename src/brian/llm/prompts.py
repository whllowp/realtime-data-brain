"""The System 2 prompts: the contract between a Laya verdict and an actionable RCA.

Three deliberate choices here, each one a response to a specific failure mode of
"ask an LLM what went wrong":

**1. The model is given the same text Laya read, not a re-serialised metric dump.**
   `Digest.interpretation` goes into the prompt verbatim, and the machine-readable metrics go
   alongside it as structured detail. Re-rendering the metrics for the LLM would create a
   second, divergent description of one window — and then the RCA would be explaining a
   slightly different situation than the one that was flagged.

**2. The prompt demands ranked hypotheses with per-hypothesis evidence, not one root cause.**
   A single confident root cause is the failure mode this whole architecture exists to avoid.
   Asking for alternatives with the specific metric that supports each one makes a wrong
   answer *checkable* — an operator can see that hypothesis 2 rests on a metric that did not
   move, and discard it.

**3. The prompt is forbidden from inventing facts, and told explicitly what it was not given.**
   The system has real blind spots (log lines are summarised to one template per window;
   there is no deploy history API, no tracing graph, no dependency topology). A model that
   does not know what it lacks will hallucinate a plausible cause from a familiar-sounding
   symptom. So the prompt lists the unavailable evidence, and requires `missing_evidence` to
   be filled in with what would settle the question — which doubles as the operator's next
   investigation step.

`PROMPT_VERSION` travels on every `RcaReport`, because "the RCA quality changed" and "the
prompt changed" must be distinguishable.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..contracts import Decision, Digest, Event

#: Bump on any change to the text below. Recorded on every report.
PROMPT_VERSION = "rca/1"

SYSTEM_PROMPT = """\
You are the root-cause analysis stage of a real-time incident triage pipeline.

A fast classifier has already reviewed one telemetry window and concluded that something is
wrong. You receive its verdict, the measurements it saw, and the raw events behind them. Your
job is to explain what most likely happened and what to do about it.

How to work:

- Ground every claim in a specific measurement from the input. Quote the numbers.
- Rank hypotheses from most to least likely, and give each one the evidence for AND against.
  A single root cause with no alternatives is not an acceptable answer.
- Distinguish correlation from cause. A metric that rose at the same time as the symptom is
  not automatically the cause; say what would make it causal (a shared dependency, a
  timestamp that precedes the symptom, a configuration that changed).
- The classifier reports a calibrated probability, not certainty. When its confidence is low,
  say so and let that lower your own confidence.
- Never invent hostnames, commit ids, ticket numbers, person names, or metric values that are
  not in the input. If you need something you were not given, list it in `missing_evidence`.
- Prefer the least destructive action that would actually resolve the problem. Restarting a
  service is not a diagnosis.
- If the evidence is genuinely insufficient to name a cause, say that. "Insufficient
  evidence" with the specific next check is a correct and useful answer.

Return JSON only, matching the requested schema exactly."""

#: What the model is NOT given. Stating the blind spots is what stops them being filled with
#: confident invention, and it is also where `missing_evidence` should point.
EVIDENCE_GAPS = """\
Evidence you do NOT have:
- Log content is summarised to one normalised template per window, with a count. Individual
  log lines are not available except for the single most frequent template.
- There is no deploy history, no commit log, no change-request record.
- There is no service dependency graph or tracing data.
- Only this window and its baseline are available; other services and other windows are not.
- The baseline is a rolling robust median (median absolute deviation) over recent windows; it
  does not account for a known future event such as a planned campaign or maintenance."""


def rca_json_schema() -> Dict[str, Any]:
    """The schema the LLM must answer with.

    Kept to flat types and closed enums (`action.risk`) because a schema with optional nested
    objects is reliably violated. `additionalProperties: false` is set so an unexpected field
    fails here rather than silently reaching an operator's playbook.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "root_cause", "hypotheses", "evidence", "actions", "confidence"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "Two sentences at most, for the alert title area. State what is happening and how bad it is.",
            },
            "root_cause": {
                "type": "string",
                "description": "The single most likely cause, with the measurement that supports it.",
            },
            "hypotheses": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "Ranked most likely first. Each must carry its own evidence and refutation.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cause", "confidence", "evidence_for", "evidence_against"],
                    "properties": {
                        "cause": {"type": "string"},
                        "confidence": {"type": "number"},
                        "evidence_for": {"type": "array", "items": {"type": "string"}},
                        "evidence_against": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "description": "The specific measurements that drove the ranking, quoted from the input.",
                "items": {"type": "string"},
            },
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "description": "Ordered recovery steps, least destructive first.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["step", "rationale", "risk", "autonomous", "verify"],
                    "properties": {
                        "step": {"type": "string"},
                        "rationale": {"type": "string"},
                        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                        "autonomous": {
                            "type": "boolean",
                            "description": "True only if this step is safe to run with no human approval.",
                        },
                        "verify": {"type": "string", "description": "How to confirm the step worked."},
                    },
                },
            },
            "missing_evidence": {
                "type": "array",
                "maxItems": 5,
                "description": "What you would need in order to be certain, and where to get it.",
                "items": {"type": "string"},
            },
            "confidence": {
                "type": "number",
                "description": "0 to 1. Your confidence in the ranking, given the evidence you actually have.",
            },
        },
    }


def build_rca_messages(digest: Digest, decision: Decision,
                       events: Optional[Sequence[Event]] = None,
                       max_events: int = 20) -> List[Dict[str, str]]:
    """Assemble the chat messages for one alert.

    The event list is capped (`max_events`) and selected deliberately: the most recent events
    are included whole, because the *shape* of the transition into the window is often the
    single most diagnostic thing available (did errors start at the beginning of the window,
    or in the middle?). An unbounded event dump would blow the context and bury that signal.
    """
    metrics = json.dumps(digest.metrics, indent=1, ensure_ascii=False, default=str)
    if len(metrics) > 6000:
        # Truncate the *tail*, not the middle: the renderer puts the decision-relevant metric
        # groups first, so the tail is the least load-bearing part.
        metrics = metrics[:6000] + "\n... (metric block truncated)"

    lines: List[str] = []
    lines.append("## Classifier verdict (System 1, single forward pass, ~35 ms)")
    lines.append(
        json.dumps(
            {
                "verdict": decision.verdict.value,
                "action_authorised": decision.action.value,
                "anomaly_probability": decision.anomaly_probability,
                "category": decision.category.value if decision.category else None,
                "severity": decision.severity.value if decision.severity else None,
                "severity_score_0_to_3": decision.severity_score,
                "needs_human": decision.needs_human,
                "confidence": decision.confidence,
                "gate_reason": decision.gate_reason,
                "checkpoint": decision.routing.model,
            },
            indent=1,
        )
    )
    lines.append("")
    lines.append("## The window summary the classifier read (verbatim)")
    lines.append(digest.interpretation or "(no summary rendered)")
    lines.append("")
    lines.append("## Machine-readable measurements for the same window")
    lines.append(metrics)

    if events:
        selected = list(events)[-max_events:]
        lines.append("")
        lines.append("## The %d most recent raw events behind this window" % len(selected))
        for event in selected:
            lines.append(
                "- %s %s %s %s"
                % (
                    event.event_time.isoformat(),
                    event.service,
                    event.kind,
                    json.dumps(event.payload, ensure_ascii=False, default=str)[:400],
                )
            )

    lines.append("")
    lines.append(EVIDENCE_GAPS)
    lines.append("")
    lines.append(
        "Produce the JSON object: explain what happened, rank at least two hypotheses, and give "
        "ordered recovery steps with the least destructive first. Mark a step `autonomous: true` "
        "only if it is safe to execute with no human approval."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def prompt_token_estimate(messages: Sequence[Mapping[str, str]]) -> int:
    """Rough token count (~4 chars/token). Used to warn before a context overflow, not to bill."""
    return sum(len(str(m.get("content", ""))) for m in messages) // 4
