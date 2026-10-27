# Laya checkpoint 微调手册（FINETUNE）

本文档是给 `brian` 的 System 1 判定器微调 Laya checkpoint 的操作手册。它回答的核心问题是：当前 checkpoint 在本领域的概率是否可以被门控（gate）信任，以及微调能把哪些指标推上去。

结论（详见第 1 节）：**基础 checkpoint 作为排序器可用（AUC 0.891，阈值扫过之后 recall 0.854 @ FPR 0.024），概率未校准（ECE 0.293），而最大的单一杠杆是渲染器而不是 checkpoint——在逐字节相同的 metrics 上，渲染方式能把 AUC 从 0.002 拉到 0.891。** 微调仍然推荐（它是把 ECE 与 0.604 的类别准确率做上去的路径），但它是**改进**，不是抢救。任何渲染器改动都是模型改动，必须先于微调定稿并重新测量。

> 相关工作：`src/brian/cli.py` 的 `brian calibrate` 在拒绝写出校准文件时会打印 “see docs/FINETUNE.md”，指向的就是本文档描述的那条升级路径。

---

## 1. 为什么仍然推荐微调（但它不再是“抢救”）

结论先写，因为它决定了后面每一步的意义：**基础 checkpoint 作为一个排序器是可用的；它的概率没有被校准；而到目前为止最大的单一杠杆不是 checkpoint，是渲染器。** 微调仍然推荐——它是把 ECE 与类别准确率做上去的路径——但它是**改进**，不是对一个“没有判别能力”的模型的抢救。

### 先纠正一段历史

本文档的早期版本写着“AUC 0.775、概率带宽 0.0517、recall 0.000，所以微调是唯一路径”。那些数字出自一个**带 bug 的渲染器**：它用平铺 key（`comparison.get("latency_ms.p99")`）去查**嵌套**存储的指标（`comparison["latency_ms"]["p99"]`），于是每次查找都返回 `None`，模型读到的状态文本塌成一句不含任何指标的话。**在一份几乎空的文档上量出来的任何数字，都是那份文档的属性，不是模型的属性。** bug 已修复；同一批数据、同一个 checkpoint 修好之后的 AUC 是 **0.891**。

### 实测证据（本机，90 个带标签的合成窗口，RTX 5070 Ti Laptop，Laya 本地服务在 8077 端口）

**证据一：排序可用，端到端结果在门槛内。** 生产渲染器 + 扫出来的阈值 0.5583：

| 指标 | 结果 |
|---|---|
| recall | **0.854**（95% CI 0.728–0.928），48 个事件抓到 41 个 |
| false positive rate | **0.024**（95% CI 0.004–0.123），42 个健康窗口 1 次误报 |
| precision | **0.976** |
| AUC | **0.891** |
| category accuracy | **0.604**（只统计被抓到的异常窗口） |
| severity MAE | **0.5** 级（0–3 scale） |
| calibration ECE | **0.293**（**不通过**） |
| latency | p50 **75 ms**、p95 **81 ms**（完整 HTTP 往返，热态） |

**证据二：概率没有校准，而这正是微调与校准要去修的那一项。** 原始概率大致落在 **0.34–0.47**（约 0.13 宽），中心低于 0.5，所以阈值必须**扫出来而不是假设**：

| 阈值 | recall | FPR | precision |
|---|---|---|---|
| P≥0.5000（常规值） | 0.875 | 0.214 | 0.824 |
| P≥0.4300 | 0.5625 | 0.0952 | 0.871 |
| **P≥0.5583（出厂默认）** | **0.8542** | **0.0238** | **0.9762** |

**证据三：`brian eval` 打印 `NOT_READY`，而这是正确的。** 三个门是 recall ≥ 0.85、FPR ≤ 0.05、ECE ≤ 0.10（合取）：前两个过了，**ECE 0.293 没过**。按 System 1 的 `AUTO_TRIAGE` 判决直接动手，等于信任一个错了约 0.29 的概率——所以 gate 继续把判定升级给 System 2。微调要改的正是这一项，以及证据一里那条 0.604 的类别准确率。

**证据四：渲染器是主导杠杆，而且“加更多信息”实测更差。** 在**逐字节相同**的 metrics 上，只换渲染方式（`python scripts/compare_renderers.py`，它复用生产 `Evaluator`，只替换 `(metrics) -> str`）：

