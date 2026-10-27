# Kafka Topics: The Memory of the Triage System

Topic catalogue, record schemas, lineage, ordering guarantees, retention, replay and configuration
for the `brian` real-time anomaly-triage system. Every field name, default and behaviour here was
read from `src/brian/`. Where the code does not yet do something, this document says so.

## 中文摘要

本系统里 Kafka 不是消息管道，而是**记忆**。它由两条性质支撑：**只追加**（任何记录都不被修改或删除，
后出现的矛盾以一条新记录引用旧记录的方式表达，因此"系统在每一刻相信什么"的历史被完整保留）与**全链路血缘**
（每条记录都带 `lineage.upstream_ids`，任何结果都能反向追溯到原始事件与作出该判断的确切模型版本）。

- 七个子主题：`events`（原始遥测，系统 of record）、`features`（Flink 窗口摘要，交给 Laya 判定）、
  `decisions`（Laya 判定与门控动作）、`alerts`（升级给值班人的告警）、`rca`（LLM 根因与恢复方案）、
  `outcomes`（实际采取的动作及效果，闭合学习回路）、`dlq`（处理失败的记录）。
- 分区键是设计的一部分：`events`/`features`/`decisions`/`alerts` 按 `service` 分区（按服务有序，正是
  Flink 窗口的单位）；`rca`/`outcomes` 按 `alert_id` 分区（同一告警的全部推理保持在一起且有序）。
- 写入顺序先于一切：原始事件先落盘再被判断；摘要先于判定，判定先于告警，告警先于 LLM 报告。
  因此推理服务宕机只会降低**解释**质量，绝不会丢失**通知**。
- `features` 被专门持久化，使得任何一天都能用新模型版本重新导出或重新判定某个决策，而无需重读原始数据洪流。
- 三种后端共用同一接口：`KafkaBus`（生产）、`JournalBus`（每主题一个只追加 JSONL 文件，逐行 fsync，
  行号即偏移量，可按键重放）、`MemoryBus`（测试）。`BRIAN_BUS_BACKEND=auto` 优先 Kafka，不可达时回退到
  journal，并在 stderr 上明确宣告回退，而不是静默地"看着完整、实则缺失"地判断。

## Verification status

| Claim | Status |
| --- | --- |
| `KafkaBus`, `ensure_topics` | Written, **never executed against a live broker in this project** |
| Flink job (`src/brian/flink/job.py`) | Written, **never run against a cluster**. Partitioning, offset behaviour, exactly-once sinks, watermarks and keyed state are design claims, not measurements |
| `JournalBus` | Exercised: `var/journal/` holds real `brian.events.jsonl`, `brian.features.jsonl`, `brian.decisions.jsonl`, `brian.alerts.jsonl`, `brian.rca.jsonl` |
| Schemas and `to_dict()` field names | Verified against `src/brian/contracts.py` and against real journal lines |
| Retention numbers, partition counts, replication factor | **Recommendations**, derived from record volume and key cardinality, not from measurement |
| `outcomes` production | The topic is catalogued and read by the lineage walk, but **no `Outcome` contract exists in `contracts.py` and nothing in this repository produces to `outcomes`** |
| Contract violations reaching `dlq` | `contracts.py` states the intent; in `pipeline.py` the only two `_publish_dlq` call sites are Laya-unreachable and RCA-exception, and `ContractError` is imported there but not caught |

## 1. Purpose: Kafka is the memory, not a pipe

A message pipe is allowed to forget. This topic set is not, because it is the artefact an operator
reads *after* an incident. Two properties make that work.

**Append-only.** Nothing in this system updates or deletes a record: `Bus` offers `append`,
`replay`, `tail`, `latest_offset`, `count`, `describe`, `close` and no editing path. When the system
later disagrees with itself, the disagreement is a *new* record that references the earlier one, so
what the system believed at 10:08 survives even after that belief is corrected.

**Full lineage.** Every record carries `lineage.upstream_ids`, the ids of the records it came from:

```
evt_*  events  ->  dgst_*  features  ->  dec_*  decisions  ->  alrt_*  alerts  ->  rca_*
(raw)              (Flink digest)        (Laya System 1)      (routing)        (System 2)
```

Reading `upstream_ids` backwards from any outcome reconstructs exactly which raw events a recovery
action was based on, and which model version judged them: `lineage` also carries `producer`,
`model_id`, `prompt_version` and `code_version`, so the *who* and the *which version* travel with the
record rather than living in a deployment note.

Topic naming is frozen in `src/brian/topics.py`. Producers and consumers import the `Topic`
constant, so a typo cannot silently create a second, parallel topic; `Topic.name(topic, prefix)`
applies the configurable prefix, `brian.` by default.

## 2. Topic catalogue

`TOPIC_SPECS` in `src/brian/topics.py` is the single source of truth for the key and purpose columns.
Full names use the default prefix `brian`; set `BRIAN_TOPIC_PREFIX` (for example `brian-staging`) so
several environments can share one cluster.

| Topic | Full name (default) | Partition key | Record type | Payload schema | Retention (recommendation) | Consumed by |
| --- | --- | --- | --- | --- | --- | --- |
| `events` | `brian.events` | `service` | `Event` | `event_id`, `event_time`, `ingest_time`, `service`, `kind`, `source`, `schema_version`, `payload` | Longest — 30-90 days, or indefinite if storage allows | Flink windower; `brian replay` rebuild; lineage walk; the `min_events` gate |
| `features` | `brian.features` | `service` | `Digest` | `digest_id`, `digest_version`, `service`, `window_start`, `window_end`, `event_count`, `metrics`, `interpretation`, `lineage` | As long as `decisions` — this is the re-derivation source | Laya judgement path; `brian eval`; re-judging a window with a new model; lineage walk |
| `decisions` | `brian.decisions` | `service` | `Decision` | `decision_id`, `digest_id`, `service`, `verdict`, `action`, `category`, `severity`, `severity_score`, `confidence`, `anomaly_probability`, `needs_human`, `gate_reason`, `latency_ms`, `decided_at`, `cache_hit`, `questions_hash`, `questions`, `answers`, `routing`, `lineage` | Effectively permanent — audit record | Alert router (`_alert_for`); `brian calibrate`; `brian eval`; `brian tail decisions`; lineage walk |
| `alerts` | `brian.alerts` | `service` | `Alert` | `alert_id`, `decision_id`, `digest_id`, `service`, `severity`, `action`, `category`, `confidence`, `title`, `owner`, `raised_at`, `dedup_key`, `lineage` | 1-3 years; shorter than `decisions`, where the judgement lives | Operator notification/routing; System 2 input; `brian tail alerts`; lineage walk |
| `rca` | `brian.rca` | `alert_id` | `RcaReport` | `rca_id`, `alert_id`, `decision_id`, `service`, `summary`, `root_cause`, `hypotheses`, `evidence`, `actions`, `confidence`, `model_id`, `prompt_version`, `latency_ms`, `degraded`, `created_at`, `lineage` | Effectively permanent — the explanation of record | Operators; `brian tail rca`; lineage walk |
| `outcomes` | `brian.outcomes` | `alert_id` | *no contract class yet* | Not defined in `contracts.py`. `lineage_of` indexes it by `outcome_id` and follows `upstream_ids`, so the intended shape carries `outcome_id` plus `lineage` pointing at the `alert_id`/`decision_id` | Effectively permanent — the labelled set | Intended source of calibration and fine-tuning labels; lineage walk. **Nothing writes to it yet** |
| `dlq` | `brian.dlq` | `service` | plain dict (header `record_type: "Dlq"`) | `source_topic`, `source_id`, `service`, `error`, `payload`, `at` | 30-90 days, or until every drained record is replayed | Operators; manual replay after a fix |

