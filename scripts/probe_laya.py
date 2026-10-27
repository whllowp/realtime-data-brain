"""Throwaway probe: does the anomaly question catalog behave on the real laya service?

Runs the candidate typed-question catalog over three hand-built window states
(obviously normal, obviously degrading, ambiguous) and prints the calibrated
answers with latency, model routing, and token usage.

    python scripts/probe_laya.py [--url http://127.0.0.1:8077]

This is a design probe, not part of the runtime: it is how the catalog under
`src/brian/laya/qcatalog.py` was first validated against the real checkpoints.
Its result is preserved in `docs/ARCHITECTURE.zh.md`; keep it for the next time
the catalog changes, because it is the cheapest way to see whether a question
is answerable at all before wiring it into the gate.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

NORMAL = {
    "service": "checkout-api",
    "window": "2026-01-14T10:00:00Z/2026-01-14T10:01:00Z",
    "requests": 41200,
    "error_rate": {"current": 0.004, "baseline": 0.005, "delta_ratio": 0.8},
    "p99_latency_ms": {"current": 182.0, "baseline": 179.0, "delta_ratio": 1.02},
    "cpu_util": {"current": 0.41, "baseline": 0.43},
    "top_error_signature": "none dominant",
    "interpretation": (
        "All metrics are within their normal band. Error rate 0.4% against a 0.5% baseline, "
        "p99 latency 182 ms against 179 ms, CPU 41%, throughput 41200 requests/min."
    ),
}

DEGRADED = {
    "service": "checkout-api",
    "window": "2026-01-14T10:07:00Z/2026-01-14T10:08:00Z",
    "requests": 28800,
    "error_rate": {"current": 0.31, "baseline": 0.005, "delta_ratio": 62.0},
    "p99_latency_ms": {"current": 8900.0, "baseline": 179.0, "delta_ratio": 49.7},
    "cpu_util": {"current": 0.98, "baseline": 0.43},
    "dependency": "orders-db connection pool",
    "top_error_signature": "TimeoutException: connection acquisition from pool 'orders-db' timed out after 5000ms (n=7412, 92% of errors)",
    "interpretation": (
        "Error rate jumped from 0.5% to 31%, a 62x increase. p99 latency rose from 179 ms to "
        "8900 ms. CPU is pegged at 98%. 92% of all errors are one single signature: a database "
        "connection-pool acquisition timeout against orders-db, which appeared at the same minute "
        "the error rate jumped and was not present before."
    ),
}

AMBIGUOUS = {
    "service": "checkout-api",
    "window": "2026-01-14T10:12:00Z/2026-01-14T10:13:00Z",
    "requests": 39100,
    "error_rate": {"current": 0.021, "baseline": 0.005, "delta_ratio": 4.2},
    "p99_latency_ms": {"current": 410.0, "baseline": 179.0, "delta_ratio": 2.3},
    "cpu_util": {"current": 0.62, "baseline": 0.43},
    "top_error_signature": "HTTP 429 from upstream rate limiter (n=610, 74% of errors)",
    "interpretation": (
        "Error rate rose from 0.5% to 2.1% and p99 latency from 179 ms to 410 ms, while throughput "
        "is flat at 39100. Most errors are HTTP 429 responses from an upstream rate limiter. This "
        "started 12 minutes ago and has been stable since, at the same time as a marketing campaign "
        "launch today."
    ),
}

QUESTIONS = {
    "is_anomaly": {
        "type": "noul",
        "instructions": (
            "Do the metrics in this state show a real anomaly that needs attention, as opposed to "
            "normal fluctuation or expected load variation?"
        ),
    },
    "category": {
        "type": "choice",
        "instructions": "What kind of anomaly is this?",
        "criteria": {
            "resource_exhaustion": "CPU, memory, disk or connection pool saturation",
            "dependency_failure": "a downstream or upstream service or database is failing or slow",
            "traffic_surge": "unusual load or traffic pattern, not a defect",
            "data_quality": "wrong, missing, duplicated or out-of-order data",
            "config_or_deploy": "a deployment, configuration or feature-flag change caused it",
            "security": "abuse, intrusion or credential problems",
            "network": "connectivity, DNS or packet-level problems",
            "none": "no identifiable anomaly class",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How severe is this anomaly?",
        "criteria": [
            "informational: no user impact",
            "minor: slight degradation, users unaffected",
            "major: users affected, service degraded",
            "critical: outage or data loss",
        ],
    },
    "blast_radius": {
        "type": "choice",
        "instructions": "How wide is the impact?",
        "criteria": {
            "single_request": "one request or user",
            "single_service": "this service only",
            "multiple_services": "several services",
            "whole_platform": "the whole platform or region",
        },
    },
    "needs_human": {
        "type": "noul",
        "instructions": (
            "Should a human operator be paged for this, rather than letting automation handle it?"
        ),
    },
}


def decide(url: str, state: dict, questions: dict, model: str) -> dict:
    body = json.dumps({"state": state, "questions": questions, "model": model}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/decide",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as fh:
        return json.loads(fh.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8077")
    ap.add_argument("--models", default="english,typed-decisions")
    args = ap.parse_args()

    states = {"normal": NORMAL, "degraded": DEGRADED, "ambiguous": AMBIGUOUS}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for name, state in states.items():
            t0 = time.perf_counter()
            try:
                res = decide(args.url, state, QUESTIONS, model)
            except urllib.error.HTTPError as exc:
                print("[%s/%s] HTTP %s: %s" % (model, name, exc.code, exc.read().decode()[:300]))
                continue
            ms = (time.perf_counter() - t0) * 1000
            ans = res["answers"]
            print("\n=== model=%s state=%s (%.0f ms, tokens=%s) ===" % (
                model, name, ms, res.get("usage", {}).get("input_tokens")))
            print("  is_anomaly   noul=%.3f conf=%.3f" % (
                ans["is_anomaly"]["noul"], ans["is_anomaly"]["confidence"]))
            print("  category     %s (p=%.3f)" % (
                ans["category"]["choice"], ans["category"]["probabilities"][ans["category"]["choice"]]))
            print("  severity     score=%.2f conf=%.3f" % (
                ans["severity"]["score"], ans["severity"]["confidence"]))
            print("  blast_radius %s (p=%.3f)" % (
                ans["blast_radius"]["choice"],
                ans["blast_radius"]["probabilities"][ans["blast_radius"]["choice"]]))
            print("  needs_human  noul=%.3f conf=%.3f" % (
                ans["needs_human"]["noul"], ans["needs_human"]["confidence"]))
            print("  routing      %s" % (res.get("routing", {}).get("model"),))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