| 渲染形式 | AUC |
|---|---|
| dotted-key 账本（修复前的生产形态） | **0.002**（同一形态另一次运行记为 0.775——一个不含信息的文本，其排序会随样本任意摆动） |
| 叙事 + 逐项复述数字（`prose_plus_deviations`） | 0.349 / 0.566 / 0.416 / 0.396，均值 **≈0.43** |
| 同一叙事 + z-score 与样本数 | **≈0.78** |
| **已发布形态**（只给 value 与 ratio，baseline 同单位） | **0.891**（seed 间 0.875–0.897） |

**所以：改渲染器就是改模型。** 任何渲染改动都必须重新测量——先 `python scripts/compare_renderers.py`（同时看 AUC、recall/FPR 与概率带宽），再 `brian eval`（端到端，走完整生产路径），并 bump `DIGEST_VERSION`，否则“上周二准确率为什么变了”就没有答案。由此得到第 5 节顺序里的一条前置项：**先让渲染器定稿，再开始微调**——否则你是在为一份马上会被改掉的文本拟合 checkpoint。

同一类教训还有一条仍然成立、并且在导出训练数据时必须遵守的：**不要把 metrics dict 一起当 state 传**（Laya 的 `serialize_state` 会对 dict 做 `json.dumps`，把 JSON 塞进 document budget，模型于是读到标点而不是散文）。`LayaClient._state_payload` 因此对 `Digest` 只返回 `state.interpretation` 这个纯字符串，导出的 `state` 字段（见第 4 节）也必须与它逐字节一致。

**证据五：上游自己也是这样说的。** Laya 服务在 `/v1/capabilities` 的 `limits.probability_calibration` 里写着：

> "the shipped temperatures are inherited from the base checkpoint (ECE 0.213); refit on your own held-out data before gating automation on confidence"

### 那校准能不能代替微调？

不能，但两者的分工和早期版本写的**不一样**。早期版本的前提是“模型分不开两个类别，所以只能靠微调”；修正后的实测是这个前提**不成立**——排序是好的（AUC 0.891），错的是概率的位置与尺度（ECE 0.293）。于是分工变成：

| | 校准（calibration） | 微调（fine-tuning） |
|---|---|---|
| 它改什么 | 概率的**位置与尺度** | 模型**怎么排序、以及类别/严重度答得多准** |
| 它能改 AUC 吗 | **不能**（单调变换的不变量） | 能（但当前 AUC 0.891 已经不错，提升空间有限） |
| 它能改 ECE 吗 | **能，这是它的本职** | 能（更好的排序与更准的类别通常会带来更好的校准） |
| 本项目的实测 | 拟合机制与护栏就位；**没有一次生产拟合被采纳**（`models/calibration/` 当前不存在，跑的是原始概率 + 扫出来的阈值） | 未运行，见下文 |
| 结论 | 必需的一级，但不是万能 | 是提升 ECE 与 category accuracy 的路径，**是改进，不是抢救** |

**为什么校准必须是 Platt（slope + intercept），而不是一个温度。** 温度是 logit 上的纯缩放，只能把分布拉宽或收窄，**不能移动它的中心**；而这里的失效模式正是位置问题（整条概率带低于 0.5）。两参数才能把决策边界搬到信号所在的地方。`fit_calibration`（`calibration.py`）用 Newton-Raphson 在 (slope, intercept) 上做最大似然。

**关于“校准修好了吗”的诚实说明。** 仓库里有的是**拟合机制与它的护栏**，不是一次被验证过的成功校准。护栏有两条：`CalibrationFit.discriminates` 以 **AUC ≥ 0.6** 为界，低于它就**拒绝写出**拟合文件（并把旧文件删掉，除非 `--force`）；并且它**把 AUC 与 ECE 一起报出来**——**AUC 在任何单调重标定下都不变**，所以它是唯一能回答“这里是真信号，还是校准造出来的假象”的指标。AUC 0.891 远在 0.6 之上，所以这台机器会接受一次拟合，但**校准不会把 ECE 0.293 变成 0**。

**`brian calibrate --from-outcomes` 是在真实操作员标签上拟合的路径，也是标签一旦存在之后的首选来源**：合成窗口的 ECE 与真实流量的 ECE 不是同一个量，而 `brian.outcomes` 是唯一带真实标签的集合（它目前还没有生产者，见第 3 节的冷启动问题）。

所以顺序是：**渲染器定稿 -> 微调 checkpoint -> 重新校准 -> 重新评测。** 微调不在“抢救信号”，它在把 ECE 与类别准确率往上推；而阈值是这条链的最后一步，不是第一步。

---

## 2. 上游的微调配方