`describe(prefix)` returns this plan machine-readably and is what `brian topics` and
`brian config` print; `key_field(topic)` resolves a constant back to its key field and
`all_topics(prefix)` lists the concrete names to create on a cluster.

In production the *producer* of `features`, `decisions`, `alerts`, `rca` and `dlq` is the Flink job
in `src/brian/flink/job.py`, which calls `Pipeline.process_digest` rather than reimplementing it.
The `events` topic is produced by ingest (or the demo), and `outcomes` has no producer yet.

## 3. Why each topic is keyed the way it is

Kafka orders records **within a partition**, and the message key selects the partition. The key
column is therefore not metadata — it defines "ordered with respect to what".

| Key | Topics | What it buys |
| --- | --- | --- |
| `service` | `events`, `features`, `decisions`, `alerts` | Per-service ordering. Windows are computed per service, so a service's events and digests must be consumed in event order — that single ordered stream is the unit Flink windows on. A service's verdict history, and the incident stream an operator reads, stay ordered too |
| `alert_id` | `rca`, `outcomes` | All reasoning about one alert stays together and ordered. A report and the outcome that closes it are never read in the wrong relative order, which is what makes the feedback loop a sequence of events rather than a set of coincidences |

The four service-keyed topics use deliberately identical keys, and the pipeline sets the key
explicitly at every hop (`key=event.service`, `key=digest.service`, `key=decision.service`,
`key=alert.service`, then `key=report.alert_id`) — a key is never left to a producer default.

**Choosing the wrong key.** A mismatched key does not merely reorder records; it interleaves two
independent streams:

| Wrong key | Failure |
| --- | --- |
| Low-cardinality key on `events` (a constant, or `kind` with three values) | Every service lands on one or two partitions: windows for different services interleave, per-service order is lost, horizontal scale disappears because there is nothing to spread |
| High-cardinality key on `events` (`event_id`) | Every event gets its own key. Per-service ordering is destroyed completely — one service's events arrive in arbitrary relative order, so a 60-second window cannot be reconstructed faithfully and `allowed_lateness_s` becomes meaningless because "late" has no defined predecessor |
| `alert_id` on `features` or `decisions` | The digest and verdict for a window no longer share a partition with that service's history, so the per-service ordering that the baseline comparison and the alert dedup key assume is gone |
| `service` on `rca` / `outcomes` | One alert's reasoning is ordered against *other alerts'* reasoning, so a report and the outcome closing it can be consumed out of order |

Partition count matters for the same reason: keys are mapped to partitions by hash, so **increasing
partition count later remaps keys** and breaks the per-key ordering a consumer relied on. Choose the
count before the topic carries data (section 9).

## 4. Record schemas

`src/brian/contracts.py` is the single definition of every record that crosses a boundary. `to_dict()`
is the authoritative field list and the name is exactly what appears on the wire. `from_dict()` is
defensive by design: a merely missing optional field never raises `KeyError`/`TypeError`, and a
genuinely malformed record raises `ContractError`.

### 4.1 `Lineage` (embedded in every record)

| Field | JSON type | Default | Meaning |
| --- | --- | --- | --- |
| `upstream_ids` | array of string | `[]` | Ids of the records this one was derived from. The load-bearing field |
| `producer` | string | `"unknown"` | `"flink-digest"`, `"gate"`, `"alert-router"`, `"offline-rule-reasoner"` |
| `model_id` | string or null | `null` | Exact model/checkpoint that produced the judgement |
| `prompt_version` | string or null | `null` | Version of the question catalog or prompt used |
| `code_version` | string or null | `null` | Version of the rendering/digest code |

### 4.2 `Routing` (inside `Decision`)

| Field | JSON type | Default | Meaning |
| --- | --- | --- | --- |
| `model` | string or null | `null` | Checkpoint Laya reports having used |
| `reason` | string or null | `null` | *Why* the router picked it, e.g. `explicit model='typed-decisions'` |
| `repo` | string or null | `null` | Checkpoint repository path |
| `detection` | object or null | `null` | Router's detection detail, copied verbatim |

Stored so a decision is debuggable without re-running anything: why the router selected a checkpoint
is part of the record, not a log line.

### 4.3 `Event` — topic `events`

| Field | JSON type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `event_id` | string | no | `evt_` + 12 hex | `new_id("evt")` |
| `event_time` | string (ISO-8601 UTC, `Z`) | **yes** | — | Observation time; what Flink windows on |
| `ingest_time` | string or null | no | `null` | When the pipeline saw it; the difference is the end-to-end lag |
| `service` | string | **yes** | — | Must be non-empty; also the partition key |
| `kind` | string | no | `"metric"` | Must be non-empty |
| `source` | string | no | `"unknown"` | |
| `schema_version` | integer | no | `1` | |
| `payload` | object | no | `{}` | Must be an object; the raw sample as observed |

Real line from `var/journal/brian.events.jsonl`, unmodified:

```json
{"topic":"brian.events","key":"checkout-api","value":{"event_id":"evt_1f0615fe8049","event_time":"2026-01-14T10:00:00Z","ingest_time":"2026-01-14T10:00:01.738833Z","service":"checkout-api","kind":"metric","source":"demo","schema_version":1,"payload":{"http_requests_total":642.1580761715701,"http_errors_total":0,"http_request_duration_ms":177.03696857961376,"cpu_util":0.4466323129812875,"memory_util":0.6278013589696019,"thread_pool_util":0.2895879987657262,"db_pool_util":0.3819959871636206,"upstream_errors_total":0,"upstream_duration_ms":102.6682443858031,"auth_failures_total":0}},"headers":{"record_type":"Event"},"timestamp":1790527647.5745623,"offset":0}
```

### 4.4 `Digest` — topic `features`

| Field | JSON type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `digest_id` | string | no | `dgst_` + 12 hex | |
| `digest_version` | string | no | `"digest/1"` | Version of the renderer that produced `interpretation` |
| `service` | string | **yes** | — | Must be non-empty; partition key |
| `window_start` | string (ISO-8601) | **yes** | — | With `service` this is the window identity (`window_key` = `service|window_start`) |
| `window_end` | string (ISO-8601) | **yes** | — | Must not be earlier than `window_start` |
| `event_count` | integer | no | `0` | Compared against `min_events` before any judgement |
| `metrics` | object | no | `{}` | Machine-readable truth from Flink. Dotted paths readable via `metric("error_rate.delta_ratio")` |
| `interpretation` | string | no | `""` | The rendered English sentence Laya actually conditions on |
| `lineage` | object (`Lineage`) | no | empty `Lineage` | Upstream `event_id`s of the window |

