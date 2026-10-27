"""HTTP client for the Laya decision service.

Laya is reached over HTTP, never imported. That is a deliberate boundary with three payoffs:

* **No torch in this process.** The Flink task manager stays a small CPU container; the GPU
  box serves every stream. Importing Laya here would put 421M parameters and a CUDA context
  inside a stream operator.
* **One place to tune.** Preloading, `max_loaded`, device selection and checkpoint residency
  are the *service's* business (`LAYA_PRELOAD`, `/health.warmed`), not the pipeline's.
* **The boundary is observable.** Every call's latency, routing reason and token usage are
  recorded on the decision, which is what makes "why did it decide that?" answerable later.

Two properties of the service shape the client:

**Determinism.** Laya does not sample — identical requests return bit-identical answers. So
an identical digest may be answered from cache, and the cache cannot change the answer. This
is the single biggest steady-state latency win, because repeated windows (a service sitting
at exactly its baseline) are the common case. `cache_hit` is recorded on the decision so the
saving is visible rather than assumed.

**Latency.** Measured on this machine: ~35 ms server-side once warm, ~150 ms of HTTP overhead,
and up to ~0.65 s for the first inference after start (CUDA tunes kernels on first forward
pass). The timeout is generous and the retry policy distinguishes transport failures (retry)
from request errors (do not — a 4xx is a bug in our payload, and retrying a malformed request
just adds latency to a systematic failure).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ..config import LayaSettings
from ..contracts import Digest, Routing, as_jsonable
from .qcatalog import DEFAULT_QUESTIONS, catalog_hash

#: Statuses worth trying again. 429 and 5xx are transient; 4xx is our own bad request.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class LayaError(RuntimeError):
    """Base class for every Laya integration failure."""


class LayaUnavailable(LayaError):
    """Transport-level failure: the service is down, unreachable, or too slow.

    Distinct from `LayaResponseError` because the correct response is different: an
    unavailable engine means *defer* (the window is still in Kafka and can be judged later,
    because the digest is stored as well as the events), whereas a malformed response means
    the integration is broken and the record belongs in the DLQ where a human will see it.
    """


class LayaResponseError(LayaError):
    """The service answered, but the answer violates the contract."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class LayaResult:
    """One answered decision request, with the metadata needed to audit it."""

    answers: Dict[str, Any]
    routing: Routing
    latency_ms: float
    input_tokens: int = 0
    cache_hit: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    model: Optional[str] = None

    @property
    def answer_count(self) -> int:
        return len(self.answers)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


