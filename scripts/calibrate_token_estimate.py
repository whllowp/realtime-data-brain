"""Is the token estimate trustworthy? Check it against a real subword tokenizer.

The whole cost accounting rests on `prompt_token_estimate`, a heuristic (words x 1.35 plus newlines). If it
is off by a large factor, every number in the ledger is wrong by that factor — and the direction matters:
an underestimate makes the system look cheaper than it is.

No API key is configured, so the real provider's tokenizer is not available. The next best check, and
arguably the more honest one to report, is against a *real subword tokenizer* available locally: the
ModernBERT tokenizer shipped with the Laya checkpoint. It is not DeepSeek's tokenizer, so the absolute counts
will not match a bill — but it measures the thing in question, which is how far a word-count heuristic sits
from an actual subword count on this kind of text (JSON-heavy, punctuation-dense).

Run with the Laya venv (it has transformers):
    D:\\project\\laya\\.venv\\Scripts\\python.exe scripts\\calibrate_token_estimate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from brian.bus import MemoryBus  # noqa: E402
from brian.config import load_settings  # noqa: E402
from brian.demo import DEFAULT_SEQUENCE, generate  # noqa: E402
from brian.llm.prompts import build_rca_messages, prompt_token_estimate  # noqa: E402
from brian.pipeline import Pipeline  # noqa: E402

TOKENIZER_CANDIDATES = (
    Path(r"D:\project\laya\models\laya-typed-decisions"),
    Path(r"D:\project\laya\models\laya"),
)


def load_tokenizer():
    """Prefer a local model tokenizer; fall back to tiktoken's BPE.

    tiktoken is not DeepSeek's tokenizer, but it *is* a real byte-pair encoding of the kind provider bills
    are computed in, which is exactly the comparison wanted here: how far is a word-count heuristic from a
    subword count on JSON-heavy text? A local ModernBERT tokenizer would be equally non-matching for the
    remote model, so nothing is lost by using the more representative one.
    """
    try:
        from transformers import AutoTokenizer

        for candidate in TOKENIZER_CANDIDATES:
            if not candidate.is_dir():
                continue
            try:
                loaded = AutoTokenizer.from_pretrained(str(candidate), local_files_only=True)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
            return (lambda text, tok=loaded: tok.encode(text, add_special_tokens=False)), \
                "local:%s" % candidate.name
    except ImportError:
        pass

    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return (lambda text: encoding.encode(text)), "tiktoken:cl100k_base"
    except Exception as exc:  # noqa: BLE001
        print("  tiktoken unavailable: %s" % exc)
    return None, None


def main() -> int:
    tokenizer, name = load_tokenizer()
    if tokenizer is None:
        print("no local tokenizer available; cannot calibrate")
        return 2
    print("tokenizer: %s" % name)
    print("")

    settings = load_settings()
    windows = generate(sequence=DEFAULT_SEQUENCE, service="checkout-api", warmup_minutes=20, seed=7)
    pipeline = Pipeline(settings, bus=MemoryBus())
    traces = []
    for window in windows:
        for event in window.events:
            pipeline.ingest(event)
        for trace in pipeline.drain_ready():
            traces.append(trace)
    traces.extend(pipeline.drain_ready(final=True))

    alerts = [t for t in traces if t.report is not None and t.decision is not None]
    if not alerts:
        print("no alerts to measure")
        return 2

    rows = []
    for trace in alerts:
        messages = build_rca_messages(trace.digest, trace.decision, events=trace.history,
                                      max_events=20)
        estimate = prompt_token_estimate(messages)
        # Tokenize each message separately: concatenating first would let BPE merge across the boundary and
        # undercount, which is not how a provider counts a chat request either.
        actual = 0
        chars = 0
        for message in messages:
            content = message.get("content", "")
            chars += len(content)
            actual += len(tokenizer(content))
        rows.append((estimate, actual, chars))

    mean_estimate = sum(r[0] for r in rows) / len(rows)
    mean_actual = sum(r[1] for r in rows) / len(rows)
    ratio = mean_actual / mean_estimate if mean_estimate else 0
    worst = max(rows, key=lambda r: abs(r[0] - r[1]))[:2]

    mean_chars = sum(r[2] for r in rows) / len(rows)
    print("alerts            %d" % len(rows))
    print("chars / alert     %.0f" % mean_chars)
    print("chars per token   %.3f" % (mean_chars / mean_actual if mean_actual else 0))
    print("estimate / alert  %.1f" % mean_estimate)
    print("tokenizer / alert %.1f" % mean_actual)
    print("actual / estimate %.3f   (%.1f%% error)" % (ratio, 100 * (ratio - 1)))
    print("worst alert       estimate %d vs tokenizer %d" % worst)
    print("")
    print("Reading this: the numbers are tokenizer-dependent, so the absolute value is not the bill.")
    print("What it establishes is whether the heuristic is conservative. A ratio below 1.0 means the")
    print("ledger UNDERSTATES the cost, which is the dangerous direction - it makes a system look")
    print("cheaper than it is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