Laya 上游（Apache 2.0，`D:\project\laya`）公开了完整的微调流程。要点：

| 项 | 内容 |
|---|---|
| 方法 | **RLCD**（reinforcement learning against strictly proper scoring rules）；用 strictly proper scoring rule 作为奖励，GRPO 风格的策略梯度 |
| 参考实现 | `D:\project\laya\notebooks\laya_finetune_typed_decisions_2xT4_kaggle.ipynb` |
| 硬件与耗时 | Kaggle 免费 **2xT4**，**约 4–5 小时**，**4 个 epoch**，约 **30k questions** |
| 流程内容 | 构建数据集 -> RLCD 训练 -> 拟合校准温度 -> 评测 -> 推送 checkpoint 到 Hub |
| 基座 | `laya-typed-decisions`（ModernBERT-large，421M 参数，上下文 1024，`head_max_len = 256`） |

### 能力全部来自微调，这一点有硬数字

上游在 typed-decisions benchmark（400 cases / 2,000 decisions）上的公开结果：

| 模型 | accuracy | ECE |
|---|---|---|
| `laya-typed-decisions`（**微调后**） | **0.766** | 0.213 |
| `laya`（base，零样本） | **0.362** | 0.175 |
| `laya-multilingual`（base，零样本） | **0.342** | 0.285 |
| 随机猜测 | 0.318 | — |
| 逐题多数类 | 0.461 | — |
| teacher self-agreement ceiling | 0.735 | — |

上游 README 的措辞是直接的：“**The base checkpoints sit below the majority-class baseline** (0.362 and 0.342 against 0.461). All of the capability on this benchmark comes from fine-tuning.” 也就是说零样本基础模型**比猜多数类还差**，微调后的 checkpoint（0.766）比多数类高 30 个百分点，并且超过了 teacher 的自一致上限 0.735。

按 primitive 拆开看，这解释了为什么 `qcatalog.py` 是现在这个形状：

| primitive | 上游 accuracy | 本项目的用法 |
|---|---|---|
| `noul` | **0.857** | `is_anomaly`（门控动作）、`needs_human` |
| `choice` | 0.733 | `category`（8 类）、`blast_radius`（4 类） |
| `score` | **0.723** | 只有 `severity` 用 `score`；不给它任何存在性决策 |

上游还明确点出 “Ordinal `score` questions are the weakest primitive”，而本项目把 `score` 限制在严重度上——因为严重度不清楚时，gate 会升级到 System 2（`low_confidence_escalation`），而不是拿它去决定是否告警。

本项目的 `src/brian/laya/qcatalog.py` 就是待微调的**问题目录规格**：`CATALOG_VERSION = "anomaly/1"`，5 个问题，`catalog_hash()` 是 `anomaly/1` 目录的 sha256 前 16 位，写在每个 `Decision.questions_hash` 上。微调必须使用**完全相同的目录**，否则训练出来的 checkpoint 与线上问的不是同一批问题。

> **不要把上面这张表和本项目的实测混为一谈。** 上游的 0.362/0.342 是它在**自己那个多域 benchmark**（400 cases / 2,000 decisions，覆盖四类工作流）上的零样本准确率；本项目测的是**本领域**的合成窗口上的排序与门控表现，而那个结果是“基础 checkpoint 作为排序器可用（AUC 0.891）”。两者不矛盾——不同的 benchmark、不同的指标（accuracy vs AUC）、不同的标签——但正因如此，**“上游说 base 不如多数类”不能拿来替代本项目自己的评测**，反之亦然。微调的理由因此不是“上游说 base 不行”，而是“本领域数据上 ECE 0.293、category accuracy 0.604，而这两项正是微调能改的”。

---

## 3. 数据来源

### 闭环：`brian.outcomes` 是标签来源

标签来自生产本身，而不是来自人工构造的数据集：

```
decision ─► alert ─► (运维关闭告警) ─► outcome record ─► 带标签的窗口
                                       brian.outcomes      (state, questions, answers, label_mask)
```

`src/brian/topics.py` 里 `Topic.OUTCOMES` 的说明就是这句话：“What was actually done, and whether it worked — closes the learning loop.” 每一条被提升的告警，在有人关闭它之后就变成一个带标签的窗口：

- `state` = 当时渲染的 digest 文本（`Digest.interpretation`，模型真正读的东西）；
- `answers` = 当时 Laya 的回答；
- `label` = 人给出的真实结论（是否异常、类别、严重度）；
- `lineage.upstream_ids` = 回溯到 `dec_*` / `dgst_*` 的链路。