Real line, trimmed (`upstream_ids` shortened — the real record carries all 72 event ids; `metrics` keys
abridged):

```json
{"topic":"brian.features","key":"checkout-api","value":{"digest_id":"dgst_cc39b5e7bbd6","digest_version":"digest/1","service":"checkout-api","window_start":"2026-01-14T10:00:00Z","window_end":"2026-01-14T10:01:00Z","event_count":72,"metrics":{"service":"checkout-api","window_seconds":60.0,"event_count":72,"log_lines":12,"requests":{"rate":12.862956428312673},"errors":{"rate":0.0033333333333333335,"ratio":0.0002591420838522261},"latency_ms":{"p50":128.69593427282052,"p99":177.03696857961376,"n":180},"cpu":{"util":0.4325122650373005},"memory":{"util":0.6518288087160852},"db_pool":{"util":0.3587684053691657},"ingest_lag_s":{"max":2.49,"p50":1.339}},"interpretation":"Service checkout-api, a 60-second window with 72 events.\nThe most frequent error (100.00% of all errors) is: INFO request completed status=<n> duration=<num><unit> path=/checkout\nDistinct error templates in this window: 1.","lineage":{"upstream_ids":["evt_1f0615fe8049","evt_94789d2a3d4b","... 70 more event ids ..."],"producer":"flink-digest","model_id":null,"prompt_version":null,"code_version":"digest/1"}},"headers":{"record_type":"Digest"},"timestamp":1790527647.628765,"offset":0}
```

The split between `metrics` and `interpretation` is deliberate: `metrics` makes the judgement
reproducible from the record alone, and `interpretation` is what Laya reads, so rendering is a
versioned, reviewable artefact (`digest_version`) rather than ad-hoc string building.

One documented divergence between the two paths: a Flink checkpointed operator cannot carry the raw
events inside its aggregate, because that state would grow without bound, so exactly one provenance
field is absent from `metrics` after a task-manager resume — `metrics.ingest_lag_s`. The rendered text
and every decision-relevant metric are byte-identical; the renderer treats a missing lag as "not
measured" rather than "no lag", and the raw events remain in `events`.

### 4.5 `Decision` — topic `decisions`

| Field | JSON type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `decision_id` | string | no | `dec_` + 12 hex | |
| `digest_id` | string | **yes** | — | Must be non-empty |
| `service` | string | **yes** | — | Partition key |
| `verdict` | string enum | **yes** | — | `anomaly` or `normal` |
| `action` | string enum | **yes** | — | `observe`, `auto_triage`, `deep_analysis`, `page_human`, `suppress` |
| `category` | string enum or null | no | `null` | One of the 8 `IncidentCategory` values |
| `severity` | string enum or null | no | `null` | `info`, `minor`, `major`, `critical` |
| `severity_score` | number or null | no | `null` | Continuous Laya `score` before discretisation |
| `confidence` | number | **yes** | — | Must be within `[0, 1]` |
| `anomaly_probability` | number or null | no | `null` | P(anomaly) after calibration; decides *whether* there is an incident |
| `needs_human` | boolean or null | no | `null` | Laya's `noul` answer on paging |
| `gate_reason` | string | no | `""` | Human-readable rule that produced the action |
| `latency_ms` | number or null | no | `null` | System 1 latency |
| `decided_at` | string (ISO-8601) | no | now | |
| `cache_hit` | boolean | no | `false` | Whether Laya answered from cache |
| `questions_hash` | string or null | no | `null` | Hash of the rubric asked, so a rubric change is visible |
| `questions` | object | no | `{}` | The exact questions asked |
| `answers` | object | no | `{}` | Per-question distributions as Laya returned them |
| `routing` | object (`Routing`) | no | empty `Routing` | |
| `lineage` | object (`Lineage`) | no | empty `Lineage` | Upstream is the `digest_id`; `producer` is `"gate"` |

`questions` is stored beside `answers` on purpose: a model is only interpretable against the question
it was asked, so if the rubric changes, old decisions stay readable in their original rubric. Real
line, with `questions`/`answers` abridged and `routing`/`lineage` verbatim:

```json
{"topic":"brian.decisions","key":"checkout-api","value":{"decision_id":"dec_4b2b7bc78f64","digest_id":"dgst_cc39b5e7bbd6","service":"checkout-api","verdict":"normal","action":"observe","category":"data_quality","severity":"major","severity_score":1.6622,"confidence":0.5757,"anomaly_probability":0.4243,"needs_human":true,"gate_reason":"P(anomaly)=0.424 is below 0.50 but confidence is 0.576, so the window is recorded rather than declared healthy","latency_ms":861.9381999596953,"decided_at":"2026-09-27T16:47:28.491550Z","cache_hit":false,"questions_hash":"sha256:99fe367030684665","questions":{"is_anomaly":{"type":"noul","instructions":"Do the measurements in this state show a real anomaly that needs attention, ..."},"category":{"type":"choice","criteria":{"resource_exhaustion":"a local resource is saturated: ...","dependency_failure":"a dependency is failing or slow: ..."}},"severity":{"type":"score","criteria":["informational: ...","minor: ...","major: ...","critical: ..."]},"blast_radius":{"type":"choice","criteria":{"single_request":"...","single_service":"...","multiple_services":"...","whole_platform":"..."}},"needs_human":{"type":"noul","instructions":"Should a human operator be paged about this now, ..."}},"answers":{"is_anomaly":{"type":"noul","noul":0.4243,"confidence":0.5757,"action":{"act_probability":1.0}},"category":{"type":"choice","choice":"data_quality","probabilities":{"resource_exhaustion":0.0796,"dependency_failure":0.1737,"traffic_surge":0.1619,"data_quality":0.2527,"config_or_deploy":0.1009,"security":0.0651,"network":0.0621,"none":0.1039},"confidence":0.055},"severity":{"type":"score","score":1.6622,"legend":{"0":"informational: no user-visible impact, worth recording only","1":"minor: slight degradation, users are not meaningfully affected","2":"major: users are affected, the service is degraded but still serving","3":"critical: an outage, data loss, or a safety or security breach"},"probabilities":{"0":0.0752,"1":0.279,"2":0.5543,"3":0.0915},"confidence":0.2089},"blast_radius":{"type":"choice","choice":"multiple_services","probabilities":{"single_request":0.1401,"single_service":0.2552,"multiple_services":0.3822,"whole_platform":0.2224},"confidence":0.0436},"needs_human":{"type":"noul","noul":0.5745,"confidence":0.5745}},"routing":{"model":"typed-decisions","reason":"explicit model='typed-decisions'","repo":"D:\\project\\laya\\models\\laya-typed-decisions","detection":null},"lineage":{"upstream_ids":["dgst_cc39b5e7bbd6"],"producer":"gate","model_id":"typed-decisions","prompt_version":"digest/1","code_version":null}},"headers":{"record_type":"Decision"},"timestamp":1790527648.4918482,"offset":0}
```

### 4.6 `Alert` — topic `alerts`

Deliberately thin: it *references* its decision rather than duplicating it, so an alert can be
re-routed, re-worded or closed without rewriting the judgement that justified it.

