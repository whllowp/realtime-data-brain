# 服务侧真实数据集：选型与第一轮评估

> 目标：补上这套架构**唯一未被检验**的部分——服务侧遥测（QPS / 错误率 / 延迟）。
> 此前的所有真实数据验证都在 SMD 的主机遥测上（CPU/load/内存/磁盘/网络），而那并不是 catalog 的主体：
> 13 个指标源里有 11 个是服务侧的，SMD 一个都填不上。

---

## 1. 候选与结论

| 数据集 | 规模 | 模态 | 标注粒度 | 结论 |
|---|---|---|---|---|
| **AnoMod**（Zenodo 18342898） | 2 系统 × 13 run，192 MB | 指标 + trace + 日志 + 覆盖 | **run 级**（1 个 Normal） | 已下载评估，**不足以支撑检测评测** |
| **RCAEval**（Zenodo 14504481 / HF） | 9 数据集 × 735 case，RE1 372 MB / RE2 4 GB / RE3 509 MB | 指标（RE2/RE3 另含 trace、日志） | case 级 + 根因服务/指标标注 | **下一轮采用**，RE1-OB（125 case，49–59 指标） |
| SMD（已用完） | 2 机器 × 23,700 行 | 主机指标 | 逐分钟点级 | 已完成 10 轮优化，见 OPTIMIZATION_LOG |

---

## 2. AnoMod：能取到什么、差在哪里

许可证 CC-BY-4.0，结构清晰，按故障类型分目录：

```
SN_data/{api_responses,metric_data,trace_data,log_data,coverage_*}
  Normal_Baseline_*  Code_Stop_MediaService_*  DB_Redis_CacheLimit_*
  Perf_CPU_Contention_*  Svc_Kill_Media_*
```

### 2.1 `api_responses/` —— 不可用

每个 case 恰好 **150 个请求、58 秒、2.58 req/s**，且 **100% 是 4xx**：
工作负载由 EvoMaster 生成，请求本身畸形（`"Incomplete arguments"`），没有 5xx、没有时间分辨率。
拿它算错误率，量的是 fuzzer，不是服务。

### 2.2 `metric_data/` —— 只有基础设施指标

每个 case 16 个 CSV，全是 `container_*` / `system_*`（cpu、memory、network、disk、load）。
按文件名逐个数过：**没有任何 HTTP 请求数、错误数或延迟直方图**。
`jaeger_spans_rate.csv` 只有 `result="dropped"` 的零值。

### 2.3 `trace_data/` —— 唯一可用，但太薄

`all_traces.csv` 每行一个 span，列为
`trace_id, span_id, parent_span_id, service, operation, start_time, duration_us, http_status_code, http_method, http_url, component, tags, logs`。

这足以构造服务侧黄金信号，见适配器 `scripts/service_data/traces_to_events.py`。

| 事实 | 数值 |
|---|---|
| case 数 | 13（其中 **Normal 只有 1 个**） |
| 每个 case 时长 | 1,008–1,351 s（约 17–22 分钟） |
| 每个 case 的 span 数 | 1,540–2,219 |
| 服务数 | 12（其中 8 个在全部 case 中出现） |
| 状态码 | 5,012 × 200、258 × 500 |
| **5xx 出现范围** | **只有 3 个 `Code_Stop_*` case**（70 / 90 / 98 条），其余 10 个 case 全 0 |
| 300 s 窗口下每服务入口 span | 10–23 个 |
| 每 run 的窗口数 | 4 |

**结论：能证明"服务侧链路可被真实数据驱动"，但不能支撑检测评测。**
一个正类丰富、负类只有 1 个 run（4 个窗口）的数据集，任何阈值都能"拟合"出一个好看的召回。

### 2.4 没有标注文件

按 `label|ground|inject|truth|annotation` 搜索了全部 44,291 个 zip 条目，命中的只有 coverage 报告里的 CSS/HTML。
**没有任何注入时间或逐分钟标签文件**——故障区间只能从目录名推断，而目录名只给类型与采集开始时间。

---

## 3. 已交付的可复用资产

| 文件 | 作用 |
|---|---|
| `scripts/service_data/profile_anomod.py` | 逐 case 的请求数/时长/状态码/延迟分位画像 |
| `scripts/service_data/profile_traces.py` | span 密度、服务集合、状态码分布 |
| `scripts/service_data/traces_to_events.py` | **trace → 每服务每窗口的 `requests.rate` / `errors.ratio` / `latency_ms.p50/p99`** |

