"""System 2: turn a fast verdict into ranked hypotheses and a recovery plan.

Two things make this stage trustworthy rather than merely plausible:

**The offline reasoner is a first-class implementation, not an error path.** When no LLM is
reachable, the engine does not return nothing and does not hallucinate — it runs a deterministic
rule-based analysis over the same digest, marks the report `degraded: true`, and (critically)
marks every proposed action `autonomous: false`. A rule engine that cannot admit uncertainty is
not allowed to authorise action, and that restriction is enforced here rather than documented.

**Ranked hypotheses with counter-evidence.** The LLM is asked for alternatives and for what
argues *against* each one; the offline reasoner follows the same shape. This is the structural
answer to the single-cause failure: an operator reading `hypotheses[1]` can see it rests on a
metric that moved only 1.2x, and discard it in two seconds. A single confident cause cannot be
audited that way.

The rules below encode the *discriminators* a human SRE would use, not correlations:

    dependency_failure vs resource_exhaustion
        a saturated local pool (db_pool) with upstream error/timeout rises  -> dependency
        CPU/thread saturation with errors concentrated *locally*           -> resource

    traffic_surge vs real failure
        throughput up AND error fraction flat                              -> surge (healthy)
        throughput up AND error fraction also up                           -> failure, not surge

    config_or_deploy
        a deploy/flag recorded inside the window AND the symptom starts with it -> correlated
        (reported as correlation, with the missing change-history named explicitly)

Each rule states the evidence for and against itself, so the same auditability holds whether
the report came from the model or from these rules.
"""
from __future__ import annotations


import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..config import LlmSettings
from ..contracts import Decision, Digest, Event, IncidentCategory, Lineage, RcaReport
from .client import LlmClient, LlmError, LlmResponseError, LlmResult
from .prompts import PROMPT_VERSION, build_rca_messages, prompt_token_estimate, rca_json_schema

#: A prompt this large is a design smell (too many metrics, or an event cap that is too high).
#: It is reported rather than enforced, because refusing to analyse an alert would be worse.
PROMPT_TOKEN_WARN = 12000


@dataclass
class Evidence:
    """One measurement used to support or refute a hypothesis."""

    key: str
    label: str
    value: Optional[float]
    baseline: Optional[float] = None
    delta_ratio: Optional[float] = None
    z: Optional[float] = None

    def sentence(self) -> str:
        if self.value is None:
            return "%s was not reported" % self.label
        text = "%s %s" % (self.label, _fmt(self.value))
        if self.baseline is not None:
            text += " (baseline %s" % _fmt(self.baseline)
            if self.delta_ratio is not None:
                text += ", %sx" % _fmt(self.delta_ratio, 1)
            if self.z is not None:
                text += ", z=%s%s" % ("+" if self.z >= 0 else "", _fmt(self.z, 1))
            text += ")"
        return text


@dataclass
class Hypothesis:
    """A candidate cause with its supporting and contradicting evidence."""

    cause: str
    confidence: float
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    category: Optional[IncidentCategory] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cause": self.cause,
            "confidence": round(self.confidence, 3),
            "evidence_for": list(self.evidence_for),
            "evidence_against": list(self.evidence_against),
        }


@dataclass
class Action:
    """A proposed recovery step."""

    step: str
    rationale: str
    risk: str = "medium"
    autonomous: bool = False
    verify: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "rationale": self.rationale,
            "risk": self.risk,
            "autonomous": self.autonomous,
            "verify": self.verify,
        }


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 1000:
        return "%d" % round(number)
    return ("%%.%df" % digits) % number