| Field | JSON type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `alert_id` | string | no | `alrt_` + 12 hex | |
| `decision_id` | string | **yes** | — | Must be non-empty |
| `digest_id` | string or null | no | `null` | |
| `service` | string | **yes** | — | Partition key |
| `severity` | string enum | **yes** | — | Falls back to `major` for an anomaly with unreadable severity |
| `action` | string enum | **yes** | — | Only `auto_triage`, `deep_analysis`, `page_human` produce an alert at all |
| `category` | string enum or null | no | `null` | |
| `confidence` | number or null | no | `null` | |
| `title` | string | no | `""` | `"<service>: <category with spaces> (<severity>)"` |
| `owner` | string or null | no | `null` | |
| `raised_at` | string (ISO-8601) | no | now | |
| `dedup_key` | string or null | no | `null` | `"<service>|<category>"`. Window start is excluded so one incident across consecutive windows is recognisable as one thing; two classes on one service are two incidents |
| `lineage` | object (`Lineage`) | no | empty `Lineage` | Upstream `[decision_id, digest_id]`, `producer` `"alert-router"`, `model_id` copied from `decision.routing.model` |

Real line from `var/journal/brian.alerts.jsonl`, unmodified:

```json
{"topic":"brian.alerts","key":"checkout-api","value":{"alert_id":"alrt_5904362fb7ce","decision_id":"dec_1e5a53bf255d","digest_id":"dgst_1bb67a3e364a","service":"checkout-api","severity":"major","action":"page_human","category":"dependency_failure","confidence":0.0629,"title":"checkout-api: dependency failure (major)","owner":null,"raised_at":"2026-09-27T16:55:53.368071Z","dedup_key":"checkout-api|dependency_failure","lineage":{"upstream_ids":["dec_1e5a53bf255d","dgst_1bb67a3e364a"],"producer":"alert-router","model_id":"typed-decisions","prompt_version":null,"code_version":null}},"headers":{"record_type":"Alert"},"timestamp":1790528153.368173,"offset":0}
```

### 4.7 `RcaReport` — topic `rca`

The System 2 output: the half of the system that is allowed to be slow. Produced only for promoted
alerts.

| Field | JSON type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `rca_id` | string | no | `rca_` + 12 hex | |
| `alert_id` | string | **yes** | — | Also the partition key |
| `decision_id` | string | **yes** | — | Both ids are mandatory |
| `service` | string | **yes** | — | |
| `summary` | string | no | `""` | |
| `root_cause` | string | no | `""` | |
| `hypotheses` | array of object | no | `[]` | Ordered most-likely-first; real entries carry `cause`, `confidence`, `evidence_for`, `evidence_against` |
| `evidence` | array of string | no | `[]` | Each element coerced with `str()` on the way in |
| `actions` | array of object | no | `[]` | Real entries carry `step`, `rationale`, `risk`, `autonomous`, `verify`. Proposals only: `autonomous` marks what is safe to execute without a human and is empty unless the operator opted in |
| `confidence` | number | no | `0.0` | |
| `model_id` | string or null | no | `null` | `"offline-rule-reasoner"` in degraded mode |
| `prompt_version` | string or null | no | `null` | |
| `latency_ms` | number or null | no | `null` | |
| `degraded` | boolean | no | `false` | True when the deterministic fallback reasoner produced it (no LLM) |
| `created_at` | string (ISO-8601) | no | now | |
| `lineage` | object (`Lineage`) | no | empty `Lineage` | |

Real line, with `hypotheses`/`evidence`/`actions` shortened and everything else verbatim:

```json
{"topic":"brian.rca","key":"alrt_5904362fb7ce","value":{"rca_id":"rca_1e80da766042","alert_id":"alrt_5904362fb7ce","decision_id":"dec_1e5a53bf255d","service":"checkout-api","summary":"checkout-api was flagged as dependency_failure (major). A dependency is failing or slow.","root_cause":"A dependency is failing or slow: the local connection pool to it is saturated at 98%","hypotheses":[{"cause":"A dependency is failing or slow: the local connection pool to it is saturated at 98%","confidence":0.6,"evidence_for":["upstream 9022 (baseline 132.40, 68.1x, z=+1772)","db pool 0.98 (baseline 0.36, 2.7x, z=+12.9)"],"evidence_against":["local CPU is not saturated (37%), so the service is waiting rather than working"]},{"cause":"Local resource exhaustion: db pool at 98%","confidence":0.55,"evidence_for":["db pool 0.98 (baseline 0.36, 2.7x, z=+12.9)"],"evidence_against":[]}],"evidence":["errors 0.39 (baseline 0.00, 1296x, z=+5885)","latency ms 12493 (baseline 177.04, 70.6x, z=+975.7)","... 5 more evidence strings ..."],"actions":[{"step":"Check the dependency's own health and latency before touching checkout-api","rationale":"The pool is saturated, which is usually a symptom of the dependency being slow, not of checkout-api failing.","risk":"low","autonomous":false,"verify":"The dependency's error rate and p99 latency return to baseline."},"... 3 more proposed actions ..."],"confidence":0.31887,"model_id":"offline-rule-reasoner","prompt_version":"rules/1","latency_ms":0.1876000314950943,"degraded":true,"created_at":"2026-09-27T16:55:53.369489Z","lineage":{"upstream_ids":["dec_1e5a53bf255d","dgst_1bb67a3e364a"],"producer":"offline-rule-reasoner","model_id":null,"prompt_version":"rca/1","code_version":null}},"headers":{"record_type":"RcaReport"},"timestamp":1790528153.3696005,"offset":0}
```

### 4.8 `outcomes` — no contract class yet

`Topic.OUTCOMES` exists in the catalogue, is keyed by `alert_id`, and `Pipeline.lineage_of` indexes
records from it by `outcome_id`. But `contracts.py` defines no `Outcome` dataclass, `RECORD_TYPES`
contains only `Event`, `Digest`, `Decision`, `Alert` and `RcaReport`, and nothing in this repository
appends to `brian.outcomes`. The intended shape, implied by the index in `lineage_of`, is a record
carrying `outcome_id` and a `lineage` whose `upstream_ids` include the `alert_id` (or `decision_id`) it
closes. Treat the topic as reserved until a contract is added.

### 4.9 `dlq` — the dead-letter record

Not a contract dataclass: `_publish_dlq` appends a plain dictionary (see section 7).

| Field | JSON type | Meaning |
| --- | --- | --- |
| `source_topic` | string | Full prefixed topic the failed record came from, e.g. `brian.features` |
| `source_id` | string | Id of the record that failed (`digest_id` or `alert_id`) |
| `service` | string | Also the partition key |
| `error` | string | `"<ExceptionType>: <message>"` |
| `payload` | object | The record as it was, so it can be replayed after a fix |
| `at` | number | `time.time()` epoch seconds |

### 4.10 Enumerations and derived sets

