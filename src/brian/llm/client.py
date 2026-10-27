"""The System 2 LLM client (OpenAI-compatible chat completions; DeepSeek by default).

Hand-written on `urllib` rather than pulling in an SDK, for the same reason the core has no
dependencies: the pipeline must import and run with nothing installed, and the wire format for
`/chat/completions` is small and stable.

The important behaviour is not the call, it is the failure policy. When the LLM is unreachable
or returns unusable text, the correct outcome is a **degraded but honest** report that says so
— never a fabricated cause. `RcaEngine` supplies that fallback; this module's job is to make
each failure mode distinguishable (`LlmUnavailable` vs `LlmResponseError`) so the engine can
degrade for a transient outage and raise loudly for a schema violation.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

from ..config import LlmSettings

_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class LlmError(RuntimeError):
    """Base class for LLM integration failures."""


class LlmUnavailable(LlmError):
    """Transport failure or a 5xx: the reasoner is down. Degrade, do not fail the alert."""


class LlmResponseError(LlmError):
    """The model answered but the answer is unusable (not JSON, or missing required keys)."""


@dataclass
class LlmResult:
    """One completion, with the accounting needed to make the RCA cost auditable."""

    content: str
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    def json(self) -> Dict[str, Any]:
        """Parse the content as JSON, tolerating a markdown code fence.

        Models wrap JSON in ```json fences often enough that failing on it would be a
        self-inflicted outage; the fence is stripped rather than retried, because a retry
        would usually reproduce it.
        """
        text = (self.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
            text = text.strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LlmResponseError(
                "model did not return valid JSON (%s); first 300 chars: %s" % (exc, text[:300])
            ) from exc
        if not isinstance(parsed, dict):
            raise LlmResponseError("model returned %s, expected a JSON object" % type(parsed).__name__)
        return parsed


class LlmClient:
    """Chat-completions client with explicit retry and error taxonomy."""

    def __init__(self, settings: LlmSettings, clock: Any = time.perf_counter) -> None:
        self.settings = settings
        self._clock = clock
        self.calls = 0
        self.failures = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def configured(self) -> bool:
        """Whether a real call is possible. `offline` forces the documented fallback path."""
        return bool(self.settings.api_key) and not self.settings.offline

    def chat(self, messages: Sequence[Mapping[str, str]], response_schema: Optional[Mapping[str, Any]] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None) -> LlmResult:
        """One completion. `response_schema` requests JSON output when the provider supports it."""
        if not self.configured:
            raise LlmUnavailable(
                "no LLM credentials configured (DEEPSEEK_API_KEY unset or BRIAN_LLM_OFFLINE=1)"
            )

        body: Dict[str, Any] = {
            "model": self.settings.model,
            "messages": list(messages),
            "max_tokens": max_tokens or self.settings.max_tokens,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "stream": False,
        }
        # DeepSeek and OpenAI both accept `response_format: {type: json_object}`; the schema
        # itself goes in the prompt because strict structured outputs are not universally
        # supported and a rejected request is worse than an extra instruction.
        if response_schema is not None:
            body["response_format"] = {"type": "json_object"}

        started = self._clock()
        payload = self._post("/v1/chat/completions", body)
        latency_ms = (self._clock() - started) * 1000.0
        self.calls += 1

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmResponseError("response has no choices: %s" % json.dumps(payload)[:300])
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlmResponseError(
                "response choice carries no content (finish_reason=%s)"
                % (choices[0].get("finish_reason"),)
            )
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        return LlmResult(
            content=content,
            model=payload.get("model") or self.settings.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            raw=payload,
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "configured": self.configured,
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
        }

    # -- transport -------------------------------------------------------------
    def _post(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer %s" % self.settings.api_key,
        }
        attempts = 3
        last: Optional[Exception] = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_s) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise LlmResponseError("%s returned %s" % (path, type(parsed).__name__))
                return parsed
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code in _RETRY_STATUSES and attempt + 1 < attempts:
                    last = LlmUnavailable("HTTP %s from %s: %s" % (exc.code, path, detail))
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                self.failures += 1
                if exc.code in (401, 403):
                    raise LlmUnavailable(
                        "LLM auth failed (HTTP %s): check DEEPSEEK_API_KEY" % exc.code
                    ) from exc
                raise LlmResponseError("HTTP %s from %s: %s" % (exc.code, path, detail)) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = LlmUnavailable("cannot reach %s: %s" % (url, exc))
                if attempt + 1 < attempts:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                self.failures += 1
                raise last from exc
        self.failures += 1
        raise LlmUnavailable("exhausted retries for %s: %s" % (url, last))


__all__ = ["LlmClient", "LlmError", "LlmUnavailable", "LlmResponseError", "LlmResult"]