class OfflineReasoner:
    """Deterministic rule-based RCA over one digest — the no-LLM path.

    The rule set is intentionally small and each rule is a *discriminator* with a stated
    refutation. A larger rule set would be more impressive and less honest: these are the
    patterns that can be argued from a window digest alone, and anything else is left to the
    LLM or to a human rather than guessed at.
    """

    def __init__(self, min_ratio: float = 1.8, min_z: float = 3.0) -> None:
        #: A metric counts as "moved" only when it clears both a ratio and a robust z. Requiring
        #: both is what stops a spike on a very stable metric (huge z, tiny ratio) from being
        #: reported as an incident.
        self.min_ratio = min_ratio
        self.min_z = min_z

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _evidence(digest: Digest, key: str) -> Optional[Evidence]:
        # Read through `Digest.metric`, which walks the dotted path. `comparison` is nested, so a flat
        # `.get("latency_ms.p99")` would return None and quietly reduce every rule below to "no
        # evidence", producing a report that names no cause while looking like it had checked.
        entry = digest.metric("comparison.%s" % key)
        if not isinstance(entry, Mapping):
            return None
        return Evidence(
            key=key,
            label=key.split(".")[0].replace("_", " "),
            value=entry.get("value"),
            baseline=entry.get("baseline"),
            delta_ratio=entry.get("delta_ratio"),
            z=entry.get("z"),
        )

    def _moved(self, digest: Digest, key: str, direction: str = "up") -> bool:
        entry = digest.metric("comparison.%s" % key)
        if not isinstance(entry, Mapping) or entry.get("value") is None:
            return False
        ratio = entry.get("delta_ratio")
        z = entry.get("z")
        if ratio is None:
            return False
        if direction == "up":
            big = ratio >= self.min_ratio
        else:
            big = ratio <= (1.0 / self.min_ratio)
        # z is None when the baseline is flat (MAD == 0). A flat baseline with a big ratio is
        # still strong evidence of change, so only *require* z when it is available.
        if z is None:
            return bool(big)
        z_big = abs(float(z)) >= self.min_z
        return bool(big and z_big)

    def _sentence(self, digest: Digest, key: str) -> Optional[str]:
        evidence = self._evidence(digest, key)
        return evidence.sentence() if evidence else None

    # -- the rules -------------------------------------------------------------
    def analyse(self, digest: Digest, decision: Decision,
                events: Optional[Sequence[Event]] = None) -> Tuple[str, str, List[Hypothesis], List[str], List[Action], float, List[str]]:
        """Return (summary, root_cause, hypotheses, evidence, actions, confidence, missing)."""
        metrics = digest.metrics
        hypotheses: List[Hypothesis] = []
        evidence: List[str] = []
        missing: List[str] = []

        error_ratio = self._evidence(digest, "errors.ratio")
        latency_p99 = self._evidence(digest, "latency_ms.p99")
        cpu = self._evidence(digest, "cpu.util")
        memory = self._evidence(digest, "memory.util")
        db_pool = self._evidence(digest, "db_pool.util")
        thread_pool = self._evidence(digest, "thread_pool.util")
        upstream_err = self._evidence(digest, "upstream.error_ratio")
        upstream_lat = self._evidence(digest, "upstream.latency_ms.p99")
        throughput = self._evidence(digest, "requests.rate")
        timeouts = self._evidence(digest, "timeouts.rate")
        auth = self._evidence(digest, "auth.failures_rate")
        schema = self._evidence(digest, "schema_errors.ratio")
        lag = self._evidence(digest, "consumer.lag")
        signature = metrics.get("error_signature") or {}
        deploys = metrics.get("deploys") or []
        config_changes = metrics.get("config_changes") or []

        for candidate in (error_ratio, latency_p99, throughput, cpu, memory, db_pool):
            if candidate and candidate.value is not None:
                evidence.append(candidate.sentence())

        rising_errors = self._moved(digest, "errors.ratio")
        rising_latency = self._moved(digest, "latency_ms.p99")
        rising_throughput = self._moved(digest, "requests.rate")
        falling_throughput = self._moved(digest, "requests.rate", direction="down")

        # -- rule 1: local resource saturation ---------------------------------
        saturated = [e for e in (cpu, memory, thread_pool, db_pool) if e and e.value is not None and e.value >= 0.9]
        pool_saturated = db_pool and db_pool.value is not None and db_pool.value >= 0.85
        if saturated:
            names = ", ".join("%s at %s%%" % (e.label, _fmt((e.value or 0) * 100, 0)) for e in saturated)
            against: List[str] = []
            if not rising_latency:
                against.append("latency did not rise, which a saturated resource usually causes")
            if upstream_err and not self._moved(digest, "upstream.error_ratio"):
                against.append("upstream error fraction is flat, arguing against a downstream cause")
            hypotheses.append(
                Hypothesis(
                    cause="Local resource exhaustion: %s" % names,
                    confidence=0.55 if rising_errors or rising_latency else 0.4,
                    evidence_for=[e.sentence() for e in saturated],
                    evidence_against=against,
                    category=IncidentCategory.RESOURCE_EXHAUSTION,
                )
            )

        # -- rule 2: dependency failure ----------------------------------------
        if pool_saturated or self._moved(digest, "upstream.error_ratio") or self._moved(digest, "timeouts.rate"):
            cause = "A dependency is failing or slow"
            if pool_saturated:
                cause += ": the local connection pool to it is saturated at %s%%" % _fmt((db_pool.value or 0) * 100, 0)
            supports = [e.sentence() for e in (upstream_err, upstream_lat, timeouts, db_pool) if e and e.value is not None]
            against = []
            if cpu and cpu.value is not None and cpu.value < 0.7:
                against.append("local CPU is not saturated (%s%%), so the service is waiting rather than working" % _fmt(cpu.value * 100, 0))
            else:
                against.append("high local CPU could equally be the cause rather than a symptom")
            hypotheses.append(
                Hypothesis(
                    cause=cause,
                    confidence=0.6 if pool_saturated and (rising_latency or rising_errors) else 0.45,
                    evidence_for=supports,
                    evidence_against=against,
                    category=IncidentCategory.DEPENDENCY_FAILURE,
                )
            )

        # -- rule 3: traffic surge vs failure (the pairing is the discriminator) --
        if rising_throughput:
            error_is_flat = not rising_errors
            if error_is_flat and not rising_latency:
                hypotheses.append(
                    Hypothesis(
                        cause="A traffic surge rather than a fault: load rose while the error fraction and latency stayed flat",
                        confidence=0.5,
                        evidence_for=[e.sentence() for e in (throughput, error_ratio, latency_p99) if e and e.value is not None],
                        evidence_against=["sustained load can turn into a real incident if capacity is finite"],
                        category=IncidentCategory.TRAFFIC_SURGE,
                    )
                )
            else:
                hypotheses.append(
                    Hypothesis(
                        cause="A failure under load, not a healthy surge: throughput and the error fraction rose together",
                        confidence=0.5,
                        evidence_for=[e.sentence() for e in (throughput, error_ratio) if e and e.value is not None],
                        evidence_against=["if the load was expected (a campaign), the failure may still be capacity, not a defect"],
                        category=IncidentCategory.RESOURCE_EXHAUSTION,
                    )
                )
        elif falling_throughput and rising_errors:
            hypotheses.append(
                Hypothesis(
                    cause="Throughput collapsed while errors rose, which points at the service shedding or failing load rather than receiving less of it",
                    confidence=0.5,
                    evidence_for=[e.sentence() for e in (throughput, error_ratio) if e and e.value is not None],
                    evidence_against=["a drop in throughput can also be an upstream caller backing off"],
                    category=IncidentCategory.RESOURCE_EXHAUSTION,
                )
            )

        # -- rule 4: deploy / config correlation --------------------------------
        if deploys or config_changes:
            change = "; ".join([str(d) for d in deploys] + [str(c) for c in config_changes])
            hypotheses.append(
                Hypothesis(
                    cause="A deployment or configuration change correlates with the start of the problem: %s" % change,
                    confidence=0.45,
                    evidence_for=["a change was recorded inside this window: %s" % change]
                    + [e.sentence() for e in (error_ratio, latency_p99) if e and e.value is not None],
                    evidence_against=[
                        "correlation only: the window has no deploy timestamps finer than one minute, "
                        "so a change that finished before the symptom cannot be separated from one that caused it"
                    ],
                    category=IncidentCategory.CONFIG_OR_DEPLOY,
                )
            )
            missing.append("Change history with commit ids and exact deploy times (not available to this pipeline)")

        # -- rule 5: data quality ----------------------------------------------
        if self._moved(digest, "schema_errors.ratio") or self._moved(digest, "consumer.lag"):
            hypotheses.append(
                Hypothesis(
                    cause="A data-quality problem rather than a service fault: schema validation or consumer lag moved",
                    confidence=0.45,
                    evidence_for=[e.sentence() for e in (schema, lag) if e and e.value is not None],
                    evidence_against=["consumer lag also rises when a downstream consumer is simply slow"],
                    category=IncidentCategory.DATA_QUALITY,
                )
            )

        # -- rule 6: security --------------------------------------------------
        if self._moved(digest, "auth.failures_rate"):
            hypotheses.append(
                Hypothesis(
                    cause="A security signal: authentication failures rose sharply",
                    confidence=0.5,
                    evidence_for=[e.sentence() for e in (auth,) if e and e.value is not None],
                    evidence_against=["a credential rotation or a misconfigured client produces the same signal"],
                    category=IncidentCategory.SECURITY,
                )
            )

        # -- rule 7: the local error template ----------------------------------
        template = str(signature.get("template") or "").strip()
        if template and float(signature.get("share") or 0) >= 0.5:
            lowered = template.lower()
            hint = None
            if any(word in lowered for word in ("timeout", "timed out", "deadline exceeded")):
                hint = "the dominant error is a timeout, which points at a dependency or a saturated pool rather than a code defect"
            elif any(word in lowered for word in ("connection refused", "no such host", "unreachable", "dns")):
                hint = "the dominant error is a connection failure, which points at network reachability or a dead dependency"
            elif any(word in lowered for word in ("outofmemory", "out of memory", "oom")):
                hint = "the dominant error is memory exhaustion, which points at a leak or an undersized heap"
            elif any(word in lowered for word in ("permission denied", "unauthorized", "403", "401")):
                hint = "the dominant error is authorisation, which points at credentials or policy rather than capacity"
            if hint:
                evidence.append(
                    "The single most frequent error template is %s (%s%% of errors): %s"
                    % (template[:120], _fmt((signature.get("share") or 0) * 100, 0), hint)
                )

        # -- rank and trim -----------------------------------------------------
        hypotheses.sort(key=lambda h: h.confidence, reverse=True)
        if not hypotheses:
            # No rule fired. Saying so is the correct answer: the classifier flagged this window
            # but the digest carries no discriminating pattern, and inventing one would be the
            # exact failure this design is built to prevent.
            hypotheses.append(
                Hypothesis(
                    cause="Insufficient evidence in this window to name a cause: no single metric moved in a pattern that discriminates between the known failure classes",
                    confidence=0.2,
                    evidence_for=evidence[:3] or ["no metric moved beyond its baseline by a meaningful margin"],
                    evidence_against=["the classifier did flag this window, so *some* signal departed from baseline"],
                )
            )
            missing.append("The specific metric that triggered the classifier's anomaly probability, at full resolution")

        if not evidence:
            evidence = ["no comparison against a baseline was available for this window"]

        missing.extend(
            [
                "Raw log lines around the onset (only one normalised template is available per window)",
                "Service dependency graph or trace data to confirm the causal path",
            ]
        )
        # De-duplicate while preserving order.
        missing = list(dict.fromkeys(missing))[:5]

        top = hypotheses[0]
        severity = decision.severity.value if decision.severity else "unknown"
        summary = "%s was flagged as %s (%s). %s" % (
            digest.service,
            decision.category.value if decision.category else "an anomaly",
            severity,
            top.cause.split(":", 1)[0] + ".",
        )
        confidence = min(0.65, top.confidence * (0.5 + 0.5 * float(decision.confidence or 0.0)))

        actions = self._actions(hypotheses, decision, digest)
        return summary, top.cause, hypotheses[:4], evidence[:8], actions, confidence, missing

    def _actions(self, hypotheses: Sequence[Hypothesis], decision: Decision, digest: Digest) -> List[Action]:
        """Least-destructive-first recovery steps, keyed off the leading category.

        Every action is `autonomous: False`. A rule engine cannot judge blast radius, and the
        only honest position for it is to propose.
        """
        category = hypotheses[0].category if hypotheses and hypotheses[0].category else decision.category
        service = digest.service
        actions: List[Action] = []

        if category is IncidentCategory.DEPENDENCY_FAILURE:
            actions.extend(
                [
                    Action(
                        step="Check the dependency's own health and latency before touching %s" % service,
                        rationale="The pool is saturated, which is usually a symptom of the dependency being slow, not of %s failing." % service,
                        risk="low",
                        verify="The dependency's error rate and p99 latency return to baseline.",
                    ),
                    Action(
                        step="If the dependency is healthy but slow, raise the connection-pool ceiling and the acquisition timeout for %s" % service,
                        rationale="A pool that is exhausted while the dependency responds is undersized for the current concurrency.",
                        risk="medium",
                        verify="Pool utilisation drops below 80%% and acquisition timeouts stop.",
                    ),
                    Action(
                        step="If the dependency is unhealthy, fail over or shed non-critical traffic to %s" % service,
                        rationale="Protects the dependency from a retry storm while it recovers.",
                        risk="high",
                        verify="Error fraction falls and the dependency's load drops.",
                    ),
                ]
            )
        elif category is IncidentCategory.RESOURCE_EXHAUSTION:
            actions.extend(
                [
                    Action(
                        step="Identify which resource is saturated on %s and whether the saturation is load-driven or leak-driven" % service,
                        rationale="The recovery differs: capacity for load, a restart or a fix for a leak.",
                        risk="low",
                        verify="The saturated resource's trajectory is explained (flat with load, or monotonically rising).",
                    ),
                    Action(
                        step="Scale out %s if the saturation tracks load" % service,
                        rationale="Adds capacity without changing behaviour.",
                        risk="medium",
                        verify="Utilisation per instance falls and latency recovers.",
                    ),
                ]
            )
        elif category is IncidentCategory.TRAFFIC_SURGE:
            actions.append(
                Action(
                    step="Confirm whether the load is expected (a campaign, a batch job, a retry storm) before mitigating",
                    rationale="An expected surge is handled by capacity planning; an unexpected one may be abuse.",
                    risk="low",
                    verify="The traffic pattern matches a known source.",
                )
            )
        elif category is IncidentCategory.CONFIG_OR_DEPLOY:
            actions.append(
                Action(
                    step="Correlate the deploy or flag change against the exact onset minute for %s, and roll back if the onset follows the change" % service,
                    rationale="A change inside the same window as the onset is the cheapest hypothesis to confirm or eliminate.",
                    risk="medium",
                    verify="The symptom disappears after the rollback and does not return.",
                )
            )
        elif category is IncidentCategory.SECURITY:
            actions.append(
                Action(
                    step="Identify the source of the authentication failures against %s and whether they are one client or many" % service,
                    rationale="A single misconfigured client and a credential-stuffing campaign need different responses.",
                    risk="low",
                    verify="Failure sources are attributed by client identity and address.",
                )
            )
        elif category is IncidentCategory.DATA_QUALITY:
            actions.append(
                Action(
                    step="Diff the rejected records against the last known-good schema and pause the affected consumer if corruption is propagating",
                    rationale="Bad data that keeps flowing multiplies the cleanup.",
                    risk="medium",
                    verify="Rejected-record ratio returns to zero and lag drains.",
                )
            )
        else:
            actions.append(
                Action(
                    step="Inspect raw logs and metrics for %s around the onset, and the last change to the service" % service,
                    rationale="No discriminating pattern was found in the window, so the next step is more resolution, not a guess.",
                    risk="low",
                    verify="A specific metric or log template is identified as the trigger.",
                )
            )
        actions.append(
            Action(
                step="Record the outcome against this alert so the classifier can be scored on it",
                rationale="Every closed alert is a labelled example; without them the detector cannot be fine-tuned or calibrated.",
                risk="low",
                verify="An outcome record referencing decision %s exists." % decision.decision_id,
            )
        )
        return actions[:5]