| Enum | Values |
| --- | --- |
| `Verdict` | `anomaly`, `normal` |
| `Severity` | `info`, `minor`, `major`, `critical` — ranks 0-3; `from_rank` maps a continuous score (`<0.5` info, `<1.5` minor, `<2.5` major, else critical) |
| `IncidentCategory` | `resource_exhaustion`, `dependency_failure`, `traffic_surge`, `data_quality`, `config_or_deploy`, `security`, `network`, `none` |
| `DecisionAction` | `observe`, `auto_triage`, `deep_analysis`, `page_human`, `suppress` |
| `ALERTING_ACTIONS` | `auto_triage`, `deep_analysis`, `page_human` — the only actions that produce an `Alert` |
| `HUMAN_ACTIONS` | `page_human` — the only action that pages |

Every enum has a `coerce()` that raises `ContractError` on an unknown value, which is how a bad record
becomes a DLQ candidate instead of a crash.

`Bus.publish` sets a `record_type` header on every record (the class name — `Event`, `Digest`,
`Decision`, `Alert`, `RcaReport` — or `Dlq`), and a consumer can dispatch on it through
`RECORD_TYPES`. Note a divergence on the Flink path: `CollectorBus.append` collects
`(topic, key, value)` and `TopicProducer.send(topic, key, value)` produces without a headers argument,
so **records published by the Flink job carry no `record_type` header**; dispatch there is by topic,
not by header. The `KafkaBus` and `JournalBus` paths do write the header.

## 5. The lineage walk

`Pipeline.lineage_of(record_id)` is the audit query, implemented:

1. It replays six topics in order — `events`, `features`, `decisions`, `alerts`, `rca`, `outcomes` —
   and indexes every id field it knows (`event_id`, `digest_id`, `decision_id`, `alert_id`, `rca_id`,
   `outcome_id`) to `{topic, record}`. A record carrying both `decision_id` and `alert_id` (an
   `RcaReport`) also registers the `alert_id` if it is not yet indexed.
2. It breadth-first walks `lineage.upstream_ids` from the given id, skipping already-seen ids and
   collecting `{id, topic, record}` for every hop.
3. It reverses the chain, so the result is in causal order, **oldest first**.

Two consequences: the walk follows `upstream_ids` only, so starting from an `event_id` returns just
that event (nothing points *up* from it) — start from the newest record you care about; and the `dlq`
topic is not in the index, so a dead-lettered record does not appear in a lineage walk. Read
`brian.dlq` directly for those.

### Worked example: "why did the system page me at 10:08, and on what evidence?"

The 10:08 page is an outcome or an alert. Taking a real alert id from the local journal:

```bash
python -m brian lineage alrt_5904362fb7ce
```

The command opens the bus, constructs a `Pipeline`, calls `lineage_of`, and prints each record in the
chain oldest first with a one-line summary per topic. The chain it walks:

| Step | Topic | Record | What it contributes |
| --- | --- | --- | --- |
| 1 | `events` | `evt_*` (dozens) | The raw samples: what was actually observed, and when |
| 2 | `features` | `dgst_1bb67a3e364a` | The window snapshot that was judged, with `metrics` and the exact `interpretation` text Laya read |
| 3 | `decisions` | `dec_1e5a53bf255d` | Verdict, category, severity, calibrated probability, `gate_reason`, and `routing.model` — the checkpoint that judged it |
| 4 | `alerts` | `alrt_5904362fb7ce` | The promotion to an operator: severity, action `page_human`, `dedup_key` |
| 5 | `rca` | `rca_1e80da766042` | Root cause, ranked hypotheses with their own confidences, evidence, proposed actions |

Starting from an `outcomes` record adds one hop at the front, because its `upstream_ids` point at the
alert and/or the decision — which is exactly why that topic is keyed by `alert_id`: the outcome and
the reasoning it closes stay in one ordered stream. No `outcomes` producer exists yet (section 4.8),
so today the deepest starting point a running system will have is an alert or an RCA report.

The same walk answers the harder question directly. `lineage` on the `Decision` carries
`producer: "gate"`, `model_id: "typed-decisions"` and `prompt_version: "digest/1"`, and `lineage` on
the `Digest` carries `code_version: "digest/1"` — so the exact model version and digest-rendering
version that produced the page are part of the trail, not a deployment ticket.

## 6. Write ordering guarantees

`Pipeline.process_digest` documents its own ordering, and the order carries the durability argument:

| # | Step | Published to | Why here |
| --- | --- | --- | --- |
| 0 | `ingest` appends the raw event | `events` | The raw event is durable **before it is ever judged** |
| 1 | Publish the digest | `features` | The feature snapshot becomes replayable before anyone interprets it |
| 2 | Laya decides | — | The only inference in the hot path |
| 3 | Gate | — | Policy turns probabilities into permission |
| 4 | Publish the decision | `decisions` | The judgement is durable before anything acts on it |
| 5 | Publish the alert | `alerts` | A human is notified even if System 2 is down |
| 6 | System 2 reasons | — | The slow, optional half |
| 7 | Publish the report | `rca` | The explanation joins the same trail |

**A reasoner outage degrades the explanation, never the notification.** The decision (step 4) and the
alert (step 5) are on disk before the LLM is called (step 6). If reasoning raises, the pipeline
dead-letters and returns — the alert is already published. "The LLM was down for an hour" is a
footnote about missing `rca` records, not a gap in the incident record.

**A crash mid-judgement loses nothing.** The event was appended at step 0 and the digest at step 1. If
the process dies between steps 1 and 4, the window is still fully re-derivable: `brian replay`
rebuilds digests from `events` alone, and the `features` record preserves the exact snapshot that was
about to be judged. Nothing load-bearing existed only in memory.

**A Laya outage cannot invent a verdict.** On `LayaError` the pipeline does not fabricate a decision:
it increments the DLQ counter, appends a DLQ record describing the failure with the digest as
`payload`, and returns the trace with `decision is None`. The window can be re-judged from the stored
events once Laya is back — the point of storing the digest as well as the events.

**On the Flink path the same order becomes atomic.** The operator gives `Pipeline` a `CollectorBus`
that *collects* instead of writing, so `process_digest` runs unchanged and its publish order is
reproduced exactly as a list. The operator then flushes that list through `TopicProducer`, a single
producer with a `transactional_id` (`init_transactions` / `begin_transaction` / `commit_transaction`)
whose pending records are checkpointed and re-emitted on recovery. A single input event can produce up
to five output records across four topics, so the consequence is the property the design rests on: a
window is either fully judged — digest, decision, alert and report all in the memory — or not at all.
A half-written trail would be worse than no trail, because it would look complete.