这条闭环的好处是标签分布和线上分布天然一致。它的坏处同样明显：**冷启动。**

### 冷启动问题

系统刚上线时，`brian.outcomes` 是空的，所以没有任何真实标签。修正后的实测说明这个循环**不是锁死的，但是很慢**：基础 checkpoint 在出厂阈值 0.5583 下 recall 0.854、precision 0.976，也就是说它**会**产生告警，运维**会**关闭其中一部分，于是 outcome 记录会开始积累——但 FPR 只有 0.024，真实事件在合成评测里是 48 个窗口，真实流量下这个速率意味着标签的积累速度取决于事故频率，而不是取决于你的耐心。所以自举仍然需要，只是理由从“一个告警都发不出来”变成“标签攒得太慢，而且分布偏”。

（另外注意：可用的 recall 依赖于**渲染器正确**。修复之前，同一套配置发出的告警是零——见第 1 节那段历史。）

必须用两条自举（bootstrap）路径打破它。

### 自举路径 A：合成场景（`brian.demo` / 导出）

`src/brian/demo.py` 的 `generate()` 按 `DEFAULT_SEQUENCE` 生成 7 个场景的带标签窗口：

| 场景 | `EXPECTED` 标签 | 刻意设置的 confuser |
|---|---|---|
| `normal` | `is_anomaly=false` | 每周流量波动上升 40%，错误率平 |
| `resource_exhaustion` | `resource_exhaustion` / `critical` | 本地 CPU/线程饱和，错误是**本地**产生的 |
| `dependency_failure` | `dependency_failure` / `major` | DB 池饱和 + 单一 timeout 模板 + **本地 CPU 很低** |
| `traffic_surge` | `traffic_surge` / `minor` | 吞吐涨 12x 而错误率与 p50 延迟**都平** |
| `data_quality` | `data_quality` / `major` | 服务本身健康，坏的是数据 |
| `security` | `security` / `major` | 认证失败洪泛，服务本身健康 |
| `config_or_deploy` | `config_or_deploy` / `minor` | 窗口内有 deploy 记录，之后只有**轻微**劣化 |

`demo.py` 的模块 docstring 说明了为什么这些 confuser 是重点：`normal` 与 `traffic_surge` 都显示吞吐大幅上升，`resource_exhaustion` 与 `dependency_failure` 都显示延迟与错误上升。**一个不用区分它们就能拿高分的检测器，是在拟合标签而不是拟合证据。**

这条路径能立刻给出训练数据，代价是分布偏差（见第 7 节失败模式）。适合**第一次**微调。

### 自举路径 B：LLM 教师标注真实窗口的抽样

在真实流量上分层抽一批窗口（按服务 × 时段 × 是否越阈分层，特别要覆盖“安静的正常窗口”这一类），用 LLM 教师按同一个问题目录逐题标注，生成 `answers`。

这条路径的三个注意点：

1. **教师标注必须被标记出来**，这正是 `label_mask` 的第一个用途（参见第 4 节）：教师标注不是人工验证标签，不能和人工关闭的 outcome 混为一谈。
2. **抽样必须分层**，否则会得到一个几乎全是正常窗口的训练集，模型会学成“永远回答正常”。
3. **教师本身要有评测**，否则你只是在把一个模型的偏见烧进另一个模型。

两条路径的关系是：A 用来打破锁死、拿到第一版 checkpoint；B 用来把分布拉回真实流量；此后 `brian.outcomes` 逐步接管，成为唯一的标签来源。

### 开放问题：合成真值在 `traffic_surge` 上与模型解读直接冲突

这是 A 路径必须先解决的一个标注决定，把它写在这里而不是藏在脚注里。

基础 checkpoint 在 `traffic_surge` 上是 **6/6 全漏**，而原因不是模型失灵：`traffic_surge` 窗口的 `mean P(anomaly)` 是 **0.367**，比**所有**健康场景都低（`normal` 是 0.398）。也就是说模型一致地认为“负载上升 12 倍、错误率与延迟都平”是**正常负载**。逐场景异常准确率：`normal` 0.976、`dependency_failure` 1.000、`resource_exhaustion` 1.000、`data_quality` 1.000、`security` 1.000、`config_or_deploy` 0.833、`traffic_surge` **0.000**。

两边都能辩护，而且**项目自己的代码站在了两边**：

