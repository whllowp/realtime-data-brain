"""Laya integration: reach the System 1 engine, ask it typed questions, gate on the answer."""
from .client import LayaClient, LayaError, LayaResponseError, LayaUnavailable
from .gate import GateOutcome, apply_gate
from .qcatalog import (
    CATALOG_VERSION,
    DEFAULT_QUESTIONS,
    QUESTION_LIMITS,
    catalog_hash,
    questions,
    validate_questions,
)

__all__ = [
    "LayaClient",
    "LayaError",
    "LayaUnavailable",
    "LayaResponseError",
    "GateOutcome",
    "apply_gate",
    "CATALOG_VERSION",
    "DEFAULT_QUESTIONS",
    "QUESTION_LIMITS",
    "catalog_hash",
    "questions",
    "validate_questions",
]
