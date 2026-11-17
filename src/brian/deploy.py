"""Reload a *running* pipeline from the deployment state file.

A version file records a decision, ``var/evolve/current.json`` records what is in force, and until this
module existed neither of them reached a process that had already started: an accepted change, and a
revert triggered by a failing claim, were both seen at the next restart. That restart is exactly the
human step the D2I.ai ladder's L3 asks the loop to remove, so the last gap in "applies changes without
a human" was not a decision problem — it was a settings-reload problem.

Only settings the pipeline reads **per window** can be swapped under a live process:

* ``gate.*`` — read once per decision by ``apply_gate``;
* ``digest.profile/host_features/scoring/host_precision/host_ratio_floor`` — read when the digest is
  rendered;
* ``digest.rolling_window`` — read when the score is turned into a rank (the window itself is rebuilt,
  so its history restarts, which is observable in ``Counters.settings_reloads``);
* ``llm.max_context_events`` — read when the event history is handed to System 2.

Everything else keeps the value it started with, and the refusal is **reported** rather than applied
quietly. The windower's buffer, the baseline tracker's history and the fitted calibration are built at
start-up; swapping the window size mid-stream would re-label every boundary in the run's own traces,
which is not a configuration change but a different experiment. A pipeline that silently made that
change would produce numbers nobody could attribute.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple, Union

from .config import ENV_VARS, Settings

#: Fields a running pipeline may swap between windows. Anything outside this set needs a restart.
RELOADABLE_FIELDS: FrozenSet[Tuple[str, str]] = frozenset({
    ("llm", "max_context_events"),
    ("gate", "always_analyze_alerts"),
    ("gate", "analyze_actions"),
    ("gate", "reuse_report_within_s"),
    ("gate", "anomaly_probability"),
    ("gate", "notify_probability"),
    ("gate", "rank_probability"),
    ("gate", "confidence_auto"),
    ("gate", "confidence_floor"),
    ("gate", "page_severity_rank"),
    ("gate", "suppress_confidence"),
    ("gate", "normal_probability"),
    ("digest", "profile"),
    ("digest", "host_features"),
    ("digest", "host_precision"),
    ("digest", "host_ratio_floor"),
    ("digest", "scoring"),
    ("digest", "rolling_window"),
})

#: Environment variable name -> ``(section, field)``. The inverse of `config.ENV_VARS`, built once.
FIELD_BY_ENV: Dict[str, Tuple[str, str]] = {name: pair for pair, name in ENV_VARS.items()}


def coerce(current: Any, raw: str) -> Any:
    """Read `raw` as the type of `current`, so a reload cannot turn a float into a string.

    ``bool`` is checked first because ``bool`` is a subclass of ``int``: without that, "false" would be
    coerced to 0 and a gate that was meant to stop analysing alerts would keep doing it.
    """
    if isinstance(current, bool):
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(float(str(raw).strip()))
    if isinstance(current, float):
        return float(str(raw).strip())
    return str(raw)


def overrides_from_state(state: Mapping[str, Any],
                         settings: Settings) -> Tuple[Dict[str, Dict[str, Any]], List[str], List[str]]:
    """Split a state file's environment into ``(reloadable, refused, unknown)``.

    Refused names are settings the pipeline legitimately owns but cannot swap under a live process;
    unknown names are ones this build does not know at all (an older or newer deployment writing the
    same file). The two are reported apart because only one of them is a version mismatch.
    """
    env = dict(state.get("env") or {})
    overrides: Dict[str, Dict[str, Any]] = {}
    refused: List[str] = []
    unknown: List[str] = []
    for name in sorted(env):
        pair = FIELD_BY_ENV.get(name)
        if pair is None:
            unknown.append(name)
            continue
        section, field_name = pair
        if pair not in RELOADABLE_FIELDS:
            refused.append(name)
            continue
        current = getattr(getattr(settings, section), field_name)
        try:
            value = coerce(current, env[name])
        except (TypeError, ValueError):
            unknown.append(name)
            continue
        overrides.setdefault(section, {})[field_name] = value
    return overrides, refused, unknown


class DeploymentState:
    """Reads the state file and reports when it changed since the last successful poll.

    The timestamp is ``updated_at``, written by `promote` every time the state is written. Comparing
    the file's content instead would be equivalent today but would also fire on a whitespace-only
    rewrite; comparing the file's mtime would fire on a checkout that touched no value. Either way the
    reload would be a no-op, but it would still be counted as one, and a counter that moves for reasons
    other than a real change is not worth printing.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self._stamp: Optional[str] = None
        #: The last record `Pipeline.reload_from_deployment` produced, kept for the CLI to print.
        self.last: Optional[Dict[str, Any]] = None

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def stamp(self) -> Optional[str]:
        payload = self._read()
        return None if payload is None else str(payload.get("updated_at") or "")

    def poll(self, settings: Settings) -> Optional[Dict[str, Any]]:
        """``None`` when the file has not moved since the last poll, else what it now asks for."""
        payload = self._read()
        if payload is None:
            return None
        stamp = str(payload.get("updated_at") or "")
        if stamp == self._stamp:
            return None
        self._stamp = stamp
        overrides, refused, unknown = overrides_from_state(payload, settings)
        return {"stamp": stamp, "overrides": overrides, "refused": refused, "unknown": unknown}


def reload_capable() -> bool:
    """Whether this build can swap settings under a running pipeline.

    Asked as a fact by ``brian evolve --grade`` rather than assumed: the ladder's L3 hangs on this one
    behaviour, and a grader that reported it from intent would be the self-confirming loop the survey
    warns about.
    """
    from .pipeline import Pipeline

    return callable(getattr(Pipeline, "reload_from_deployment", None))