Two related behaviours: a window below `min_events` is not judged, but its digest is still published,
so the gap is visible in the memory rather than absent from it; and the baseline tracker is updated
*after* the decision (`baselines.observe` is called at the end of `drain_ready`, and on the Flink side
after the timer's windows are judged), so a window never contributes to the baseline it is compared
against — otherwise a slow-burn incident would baseline itself into normality.

## 7. Dead letter topic

`brian.dlq` holds records that could not be processed, with enough context to replay them after the
cause is fixed. `_publish_dlq` writes the flat dictionary of section 4.9, keyed by `service`.

| Cause | Trigger in code | `source_topic` | `source_id` | `payload` |
| --- | --- | --- | --- | --- |
| Laya unreachable | `LayaError` from `self.laya.decide(digest)` | `brian.features` | `digest.digest_id` | `digest.to_dict()` |
| Reasoning exception | Any exception from `self.rca.analyse(...)` | `brian.rca` | `alert.alert_id` | `decision.to_dict()` |
| Contract violation | Intended: a malformed record raises `ContractError` from `loads`. **In `pipeline.py` today `ContractError` is imported but not caught**, so it surfaces at the call site; `brian replay` skips unreadable events with `skipping unreadable event <record_id>: <error>` rather than dead-lettering them | — | — | — |

Shape of the record as `_publish_dlq` builds it:

```json
{"source_topic":"brian.features","source_id":"dgst_1bb67a3e364a","service":"checkout-api","error":"LayaUnavailable: cannot reach http://127.0.0.1:8077","payload":{"digest_id":"dgst_1bb67a3e364a","service":"checkout-api","window_start":"2026-01-14T10:00:00Z","window_end":"2026-01-14T10:01:00Z","event_count":72,"metrics":{},"interpretation":"...","lineage":{"upstream_ids":["evt_1f0615fe8049"]}},"at":1790528153.42}
```

Replay after a fix:

```bash
python -m brian tail dlq --raw --limit 20     # what failed, and why
python -m brian replay --decide --limit 100000 # rebuild from events, not from the DLQ
```

Replaying from `events` is preferred over replaying the DLQ `payload` directly, because it produces a
trail independent of the failed attempt and therefore auditable against it. `brian replay` reads
`events` and does **not** read `features` or `decisions`. If a rebuild disagrees with a stored
decision, either the renderer or the question catalog changed — which the version fields will show —
or something is wrong; either way, that is exactly what an audit is for.

## 8. Retention and replay

Retention here is a recommendation, not a measured value: no cluster has been configured or observed
in this project. The numbers follow from what each topic *is*, and from the rule that the two topics
which make an audit possible must outlive everything else.

| Topic | Recommended retention | Rationale |
| --- | --- | --- |
| `events` | **Longest — 30-90 days, or indefinite** | The system of record and the rebuild source: `brian replay` reconstructs digests and decisions from `events` alone, so they define the furthest back an independent audit reaches. Volume is the highest of any topic, which is what bounds the number |
| `features` | **As long as `decisions`** | Stored specifically so a decision can be re-derived, or re-judged by a new model version, forever, without re-reading the raw firehose. A snapshot is far smaller than the events behind it |
| `decisions` | **Effectively permanent** | The audit record: what the system concluded, on which rubric, at which probability, with which model version, and what the gate did about it. Deleting one deletes the answer to "why did you page me?" |
| `alerts` | 1-3 years | The operator-facing promotion. Cheaper to keep and less load-bearing as evidence, because it references its decision rather than duplicating it; `dedup_key` makes long retention useful for spotting recurring incidents |
| `rca` | **Effectively permanent** | The explanation of record: root cause, ranked hypotheses with confidences, evidence, and proposed actions including `autonomous`. This is what a post-incident review reads |
| `outcomes` | **Effectively permanent** | The labelled set. Closed alerts are the only source of ground truth for calibration and fine-tuning, and `brian calibrate` becomes possible only once they exist |
| `dlq` | 30-90 days, or until drained | Operational, not evidentiary. Keep it until the fix is applied and the records replayed; after that `events` is the durable copy of the same information |

**Why `features` exists at all.** Keeping only `events` and re-deriving digests on demand would work,
but the re-derivation would pin the answer to today's renderer version. Storing the digest fixes the
*input* that was judged, so a new model version can be scored against exactly the states the old one
saw, on the same rendering (`digest_version`), indefinitely and cheaply. `brian replay --decide`
demonstrates the mechanism: it rebuilds digests from `events`, and `brian eval` scores decisions
against them.

Replay is address-based, which is what makes retention useful rather than decorative:

| Operation | API | CLI |
| --- | --- | --- |
| Read from an offset, in order | `bus.replay(topic, start_offset, limit)` | `brian tail <topic> --offset N --limit M` |
| Follow a topic | `bus.tail(topic, start_offset, stop, timeout_s)` | not exposed |
| Resume exactly | `bus.latest_offset(topic)` + 1 | — |

`Record.offset` is the unit of replay and of checkpointing: a consumer that records the offset of the
last record it processed resumes with no duplicates and no gaps. `Record.record_id` is the
human-quotable coordinate, `topic@offset`, which is how a decision is cited in a discussion.

## 9. Production Kafka setup

**Partition counts — recommendations.** The code defaults are **3 partitions** and **replication
factor 1** (`ensure_topics(bus, settings, partitions=3, replication=1)`, and `brian topics
--partitions 3`). Replication factor is not a CLI flag; it is a parameter of `ensure_topics` that a
caller must set. Creation is explicit: on a Kafka backend `ensure_topics` uses `KafkaAdminClient` to
create the topics that do not already exist, so partitioning is chosen by this design rather than by a
producer default, and on a journal or memory backend it is a no-op that reports intent.

| Topic | Recommended partitions | Basis |
| --- | --- | --- |
| `events` | At least the number of distinct services, rounded up (e.g. 12 for 10 services) | Keyed by `service`; one partition per key group is the unit of parallelism, and headroom keeps a hot service from sharing a partition with every other service |
| `features` | Same as `events` | Same key space, one digest per service-window |
| `decisions` | Same as `events` | Same key space; a service's verdict history must stay ordered |
| `alerts` | Same as `events`, or half if alert volume is low | Keyed by `service`; per-operator-stream ordering matters more than parallelism, and volume is far below `events` |
| `rca` | 3-6 | Keyed by `alert_id`; one record per promoted alert |
| `outcomes` | 3-6 | Keyed by `alert_id`; one record per closed alert |
| `dlq` | 1-3 | Should normally be near-empty; a large DLQ is a signal, not a workload |

Replication factor: use the cluster's standard production factor (**3** is the conventional choice) for
`events`, `features`, `decisions`, `rca` and `outcomes`. The code default of 1 is a development
setting — with Kafka as the memory, an unreplicated broker failure loses the trail this design exists
to preserve.

**`acks=all`.** `BRIAN_KAFKA_ACKS` defaults to `all`, and `KafkaBus` builds the producer with
`acks=settings.acks`, `linger_ms=5`, `request_timeout_ms=15000`, `retries=3` and
`client_id=settings.client_id`. The Flink path's `TopicProducer` reads the same three settings from
`KafkaSettings` and adds `transactional_id`; it does not set `retries`. The default is `all` on
purpose: Kafka is the memory, so fire-and-forget would defeat the entire point — a producer that
acknowledges before the in-sync replicas hold the record means an acknowledged alert can still
disappear.

**Consumer group naming.** The readers built into the CLI deliberately use **no consumer group**:
`KafkaBus._consumer` sets `group_id=None` with `enable_auto_commit=False` and
`auto_offset_reset="earliest"`, addressing records by explicit `seek` on assigned partitions. That is
an assignment-free read — `brian tail`, `brian lineage` and `brian replay` are lookups by offset, not
competing consumers, so they must not join a group or commit offsets a streaming consumer depends on.

Exactly one group id is declared in this codebase, by the Flink source:

| Consumer | Group id | Source |
| --- | --- | --- |
| Flink digest job reading `events` | `brian-digest` | `KafkaSource...set_group_id("%s-digest" % settings.kafka.client_id)` in `flink/job.py`, so it tracks `BRIAN_CLIENT_ID` |
| Any other streaming consumer | **not defined in code** | A service consuming `alerts`, `rca` or `outcomes` must choose its own group; follow the same `<client_id>-<purpose>` shape, e.g. `brian-alert-router`, `brian-rca-worker`, `brian-outcome-recorder` |
| Ad-hoc audit (`tail`, `lineage`, `replay`) | none | Assignment-free by design |

Separate groups get separate offsets, which is what lets the Flink job reprocess a range by resetting
its own group offsets without disturbing anything else. On the producing side the Flink job sets
`client_id` to `<BRIAN_CLIENT_ID>-flink` (that is `brian-flink` by default), so its records are
distinguishable from the CLI's.

**Running the Flink job.** The production path is the job itself, not a wrapper:

```bash
pip install apache-flink kafka-python
flink run -py src/brian/flink/job.py --bootstrap 127.0.0.1:9092 --laya-url http://127.0.0.1:8077
```

Its CLI defaults are `--parallelism 2`, `--checkpoint-interval-ms 30000` and `--out-of-orderness-s 10`
— note that the job's bounded out-of-orderness (10 s) and `allowed_lateness_s` (30 s) are separate
knobs. Throughput is bounded by the synchronous Laya call inside `processElement` (roughly 10-28
windows/second at 35-100 ms per decision, per the job's own note); since a window is 60 seconds of one
service's traffic, that is not the bottleneck at any realistic service count.

**Replay bound.** `KafkaBus.replay` reads a bounded slice (`max_replay_records`, default 200000) and
sorts the result by `(timestamp, offset)` before returning, because Kafka interleaves partitions and a
rebuild needs a deterministic order rather than broker arrival order. Asking for 100 records from
offset 5,000 costs 100 records, not 5,100. With multiple partitions, `start_offset` addresses each
partition's own log.

**Backend selection.** `BRIAN_BUS_BACKEND` chooses the transport:

| Value | Behaviour |
| --- | --- |
| `kafka` | `KafkaBus` only; fails loudly if the cluster is unreachable — the right setting in production |
| `journal` | `JournalBus` only; never tries Kafka |
| `memory` | `MemoryBus`: volatile, correct semantics, no durability — tests and one-shot runs |
| `auto` (default) | Try `KafkaBus`; on failure fall back to `JournalBus` if the broker is unreachable |
| anything else | `BusError: unknown BRIAN_BUS_BACKEND=... (auto|kafka|journal|memory)` |

`ensure_topics` and the topic catalogue are backend-independent: `brian topics` prints the same plan on
every backend and creates topics only on Kafka. `brian demo --bus memory` overrides the configured
backend for a single run.

## 10. The journal fallback

`JournalBus` is one append-only JSONL file per topic in `BRIAN_JOURNAL_DIR` (`<repo>/var/journal` by
default), with two properties that make it a real backend rather than a stub.

**The line number is the offset.** `append` writes one JSON object per line and derives the next
offset from a cached line count, so `replay(topic, start_offset)` means the same thing here as on a
cluster — which is what lets a decision made offline be re-derived byte-for-byte later.

**Every append is flushed and fsynced.** `fh.write(...)`, `fh.flush()`, then `os.fsync(fh.fileno())`
unless `fsync=False` was passed. The durability is the point: a memory that loses its tail on a crash
cannot be used to explain an incident that crashed the process.

| Property | Behaviour |
| --- | --- |
| File naming | `<topic>.jsonl` with dots kept verbatim — `brian.events.jsonl` — so the file name is the topic name and can be grepped |
| On-disk line shape | `{"topic", "key", "value", "headers", "timestamp", "offset"}`, compact separators, `ensure_ascii=false` |
| Torn final line | A line that fails `json.JSONDecodeError` is skipped, not fatal: one damaged record must not make the whole memory unreadable |
| Unknown topic | `replay` on a file that does not exist returns `[]` rather than raising |
| Delete | `JournalBus.reset(topic=None)` unlinks every `*.jsonl` (or one topic); only ever called by `brian reset` |
| `describe()` | `{"backend": "journal", "dir": ..., "topics": {name: count}}` |

Because both backends honour one interface, the same code path runs in a demo, in CI and against a
cluster: `bus.append` to write, `bus.replay` to read, with no branch anywhere above `bus.py`.
`Pipeline` never asks which backend it holds; the only place the backend name is read is
`ensure_topics`, to decide whether to call the admin client.

A fourth `Bus` class exists, but it is not selectable by `BRIAN_BUS_BACKEND`: `CollectorBus` in
`src/brian/flink/job.py` (backend `"collector"`) is the seam that lets `Pipeline.process_digest` run
inside a Flink operator unchanged. It *collects* `(topic, key, value)` instead of writing, and its
`replay` raises `NotImplementedError` on purpose — a streaming operator re-reading its own output topic
would be a correctness bug, and an empty result would hide it. Use `brian replay` (the in-process
pipeline) for that.

**Why `auto` announces the fallback.** With `verbose=True` (the default), `open_bus` prints when Kafka
is not reachable:

```
[brian] Kafka at 127.0.0.1:9092 is not reachable (<ExcType>); using the append-only journal at <dir>.
        Set BRIAN_BUS_BACKEND=kafka to fail instead of falling back.
```

Silently judging without the cluster would be the worst outcome, because the trail would look complete
while missing everything another process wrote. The fallback is announced, and it distinguishes two
cases: if a cheap TCP probe (`_kafka_reachable`, a 2-second timeout on the first bootstrap host) says
something is listening, the original exception is re-raised rather than masked — a reachable broker
that rejects us is a real error, not a reason to write to a local file.

## 11. Verifying the memory

| Command | What it proves |
| --- | --- |
| `python -m brian topics` | The plan matches the catalogue: backend, a `topic` / `key` / `purpose` table, then `N topics ensured`. `--partitions N` changes the requested partition count |
| `python -m brian tail decisions --limit 20` | Decisions are on the bus, in order, with offsets. Prints `<topic>@<offset>` plus `verdict= action= p= category= severity= confidence=` per record |
| `python -m brian tail alerts --raw` | The full JSON of each alert, unsummarised — the fastest check that `lineage.upstream_ids` and `dedup_key` are what you expect |
| `python -m brian replay --decide` | The events on the bus suffice to rebuild windows and re-ask Laya: one line per rebuilt window, plus the fresh verdict, category and severity. Independent of the stored `features` and `decisions` |
| `python -m brian lineage <id>` | The audit walk works end to end for a real id (e.g. `alrt_5904362fb7ce`), printing the chain oldest first. Exits 2 with `no record with id '...' in the memory (checked every topic)` when the id is unknown |
| `python -m brian reset --yes` | The journal is disposable and deletion is explicit: without `--yes` it lists the files and exits 1; with `--topic events` it deletes one topic only |

Companions: `python -m brian config --probe` (masked settings, topic plan, gate policy, live backend
`describe()`), `python -m brian demo` (the full chain, ending with `memory (what is stored for the
audit trail)`), `python -m brian eval` (score System 1 against labelled windows before trusting the
trail) and `python -m brian calibrate` (fit the confidence correction that makes the gate's thresholds
mean something).

## 12. Environment variable reference

Precedence is real environment first, then `.env` (or `BRIAN_ENV_FILE`), then the default. A variable
set to the empty string counts as unset. Flags accept `1`, `true`, `yes`, `on` (case-insensitive) as
true and **any other non-empty value as false**, so `BRIAN_ECHO=maybe` means `False`, not an error.
The `.env` format is `KEY=value` lines, with an optional `export ` prefix, `#` comments, and
surrounding quotes stripped.

### Memory / Kafka

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_KAFKA_BOOTSTRAP` | `127.0.0.1:9092` | Comma-separated broker list for producer, admin client and the reachability probe |
| `BRIAN_BUS_BACKEND` | `auto` | `auto` \| `kafka` \| `journal` \| `memory` — selects the bus |
| `BRIAN_TOPIC_PREFIX` | `brian` | Prefix applied by `Topic.name`: `brian.events`, `brian-staging.events`, ... |
| `BRIAN_JOURNAL_DIR` | `<repo>/var/journal` | Directory for the JSONL journal (the dataclass default is the relative `var/journal`; `load_settings` resolves it against the repo root) |
| `BRIAN_CLIENT_ID` | `brian` | Kafka `client_id` for producer, admin client and consumers |
| `BRIAN_KAFKA_ACKS` | `all` | Producer `acks`; `all` means every in-sync replica |
| `BRIAN_MAX_REPLAY_RECORDS` | `200000` | Cap on a single `KafkaBus.replay` with no `limit`, so an unbounded replay on a large topic cannot exhaust memory |

Not settable from the environment (dataclass fields only): `linger_ms=5`, `request_timeout_ms=15000`,
and the hardcoded producer `retries=3`.

### System 1 — Laya

| Variable | Default | Effect |
| --- | --- | --- |
| `LAYA_URL` | `http://127.0.0.1:8077` | Decision-engine endpoint; a trailing `/` is stripped |
| `BRIAN_LAYA_MODEL` | `typed-decisions` | Checkpoint name sent with every question set (`english` for the base checkpoint) |
| `BRIAN_LAYA_TIMEOUT_S` | `60.0` | Per-request read timeout |
| `LAYA_API_KEY` | unset | Bearer credential, if the service requires one |
| `BRIAN_LAYA_CACHE_SIZE` | `4096` | Digest-answer cache size. Laya is deterministic, so identical digests may be answered from cache — the biggest steady-state latency win |

Not settable from the environment: `connect_timeout_s=5.0`, `max_retries=2` (transport failures only —
a 4xx is a bug in the request, not a blip), `retry_backoff_s=0.5`, `batch_questions=True`.

### System 2 — LLM reasoner

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_LLM_PROVIDER` | `deepseek` | Provider label recorded on the engine |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Base URL for the chat-completions API |
| `BRIAN_LLM_MODEL` | `deepseek-chat` | Model name |
| `DEEPSEEK_API_KEY` (or `LLM_API_KEY`) | unset | Credential; `DEEPSEEK_API_KEY` wins. With neither set the engine runs offline |
| `BRIAN_LLM_TIMEOUT_S` | `120.0` | Request timeout; this half is allowed to be slow |
| `BRIAN_LLM_MAX_TOKENS` | `2048` | Response cap |
| `BRIAN_LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `BRIAN_LLM_OFFLINE` | `0` | `1` forces the deterministic template reasoner even when a key is present. A first-class mode, not an error path: reports are written with `degraded: true` and `model_id: "offline-rule-reasoner"` |
| `BRIAN_LLM_MAX_CONTEXT_EVENTS` | `20` | Caps the prompt at the digest plus the k most recent events; unbounded prompts are how an RCA bill becomes unpredictable |

Not settable from the environment: `prompt_version="rca/1"`.

### Gate policy

Every judgement-changing number lives in `GateThresholds`. Two independent scales decide the action:
`anomaly_probability` decides **whether** there is an incident, and `confidence` decides **how much
the system is allowed to do about it**.

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_GATE_ANOMALY_P` | `0.5583` | P(anomaly) at or above this is treated as an anomaly at all |
| `BRIAN_GATE_NOTIFY_P` | `0.5583` | P(anomaly) at or above this is alert-worthy even when class/severity confidence is low |
| `BRIAN_GATE_CONF_AUTO` | `0.75` | Confidence needed to act on category and severity without System 2 |
| `BRIAN_GATE_CONF_FLOOR` | `0.45` | Below this the judgement is too weak to route at all, so it becomes `deep_analysis` |
| `BRIAN_GATE_PAGE_RANK` | `2` | Severity rank at or above which a human is paged (0 info, 1 minor, 2 major, 3 critical) |
| `BRIAN_GATE_SUPPRESS_CONF` | `0.70` | Confidence needed to suppress. Deliberately higher than `anomaly_probability`: a false silence is worse than a false alert |
| `BRIAN_GATE_NORMAL_P` | `0.35` | P(anomaly) at or below this counts as a confident normal when confidence is high |

Not settable from the environment: `trust_needs_human=True` (a confident `needs_human` verdict pages
whatever the severity) and `always_analyze_alerts=True` (System 2 always explains a promoted alert).

### Windowing

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_WINDOW_SIZE_S` | `60` | Window length in seconds |
| `BRIAN_WINDOW_SLIDE_S` | `60` | Slide; equal to size means tumbling windows |
| `BRIAN_ALLOWED_LATENESS_S` | `30` | How long to wait for late events before closing a window |
| `BRIAN_IDLE_TIMEOUT_S` | `120` | Watermark idle timeout; without it, one silent service stalls every window |
| `BRIAN_BASELINE_WINDOWS` | `30` | Number of baseline windows kept per service |
| `BRIAN_MIN_WINDOW_EVENTS` | `10` | Minimum events for a window to be judged at all; below this the statistical basis is too thin to accuse a service of anything, and the digest is still published |

### Calibration

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_CALIBRATION_PATH` | `models/calibration/anomaly_temperature.json` | Fitted confidence calibration, resolved against the repo root so it travels with the code |
| `BRIAN_CALIBRATION_ENABLED` | `1` | `0` reproduces raw model behaviour exactly (T=1) — the right setting for a benchmark that is measuring the checkpoint rather than operating it |

### Process

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIAN_LOG_LEVEL` | `info` | Log level |
| `BRIAN_ECHO` | `0` | `1` prints every published record to stdout as JSON; useful when the journal is the only memory |
| `BRIAN_ENV_FILE` | `<repo>/.env` | Alternative `.env` path |

`brian config` prints these settings through `Settings.masked()`, which replaces both API keys with
`"set"` / `"unset"` and reports the calibrated state (`loaded`, `slope`, `intercept`,
`equivalent_temperature`, `fitted_on`, `auc`, `fitted_at`, `notes`) — the safe way to attach
configuration to a ticket.