- `demo.EXPECTED` 把它标成 `traffic_surge / minor` 异常——容量翻一个数量级，即便没有报错，也应该有人看一眼；
- `OfflineReasoner` 的判别规则写的是“吞吐上升 **且** 错误率平 -> 是 surge（健康）；吞吐上升 **且** 错误率也上升 -> 是故障不是 surge”（见 `ARCHITECTURE.zh.md` 第 10 节）。

后果对微调很直接：如果你把这份合成真值原样喂给 RLCD，你训练的是“把良性负载上升报成异常”这个特定的标注立场，而**微调不会告诉你这个立场对不对——它只会把它烧进权重**。所以：

- 要么把 `traffic_surge` 从异常集合里移出，改走一条容量信号通道（这时 `demo.EXPECTED` 与训练数据都要跟着改）；
- 要么保留它作为异常，并接受你需要提供区分“良性负载上升”与“负载压垮了什么东西”的证据（容量余量、队列深度、饱和度趋势）——而当前 digest 里没有这些字段，加字段又会撞上 `MAX_DIGEST_WORDS = 240` 的词预算。

在真实部署里这是一个**必须刻意做出**的决定，而不是一个可以留给模型去猜的标签。在做出决定之前，`traffic_surge` 的样本要么排除，要么用 `label_mask` 标出来单独统计。

---

## 4. `brian export-training`

导出端产出的是给微调笔记本吃的训练数据：**每行一个 JSONL 记录，每个窗口一条**。

```json
{
  "window": {
    "service": "checkout-api",
    "window_start": "2026-01-14T10:07:00Z",
    "window_end": "2026-01-14T10:08:00Z",
    "digest_id": "dgst_1bb67a3e364a",
    "digest_version": "digest/1",
    "catalog_version": "anomaly/1"
  },
  "state": "Service checkout-api, a 60-second window with 60 events.\nError fraction 30.77% against a baseline of 0.50% (61.5x, z=+41.2); 3.5 errors per second.\np99 latency 8900 ms against a baseline of 179 ms (49.7x, z=+18.4); median 1200 ms.\nResources: CPU 98.00%.\nThe most frequent error (92.00% of all errors) is: TimeoutException: connection acquisition from pool orders-db timed out after <num><unit>",
  "questions": { "...": "与 qcatalog.DEFAULT_QUESTIONS 逐字节一致" },
  "answers": {
    "is_anomaly": { "type": "noul", "label": true },
    "category": { "type": "choice", "label": "dependency_failure" },
    "severity": { "type": "score", "label": 2 },
    "blast_radius": { "type": "choice", "label": "single_service" },
    "needs_human": { "type": "noul", "label": true }
  },
  "label_mask": {
    "is_anomaly": "human",
    "category": "human",
    "severity": "derived",
    "blast_radius": "teacher",
    "needs_human": "teacher"
  },
  "source": "outcome",
  "lineage": { "upstream_ids": ["dec_1e5a53bf255d", "dgst_1bb67a3e364a"] }
}
```

字段含义：

| 字段 | 内容 |
|---|---|
| `state` | **渲染后的 digest 文本**（`Digest.interpretation`），纯字符串，不是 dict。必须与线上 `LayaClient._state_payload` 发送的内容完全一致 |
| `questions` | 问题目录本身，逐字节等于 `qcatalog.DEFAULT_QUESTIONS`（用 `catalog_hash()` 校验） |
| `answers` | 每题的金标，**只在已知时存在**；未知的题不该出现在这里，而不是填一个默认值 |
| `label_mask` | 逐题标记这个答案的**来源与可信度**，见下 |
| `source` | 这条记录从哪来（`demo` / `outcome` / `teacher`） |
| `lineage` | 回溯到 `dec_*` / `dgst_*`，用于事后核对 |

### `label_mask` 为什么必须存在

因为**不能拿一个捏造的标签去训练一个问题**。

`label_mask` 的取值（同一题在一条记录里只有一个）：

| 值 | 含义 | 能不能用于训练 |
|---|---|---|
| `human` | 有人真的做过判断（例如关闭该告警并写明结论 / 人工审阅） | 能 |
| `teacher` | LLM 教师按目录标注，未经过人 | 能，但必须与 `human` 区分开统计，并且要抽样审计 |
| `derived` | 从别的答案推出来的，例如 `severity` 由 `category` 加规则映射（`demo.py` 的 `EXPECTED` 就是这样给出的） | 谨慎：它通常只是重复了规则的偏见，容易让模型学会背规则而不是学会判断 |
| `absent` | 这道题在这个窗口上根本没有可信答案 | **不能**；训练时必须对这道题做 loss masking |

