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

## 4. 下一轮计划（RCAEval RE1-OB）

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 取 RCAEval RE1-OB（125 case，49–59 指标，纯指标无需 trace 解析） | 本地可读，schema 记录在案 |
| 2 | 找到每个 case 的故障注入区间（case 内是否含 pre-fault 基线） | 能定义逐窗口标签，而非 run 级 |
| 3 | 把指标映射到 catalog 的 13 个源，确认 `http_requests_total` / `errors` / `latency` 是否存在 | 填充率报告 |
| 4 | 跑同一套评测（bench / control / operating curve / incremental value） | 与 SMD 可比的数字 |

**关键判据**：如果 RE1-OB 的指标里同样没有请求数/错误数/延迟，那么"服务侧从未被真实数据检验"
这个结论就要从"缺数据"升级为"公开数据集普遍缺少"，那是另一个需要写清楚的边界。