适配器里有两个会静默改变结果的决定，已写进注释：

1. **只把 5xx 记为错误**。4xx 在客户端观测里 100% 是 fuzzer 的畸形请求，计入就等于量 fuzzer。
2. **请求数只数入口 span**（`operation` 以 `_server` 结尾），否则一次用户请求会被内部调用放大成多次。

验证输出（Normal run，60 s 窗口）：

```
window_start               req     req/s error_ratio     p50_ms     p99_ms
22:05:14                     3     0.050           -       0.02       0.13
22:11:14                     3     0.050           -       0.52       2.00
22:13:14                     8     0.133           -       0.57       2.25
```

---

## 4. RCAEval RE1-OB：找到并跑通（本轮结论）

### 4.1 数据确认

从 Hugging Face 取（`scripts/service_data/fetch_rcaeval.py`，125 case，32.3 MB，`data/` 已 gitignore）：

| 事实 | 数值 |
|---|---|
| case 数 | **125** = 5 服务 × 5 故障类型 × 5 次重复 |
| 每 case 时长 | **4,200 s**（cpu 类）或 720 s（delay 类），**cadence = 1 s** |
| 标签 | `inject_time.txt` 给注入时间戳；录制为 **注入前 ~2,100 s + 注入后 ~2,100 s** |
| 每服务信号 | `<svc>_cpu`、`<svc>_mem`（全部 case）；`<svc>_load`(QPS) + `<svc>_latency`（50 个 case）；`<svc>_latency-50/-90`（75 个 case）；`frontend_error` / `PassthroughCluster_error` |

**这解决了 AnoMod 的根本缺陷**：有逐秒数据、有注入时刻，因此可以做逐窗口标签（前 = 正常，后 = 故障），
而不是 1 个 Normal run 对 12 个故障 run。

### 4.2 出厂 service 路径在真实服务遥测上跑通

`scripts/service_data/rcaeval_to_events.py` 把 case 转成事件流；**复用出厂 catalog 的指标键名**
（`requests.rate` / `errors.ratio` / `latency_ms.p99` / `cpu.util`），因此 `render_digest(profile="service")`
原样产出生产文本，不做任何特判。实测输出：

```
Service adservice, a 60-second window with 4 events.
Error fraction 0.00% against a baseline of 0.00%.
Latency 4.7 ms against a baseline of 4.7 ms (1.00x).
Resources: CPU 1.68%.
Throughput 59.7 requests per second against a baseline of 58.1 req/s (1.03x).
```

### 4.3 第一个真实缺陷：CPU 从 1.7% 升到 96%，模型判为正常

注入 CPU 压力后，同一 case 的窗口：

```
Service adservice, a 60-second window with 4 events.
Error fraction 0.00% against a baseline of 0.00%.
Latency 4.7 ms against a baseline of 4.7 ms (1.00x).
Resources: CPU 96.05%.
Throughput 56.6 requests per second against a baseline of 58.7 req/s (0.96x).
```

| 窗口 | CPU | 模型 P(anomaly) | 判定 |
|---|---|---|---|
| 注入前 | 1.68% | 0.378 | 正常 |
| 注入后 | **96.05%** | **0.389** | **判为正常** |
| 注入后 | **99.43%** | **0.365** | **判为正常** |

原因直接可读：`Resources: CPU 96.05%` **不带任何参照**，而错误率与延迟都带了
`against a baseline of …`。模型拿不到"96% 对这个服务算不算高"的信息，于是无法判断——
这与 SMD 上"资源行缺基线"的发现**在同一位置复现**，只是这次是在服务侧真实数据上。

这也解释了为什么 SMD 上 `Resources` 加基线会让合成 HTTP 场景 FPR 变差（那是把两个 profile 混在一起）：
正确的解法是给 service profile 的资源行一个**同尺度的参照**，而不是删掉参照。

**下一轮**：把 `Resources:` 行的基线补上（并测量它对合成 HTTP 套件的影响），然后跑全量 125 case
的检测评测（bench / control / operating curve / incremental value），确认服务侧是否也存在
"单指标阈值同等或更好"的结论。

---

## 5. 全量评测结果（125 case，3,064 窗口）

窗口 60 s，逐 case 独立基线，标签由 `inject_time` 给出（前 = 正常，后 = 故障）。