没有这个掩码会发生什么：一个窗口只有 `category` 的人工标签，`severity` 是脚本按规则推的。如果导出时把两者都当作金标，你就在教模型“按 category 映射 severity”，而模型在线上要面对的恰恰是**没人知道严重度**的那些窗口（那正是 `low_confidence_escalation` 把问题升级给 System 2 的场景）。训练出来的高置信度是假的：它来自一个**由构造决定的标签**，而不是来自证据。

因此训练循环必须逐题应用掩码：只有 `human` 与 `teacher` 的题参与 loss；`derived` 默认不参与（除非你有证据说明该映射在本领域成立）；`absent` 一定不参与。

### 现状说明

`src/brian/cli.py` 的模块 docstring 已经把 `export` 列在 “REBUILD IT” 一组里（“export emit fine-tuning data for the Laya checkpoint”），但 `build_parser()` **目前还没有注册 `export` / `export-training` 子命令**。也就是说上面这份 JSONL 结构是导出端的契约（以及训练端应当消费的形状），而不是今天就能直接跑的 CLI。在接线完成之前，等价的导出可以用 `brian replay` 重建 digest、再按本节的字段拼装（所有素材都在 `brian.features` / `brian.decisions` / `brian.outcomes` 里，并且都带 lineage）。

---

## 5. 校准与评测闭环

微调完成之后，严格按这个顺序做，不能跳步：

```bash
# 0a. 先让渲染器定稿（渲染器是模型参数：改它就必须重新测量）
python scripts/compare_renderers.py --repeat 4 --renderer current,prose,ledger

# 0b. 让服务加载新 checkpoint，先确认它是谁
brian config --laya

# 1. 微调后的 checkpoint 上，先看没校准的原始表现
BRIAN_CALIBRATION_ENABLED=0 brian eval --repeat 6 --json-out var/eval_finetuned_raw.json

# 2. 在这个 checkpoint 上重新拟合校准（必须先跑 1，再用 2）
brian calibrate --repeat 6 --max-fpr 0.10 --alert-rate 0.01 --json-out var/calibrate_finetuned.json

# 3. 带上校准再评一次：这才是要写进结论的数字
brian eval --repeat 6 --json-out var/eval_finetuned_calibrated.json

# 4.（可选，但强烈建议）确认阈值不是魔法数字
python scripts/threshold_sweep.py --repeat 6 --recall-floor 0.85 --max-fpr 0.10
```

**顺序不能颠倒，理由有四个：**

1. **渲染器必须先定稿。** 渲染器不是格式，是模型的输入；在逐字节相同的 metrics 上它把 AUC 从 0.002 拉到 0.891（第 1 节证据四）。在一份还会变的文本上微调，等于为一个马上要换掉的输入拟合权重，而微调之后再去改渲染器会让微调结果的评测全部作废。任何渲染改动都走 `scripts/compare_renderers.py` -> `brian eval`，并 bump `DIGEST_VERSION`。
2. **必须在微调之后才能拟合校准。** 校准拟合的是“这个模型的概率偏了多少”，换了 checkpoint 就换了偏差；在旧 checkpoint 上拟合出来的 slope/intercept 对新模型毫无意义。
3. **`brian calibrate` 内部会先关掉校准再采样**（`CalibrationSettings(enabled=False)`，见 `cmd_calibrate`），因为校准是修正、而这一步要测的是**模型本身**的原始概率。它同时会把拟合应用到已存下来的 raw 概率上做一次重放（`_replay_fit`），不额外调用 Laya。
4. **`brian eval` 是唯一走完整生产路径的评测**：`build_digest` -> `LayaClient.decide` -> `apply_gate`。`eval.py` 的 docstring 写着 “A benchmark that takes a shortcut measures the shortcut”。

### 验收标准（三个门，合取）

`src/brian/eval.py` 的 `gate_recommendation()` 定义的门槛：

| 门 | 要求 | 当前基础 checkpoint（生产渲染器 + 阈值 0.5583） | 结论 |
|---|---|---|---|
| recall | **≥ 0.85** | **0.854**（95% CI 0.728–0.928） | 通过 |
| false positive rate | **≤ 0.05** | **0.024**（95% CI 0.004–0.123） | 通过 |
| calibration ECE | **≤ 0.10** | **0.293** | **未通过** |