class LayaClient:
    """Synchronous, thread-safe client for `POST /v1/decide`.

    Synchronous because the pipeline is a stream of independent windows: concurrency is
    obtained by running several partitions, which is both simpler and closer to how Flink
    scales than an async client inside a single operator.
    """

    def __init__(self, settings: LayaSettings, questions: Optional[Mapping[str, Any]] = None,
                 clock: Any = time.perf_counter) -> None:
        self.settings = settings
        self.questions = dict(questions or DEFAULT_QUESTIONS)
        self.questions_hash = catalog_hash(self.questions)
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: Dict[str, LayaResult] = {}
        self._order: list = []
        self.cache_stats = CacheStats()

    # -- introspection ---------------------------------------------------------
    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def health(self) -> Dict[str, Any]:
        """`GET /health`. Liveness plus warm-up bookkeeping (boot_s, warm_s, loaded)."""
        return self._get("/health")

    def ready(self) -> bool:
        """`GET /ready` — 503 until the checkpoints finished warming."""
        try:
            self._get("/ready")
            return True
        except LayaUnavailable:
            return False

    def capabilities(self) -> Dict[str, Any]:
        """`GET /v1/capabilities` — the service's self-description.

        Worth calling once at startup: it reports which checkpoints are actually present and
        their `fine_tuned` flag, so a pipeline configured against a checkpoint that is not
        deployed fails loudly at boot instead of quietly judging with the wrong model.
        """
        return self._get("/v1/capabilities")

    def verify_model(self, model: Optional[str] = None) -> Dict[str, Any]:
        """Fail fast if the configured checkpoint is not available on the service.

        Returns the checkpoint's metadata. Raises `LayaResponseError` with the list of what
        *is* available, because "wrong model name" and "model not downloaded" look identical
        from a stream of poor decisions, and this makes them distinguishable at boot.
        """
        target = model or self.settings.model
        caps = self.capabilities()
        available = caps.get("checkpoints") or []
        names = [c.get("name") for c in available]
        for checkpoint in available:
            if checkpoint.get("name") == target:
                return checkpoint
        raise LayaResponseError(
            "checkpoint %r is not available on %s (available: %s)"
            % (target, self.settings.url, ", ".join(str(n) for n in names))
        )

    # -- the call --------------------------------------------------------------
    def decide(self, state: Any, questions: Optional[Mapping[str, Any]] = None,
               model: Optional[str] = None, use_cache: bool = True,
               task: Optional[str] = None) -> LayaResult:
        """Answer the catalog over `state`. `state` is a `Digest`, a dict, or a string."""
        payload_state = self._state_payload(state)
        catalog = dict(questions or self.questions)
        target_model = model or self.settings.model
        cache_key = self._cache_key(payload_state, catalog, target_model, task)

        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                with self._lock:
                    self.cache_stats.hits += 1
                return LayaResult(
                    answers=cached.answers,
                    routing=cached.routing,
                    latency_ms=0.0,
                    input_tokens=cached.input_tokens,
                    cache_hit=True,
                    raw=cached.raw,
                    model=cached.model,
                )
            with self._lock:
                self.cache_stats.misses += 1

        body: Dict[str, Any] = {"state": payload_state, "questions": catalog}
        if target_model:
            body["model"] = target_model
        if task:
            body["task"] = task

        started = self._clock()
        raw = self._post("/v1/decide", body)
        latency_ms = (self._clock() - started) * 1000.0

        answers = raw.get("answers")
        if not isinstance(answers, Mapping) or not answers:
            raise LayaResponseError(
                "response carries no answers (keys: %s)" % sorted(raw.keys()), body=json.dumps(raw)[:500]
            )
        missing = [qid for qid in catalog if qid not in answers]
        if missing:
            raise LayaResponseError(
                "response is missing answers for %s; the catalog and the checkpoint disagree "
                "about the question set" % missing,
                body=json.dumps(raw)[:500],
            )

        usage = raw.get("usage") or {}
        result = LayaResult(
            answers=dict(answers),
            routing=self._routing(raw, target_model),
            latency_ms=latency_ms,
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_hit=False,
            raw=raw,
            model=target_model,
        )
        if use_cache:
            self._cache_put(cache_key, result)
        return result

    # -- plumbing --------------------------------------------------------------
    @staticmethod
    def _state_payload(state: Any) -> Any:
        """Digest -> the state the service is given.

        **A `Digest` becomes a plain string, deliberately.** The service serialises a dict state
        with `json.dumps` (Laya's own `common.serialize_state`) and drops the result into the
        document segment of the sequence, so passing the metric dict means the model reads a JSON
        blob rather than the prose it was rendered for. Measured on this project's labelled
        windows, that alone cost ~0.1 AUC (0.782 with the metrics attached, 0.876 without) — the
        encoder spent its budget on punctuation instead of on the sentence that says what moved.

        The machine-readable metrics are not lost: they live in the `Digest` record that was
        already appended to the `features` topic, and `Decision.answers` carries the questions and
        probabilities. The store keeps the numbers; the model only ever sees evidence phrased for
        it to read.
        """
        if isinstance(state, Digest):
            return state.interpretation
        if isinstance(state, (str, dict, list)):
            return state
        raise TypeError("state must be a Digest, dict, list or str, got %s" % type(state).__name__)

    def _routing(self, raw: Mapping[str, Any], target_model: Optional[str]) -> Routing:
        routing = Routing.from_dict(raw.get("routing"))
        # Trust the service's own statement of which checkpoint answered; fall back to what we
        # asked for only when it reports nothing, so a silent model substitution is visible.
        if not routing.model:
            routing.model = target_model
        return routing

    def _cache_key(self, state: Any, catalog: Mapping[str, Any], model: Optional[str],
                   task: Optional[str]) -> str:
        canonical = json.dumps(
            {
                "state": state,
                "questions": catalog,
                "model": model,
                "task": task,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[LayaResult]:
        with self._lock:
            return self._cache.get(key)

    def _cache_put(self, key: str, result: LayaResult) -> None:
        with self._lock:
            capacity = max(1, int(self.settings.cache_size))
            if key in self._cache:
                return
            self._cache[key] = result
            self._order.append(key)
            while len(self._order) > capacity:
                evicted = self._order.pop(0)
                self._cache.pop(evicted, None)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._order.clear()
            self.cache_stats = CacheStats()

    def _post(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, body)

    def _get(self, path: str) -> Dict[str, Any]:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        url = self.settings.url.rstrip("/") + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.settings.api_key:
            headers["X-API-Key"] = self.settings.api_key

        attempts = max(1, int(self.settings.max_retries) + 1)
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_s) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LayaResponseError(
                        "%s returned non-JSON (%s): %s" % (path, exc, raw[:200]), body=raw[:500]
                    ) from exc
                if not isinstance(parsed, dict):
                    raise LayaResponseError("%s returned %s, expected an object" % (path, type(parsed).__name__))
                return parsed
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code in _RETRY_STATUSES and attempt + 1 < attempts:
                    last_error = LayaResponseError(
                        "HTTP %s from %s" % (exc.code, path), status=exc.code, body=detail
                    )
                    time.sleep(self.settings.retry_backoff_s * (2 ** attempt))
                    continue
                # 503 on /ready is not an error, it is the documented readiness signal; let
                # the caller decide by surfacing it as unavailability rather than a bug.
                if exc.code == 503:
                    raise LayaUnavailable("service not ready (HTTP 503) at %s" % url) from exc
                raise LayaResponseError(
                    "HTTP %s from %s: %s" % (exc.code, path, detail), status=exc.code, body=detail
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = LayaUnavailable("cannot reach %s: %s" % (url, exc))
                if attempt + 1 < attempts:
                    time.sleep(self.settings.retry_backoff_s * (2 ** attempt))
                    continue
                raise last_error from exc
        raise LayaUnavailable("exhausted retries for %s: %s" % (url, last_error))


def digest_for_decision(digest: Digest) -> Dict[str, Any]:
    """The exact payload that will be sent, for logging and for the DLQ."""
    return as_jsonable(
        {
            "state": LayaClient._state_payload(digest),
            "questions": DEFAULT_QUESTIONS,
        }
    )


__all__ = [
    "LayaClient",
    "LayaError",
    "LayaUnavailable",
    "LayaResponseError",
    "LayaResult",
    "CacheStats",
    "digest_for_decision",
]
