# brian

**Flink 算特征，Laya 判定异常，LLM 做根因分析，Kafka 做 memory。**

Real-time anomaly triage with a deliberate split between two kinds of intelligence:

| | System 1 | System 2 |
|---|---|---|
| **engine** | [Laya](https://github.com/NandhaKishorM/laya) — non-autoregressive, one forward pass | DeepSeek (or any OpenAI-compatible LLM) |
| **question** | *is something wrong, what kind, how bad?* | *why did it happen, and what should we do?* |
| **cost** | ~35 ms server-side, no text generation, nothing to parse | seconds, tokens, and a bill |
| **runs on** | every 60-second window of every service | only promoted alerts |
| **authority** | gates the alert (page / analyse / observe / suppress) | never gates; explains and proposes |

The LLM never decides whether to wake somebody up. That is the architecture in one line: a 35 ms
classifier decides *how much reasoning a situation deserves*, and the LLM supplies the reasoning.

---

## Status: measured, and honest about the gaps

Measured on 90 labelled windows, `convaiinnovations/laya-typed-decisions`, served locally on a
RTX 5070 Ti, judged through the production path with the shipped configuration (a fitted Platt
calibration and the swept threshold carried in `GateThresholds`):

| metric | value | what it means |
|---|---|---|
| **recall** | **0.854** (95% CI 0.73–0.93) | 41 of 48 real incidents caught |
| **false positive rate** | **0.000** (95% CI 0.00–0.08) | no false alarm on 42 healthy windows |
| **precision** | **1.000** | every alert raised was real |
| AUC (ranking) | 0.873 | threshold-free ordering; unmoved by any recalibration |
| calibration | log loss 0.698→0.522, Brier 0.252→0.147, ECE 0.293→0.263 | better, and still the weak point |
| category accuracy | 0.604 | on the incidents it caught |
| latency | p50 76 ms, p95 81 ms | full HTTP round trip, warm |

**The detector works, and `brian eval` still reports `NOT_READY` — both are correct.** Recall and
false-positive rate clear their bars; **ECE does not** (0.263 against a 0.10 bar). The gate branches on
confidence, so acting on System 1's `AUTO_TRIAGE` verdict today would mean trusting a probability that
is wrong by ~0.26. The gate therefore escalates to System 2, and `brian calibrate` refuses to write a
fit whose model cannot separate the classes at all. That is the design working, not a gap in it.

### External validation, and the scope it defines

Those numbers come from scenarios I wrote, which is a closed loop. The system was therefore re-measured
on an external dataset — **SMD** (KDD 2019 OmniAnomaly): 38 real server metrics per minute, human-labelled
per-minute anomaly points, fault intervals up to 3,161 minutes. Two machines were used: `machine-1-6`
(23,689 samples, 3,708 anomalous points) and `machine-2-2` (23,700 samples, 2,833 anomalous points).

Ten rounds of measurement followed; the log is [docs/OPTIMIZATION_LOG.zh.md](docs/OPTIMIZATION_LOG.zh.md)
and the first-pass findings are [docs/REAL_DATA_VALIDATION.zh.md](docs/REAL_DATA_VALIDATION.zh.md). Final
state, all at a 10% false-positive budget:

| configuration | AUC | recall | vs the one-line threshold |
|---|---|---|---|
| `machine-1-6`, system tuned (600 s windows, 3 metrics, long baseline) | 0.745 | 0.380 | threshold wins (0.797) |
| `machine-1-6`, best single-metric threshold | 0.904 | 0.797 | — |
| `machine-2-2`, same config, **not retuned** | 0.698 | **0.264** | system edges past it (0.236) |
| `machine-2-2`, best single-metric threshold | 0.775 | 0.236 | — |

The two machines disagree, and that disagreement is the finding:

**Where a fault reduces to "one metric is high", a threshold wins.** On `machine-1-6` the labels are
almost equivalent to "load or CPU is elevated", so a one-line cut on `load_1` reaches recall 0.797 against
the system's 0.380. 421M parameters add nothing to a problem that is already one-dimensional.

**Where it does not, multi-signal judgement starts to pay.** On `machine-2-2` no single metric carries the
fault (`net_out_bytes` 0.775 AUC is the best of eight, `mem_free` is at 0.493 — below chance), and the
system, transferred without retuning, reaches 0.264 against the threshold's 0.236. That margin is small
enough to be a tie rather than a win, and its AUC is lower — the honest reading is "competitive", not
"better".

So the scope is narrow and should be stated before the architecture: **use this where faults are
multi-signal, or where the output has to be a category and a disposition rather than a yes/no.** Use a
threshold where the question is "is this number high". The catalog, the gate and the LLM escalation are
built for the first case; SMD only partly represents it.

Four things the real data changed, in order of severity:

**The architecture was structurally blind to sustained anomalies, and is not any more.** Absolute level
ranked at AUC 0.815 across the eight features while ratio-to-a-rolling-baseline ranked at 0.493: a fault
that outlasts the baseline is absorbed *into* it, so `CPU 1.03x its baseline` is the correct comparison and
still hides the fault. My synthetic scenarios run 1–2 windows, so they never exposed this. Lengthening the
reference horizon to 480 windows moved AUC from 0.562 to 0.758; the tracker now carries both horizons.

**A verdict inside the state costs accuracy.** Appending "N of M measurements are outside ±25% of their
baseline" to an otherwise identical digest dropped AUC from 0.758 to 0.583. The model is asked for the
verdict, so the state must not contain one. The same held for explanatory rubrics (0.564 vs 0.756) and for
extra metrics: only "value (baseline, ratio)" survived every ablation.

**Calibration fixes meaning, never ranking.** Refitting Platt on this domain took ECE from 0.141 to 0.016
and log loss 0.484 → 0.388, while AUC stayed at 0.7501 exactly — as a monotone transform must.

**An unconfigured threshold raises nothing at all.** No window on `machine-1-6` scores at or above 0.5.
A deployment left on the default cut reports "healthy" forever, which is the failure mode this project
was built to avoid, appearing in its own gate. The operating curve and per-domain refit are in the log.

Also measured: of the 13 HTTP-catalog metric sources, **11 cannot be populated by host telemetry at
all**, so the shipped catalog is service-shaped and a host feed gets a short state.

Four findings from building this are worth more than the headline numbers.

**1. The renderer is a model parameter, and the biggest lever in the whole system.** On byte-identical
metrics, different phrasings of the same state gave:

| state rendering | AUC |
|---|---|
| dotted-key ledger (an early production version) | 0.002 |
| narrative with the comparisons restated numerically | 0.43 |
| the same narrative plus z-scores and sample counts | 0.78 |
| **shipped form** (value and ratio, baseline in the same unit, nothing else) | **0.891**, stable over 6 seeds |

Adding *more* accurate information made it *worse*, twice. `tests/test_digest_renderer.py` now pins the
exact text, and asserts that the benchmark script renders the same thing production does — because when
those drifted, a threshold swept against the benchmark was applied to production text and the detector
raised a false positive on **every** healthy window.

**2. A conventional 0.5 threshold finds almost nothing, and the scale must match.** This checkpoint's
raw probabilities occupy a 0.13-wide band below 0.5, so the operating point is swept, not assumed
(`scripts/threshold_sweep.py`) — and it is stated on the *calibrated* scale, because the gate applies
the fit first. Mixing the two scales is a silent failure: a calibrated threshold compared against raw
probabilities never fires, recall goes to 0.000, and every other number in the report still looks
plausible. `brian eval` now warns when it detects that state.

**3. `traffic_surge` is missed systematically (6 of 6), and that is a labelling argument, not a model
bug.** Surge windows have the **lowest** mean P(anomaly) of any scenario (0.367 raw, 0.42 calibrated) —
lower than every healthy scenario. The model reads "load up 12x with the error fraction flat" as normal
load, which is defensible and arguably correct, while the synthetic ground truth calls it anomalous. A
real deployment has to decide that question deliberately rather than inherit it from a scenario file.

**4. Three bugs that no unit test would have caught were found by measuring.** The digest silently
collapsed to one sentence (a flat-vs-nested key lookup returning `None` for every metric); a Flink
checkpoint resumed with `event_count = 0` (counted from an event list that is deliberately not
checkpointed), which would have made a restarted job judge every window as empty; and the audit query
filed each record under a *different* record's id. All three are now regression-tested.

**Not yet done:** the checkpoint has not been fine-tuned. ECE 0.263 and category accuracy 0.604 both say
so, and [docs/FINETUNE.zh.md](docs/FINETUNE.zh.md) is the runbook. `brian export-training` produces the
data, and `brian outcome` is how real labels get recorded.

**Also unverified**: Kafka and Flink have not been run against a live cluster here. The code is
written, imports cleanly, and shares the entire judgement path with the tested in-process pipeline —
but the transport itself is untested. See the verification-status section of
[docs/KAFKA_TOPICS.md](docs/KAFKA_TOPICS.md).

---

## Architecture

```
                    +---------------- Kafka: the memory ----------------+
                    |  events -> features -> decisions -> alerts -> rca |
                    |  every hop appended, never mutated, fully linked  |
                    +---------------------------------------------------+
   telemetry                                      ^            ^
       |                                          |            |
       v                                          |            |
 +-----------+   +--------------+   +--------------+--+   +-----+------+
 |  ingest   |-->|    Flink     |-->|  Laya (S1)      |-->|  gate      |
 |  ->Kafka  |   | event-time   |   |  5 typed        |   | thresholds |
 |           |   | windows,     |   |  questions,     |   | -> action  |
 |           |   | baselines,   |   |  1 forward pass |   |            |
 |           |   | digest text  |   |  ~60-110 ms     |   |            |
 +-----------+   +--------------+   +-----------------+   +-----+------+
                                                                |
                                       +------------------------+-----------+
                                       | observe / suppress / auto_triage   |
                                       | deep_analysis / page_human         |
                                       +------------------------+-----------+
                                                                | alerting actions
                                                                v
                                     +--------------------------------------+
                                     |  LLM (S2): ranked hypotheses,         |
                                     |  evidence for and against,            |
                                     |  ordered recovery plan                |
                                     +------------------+-------------------+
                                                        |
                                                        v
                                           outcome recorded by a human
                                          (the label that closes the loop)
```

Full detail: **[docs/ARCHITECTURE.zh.md](docs/ARCHITECTURE.zh.md)** (Chinese, 12 sections) ·
**[docs/KAFKA_TOPICS.md](docs/KAFKA_TOPICS.md)** (topic schemas, keying, lineage walk)

### The four design decisions worth knowing

**1. The digest text is the model.** Laya is an encoder over text, not a calculator: what the rendered
sentence *says* is the decision input. So the renderer is a versioned, unit-tested artefact
(`digest_version`) rather than ad-hoc string building — and the measurements above show that changing
it moves AUC between 0.02 and 0.89 on byte-identical underlying data.

**2. State is never sent as a dict.** The first version passed Laya
`{"summary": ..., "metrics": {...}}`. Laya's `serialize_state` then runs `json.dumps` on that dict and
drops the result into the document segment, so the model read a JSON blob instead of the prose written
for it. It is now sent as a plain string; the metrics live in the `features` record, where a human and a
replay can read them.

**3. Calibration is separated from thresholds.** Calibration is measurement; thresholds are policy.
Fitting thresholds around a miscalibrated model is how a system acquires magic numbers nobody can
defend. `models/calibration/` holds a fitted Platt `(slope, intercept)` applied *before* any threshold
sees the probability; `Decision.answers` keeps the model's raw output verbatim and the `Decision`
records the transform that was applied.

**4. Kafka is a memory, not a pipe.** Append-only, offset-addressable, and every record carries
`lineage.upstream_ids`. A recovery action can therefore be traced back through
`outcomes -> rca -> alerts -> decisions -> features -> events` to the raw samples and the exact model
version:

```powershell
python -m brian lineage alrt_5904362fb7ce
```

---

## Quick start

No Kafka, no Docker, no LLM key required — only the Laya service. Deliberately: the plumbing must be
exercisable on a laptop, and the one thing never faked is the Laya call, because a benchmark against a
stub measures the stub.

```powershell
# 1. start the decision engine (from the laya checkout)
D:\project\laya\service\start.cmd

# 2. this repository
cd D:\project\realtime-data-brian
$env:PYTHONPATH = "src"

# 3. the whole chain over synthetic telemetry, printing every step of the reasoning
python -m brian demo

# 4. score System 1 against labelled windows, and say whether it may gate automation
python -m brian eval --repeat 4

# 5. fit the confidence calibration (refuses when the model cannot separate the classes)
python -m brian calibrate --repeat 6 --max-fpr 0.10

# 6. export fine-tuning data
python -m brian export-training --repeat 6

# 7. inspect the memory
python -m brian config --laya
python -m brian topics
python -m brian tail decisions --limit 20
python -m brian tail alerts
python -m brian lineage <decision-or-alert-id>
python -m brian replay --decide
```

With Docker for Kafka + Flink:

```powershell
docker compose up -d
python -m brian topics                        # create the seven topics
flink run -py src/brian/flink/job.py --bootstrap 127.0.0.1:9092
```

---

## Layout

```
src/brian/
  contracts.py        the wire format: Event, Digest, Decision, Alert, RcaReport, Outcome, Lineage
  topics.py           the Kafka topic catalogue: keying, purpose, retention
  bus.py              append-only log behind one interface: Kafka / JSONL journal / memory
  config.py           every knob, with the whole gate policy in one reviewable place
  state.py            Flink's output: windowing, baselines, and the digest text Laya reads
  laya/
    qcatalog.py       the five typed questions, their architectural limits, their evidence map
    client.py         HTTP client for the Laya service (never imports torch)
    gate.py           calibrated probabilities -> permission to act
  calibration.py      Platt fitting, AUC/ECE, and the operating-point choice
  llm/
    prompts.py        the System 2 contract, including what it is NOT told
    client.py         OpenAI-compatible chat client with an explicit failure taxonomy
    rca.py            the LLM path plus a deterministic reasoner as the floor
  pipeline.py         the chain, and the write-ordering guarantees
  outcomes.py         human outcomes read back as labels
  export.py           training data with per-answer provenance
  eval.py             scoring: recall, FPR, per-scenario breakdown, ECE, latency
  demo.py             seven scenarios built to be discriminated from each other
  flink/job.py        the production transport
  cli.py              the command line
scripts/              measurement tools (renderer comparison, threshold sweep, probes)
docs/                 architecture, topics, fine-tuning runbook
tests/                140 tests, including the safety properties
```

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests -q
```

The suite deliberately covers the failure modes this design exists to prevent rather than the happy
path: that a monotone recalibration cannot move a ranking, that a calibration refuses to certify an
indiscriminate model, that an unreadable probability never satisfies a confidence requirement, that a
late event is counted rather than dropped, that a malformed record is dead-lettered instead of killing
the stream, that the digest survives a Flink checkpoint byte-for-byte, and that no source file has been
corrupted by a mis-encoded write (which happened once, and is now a test).

## License

Apache 2.0.
