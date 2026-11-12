"""The token estimate is an accounting input, so it is pinned like one.

Round 8 measured the old heuristic (`chars // 4`) under-estimating by 30.3% on real RCA prompts: 3,620
estimated against 4,715 tokens from a real BPE, because JSON-heavy text tokenizes at ~3.07 chars/token, not
4.0. Every cost figure this project reported before that correction was ~23% low.

These tests keep the correction in place and keep its direction conservative.
"""
from __future__ import annotations

from brian.llm.prompts import CHARS_PER_TOKEN, prompt_token_estimate


def _messages(text: str):
    return [{"role": "system", "content": text}]


def test_the_estimate_is_not_chars_over_four_again() -> None:
    """The exact regression: a 4,000-character prompt must not be reported as 1,000 tokens."""
    estimate = prompt_token_estimate(_messages("x" * 4000))
    assert estimate > 1200, "estimate regressed toward the old chars//4 assumption"
    assert 1200 <= estimate <= 1500, "estimate should sit near chars/3, not chars/4"


def test_the_estimate_errs_high_not_low() -> None:
    """Measured at 3.071 chars/token; the constant is deliberately stricter than that.

    Over-estimating makes the ledger pessimistic about cost. Under-estimating is the direction that lets a
    system look cheaper than it is, which is the failure the calibration exists to prevent.
    """
    assert CHARS_PER_TOKEN <= 3.071


def test_estimate_scales_linearly_with_content() -> None:
    small = prompt_token_estimate(_messages("a" * 3000))
    large = prompt_token_estimate(_messages("a" * 6000))
    assert large >= 2 * small - 2


def test_empty_content_still_costs_something() -> None:
    """A request with no content is not free; returning 0 would hide a degenerate prompt."""
    assert prompt_token_estimate([]) >= 0
    assert prompt_token_estimate(_messages("")) >= 1