| 故障族 | 窗口 | 模型 AUC | 模型 recall@FPR10% | **最佳单指标阈值 AUC** | 该阈值 recall@FPR10% |
|---|---|---|---|---|---|
| cpu | 1,414 | 0.514 | 0.123 | **1.0000**（cpu） | 1.000 |
| mem | 1,500 | 0.447 | 0.069 | **1.0000**（cpu） | 1.000 |
| delay | 225 | 0.294 | 0.055 | **0.9274**（latency） | — |
| disk | 225 | 0.355 | 0.088 | **1.0000**（cpu） | 1.000 |
| loss | 227 | 0.418 | 0.038 | **0.7524**（latency） | — |

（`report_full.json` 覆盖 cpu/mem；`report_short.json` 覆盖 delay/disk/loss，两类录制长度不同，
warmup 已按可用注入前时长自适应。）

**模型在全部五个故障族上都低于 0.5（系统性反相关），而单一指标阈值达到 0.75–1.00。**

### 5.1 信号在状态里，模型没有用

同一窗口，`re1ob_adservice_cpu_1` 注入后：

```
Service adservice, a 60-second window with 4 events.
Error fraction 0.00% against a baseline of 0.00%.
Latency 4.7 ms against a baseline of 4.7 ms (1.00x).
Resources: CPU 96.05% (baseline 1.66%, 57.74x).
Throughput 56.6 requests per second against a baseline of 58.7 req/s (0.96x).
```

CPU 明确写着 **57.74 倍偏离**，错误率 0%、延迟 1.00x。模型给出的 P(anomaly) = **0.3908**（正常）。

### 5.2 输出不是常数，只是与标签无关

| 集合 | 窗口 | p 范围 | 中位数 | stdev | 不同取值 |
|---|---|---|---|---|---|
| report_full | 3,064 | 0.246–0.442 | 0.369 | 0.024 | 931 |
| 其中 label=0 | 1,274 | — | — | 0.019 | 均值 0.3705 |
| 其中 label=1 | 1,790 | — | — | 0.026 | 均值 0.3639 |

模型确实给出了分布，但**故障窗口的均值反而略低**。所以这不是"输出饱和"，是排序与故障无关。

### 5.3 排除"基线吸收"这一解释

在 SMD 上，短滚动基线会吸收持续故障（30 → 480 窗使 AUC 从 0.562 升到 0.758）。
这里把基线从 10 窗加到 30 窗（cpu 族，20 case，1,034 窗口）：AUC 0.514 → **0.534**，**没有改善**。

所以服务侧的问题不是基线时程，而是**模型本身没有使用资源通道的证据**：
错误率为 0、延迟持平时，即使状态里写着 57 倍 CPU 偏离，模型仍判为正常。

这与 SMD 的结论一致但更强：SMD 上模型还能靠"某个指标水平"勉强排序（AUC 0.745），
服务侧上它连这一步都没有。

### 5.4 这对架构意味着什么

catalog 的设计假设是"多信号综合能做出阈值做不到的判断"。在**唯一可得的真实服务侧数据集**上，
这个假设没有被验证：阈值在五族里全面胜出，其中三族是完美分离（AUC 1.000）。

需要说明的边界：
- 这是 `typed-decisions` checkpoint 的表现；它是在合成工作流文本上微调的，未必代表基座模型能力。
- RE1-OB 的故障都是**注入型、单点、强信号**（CPU 打满、延迟 10x），本就有利于阈值。
- 真正的多信号价值场景（指标都不越线但组合异常）在该数据集里不存在，所以**无法用现有公开数据检验**。

---

## 6. 结论

1. **目的达成**：找到并跑通了服务侧真实数据集（RCAEval RE1-OB，125 case，1 秒精度，含 QPS/延迟/错误/CPU）。
2. **实测结果**：模型在五类注入故障上 AUC 0.29–0.51，单指标阈值 0.75–1.00。
3. **缺陷复现**：`Resources:` 行原先无参照（已按"仅在偏离时给出基线"修复，合成套件零回归）。
4. **诊断**：模型不使用资源通道证据；不是基线时程问题（加长基线无改善）。
5. **未能验证**：多信号综合的价值——公开数据集里没有"单指标都不越线但组合异常"的样本。

下一步（若继续）：
- 用不同 checkpoint（english 基座）重跑，判断是否微调导致；
- 构造"弱信号多指标"场景（每个指标偏离 1.5–2 倍、无单指标越线），那才是这套架构该证明自己的地方。