class RcaEngine:
    """Produce an `RcaReport` from an alert: LLM first, deterministic reasoner as the floor."""

    def __init__(self, settings: LlmSettings, client: Optional[LlmClient] = None,
                 offline_reasoner: Optional[OfflineReasoner] = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.settings = settings
        self.client = client if client is not None else LlmClient(settings)
        self.offline = offline_reasoner or OfflineReasoner()
        self._clock = clock
        self.llm_reports = 0
        self.fallback_reports = 0
        self.warnings: List[str] = []

    @property
    def mode(self) -> str:
        return "llm" if self.client.configured else "offline"

    def analyse(self, digest: Digest, decision: Decision, alert_id: str,
                events: Optional[Sequence[Event]] = None) -> RcaReport:
        """Analyse one alert. Never raises for a reasoner outage: it degrades instead.

        The distinction matters operationally. An LLM outage must not stop the pipeline (the
        alert still needs to reach a human), but it must be *visible*, so the report carries
        `degraded: true` and the engine counts the fallbacks.
        """
        messages = build_rca_messages(digest, decision, events, max_events=self.settings.max_context_events)
        estimated = prompt_token_estimate(messages)
        if estimated > PROMPT_TOKEN_WARN:
            self.warnings.append(
                "RCA prompt for %s is ~%d tokens; consider lowering BRIAN_LLM_MAX_CONTEXT_EVENTS"
                % (digest.service, estimated)
            )

        started = self._clock()
        if self.client.configured:
            try:
                result = self.client.chat(messages, response_schema=rca_json_schema())
                report = self._from_llm(result, digest, decision, alert_id, events, estimated)
                self.llm_reports += 1
                report.latency_ms = (self._clock() - started) * 1000.0
                return report
            except LlmError as exc:
                self.fallback_reports += 1
                self.warnings.append("LLM analysis failed for %s: %s" % (digest.service, exc))
        else:
            self.fallback_reports += 1

        report = self._from_rules(digest, decision, alert_id, events)
        report.latency_ms = (self._clock() - started) * 1000.0
        return report

    # -- LLM path --------------------------------------------------------------
    def _from_llm(self, result: LlmResult, digest: Digest, decision: Decision, alert_id: str,
                  events: Optional[Sequence[Event]], estimated_tokens: int) -> RcaReport:
        payload = result.json()
        for key in ("summary", "root_cause", "hypotheses", "evidence", "actions"):
            if key not in payload:
                raise LlmResponseError("LLM report is missing required key %r" % key)
        hypotheses = payload.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            raise LlmResponseError("LLM report carries no hypotheses; a single unqualified cause is not acceptable")

        actions = [
            Action(
                step=str(a.get("step", "")),
                rationale=str(a.get("rationale", "")),
                risk=str(a.get("risk", "medium")),
                autonomous=bool(a.get("autonomous", False)),
                verify=str(a.get("verify", "")),
            )
            for a in payload.get("actions") or []
            if isinstance(a, Mapping)
        ]
        # A report that names no action is unusable at 3 am; substitute the deterministic ones.
        if not actions:
            actions = self.offline._actions([], decision, digest)
            self.warnings.append("LLM returned no actions for %s; substituted deterministic proposals" % digest.service)

        return RcaReport(
            alert_id=alert_id,
            decision_id=decision.decision_id,
            service=digest.service,
            summary=str(payload.get("summary", ""))[:1000],
            root_cause=str(payload.get("root_cause", ""))[:2000],
            hypotheses=[
                {
                    "cause": str(h.get("cause", "")),
                    "confidence": _clamp(h.get("confidence")),
                    "evidence_for": [str(x) for x in (h.get("evidence_for") or [])][:6],
                    "evidence_against": [str(x) for x in (h.get("evidence_against") or [])][:6],
                }
                for h in hypotheses
                if isinstance(h, Mapping)
            ][:4],
            evidence=[str(e) for e in (payload.get("evidence") or [])][:8],
            actions=[a.to_dict() for a in actions][:5],
            confidence=_clamp(payload.get("confidence")) or 0.0,
            model_id=result.model,
            prompt_version=PROMPT_VERSION,
            degraded=False,
            lineage=self._lineage(digest, decision, "llm-rca", result.model),
        )

    # -- deterministic path ----------------------------------------------------
    def _from_rules(self, digest: Digest, decision: Decision, alert_id: str,
                    events: Optional[Sequence[Event]]) -> RcaReport:
        summary, root_cause, hypotheses, evidence, actions, confidence, missing = self.offline.analyse(
            digest, decision, events
        )
        return RcaReport(
            alert_id=alert_id,
            decision_id=decision.decision_id,
            service=digest.service,
            summary=summary,
            root_cause=root_cause,
            hypotheses=[h.to_dict() for h in hypotheses],
            evidence=evidence,
            actions=[a.to_dict() for a in actions],
            confidence=confidence,
            model_id="offline-rule-reasoner",
            prompt_version="rules/1",
            degraded=True,
            lineage=self._lineage(digest, decision, "offline-rule-reasoner", None),
        )

    @staticmethod
    def _lineage(digest: Digest, decision: Decision, producer: str, model: Optional[str]) -> Lineage:
        return Lineage(
            upstream_ids=[decision.decision_id, digest.digest_id],
            producer=producer,
            model_id=model,
            prompt_version=PROMPT_VERSION,
        )

    def stats(self) -> Dict[str, Any]:
        llm = self.client.stats()
        return {
            "mode": self.mode,
            "reports_llm": self.llm_reports,
            "reports_fallback": self.fallback_reports,
            "fallback_rate": (
                self.fallback_reports / (self.llm_reports + self.fallback_reports)
                if (self.llm_reports + self.fallback_reports)
                else 0.0
            ),
            "llm": llm,
            "warnings": self.warnings[-10:],
        }


def _clamp(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, 0.0), 1.0)


__all__ = ["RcaEngine", "OfflineReasoner", "Hypothesis", "Action", "Evidence", "PROMPT_TOKEN_WARN"]