**当前基础 checkpoint 只差第三门，而它恰恰是那个最容易被跳过的门。** 一个 recall 0.95、ECE 0.293 的检测器，它给出的“0.7”并不代表 70%，而 gate 恰恰按这个置信度动作——所以 `gate_recommendation` 正确地给出 `NOT_READY`，gate 继续升级给 System 2。`gate_recommendation` 还有一条硬规则：**样本少于 30 个窗口直接判 `not_ready`**，不管上面三个数字多好看；recall 与 FPR 都会同时给出 Wilson 95% 区间，因为“10 个正例上的 0.9 recall”不是证据——注意 0.024 那个 FPR 的区间是 0.004–0.123，**上界 12% 比点估计更接近你的真实处境**。

### 微调后应当期望什么

诚实的预期是：**微调的目标不是把 AUC 从 0.891 提到 0.99**（排序已经够用了，提升空间有限），**而是把 ECE 压到 0.10 以内、并把 category accuracy 从 0.604 抬上去。** 这两项才是当前真正的短板：0.604 意味着每三个被抓到的异常里就有一个被归到错误的类别，而类别决定告警标题、owner 路由与 dedup key。至于 recall / FPR，基础 checkpoint 已经在门槛内（0.854 / 0.024），微调要守住它们，而不是从零开始挣。

在微调与重新校准完成之前，出厂的配置应该保持 `confidence_floor = 0.45`，让 gate 继续把判定升级给 System 2——这是配置，不是故障。

---

## 6. 上线检查清单

在把一个微调后的 checkpoint 用于自动化之前，逐条确认：

**数据**

- [ ] 训练集里每一题的标签都有 `label_mask`，且训练循环**逐题**应用了掩码。
- [ ] `human` / `teacher` / `derived` 三类样本的比例被记录下来；`derived` 默认不参与 loss。
- [ ] 训练集同时包含**正常窗口与异常窗口**，并且检查了类别分布（单类样本会让校准不可辨识，见第 7 节）。
- [ ] 至少有一部分窗口来自真实流量（不只是 `demo.py` 的合成数据）。
- [ ] 存在一个**留出集（held-out）**，且它从未参与训练或校准拟合。

**目录与文本**

- [ ] 训练用的 `questions` 与线上 `qcatalog.DEFAULT_QUESTIONS` 哈希一致（比对 `catalog_hash()` / `Decision.questions_hash`）。
- [ ] `state` 用的是渲染后的纯文本字符串，不是包含 metrics 的 dict（dict 会被 `serialize_state` 用 `json.dumps` 塞进 document budget，模型读到标点而不是散文）。
- [ ] **渲染器已经定稿**：跑过 `python scripts/compare_renderers.py`，并在当前已发布形态上跑过 `brian eval`；任何进一步的渲染改动都当作模型改动重新测量，并 bump `DIGEST_VERSION`。
- [ ] `MAX_DIGEST_WORDS = 240` 没有被突破；`metrics.render.dropped` 非空的窗口比例可接受。

**校准与评测**

- [ ] `brian config --laya` 显示的 checkpoint 名称与预期一致（避免“配错模型名”和“模型没下载”被混为一谈）。
- [ ] 微调后的 checkpoint 上重新跑过 `brian calibrate`，并且新拟合的 `AUC ≥ 0.6`、`improved = true`（低于 0.6 时它**会拒绝写出**拟合文件）。
- [ ] 有真实标签时优先用 `brian calibrate --from-outcomes` 在操作员的真实标签上拟合，而不是只在合成窗口上拟合。
- [ ] `brian eval` 的三个门（recall ≥ 0.85 / FPR ≤ 0.05 / ECE ≤ 0.10）全部 pass，且评测窗口数 ≥ 90。
- [ ] Wilson 95% 区间被看过，而不是只看点估计（基础 checkpoint 的 FPR 0.024 区间是 0.004–0.123）。
- [ ] `scripts/threshold_sweep.py` 的结果与配置里的 `GateThresholds.anomaly_probability`（当前 0.5583）一致，阈值是**刻意扫出来的**，不是继承自 sigmoid 的中点。
- [ ] 所有阈值改动写进了配置文件，并附上这张代价表。

**运行时**

