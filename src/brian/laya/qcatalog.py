"""The typed-question catalog — the exact questions Laya is asked about every window.

This file is the *specification* of the System 1 detector, and it is versioned. Three rules
shape it, all of them consequences of how Laya actually works:

**1. One question per decision primitive, chosen for its measured strength.**
   Laya's `noul` primitive is its strongest (0.857 on the upstream typed-decisions
   benchmark), `choice` is next (0.733), and `score` is the weakest (0.723, and the README
   calls ordinal `score` questions out explicitly). So the question that gates *action* —
   "is this an anomaly?" — is `noul`, not a `score`, and severity is deliberately the only
   `score` question in the catalog.

**2. Every question must be answerable from the digest alone.**
   A question whose evidence is not in the rendered text is not a hard question, it is an
   unanswerable one, and fine-tuning cannot repair it — the information is simply absent.
   `EVIDENCE` records, for each question, which digest fields carry its evidence, and
   `validate_questions` fails if a question declares evidence that the renderer never emits.
   That check is what keeps the catalog and the renderer from drifting apart.

**3. Keep the `choice` label spaces small (< 20).**
   Options share a fixed head token budget (`head_max_len`: 192 on `english`, 256 on the
   multilingual/typed checkpoints). Laya's own benchmarks show accuracy collapsing at 77
   options (~3-4 tokens per label). Eight categories at ~20 tokens each fits comfortably;
   a 40-label taxonomy would fit on paper and fail in practice.

**Language.** The digest is rendered in English and the rubric is written in English,
because `english` and `typed-decisions` only read English (their README is explicit that the
English checkpoint collapses off-language while staying confident). A Chinese or Japanese
operator interface belongs in the *renderer* and in the LLM layer, not here: Laya's
`multilingual` checkpoint would be the routing target for a non-English digest, at a measured
cost in English accuracy (MASSIVE intent: 0.783 English vs 0.657 multilingual on English
input). The catalog is therefore checkpoint-agnostic and the *language* decision is explicit
in `LayaClient.model`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Bump when any question text, criterion, or label changes. Travels with every decision
#: (`Decision.questions_hash`) so a change in detector behaviour is attributable to a
#: catalog revision rather than an unexplained accuracy move.
CATALOG_VERSION = "anomaly/1"

#: Hard limits imposed by the checkpoint architecture, and the validator's budgets.
QUESTION_LIMITS = {
    #: Laya's own recommendation; a deliberate architectural constraint, not a style rule.
    "choice_options_max": 20,
    #: Beyond this the rubric text starts competing with the digest for `max_len`.
    "questions_max": 6,
    #: Ordinal rubrics: enough resolution to rank, few enough levels to stay distinguishable.
    "score_levels_max": 6,
    #: A prompt-rubric word budget. Measured: the 5-question catalog costs ~1.2-1.6k input
    #: tokens on the service; this cap keeps adding a question from silently eating the
    #: document budget.
    "rubric_words_max": 700,
}

#: The catalog. Order is the order answers are reported in, and is stable for readability.
#:
#: `is_anomaly` is first and carries the whole alerting decision. If it had been a `choice`
#: over {normal, minor, major, critical} the two questions "is anything wrong" and "how bad"
#: would be entangled, and a model that is unsure about severity would report uncertainty
#: about existence. Splitting them is what lets the gate act on existence with high
#: confidence while escalating severity to System 2 when it is unclear.
DEFAULT_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "is_anomaly": {
        "type": "noul",
        "instructions": (
            "Do the measurements in this state show a real anomaly that needs attention, as "
            "opposed to normal variation for this service? Answer yes if any signal departs "
            "clearly from its own baseline in a way that indicates a fault, a degradation, or "
            "an unexpected change. Answer no if the values are consistent with normal "
            "operation, including normal daily load variation and steady traffic growth."
        ),
    },
    "category": {
        "type": "choice",
        "instructions": "What kind of anomaly is this?",
        "criteria": {
            "resource_exhaustion": (
                "a local resource is saturated: CPU, memory, worker threads, a connection "
                "pool, disk, or a queue"
            ),
            "dependency_failure": (
                "a dependency is failing or slow: a database, an upstream service, a cache, "
                "or a third-party API"
            ),
            "traffic_surge": (
                "unusual load or a changed traffic pattern where the service itself is "
                "healthy, such as a campaign, a retry storm, or a hot key"
            ),
            "data_quality": (
                "the data is wrong: missing records, duplicates, schema or validation "
                "failures, out-of-order or delayed records"
            ),
            "config_or_deploy": (
                "a deployment, configuration change or feature flag correlates with the "
                "start of the problem"
            ),
            "security": (
                "abuse or intrusion: authentication failures, credential stuffing, scanning "
                "or a suspected compromise"
            ),
            "network": (
                "connectivity: DNS failures, connection timeouts, retransmissions, or "
                "partition-level errors"
            ),
            "none": "no identifiable anomaly class fits this window",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How severe is the impact of this situation?",
        "criteria": [
            "informational: no user-visible impact, worth recording only",
            "minor: slight degradation, users are not meaningfully affected",
            "major: users are affected, the service is degraded but still serving",
            "critical: an outage, data loss, or a safety or security breach",
        ],
    },
    "blast_radius": {
        "type": "choice",
        "instructions": "How wide is the impact of this situation?",
        "criteria": {
            "single_request": "one request, one user, or one record",
            "single_service": "this service only, other services are unaffected",
            "multiple_services": "several services or a shared dependency",
            "whole_platform": "the whole platform, a region, or all customers",
        },
    },
    "needs_human": {
        "type": "noul",
        "instructions": (
            "Should a human operator be paged about this now, rather than letting automated "
            "analysis and recovery handle it? Answer yes for anything that risks data loss, "
            "money, customer trust or security, or that the automated analysis might "
            "misjudge. Answer no for routine degradations that automation can handle."
        ),
    },
}

#: Which digest fields carry each question's evidence.
#:
#: This is a *checkable* claim, not documentation. `validate_questions(metrics)` asserts that
#: every field named here is a field the renderer can actually emit, so a question cannot
#: silently become unanswerable when the renderer changes. The upstream guidance is blunt
#: about the stakes: Laya "is a fast base to specialise, not a zero-shot decision engine", so
#: a question that cannot be answered from the state will produce a confident wrong answer
#: rather than an admission of ignorance.
EVIDENCE: Dict[str, Tuple[str, ...]] = {
    "is_anomaly": ("comparison.errors.ratio", "comparison.latency_ms.p99", "error_signature"),
    "category": (
        "comparison.cpu.util",
        "comparison.memory.util",
        "comparison.db_pool.util",
        "comparison.upstream.error_ratio",
        "comparison.requests.rate",
        "comparison.auth.failures_rate",
        "comparison.timeouts.rate",
        "comparison.schema_errors.ratio",
        "deploys",
        "config_changes",
    ),
    "severity": ("comparison.errors.ratio", "comparison.latency_ms.p99", "requests.rate"),
    "blast_radius": ("comparison.upstream.error_ratio", "comparison.requests.rate"),
    "needs_human": ("comparison.errors.ratio", "comparison.latency_ms.p99", "error_signature"),
}

#: Every metric path the renderer is capable of emitting. Kept as an explicit allow-list so
#: `validate_questions` can distinguish "the renderer omits this because this window had no
#: such data" (fine — the model is told there is no data) from "the renderer can never emit
#: this" (a design error).
RENDERABLE_METRICS: Tuple[str, ...] = (
    "requests.rate",
    "errors.rate",
    "errors.ratio",
    "latency_ms.p50",
    "latency_ms.p90",
    "latency_ms.p99",
    "latency_ms.max",
    "cpu.util",
    "memory.util",
    "thread_pool.util",
    "db_pool.util",
    "upstream.error_ratio",
    "upstream.latency_ms.p99",
    "auth.failures_rate",
    "schema_errors.ratio",
    "consumer.lag",
    "timeouts.rate",
    "error_signature",
    "deploys",
    "config_changes",
    "ingest_lag_s",
)


def questions() -> Dict[str, Dict[str, Any]]:
    """A fresh copy of the catalog.

    A copy, not the module-level dict: `LayaClient` sends this straight into a request body,
    and a caller that mutated the shared catalog would change the detector for the whole
    process. Cheap insurance against a bug that would be very hard to find.
    """
    return json.loads(json.dumps(DEFAULT_QUESTIONS))


def catalog_hash(catalog: Optional[Mapping[str, Any]] = None) -> str:
    """Stable hash of a catalog, recorded on every decision.

    Canonicalised through sorted keys so a reformatting of this file does not look like a
    behaviour change in the audit trail.
    """
    payload = catalog if catalog is not None else DEFAULT_QUESTIONS
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def rubric_word_count(catalog: Optional[Mapping[str, Any]] = None) -> int:
    payload = catalog if catalog is not None else DEFAULT_QUESTIONS
    total = 0
    for definition in payload.values():
        total += len(str(definition.get("instructions", "")).split())
        criteria = definition.get("criteria")
        if isinstance(criteria, Mapping):
            for label, description in criteria.items():
                total += len(str(label).split()) + len(str(description or "").split())
        elif isinstance(criteria, Sequence):
            for level in criteria:
                total += len(str(level).split())
    return total


def validate_questions(catalog: Optional[Mapping[str, Any]] = None,
                       strict_evidence: bool = True) -> List[str]:
    """Check a catalog against the limits Laya actually imposes. Returns problem strings.

    Returns a list rather than raising so a caller can report every problem at once — a
    catalog with three mistakes should take one round trip to fix, not three.

    `strict_evidence` additionally checks the `EVIDENCE` map. It is on by default because
    the failure it prevents is otherwise invisible: a question that cannot be answered from
    the digest does not error, it returns a confident answer built on nothing.
    """
    payload = catalog if catalog is not None else DEFAULT_QUESTIONS
    problems: List[str] = []

    if len(payload) > QUESTION_LIMITS["questions_max"]:
        problems.append(
            "catalog has %d questions, limit is %d (the rubric competes with the digest "
            "for the checkpoint's max_len)"
            % (len(payload), QUESTION_LIMITS["questions_max"])
        )

    for qid, definition in payload.items():
        if not isinstance(definition, Mapping):
            problems.append("question %r is not an object" % qid)
            continue
        qtype = definition.get("type")
        if qtype not in ("choice", "score", "noul"):
            problems.append("question %r has unknown type %r" % (qid, qtype))
            continue
        if not str(definition.get("instructions") or "").strip():
            problems.append("question %r has no instructions" % qid)
        criteria = definition.get("criteria")
        if qtype in ("choice", "score") and criteria is None:
            problems.append("question %r is %s but has no criteria" % (qid, qtype))
            continue
        if qtype == "choice" and isinstance(criteria, Mapping):
            if not criteria:
                problems.append("question %r has an empty label space" % qid)
            if len(criteria) > QUESTION_LIMITS["choice_options_max"]:
                problems.append(
                    "question %r has %d options, limit is %d (options share head_max_len; "
                    "accuracy collapses as labels get fewer tokens)"
                    % (qid, len(criteria), QUESTION_LIMITS["choice_options_max"])
                )
            for label, description in criteria.items():
                if not str(label).strip():
                    problems.append("question %r has an empty label" % qid)
                if description is not None and not str(description).strip():
                    problems.append(
                        "question %r label %r has an empty description; a bare label costs "
                        "accuracy (measured: 'manual_review' -> 'reject' on the same ticket)"
                        % (qid, label)
                    )
        if qtype == "score":
            levels = list(criteria) if isinstance(criteria, Sequence) and not isinstance(criteria, str) else []
            if not levels:
                problems.append("question %r is score but criteria is not an ordered list" % qid)
            elif len(levels) > QUESTION_LIMITS["score_levels_max"]:
                problems.append(
                    "question %r has %d rubric levels, limit is %d"
                    % (qid, len(levels), QUESTION_LIMITS["score_levels_max"])
                )
        if qtype == "noul" and criteria is not None:
            problems.append(
                "question %r is noul and must not carry criteria (noul is a pure binary "
                "question; adding criteria changes what is measured)" % qid
            )

    words = rubric_word_count(payload)
    if words > QUESTION_LIMITS["rubric_words_max"]:
        problems.append("rubric is %d words, budget is %d" % (words, QUESTION_LIMITS["rubric_words_max"]))

    if strict_evidence:
        for qid in payload:
            if qid not in EVIDENCE:
                problems.append(
                    "question %r declares no evidence; add it to EVIDENCE so the renderer "
                    "check can confirm it is answerable from the digest" % qid
                )
                continue
            for path in EVIDENCE[qid]:
                field = path.split("comparison.", 1)[1] if path.startswith("comparison.") else path
                if field not in RENDERABLE_METRICS:
                    problems.append(
                        "question %r relies on %r, which the digest renderer never emits"
                        % (qid, path)
                    )
    return problems


def describe() -> Dict[str, Any]:
    """Catalog summary for `/capabilities`-style introspection and for the docs."""
    return {
        "catalog_version": CATALOG_VERSION,
        "catalog_hash": catalog_hash(),
        "questions": DEFAULT_QUESTIONS,
        "evidence": {k: list(v) for k, v in EVIDENCE.items()},
        "limits": dict(QUESTION_LIMITS),
        "rubric_words": rubric_word_count(),
        "problems": validate_questions(),
    }
