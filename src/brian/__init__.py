"""Public surface of the brian real-time triage engine.

    Kafka (Source)  ->  Flink (Window/Digest)  ->  Laya (System 1)  ->  LLM (RCA/Recovery)
                              |                        |                     |
                              +---------- Kafka memory / audit trail --------+

Import order matters: `contracts` holds the only definition of the wire format, and
`topics` holds the only definition of the topic names. Everything else references those.
"""
from .config import Settings, load_settings
from .contracts import (
    Alert,
    Decision,
    DecisionAction,
    Digest,
    Event,
    IncidentCategory,
    RcaReport,
    Severity,
    Verdict,
)
from .topics import Topic, all_topics

__all__ = [
    "Settings",
    "load_settings",
    "Event",
    "Digest",
    "Decision",
    "DecisionAction",
    "Alert",
    "RcaReport",
    "Severity",
    "IncidentCategory",
    "Verdict",
    "Topic",
    "all_topics",
]

__version__ = "0.1.0"
