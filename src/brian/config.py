"""Runtime configuration: environment first, `.env` second, defaults last.

Every knob that changes a *judgement* is in `GateThresholds`, not scattered through the
code. That matters more than it looks: a threshold is a policy decision, and a policy
decision that cannot be read off one screen cannot be reviewed or changed safely.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from .topics import DEFAULT_PREFIX, env_prefix

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default location of the fitted confidence calibration. Relative paths are resolved against the
#: repository root so the file travels with the code and `brian calibrate` can write it without a
#: flag. See `brian/calibration.py` for why this file is load-bearing rather than optional.
DEFAULT_CALIBRATION_PATH = "models/calibration/anomaly_temperature.json"


# --------------------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------------------


def _load_dotenv(path: Path) -> Dict[str, str]:
    """Minimal .env reader. Real environment variables always win.

    Deliberately not a dependency: the engine must import with nothing installed.
    """
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    return _dotenv().get(name, default)


_DOTENV_CACHE: Optional[Dict[str, str]] = None


def _dotenv() -> Dict[str, str]:
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        override = os.environ.get("BRIAN_ENV_FILE")
        path = Path(override) if override else REPO_ROOT / ".env"
        _DOTENV_CACHE = _load_dotenv(path)
    return _DOTENV_CACHE


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------------------


@dataclass
class KafkaSettings:
    """Where the memory lives.

    `backend` is the important field. `auto` uses a real cluster when kafka-python is
    importable *and* a broker responds, otherwise falls back to a local append-only JSONL
    journal. The fallback is not a toy: it preserves the two properties the design depends
    on —?append-only order and replay-by-offset —?so `brian demo` exercises the same code
    path (`bus.append` / `bus.replay`) that a cluster does, and switching is one env var.
    """

    bootstrap: str = "127.0.0.1:9092"
    backend: str = "auto"  # auto | kafka | journal | memory
    prefix: str = DEFAULT_PREFIX
    journal_dir: str = "var/journal"
    client_id: str = "brian"
    #: How long a producer waits for acks before failing. Kafka is the memory, so the
    #: default is `all` (every in-sync replica) rather than fire-and-forget.
    acks: str = "all"
    linger_ms: int = 5
    request_timeout_ms: int = 15000
    #: Bounded replays keep a consumer from loading an unbounded journal into memory.
    max_replay_records: int = 200000

    def topic(self, topic: str) -> str:
        return "%s.%s" % (self.prefix, topic)


@dataclass
class LayaSettings:
    """How to reach the System 1 decision engine.

    Laya is reached over HTTP rather than imported, so this process never holds torch or
    VRAM. That is what lets the Flink task manager stay CPU-only and small, and lets the
    GPU box serve several streams.
    """

    url: str = "http://127.0.0.1:8077"
    #: `english` for the base checkpoint, `typed-decisions` once the anomaly catalog has
    #: been fine-tuned. The probe in `scripts/probe_laya.py` is the evidence for which.
    model: str = "typed-decisions"
    #: Explicit checkpoint override per question type is not used; the gate sends one model.
    timeout_s: float = 60.0
    connect_timeout_s: float = 5.0
    api_key: Optional[str] = None
    #: Retries only cover transport failures —?a 4xx is a bug in the request, not a blip.
    max_retries: int = 2
    retry_backoff_s: float = 0.5
    #: Deterministic answers mean an identical digest may be answered from cache. This is
    #: the single biggest latency win in steady state (repeat windows are common in
    #: steady-state telemetry).
    cache_size: int = 4096
    batch_questions: bool = True


@dataclass
class LlmSettings:
    """How to reach the System 2 reasoner (DeepSeek by default).

    `offline` is a first-class mode, not an error path: the pipeline must be runnable and
    testable with no API key and no network, and a deterministic offline report keeps the
    end-to-end contract exercisable in CI.
    """

    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: Optional[str] = None
    timeout_s: float = 120.0
    max_tokens: int = 2048
    temperature: float = 0.2
    #: `offline` forces the deterministic template reasoner even when a key is present.
    offline: bool = False
    #: Cap the prompt: the digest plus the k most recent events. Unbounded prompts are how
    #: an RCA bill becomes unpredictable.
    max_context_events: int = 20
    prompt_version: str = "rca/1"


@dataclass
class GateThresholds:
    """The policy. Every number here changes what the system does, so they live together.

    Two independent scales decide the action, and conflating them is the classic mistake:

    * `anomaly_probability` decides **whether** there is an incident at all.
    * model `confidence` decides **how much the system is allowed to do about it**.

    A window can be a confident anomaly (act) or a hesitant one (ask the LLM). It can also
    be a confident normal (suppress) —?which is just as valuable, because every suppressed
    window is an alert that did not wake anybody up.

    Where the default thresholds come from
    --------------------------------------
    They are **measured, not conventional**, and they are stated on the scale the gate actually sees —?
    which is the calibrated one, because `brian calibrate` fits a Platt correction and the gate applies
    it before any threshold. (Mixing the scales is a silent failure: a calibrated threshold compared
    against raw probabilities simply never fires, and recall goes to 0.000 while every other number in
    the report still looks reasonable. `brian eval` now warns when it detects that situation.)

    Against a fitted fit (slope 17.15, intercept +6.45; AUC 0.8735 unchanged, log loss 0.698 -> 0.522,
    Brier 0.252 -> 0.147, ECE 0.293 -> 0.263) on 90 labelled windows:

        P>=0.5583 (calibrated)   recall 0.854   FPR 0.000   precision 1.000   <- shipped
        P>=0.4104 (raw)          recall 0.854   FPR 0.024   precision 0.976

    `scripts/threshold_sweep.py` sweeps every reachable point; the shipped default is the quietest one
    that still reaches 0.85 recall. Re-derive it for your own traffic, and again after fine-tuning the
    checkpoint. These are one domain's false-page versus missed-incident trade, and they belong in a
    config file with that table next to them rather than in a convention like "0.5".
    """

    #: P(anomaly) at or above this is treated as an anomaly at all, on the calibrated scale.
    anomaly_probability: float = 0.5583
    #: P(anomaly) at or above this is recorded as an alert-worthy anomaly even when the
    #: class/severity confidence is low.
    notify_probability: float = 0.5583
    #: Confidence needed to act on the *category and severity* without System 2.
    confidence_auto: float = 0.75
    #: Below this the judgement is too weak to route at all -> deep analysis.
    confidence_floor: float = 0.45
    #: Severity at or above this pages a human (rank: 0 info, 1 minor, 2 major, 3 critical).
    page_severity_rank: int = 2
    #: A confident `needs_human` verdict also pages, whatever the severity.
    trust_needs_human: bool = True
    #: Suppress only when the model is this sure it is normal. Deliberately higher than
    #: `anomaly_probability`: a false silence is worse than a false alert.
    suppress_confidence: float = 0.70
    #: P(anomaly) at or below this counts as a confident normal when confidence is high.
    normal_probability: float = 0.35
    #: Escalate to the LLM for every alerting action (System 2 always explains).
    always_analyze_alerts: bool = True


@dataclass
class WindowSettings:
    """Event-time windowing, mirroring what the Flink job does."""

    size_s: int = 60
    slide_s: int = 60
    #: How long to wait for late events before closing a window.
    allowed_lateness_s: int = 30
    #: Watermark idle timeout: without it, one silent service stalls every window.
    idle_timeout_s: int = 120
    #: Baseline window used to judge the current one (e.g. same window 24h earlier, or a
    #: trailing median). The engine keeps a rolling baseline per service.
    baseline_windows: int = 30
    #: Second, much longer baseline kept alongside the short one. `0` disables it.
    #:
    #: The short window answers "did this just move?", which detects a spike. It cannot detect a fault
    #: that outlasts it: the rolling median follows the fault, so the ratio collapses to 1.0x exactly
    #: when the fault is at its worst. On the SMD server dataset (300 s windows) the short-horizon
    #: reading ranked with AUC 0.562 and the long-horizon reading 0.772, on identical windows.
    #:
    #: 480 windows = 40 h at a 5-minute cadence, the point where that curve flattened. Set it to a small
    #: multiple of the cadence if windows are configured differently.
    baseline_windows_long: int = 480
    #: Minimum events for a window to be judged at all. Below this the statistical basis is
    #: too thin to accuse a service of anything.
    min_events: int = 10


@dataclass
class CalibrationSettings:
    """Confidence calibration: the correction that makes the gate's thresholds mean something.

    `enabled=False` reproduces the raw model behaviour exactly (T=1), which is the right setting
    for a benchmark that is *measuring* the checkpoint rather than operating it.
    """

    path: str = DEFAULT_CALIBRATION_PATH
    enabled: bool = True


@dataclass
class Settings:
    kafka: KafkaSettings = field(default_factory=KafkaSettings)
    laya: LayaSettings = field(default_factory=LayaSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    gate: GateThresholds = field(default_factory=GateThresholds)
    window: WindowSettings = field(default_factory=WindowSettings)
    calibration: CalibrationSettings = field(default_factory=CalibrationSettings)
    log_level: str = "info"
    #: Emit every record to stdout as JSON (useful when the journal is the only memory).
    echo: bool = False

    def topic(self, topic: str) -> str:
        return self.kafka.topic(topic)

    def calibration_file(self) -> Path:
        """Absolute path of the calibration file, resolved against the repository root."""
        return _resolve_path(self.calibration.path)

    def load_calibration(self) -> Optional[Any]:
        """The fitted temperature, or None when absent/disabled.

        Imported lazily so `config` stays free of engine dependencies and the module graph stays
        acyclic (calibration does not import config).
        """
        if not self.calibration.enabled:
            return None
        from .calibration import CalibrationFit

        return CalibrationFit.load(str(self.calibration_file()))

    def masked(self) -> Dict[str, object]:
        """Settings safe to log or print: secrets replaced by a presence flag."""
        calibration = self.load_calibration()
        return {
            "kafka": {
                "bootstrap": self.kafka.bootstrap,
                "backend": self.kafka.backend,
                "prefix": self.kafka.prefix,
                "journal_dir": self.kafka.journal_dir,
            },
            "laya": {
                "url": self.laya.url,
                "model": self.laya.model,
                "api_key": "set" if self.laya.api_key else "unset",
            },
            "llm": {
                "provider": self.llm.provider,
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "api_key": "set" if self.llm.api_key else "unset",
                "offline": self.llm.offline,
            },
            "gate": _dataclass_dict(self.gate),
            "window": _dataclass_dict(self.window),
            "calibration": {
                "path": str(self.calibration_file()),
                "enabled": self.calibration.enabled,
                "loaded": calibration is not None,
                "slope": round(calibration.slope, 4) if calibration else None,
                "intercept": round(calibration.intercept, 4) if calibration else None,
                "equivalent_temperature": round(calibration.temperature, 3) if calibration else None,
                "fitted_on": calibration.n if calibration else None,
                "auc": round(calibration.auc, 4) if calibration and calibration.auc else None,
                "fitted_at": calibration.fitted_at if calibration else None,
                "notes": list(calibration.notes) if calibration else [],
            },
        }

    def with_overrides(self, **kwargs: object) -> "Settings":
        """A copy with top-level sections replaced —?used by the CLI and tests."""
        return replace(self, **kwargs)  # type: ignore[arg-type]


def _dataclass_dict(obj: object) -> Dict[str, object]:
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def _resolve_path(value: str) -> Path:
    """Resolve a configured path: absolute stays, relative resolves against the repo root."""
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (REPO_ROOT / candidate)


def load_settings() -> Settings:
    """Build `Settings` from the environment (and `.env`).

    Each numeric default is read off the corresponding section's dataclass rather than restated here.
    Restating them is how the measured gate thresholds silently kept their conventional values: the
    dataclass was updated with the swept operating point while this function still passed `0.5`, so the
    shipped policy was the one nobody had measured.
    """
    prefix = _env("BRIAN_TOPIC_PREFIX") or env_prefix()
    api_key = _env("DEEPSEEK_API_KEY") or _env("LLM_API_KEY")
    gate_defaults = GateThresholds()
    window_defaults = WindowSettings()
    laya_defaults = LayaSettings()
    llm_defaults = LlmSettings()
    kafka_defaults = KafkaSettings()
    return Settings(
        kafka=KafkaSettings(
            bootstrap=_env("BRIAN_KAFKA_BOOTSTRAP", kafka_defaults.bootstrap) or kafka_defaults.bootstrap,
            backend=_env("BRIAN_BUS_BACKEND", kafka_defaults.backend) or kafka_defaults.backend,
            prefix=prefix,
            journal_dir=_env("BRIAN_JOURNAL_DIR", str(REPO_ROOT / "var" / "journal"))
            or str(REPO_ROOT / "var" / "journal"),
            client_id=_env("BRIAN_CLIENT_ID", kafka_defaults.client_id) or kafka_defaults.client_id,
            acks=_env("BRIAN_KAFKA_ACKS", kafka_defaults.acks) or kafka_defaults.acks,
            max_replay_records=_int("BRIAN_MAX_REPLAY_RECORDS", kafka_defaults.max_replay_records),
        ),
        laya=LayaSettings(
            url=(_env("LAYA_URL", laya_defaults.url) or laya_defaults.url).rstrip("/"),
            model=_env("BRIAN_LAYA_MODEL", laya_defaults.model) or laya_defaults.model,
            timeout_s=_float("BRIAN_LAYA_TIMEOUT_S", laya_defaults.timeout_s),
            api_key=_env("LAYA_API_KEY"),
            cache_size=_int("BRIAN_LAYA_CACHE_SIZE", laya_defaults.cache_size),
        ),
        llm=LlmSettings(
            provider=_env("BRIAN_LLM_PROVIDER", llm_defaults.provider) or llm_defaults.provider,
            base_url=(_env("DEEPSEEK_BASE_URL", llm_defaults.base_url) or llm_defaults.base_url).rstrip("/"),
            model=_env("BRIAN_LLM_MODEL", llm_defaults.model) or llm_defaults.model,
            api_key=api_key,
            timeout_s=_float("BRIAN_LLM_TIMEOUT_S", llm_defaults.timeout_s),
            max_tokens=_int("BRIAN_LLM_MAX_TOKENS", llm_defaults.max_tokens),
            temperature=_float("BRIAN_LLM_TEMPERATURE", llm_defaults.temperature),
            offline=_flag("BRIAN_LLM_OFFLINE", llm_defaults.offline),
            max_context_events=_int("BRIAN_LLM_MAX_CONTEXT_EVENTS", llm_defaults.max_context_events),
        ),
        gate=GateThresholds(
            anomaly_probability=_float("BRIAN_GATE_ANOMALY_P", gate_defaults.anomaly_probability),
            notify_probability=_float("BRIAN_GATE_NOTIFY_P", gate_defaults.notify_probability),
            confidence_auto=_float("BRIAN_GATE_CONF_AUTO", gate_defaults.confidence_auto),
            confidence_floor=_float("BRIAN_GATE_CONF_FLOOR", gate_defaults.confidence_floor),
            page_severity_rank=_int("BRIAN_GATE_PAGE_RANK", gate_defaults.page_severity_rank),
            suppress_confidence=_float("BRIAN_GATE_SUPPRESS_CONF", gate_defaults.suppress_confidence),
            normal_probability=_float("BRIAN_GATE_NORMAL_P", gate_defaults.normal_probability),
        ),
        window=WindowSettings(
            size_s=_int("BRIAN_WINDOW_SIZE_S", window_defaults.size_s),
            slide_s=_int("BRIAN_WINDOW_SLIDE_S", window_defaults.slide_s),
            allowed_lateness_s=_int("BRIAN_ALLOWED_LATENESS_S", window_defaults.allowed_lateness_s),
            idle_timeout_s=_int("BRIAN_IDLE_TIMEOUT_S", window_defaults.idle_timeout_s),
            baseline_windows=_int("BRIAN_BASELINE_WINDOWS", window_defaults.baseline_windows),
        baseline_windows_long=_int("BRIAN_BASELINE_WINDOWS_LONG", window_defaults.baseline_windows_long),
            min_events=_int("BRIAN_MIN_WINDOW_EVENTS", window_defaults.min_events),
        ),
        calibration=CalibrationSettings(
            path=_env("BRIAN_CALIBRATION_PATH", DEFAULT_CALIBRATION_PATH) or DEFAULT_CALIBRATION_PATH,
            enabled=_flag("BRIAN_CALIBRATION_ENABLED", True),
        ),
        log_level=_env("BRIAN_LOG_LEVEL", "info") or "info",
        echo=_flag("BRIAN_ECHO", False),
    )