- [ ] `BRIAN_CALIBRATION_ENABLED=1`（默认），且 `Settings.masked()` 里能看到 slope / intercept / 拟合样本数。
- [ ] 服务预热过一次（第一次推理约 0.65 s 是 CUDA 编译 kernel）。
- [ ] `BRIAN_LAYA_MODEL` 指向新 checkpoint，且 `brian config --laya` 的 `model_available = true`。
- [ ] 观察 `Decision.gate_reason` 的分布：`rule` 应该是混合的，如果全部落进 `low_confidence_escalation`，说明置信度门槛（`confidence_auto = 0.75`）还没被满足。
- [ ] DLQ 计数、`rca_degraded` 率、`laya_cache_hit_rate`、`mean_laya_latency_ms` 都在监控里。
- [ ] 回滚路径可用：旧 checkpoint 仍在服务上、旧校准文件可恢复。

---

## 7. 常见失败

| 失败模式 | 症状 | 为什么会这样 | 正确做法 |
|---|---|---|---|
| **用没有真实标签的题去训练** | 某一题在验证集上准确率很高，但线上那道题经常错 | 标签是 `derived`（按规则推的）或填充的默认值。模型学到的是规则的偏见，不是证据 | 逐题检查 `label_mask`；`derived` 与 `absent` 的题不参与 loss |
| **先拟合校准，后微调** | 校准参数很好看，微调后指标反而变差 | 校准拟合的是**旧** checkpoint 的概率偏差（slope/intercept 是为那个 checkpoint 拟合的），换模型后完全失效 | 顺序固定：渲染器定稿 -> 微调 -> `brian eval`（原始）-> `brian calibrate` -> `brian eval`（校准后） |
| **把渲染器当成格式，改完不重测** | 某一版渲染上线后 AUC 从 0.891 掉到 0.43 甚至 0.002，而代码看起来只是"把句子写清楚了一点" | 渲染器是模型的输入，不是输出格式。同一批逐字节相同的 metrics，换一种措辞就能把 AUC 从 0.891 拉到 0.002；而"加更多精确信息"（z-score、样本数）实测是**更差**而不是更好 | 任何渲染改动都先跑 `python scripts/compare_renderers.py` 再跑 `brian eval`，并 bump `DIGEST_VERSION`；把渲染器的行序与"只陈述比较本身"当作实测约束而不是文风 |
| **用调阈值来掩盖校准问题** | 沿用常规阈值 0.5，结果 FPR 0.214、precision 0.824（每 1000 窗口 214 条误报）；或者一路压低阈值想换 recall，precision 随之崩掉 | 阈值是策略，校准是测量。把两者混在一起，就会得到一堆没人能辩护的魔法数字，而**排序本身（AUC 0.891）并没有被改动**——它能被阈值改的只有 recall 与 FPR 的交换比 | 先修概率（校准 + 微调），阈值只在"概率已经说人话"的前提下按策略选；当前 0.5583 是"仍能达到 0.85 recall 的最安静的点"（FPR 0.0238 / precision 0.9762），把 `scripts/threshold_sweep.py` 的代价表贴在配置旁边 |
| **把英文 checkpoint 用在非英文日志上** | 在某类服务上准确率突然暴跌，而模型仍然很自信 | `english` / `typed-decisions` 只读英文。上游实测：英文 checkpoint 在非英文输入上崩塌且保持自信（Khmer 0.000 accuracy @ 0.952 confidence），**置信度门控救不了** | digest 一律渲染成英文（`render_digest`），运维界面本地化留在渲染层与 LLM 层；非英文 digest 走 `multilingual` checkpoint，并接受英文准确率的代价（MASSIVE intent：0.783 vs 0.657） |
| **超出词预算，证据被截断** | 某些窗口的判定莫名其妙，`metrics.render.dropped` 非空 | 序列被切成 head（问题 rubric，192 tokens）与 document（state）两段，可用预算约 `max_len - head_max_len` ≈ 320 tokens；`MAX_DIGEST_WORDS = 240` 一破，被丢掉的是**排在最后的上下文**（deploy、负载形状、迟到警告），而 encoder 保留头部 | `brian eval` 时检查 `metrics.render.dropped`；要加字段就先减字段；把最决策相关的句子留在前面（`render_digest` 的行序就是为此固定的） |
| **单类标签样本** | 校准拟合返回 slope=1、intercept=0，并带一条 note；或者 ECE 好得不真实 | `fit_calibration` 在样本全为异常或全为健康时会显式放弃：似然是单调的，最优在无穷远，校准**不可辨识**；而 ECE 好只是因为模型在预测基础概率 | 保证样本同时含正常与异常窗口；单类样本还会掩盖“模型根本没有判别能力”这件事。`brian calibrate` 在 `discriminates = false`（AUC < 0.6）时会**拒绝写出**校准文件并删除旧文件——不要用 `--force` 绕过它 |
