"""Kafka is the memory: one place that names every stage of the reasoning chain.

Two rules make the trail *traceable* rather than merely *logged*:

1. **Append-only, never mutated.** Nothing in this system updates or deletes a record.
   A later contradiction is a new record that references the earlier one, so the history
   of what the system believed at each moment survives.
2. **Full lineage.** Every record carries the ids of the records it was derived from:

       evt_*  events   ->  dgst_*  digests  ->  dec_*  decisions  ->  rca/rpl and outcome
       (raw)              (features)            (Laya System 1)      (LLM System 2)

   Reading `lineage.upstream_ids` backwards from any outcome reconstructs exactly which
   raw events a recovery action was based on, and which model version judged them.

Topic naming is frozen in this module. Producers and consumers import the constant, so a
typo cannot silently create a second, parallel topic.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

#: Prefix for every topic the engine owns. Overridable so several environments can share
#: one Kafka cluster without colliding.
DEFAULT_PREFIX = "brian"


class Topic:
    """The topic catalogue. `name()` applies the configured prefix."""

    # --- raw layer: what actually happened, before any interpretation -------------
    #: Raw telemetry: metric samples, log lines, traces. The system of record.
    EVENTS = "events"
    #: Flink window digests: the deterministic feature snapshot handed to Laya.
    #: Stored so a decision can be re-derived (or re-judged by a new model) forever.
    FEATURES = "features"

    # --- decision layer: what Laya concluded, and why ---------------------------
    #: Laya's calibrated verdict per digest, with the exact questions asked.
    DECISIONS = "decisions"
    #: Promoted anomalies (a view of DECISIONS plus operator-facing routing).
    ALERTS = "alerts"

    # --- reasoning layer: what the LLM concluded --------------------------------
    #: Root-cause analysis and recovery plan per promoted alert.
    RCA = "rca"
    #: Actions actually taken by a human or an executor, and their effect.
    OUTCOMES = "outcomes"

    #: Dead letters: records that could not be processed, with the failure reason.
    DLQ = "dlq"

    @staticmethod
    def name(topic: str, prefix: str = DEFAULT_PREFIX) -> str:
        return "%s.%s" % (prefix, topic)


#: Ordered list of (constant, purpose) — used by `brian init`/compose to create topics.
#:
#: Partitioning rationale (the key column is what Kafka orders by):
#:   events    keyed by service  -> per-service ordering, the unit Flink windows on
#:   features  keyed by service  -> per-service digest stream stays ordered
#:   decisions keyed by service  -> a service's verdict history is ordered
#:   alerts    keyed by service  -> an operator's incident stream is ordered
#:   rca       keyed by alert id -> all reasoning for one alert is ordered together
#:   outcomes  keyed by alert id -> the feedback loop reads per-alert
TOPIC_SPECS: List[Tuple[str, str, str]] = [
    (Topic.EVENTS, "service", "Raw telemetry events (metrics, logs, traces) — the system of record."),
    (Topic.FEATURES, "service", "Deterministic Flink window digests handed to Laya for judgement."),
    (Topic.DECISIONS, "service", "One record per Laya decision: verdict, questions, calibrated probabilities, gate action."),
    (Topic.ALERTS, "service", "Promoted anomalies routed to an owner, carrying the decision that raised them."),
    (Topic.RCA, "alert_id", "LLM root-cause analysis and recovery plan for one alert."),
    (Topic.OUTCOMES, "alert_id", "What was actually done, and whether it worked — closes the learning loop."),
    (Topic.DLQ, "service", "Records that failed processing, with the error, for replay after a fix."),
]


def all_topics(prefix: str = DEFAULT_PREFIX) -> List[str]:
    """Concrete topic names to create on a cluster."""
    return [Topic.name(t, prefix) for t, _, _ in TOPIC_SPECS]


def topic_purpose(topic: str) -> str:
    for const, _key, purpose in TOPIC_SPECS:
        if const == topic:
            return purpose
    return ""


def key_field(topic: str) -> str:
    """Which field must be used as the Kafka message key for a topic."""
    for const, key, _purpose in TOPIC_SPECS:
        if const == topic:
            return key
    return "service"


def env_prefix() -> str:
    """Topic prefix from the environment, e.g. `brian-staging`."""
    return os.environ.get("BRIAN_TOPIC_PREFIX", DEFAULT_PREFIX)


def describe(prefix: str = DEFAULT_PREFIX) -> List[Dict[str, str]]:
    """Machine-readable topic plan, used by the compose bootstrap and the docs."""
    return [
        {"topic": Topic.name(const, prefix), "key": key, "purpose": purpose}
        for const, key, purpose in TOPIC_SPECS
    ]
